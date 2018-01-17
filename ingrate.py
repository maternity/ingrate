import argparse
import asyncio
from contextlib import suppress
import difflib
from itertools import repeat
import logging
from os import fspath
from pathlib import Path

from aiohttp import ClientResponseError
import mako.exceptions
import mako.template
import yaml

from ak8s.apis import APIRegistry
from ak8s.apis.operation import K8sAPIOperation
from ak8s.apis.operation import StreamingMixin
from ak8s.autils import amingle
from ak8s.autils import astaple
from ak8s.autils import athrottle
from ak8s.autils import azip
from ak8s.client import AK8sClient
from ak8s.client import AK8sNotFound


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', dest='verbose', action='count', default=0)
    parser.add_argument('-l', dest='selector', metavar='SELECTOR',
            default='', help='Label selector')
    parser.add_argument('namespace', metavar='NAMESPACE')
    parser.add_argument('name', metavar='NAME')
    args = parser.parse_args()

    level = logging.WARN
    level -= args.verbose*10
    logging.basicConfig(level=level, format='%(asctime)s:%(levelname)s:%(name)s:%(message)s')

    logger = logging.getLogger('ingrate')

    registry = APIRegistry(release='1.8')

    # TODO: these should be part of a standard config
    @registry.add_api_base(r'(?:\w+\.)?watch\w+')
    class K8sAPIWatchOperation(StreamingMixin, K8sAPIOperation):
        pass

    @registry.add_api_base(r'(?:\w+\.)?(?:read|list)\w+')
    class K8sAPIReadItemOrCollectionOperation(
            StreamingMixin.bind_stream_condition(lambda self: self.args.get('watch')),
            K8sAPIOperation):
        pass

    templates = Path(__file__).parent
    haproxy_cfg_tpl = mako.template.Template(
            filename=fspath(templates/'haproxy.cfg.mako'))
    deployment_tpl = mako.template.Template(
            filename=fspath(templates/'deployment.yaml.mako'))

    async with AK8sClient(registry=registry) as ak8s:
        # - Initially, just point at service IPs.
        # - Look at having haproxy serve a healthcheck so k8s can tell when
        #   it is ready.
        # - Look at configuring an ELB to frontend haproxy.
        # - Consider just configuring an ELB?  Ok for staging, but we'll
        #   pay more to have ELB do routing.
        # - Later, consider whether there is a benefit to pushing endpoint
        #   information into haproxy.

        # A full config and deployment takes:
        # - 1 or more ingresses
        # - an haproxy config template
        # - a deployment template
        # And generates
        # - An haproxy config
        # - A versioned config map containing that haproxy config
        # - A deployment referencing that config map
        # - A service to expose the deployment
        # The versioned configmap is a bit tricky to manage, but it is expected
        # that deployments will be able to generate versioned configmaps in the
        # future.

        apis = ak8s.bind_api_group(registry.apis)
        ic = IngrateController(ak8s=ak8s, registry=registry)

        mingler = amingle()

        mingler.add(azip(
            repeat('ingresses'),
            ic.watch_ingresses_and_related_resources(label_selector=args.selector, throttle=0.5)))
        mingler.add(azip(
            repeat('load_balancers'),
            athrottle(
                ic.watch_for_deployment_exposure(args.namespace, args.name),
                0.5)))

        ingresses = None
        load_balancers = None

        async for tag,d in mingler:
            if tag == 'ingresses':
                # Sort to stabilize output.
                ingresses = d['ingresses'] = sorted(
                        d['ingresses'],
                        key=lambda ing: (ing.metadata.namespace, ing.metadata.name))

                try:
                    haproxy_cfg = haproxy_cfg_tpl.render(**d)
                except Exception:
                    print(mako.exceptions.text_error_template().render())
                    continue

                deployment_name = f'ingrate-{args.name}-proxy'
                serviceaccount_name = f'ingrate-{args.name}-proxy'

                ### Load existing deployment, to find the current version of the
                ### configmap.
                existing_deployment = await ic.read_deployment(
                        args.namespace, deployment_name)
                configmap = await ic.validate_or_create_ingrate_configmap(
                        {'haproxy.cfg': haproxy_cfg},
                        deployment=existing_deployment,
                        namespace=args.namespace,
                        ingrate_name=args.name)

                ### Generate new deployment.
                try:
                    deployment_yaml = deployment_tpl.render(
                            configmap=configmap,
                            serviceaccount_name=serviceaccount_name,
                            **d)
                except Exception:
                    print(mako.exceptions.text_error_template().render())
                    continue

                if existing_deployment:
                    existing_deployment_yaml = existing_deployment.metadata.annotations.get('ingress-deployment-yaml')

                    if existing_deployment_yaml and existing_deployment_yaml != deployment_yaml:
                        logger.info(
                                'diff %s\n%s',
                                'deployment.yaml',
                                ''.join(difflib.unified_diff(
                                    existing_deployment_yaml.splitlines(True),
                                    deployment_yaml.splitlines(True))))

                deployment = registry.models.io.k8s.kubernetes.pkg.apis.apps.v1beta1.Deployment._project(
                        yaml.load(deployment_yaml))
                ic.init_deployment(deployment, name=args.name, configmap=configmap)
                deployment.metadata.name = deployment_name
                deployment.metadata.annotations['ingress-deployment-yaml'] = deployment_yaml

                deployment = await ic.replace_or_create_deployment(
                        args.namespace, deployment)
                deployment = await ic.watch_for_deployment_revision_to_post(
                        deployment)

                existing_deployment_revision = existing_deployment.metadata.annotations[DEPLOYMENT_REVISION_ANNOTATION]
                deployment_revision = deployment.metadata.annotations[DEPLOYMENT_REVISION_ANNOTATION]
                if deployment_revision == existing_deployment_revision:
                    logger.debug('Existing deployment suffices')
                    continue

                logger.info('Deployment revision is now %r, hang on...', deployment_revision)

                replicaset = await ic.watch_for_replicaset_matching_deployment_revision(
                        deployment)

                # Referencing the replicaset in the configmap owner references should
                # cause the configmap to be cleaned up when the replicaset is removed.
                await ic.add_configmap_owner_ref(configmap, replicaset)

            elif tag == 'load_balancers':
                # This is the shape of both ServiceStatus and IngressStatus
                #   status:
                #     loadBalancer:
                #       ingress:
                #       - hostname: abc123...elb.amazonaws.com

                # There could be more than one service, and there could be more
                # than one ingress for each one.  Maybe we need to be select
                # just one ingress, or just one service, or maybe we want all
                # of them.

                load_balancers = d['load_balancers']

            # TODO: Allow discovery to be overriden with an annotation.
            if none_is_none(ingresses, load_balancers):
                if not load_balancers:
                    # No exposures found
                    continue

                # Merge multiple services into one.
                load_balancer = registry.models.io.k8s.kubernetes.pkg.api.v1.LoadBalancerStatus(ingress=[])
                for _,lb in sorted(load_balancers.items()):
                    load_balancer.ingress.extend(lb.ingress)

                for ing in ingresses:
                    if ing.status and ing.status.loadBalancer and ing.status.loadBalancer == load_balancer:
                        continue

                    logger.info('Updating ingress %s:%s status with %r',
                            ing.metadata.namespace,
                            ing.metadata.name,
                            load_balancer)
                    await apis.extensions_v1beta1.replace_namespaced_ingress_status(
                            ing.metadata.namespace,
                            ing.metadata.name,
                            registry.models.io.k8s.kubernetes.pkg.apis.extensions.v1beta1.Ingress(
                                metadata=registry.models.io.k8s.apimachinery.pkg.apis.meta.v1.ObjectMeta(
                                    namespace=ing.metadata.namespace,
                                    name=ing.metadata.name),
                                status=registry.models.io.k8s.kubernetes.pkg.apis.extensions.v1beta1.IngressStatus(
                                    loadBalancer=load_balancer)))


DEPLOYMENT_REVISION_ANNOTATION = 'deployment.kubernetes.io/revision'
INGRATE_CONFIGMAP_VERSION_ANNOTATION = 'ingrate.maternity.io/configmap-version'
INGRATE_NAME_LABEL = 'ingrate.maternity.io/name'
INGRATE_RELEASE_COOKIE_ANNOTATION = 'ingrate.maternity.io/release-cookie'
INGRATE_RELEASE_DEFAULT_ANNOTATION = 'ingrate.maternity.io/release-default'
INGRATE_RELEASE_SELECTOR_ANNOTATION = 'ingrate.maternity.io/release-selector'


class IngrateController:
    def __init__(self, *, ak8s, registry):
        self._logger = logging.getLogger(self.__class__.__name__)
        self._models = registry.models
        self._apis = ak8s.bind_api_group(registry.apis)

    async def watch_ingresses_and_related_resources(
            self, *,
            label_selector='',
            throttle=0.5):
        mingler = amingle()

        ingresses = None
        services = None
        secrets = None
        release_services = None
        release_map = None

        mingler.add(azip(
            repeat('ingresses'),
            athrottle(
                self.watch_ingresses(label_selector=label_selector),
                throttle)))
        services_stream = None
        secrets_stream = None
        release_services_stream = None

        async for tag,d in mingler:
            if tag == 'ingresses':
                ingresses = list(d['ingresses'])

                # (re)start services stream
                if services_stream is not None:
                    await services_stream.aclose()
                services_stream = athrottle(
                        self.watch_ingress_services(ingresses),
                        throttle)
                mingler.add(azip(repeat('services'), services_stream))
                services = None

                # (re)start secrets stream
                if secrets_stream is not None:
                    await secrets_stream.aclose()
                secrets_stream = athrottle(
                        self.watch_ingress_secrets(ingresses),
                        throttle)
                mingler.add(azip(repeat('secrets'), secrets_stream))
                secrets = None

            elif tag == 'services':
                services = d['services'].copy()

                # (re)start release services stream
                if release_services_stream is not None:
                    await release_services_stream.aclose()
                release_services_stream = athrottle(
                        self.watch_release_service_services(services.values()),
                        throttle)
                mingler.add(azip(
                        repeat('release-services'),
                        release_services_stream))
                release_services = None
                release_map = None

            elif tag == 'secrets':
                secrets = d['secrets'].copy()

            elif tag == 'release-services':
                release_services = d['services'].copy()
                release_map = d['release_map'].copy()

            if none_is_none(ingresses, services, secrets, release_services, release_map):
                yield dict(
                        ingresses=ingresses,
                        services={**services, **release_services},
                        secrets=secrets,
                        release_map=release_map)

    async def watch_ingresses(self, *, label_selector=''):
        '''Emit a series of ingress sets as they are updated.'''
        # TODO: namespaces filter

        ingress_list = await self._apis.extensions_v1beta1.list_ingress_for_all_namespaces(
                #includeUninitialized=True,
                labelSelector=label_selector)
        ingresses = {
                (ing.metadata.namespace, ing.metadata.name): ing
                for ing in ingress_list.items }
        version = ingress_list.metadata.resourceVersion
        yield dict(ingresses=ingresses.values())

        async for ev, ing in self._apis.extensions_v1beta1.watch_ingress_list_for_all_namespaces.watch(
                labelSelector=label_selector,
                resourceVersion=version):
            if ev in {'ADDED', 'MODIFIED'}:
                ingresses[ing.metadata.namespace, ing.metadata.name] = ing
            elif ev == 'DELETED':
                del ingresses[ing.metadata.namespace, ing.metadata.name]
            else:
                continue
            version = ing.metadata.resourceVersion
            yield dict(ingresses=ingresses.values())

    async def watch_ingress_services(self, ingresses):
        '''Load and monitor the services related to the given ingresses.'''

        backend_refs = {
                (ing.metadata.namespace, path.backend.serviceName)
                for ing in ingresses if ing.spec.rules
                for rule in ing.spec.rules if rule.http
                for path in rule.http.paths }
        backend_refs.update(
                (ing.metadata.namespace, ing.spec.backend.serviceName)
                for ing in ingresses if ing.spec.backend)

        pending = {
                self._apis.core_v1.read_namespaced_service(ns, sn)
                for ns, sn in backend_refs }

        done, pending = await asyncio.wait(pending)

        services = {}
        while done:
            svc = await done.pop()
            services[svc.metadata.namespace, svc.metadata.name] = svc
        yield dict(services=services)

        async for ev, svc in amingle(
                self._apis.core_v1.watch_namespaced_service.watch(
                    ns, sn,
                    resourceVersion=services[ns, sn].metadata.resourceVersion)
                for ns, sn in backend_refs ):
            if ev in {'ADDED', 'MODIFIED'}:
                services[svc.metadata.namespace, svc.metadata.name] = svc

            elif ev == 'DELETED':
                try:
                    del services[svc.metadata.namespace, svc.metadata.name]

                except KeyError:
                    continue

            else:
                continue

            yield dict(services=services)

    async def watch_ingress_secrets(self, ingresses):
        '''Load and monitor the secrets related to the given ingresses.'''

        secret_refs = {
                (ing.metadata.namespace, tls.secretName)
                for ing in ingresses if ing.spec.tls
                for tls in ing.spec.tls }

        pending = {
                self._apis.core_v1.read_namespaced_secret(ns, sn)
                for ns, sn in secret_refs }

        done, pending = await asyncio.wait(pending)

        secrets = {}
        while done:
            secret = await done.pop()
            secrets[secret.metadata.namespace, secret.metadata.name] = secret
        yield dict(secrets=secrets)

        async for ev,secret in amingle(
                self._apis.core_v1.watch_namespaced_secret.watch(
                    ns, sn,
                    resourceVersion=secrets[ns, sn].metadata.resourceVersion)
                for ns, sn in secret_refs ):
            if ev in {'ADDED', 'MODIFIED'}:
                secrets[secret.metadata.namespace, secret.metadata.name] = secret

            elif ev == 'DELETED':
                try:
                    del secrets[secret.metadata.namespace, secret.metadata.name]

                except KeyError:
                    continue

            else:
                continue

            yield dict(secrets=secrets)

    async def watch_release_service_services(self, services):
        '''Load and monitor the services referred to by any release services.
        '''

        release_stubs = []
        for rsvc in services:
            if rsvc.metadata.annotations is None or INGRATE_RELEASE_SELECTOR_ANNOTATION not in rsvc.metadata.annotations:
                continue

            self._logger.info('Release service stub: %s:%s with selector %r',
                    rsvc.metadata.namespace, rsvc.metadata.name,
                    rsvc.metadata.annotations[INGRATE_RELEASE_SELECTOR_ANNOTATION])

            release_stubs.append(rsvc)

        pending = set()
        for rsvc in release_stubs:
            pending.add(astaple(
                    rsvc,
                    self._apis.core_v1.list_namespaced_service(
                        rsvc.metadata.namespace,
                        labelSelector=rsvc.metadata.annotations[INGRATE_RELEASE_SELECTOR_ANNOTATION])))

        # A mapping of { (namespace, release_name) -> { service_names } }
        release_map = {}
        services = {}
        versions = {}
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

            while done:
                rsvc, service_list = await done.pop()
                services.update(
                        ((svc.metadata.namespace, svc.metadata.name), svc)
                        for svc in service_list.items )
                release_map[rsvc.metadata.namespace, rsvc.metadata.name] = {
                        svc.metadata.name for svc in service_list.items }
                versions[rsvc.metadata.annotations[INGRATE_RELEASE_SELECTOR_ANNOTATION]] = service_list.metadata.resourceVersion
        yield dict(services=services, release_map=release_map)

        if not release_stubs:
            # Nothing to watch.
            return

            # Simulate 0 watches by waiting here forever.  Alternately we could
            # return before the initial result, but the caller would have to
            # handle that case.
            await asyncio.Future()

        async for rsvc,(ev,svc) in amingle(
                # TODO: add resourceVersion from the service list response
                azip(
                    repeat(rsvc),
                    self._apis.core_v1.watch_namespaced_service_list.watch(
                        rsvc.metadata.namespace,
                        labelSelector=rsvc.metadata.annotations[INGRATE_RELEASE_SELECTOR_ANNOTATION],
                        resourceVersion=versions[rsvc.metadata.annotations[INGRATE_RELEASE_SELECTOR_ANNOTATION]]))
                for rsvc in release_stubs):
            if ev in {'ADDED', 'MODIFIED'}:
                services[svc.metadata.namespace, svc.metadata.name] = svc
                release_map[rsvc.metadata.namespace, rsvc.metadata.name].add(svc.metadata.name)
            elif ev == 'DELETED':
                del services[svc.metadata.namespace, svc.metadata.name]
                release_map[rsvc.metadata.namespace, rsvc.metadata.name].discard(svc.metadata.name)
            else:
                continue
            yield dict(services=services, release_map=release_map)

    async def read_deployment(self, namespace, name):
        with suppress(AK8sNotFound):
            return await self._apis.extensions_v1beta1.read_namespaced_deployment(
                    namespace, name)

    async def read_configmap(self, namespace, name):
        with suppress(AK8sNotFound):
            return await self._apis.core_v1.read_namespaced_config_map(namespace, name)

    async def create_configmap(self, namespace, configmap):
        return await self._apis.core_v1.create_namespaced_config_map(namespace, configmap)

    async def validate_or_create_ingrate_configmap(self, data, *,
            deployment=None,
            ingrate_name,
            namespace):
        if deployment is not None:
            existing_configmap_version = deployment.metadata.annotations.get(
                    INGRATE_CONFIGMAP_VERSION_ANNOTATION)
        else:
            existing_configmap_version = None

        if existing_configmap_version is not None:
            existing_configmap = await self.read_configmap(
                    namespace,
                    existing_configmap_version)

            if existing_configmap.data == data:
                self._logger.info(
                        'Existing configmap is up to date: %s',
                        existing_configmap.metadata.name)
                return existing_configmap

            self._logger.info(
                    'Existing configmap is not up to date: %s',
                    existing_configmap.metadata.name)

            for key in data:
                if key not in existing_configmap.data:
                    self._logger.debug('Existing configmap is missing %s', key)

                else:
                    value = data[key]
                    existing_value = existing_configmap.data.get(key)
                    self._logger.info(
                            'diff %s\n%s',
                            key,
                            ''.join(difflib.unified_diff(
                                existing_value.splitlines(True),
                                value.splitlines(True))))

            for key in existing_configmap.data:
                if key not in data:
                    self._logger.debug('Existing configmap has extra %s', key)

        configmap = self._models.io.k8s.kubernetes.pkg.api.v1.ConfigMap(
                metadata=self._models.io.k8s.apimachinery.pkg.apis.meta.v1.ObjectMeta(
                    generateName=f'ingrate-{ingrate_name}-',
                    labels={INGRATE_NAME_LABEL: ingrate_name}),
                data=data)

        configmap = await self.create_configmap(namespace, configmap)

        self._logger.info('Created new configmap: %s', configmap.metadata.name)

        return configmap

    def init_deployment_metadata(self, deployment, *, configmap):
        if deployment.metadata.annotations is None:
            deployment.metadata.annotations = {}
        deployment.metadata.annotations[INGRATE_CONFIGMAP_VERSION_ANNOTATION] = configmap.metadata.name

    def init_deployment(self, deployment, *, name, configmap):
        self.init_deployment_metadata(
                deployment,
                configmap=configmap)
        if deployment.spec.template.metadata is None:
            deployment.spec.template.metadata = self._models.io.k8s.apimachinery.pkg.apis.meta.v1.ObjectMeta()
        # Deployment selector will be set to match labels of deployment
        # template if no selector is provided.
        if deployment.spec.template.metadata.labels is None:
            deployment.spec.template.metadata.labels = {}
        deployment.spec.template.metadata.labels[INGRATE_NAME_LABEL] = name

    async def replace_or_create_deployment(
            self, namespace, deployment):
        try:
            deployment = await self._apis.extensions_v1beta1.replace_namespaced_deployment(
                    namespace, deployment.metadata.name, deployment)

            self._logger.info('Updated deployment')

        except AK8sNotFound:
            deployment = await self._apis.extensions_v1beta1.create_namespaced_deployment(
                    namespace, deployment)

            self._logger.info('Created deployment')

        return deployment

    async def add_configmap_owner_ref(self, target, referrent):
        if target.metadata.ownerReferences and any(
                ref.uid==referrent.metadata.uid
                for ref in target.metadata.ownerReferences ):
            return

        self._logger.info('Updating references on ConfigMap %r',
                target.metadata.name)

        if target.metadata.ownerReferences is None:
            target.metadata.ownerReferences = []

        target.metadata.ownerReferences.append(
                self._models.io.k8s.apimachinery.pkg.apis.meta.v1.OwnerReference(
                    apiVersion=referrent.apiVersion,
                    kind=referrent.kind,
                    name=referrent.metadata.name,
                    uid=referrent.metadata.uid))

        # TODO: Send a patch, instead?

        return await self._apis.core_v1.replace_namespaced_config_map(
                target.metadata.namespace,
                target.metadata.name,
                target)

    async def watch_for_deployment_revision_to_post(self, deployment):
        # Watch for the controller to post revision info.
        async for ev, obj in self._apis.extensions_v1beta1.watch_namespaced_deployment.watch(
                deployment.metadata.namespace, deployment.metadata.name,
                resourceVersion=deployment.metadata.resourceVersion):
            if ev not in {'ADDED', 'MODIFIED', 'DELETED'}:
                continue
            if DEPLOYMENT_REVISION_ANNOTATION in obj.metadata.annotations:
                return obj
        else:
            raise RuntimeError('Deployment revision never posted!')

    async def watch_for_replicaset_matching_deployment_revision(self, deployment):
        namespace = deployment.metadata.namespace
        ingrate_name = deployment.metadata.labels[INGRATE_NAME_LABEL]
        revision = deployment.metadata.annotations[DEPLOYMENT_REVISION_ANNOTATION]

        async for ev, rs in self._apis.extensions_v1beta1.watch_namespaced_replica_set_list(
                namespace,
                # Try to find ingrate deployment exposures using the name
                # selector, which is what you would get if you ran
                # `kubectl expose deployment NAME`.
                labelSelector=f'{INGRATE_NAME_LABEL}={ingrate_name}'):
            if ev not in {'ADDED', 'MODIFIED', 'DELETED'}:
                continue
            if rs.metadata.annotations[DEPLOYMENT_REVISION_ANNOTATION] == revision:
                self._logger.info(
                        'Found ReplicaSet %r for deployment revision %r',
                        rs.metadata.name,
                        revision)
                return rs

    async def watch_for_deployment_exposure(self, namespace, name):
        service_list = await self._apis.core_v1.list_namespaced_service(
                namespace,
                labelSelector=f'{INGRATE_NAME_LABEL}={name}')
        load_balancers = {
                svc.metadata.name: svc.status.loadBalancer
                for svc in service_list.items
                if svc.status and svc.status.loadBalancer }
        yield dict(load_balancers=load_balancers)

        async for ev, svc in self._apis.core_v1.watch_namespaced_service_list.watch(
                namespace,
                labelSelector=f'{INGRATE_NAME_LABEL}={name}'):
            if ev in {'ADDED', 'MODIFIED'} and svc.spec.type == 'LoadBalancer':
                if svc.status and svc.status.loadBalancer:
                    load_balancers[svc.metadata.name] = svc.status.loadBalancer
            elif ev == 'DELETED' and svc.spec.type == 'LoadBalancer':
                if svc.metadata.name in load_balancers:
                    del load_balancers[svc.metadata.name]
            else:
                continue
            yield dict(load_balancers=load_balancers)


def none_is_none(*items):
    return not any( item is None for item in items )


if __name__ == '__main__':
    try:
        rc = asyncio.get_event_loop().run_until_complete(main()) or 0

    except KeyboardInterrupt as e:
        rc = 1

    exit(rc)
