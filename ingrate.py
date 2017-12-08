import argparse
import asyncio
from collections import defaultdict
from contextlib import suppress
import difflib
import json
import logging
from os import fspath
from pathlib import Path

from aiohttp import ClientResponseError
import mako.template
import yaml

from ak8s.apis import APIRegistry
from ak8s.apis import fixup_path_path_params
from ak8s.client import AK8sClient
from ak8s.client import AK8sNotFound


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', dest='verbose', action='count', default=0)
    parser.add_argument('-l', dest='selector', metavar='SELECTOR',
            default='', help='Label selector')
    parser.add_argument('specs', metavar='SPEC', type=Path, nargs='+',
            help='Location of k8s swagger specs')
    #parser.add_argument('kubeconfig', metavar='KUBECONFIG', nargs='?')
    parser.add_argument('namespace', metavar='NAMESPACE')
    parser.add_argument('name', metavar='NAME')
    args = parser.parse_args()

    level = logging.INFO
    level -= args.verbose*10
    logging.basicConfig(level=level)

    logger = logging.getLogger('ingrate')

    registry = APIRegistry()

    for pth in args.specs:
        if pth.is_dir():
            for pth in pth.rglob('*.json'):
                with pth.open() as fh:
                    spec = json.load(fh)
                fixup_path_path_params(spec)

                registry.add_spec(spec)

        else:
            with pth.open() as fh:
                spec = json.load(fh)
            fixup_path_path_params(spec)

            registry.add_spec(spec)

    templates = Path(__file__).parent
    haproxy_cfg_tpl = mako.template.Template(
            filename=fspath(templates/'haproxy.cfg.mako'))
    deployment_tpl = mako.template.Template(
            filename=fspath(templates/'deployment.yaml.mako'))

    async with AK8sClient.from_kubeconfig(models=registry.models) as ak8s:
        # When a rule is deleted we'll get some event saying so.  It likely
        # will be just a stub of what was deleted or modified.  We'll want
        # to track what rules were generated from that, so we can replace
        # or remove them.

        # 1. Collect certs
        # 2. Collect frontends (default, plus one for each host)
        # 3. Collect backends
        # 4. Generate config using template from configmap.
        # 5. Push config into a versioned configmap.
        # 6. Update deployment to use new configmap.

        # - Initially, just point at service IPs.
        # - Look at having haproxy serve a healthcheck so k8s can tell when
        #   it is ready.
        # - Look at configuring an ELB to frontend haproxy.
        # - Consider just configuring an ELB?  Ok for staging, but we'll
        #   pay more to have ELB do routing.
        # - Later, consider whether there is a benefit to pushing endpoint
        #   information into haproxy.

        # On watches:
        # - Need to load subjects of watches, then continue to receive
        #   updates.
        # - Initially watches will send the latest values of the subjects.
        #   It's not possible to tell when this initial response is
        #   complete, other than by timing.  Maybe it is better to load
        #   this explicitly, in a list request.  Of course there will still
        #   be timing issues in live updates, but the initial configuration
        #   from already existing resources shouldn't be subject to such
        #   issues.

        # Incremental development plan:
        # - Start static, add watches later.
        # - Generate haproxy config.
        # - Generate configmap.
        # - Generate deployment.
        # - Make health checks work.
        # - Get rollout working.

        # For the moment, I am working with a multiple ingress -> haproxy
        # config.  But this does lead to some quandries.
        # 1. The order of in which ingresses are processed is not defined, so
        #    when ingresses have overlapping rules with differing backends, the
        #    resulting behavior may not be stable.
        # 2. If one ingress defines one haproxy config then we can use ingress
        #    annotations to set some extra behaviors.  If there are multiple
        #    ingresses, then we can have surprising or broken behaviors when
        #    the switches are applied across ingresses.
        # 3. Referencing resources across namespaces isn't possible.  This
        #    means the secrets would need to be copied in order to expose them,
        #    which isn't great hygiene for secrets.
        #
        # In light of the secrets/namespaces issue, I am inclined to switch to
        # a one ingress -> one haproxy config.  But if we do that, now we can't
        # reference services across namespaces.  Ugh.
        # Services can be referenced across namespaces using externalName
        # services, but these only provide a symbolic mapping, meaning now our
        # ingress controller (or haproxy itself) must resolve the name and
        # propagate udpates.  Not really very tempting that.
        #
        # Other ideas:
        # 1. Overload v1beta1.IngressBackend.serviceName to encode a namespace
        #    name.  The k8s API server may be doing some validation making this
        #    difficult (not allow a separator character), or impossible
        #    (resolving the reference directly).
        # 2. Copy secrets directly into the container, using an init container
        #    or sidecar.
        # 3. Just require all TLS secrets to be in the namespace of the
        #    controller.
        #
        # 3 is the simplest to implement.
        # 2 is more flexible, though it may require some extra steps to allow
        # access in a cluster with RBAC (arguably a good thing).
        # 1 is just not a very good idea, let us never speak of it again.

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

        v1_api = ak8s.bind_api_group(registry.apis['v1'])
        v1beta1_api = ak8s.bind_api_group(registry.apis['extensions/v1beta1'])
        ic = IngrateController(ak8s=ak8s, registry=registry)

        # TODO: May want to restrict or prioritize namespaces.
        ingress_list = await v1beta1_api.list_ingress_for_all_namespaces(
                #includeUninitialized=True,
                labelSelector=args.selector)

        # Sort to stabilize output.
        ingresses = sorted(
                ingress_list.items,
                key=lambda ing: (ing.metadata.namespace, ing.metadata.name))

        subs = set()
        services = {}

        # Load related resources
        for ing in ingresses:
            if ing.spec.rules:
                for rule in ing.spec.rules:
                    for path in rule.http.paths:
                        subs.add(v1_api.read_namespaced_service(
                                ing.metadata.namespace,
                                path.backend.serviceName))

            if ing.spec.backend:
                subs.add(v1_api.read_namespaced_service(
                        ing.metadata.namespace,
                        ing.spec.backend.serviceName))

        done, pending = await asyncio.wait(subs)
        for f in done:
            sub = await f
            if isinstance(sub, registry.models.v1.Service):
                services[sub.metadata.namespace, sub.metadata.name] = sub

            else:
                raise TypeError(f'Unhandled subresource: {sub!r}')

        haproxy_cfg = haproxy_cfg_tpl.render(
                ingresses=ingresses,
                services=services)

        ### Load existing deployment, to find the current version of the
        ### configmap.
        existing_deployment = await ic.read_deployment(args.namespace, args.name)
        configmap = await ic.validate_or_create_ingrate_configmap(
                {'haproxy.cfg': haproxy_cfg},
                deployment=existing_deployment,
                namespace=args.namespace,
                name=args.name)

        ### Generate new deployment.
        deployment = registry.models.v1beta1.Deployment._project(
                yaml.load(deployment_tpl.render(
                    configmap=configmap,
                    ingresses=ingresses)))
        ic.init_deployment(deployment, name=args.name, configmap=configmap)

        deployment = await ic.replace_or_create_deployment(
                args.namespace, args.name, deployment, configmap)


class IngrateController:
    def __init__(self, *, ak8s, registry):
        self._logger = logging.getLogger(self.__class__.__name__)
        self._models = registry.models
        self._v1_api = ak8s.bind_api_group(registry.apis['v1'])
        self._v1beta1_api = ak8s.bind_api_group(registry.apis['extensions/v1beta1'])

    async def read_deployment(self, namespace, name):
        with suppress(AK8sNotFound):
            return await self._v1beta1_api.read_namespaced_deployment(
                    namespace, name)

    async def read_configmap(self, namespace, name):
        with suppress(AK8sNotFound):
            return await self._v1_api.read_namespaced_config_map(namespace, name)

    async def create_configmap(self, namespace, configmap):
        return await self._v1_api.create_namespaced_config_map(namespace, configmap)

    async def validate_or_create_ingrate_configmap(self, data, *,
            deployment=None,
            name=None,
            namespace=None):
        if name is None:
            name = deployment.metadata.name
        if namespace is None:
            namespace = deployment.metadata.name

        if deployment is not None:
            existing_configmap_version = deployment.metadata.annotations.get(
                    'ingrate-configmap-version')
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
                    self._logger.debug(
                            'diff %s\n%s',
                            key,
                            ''.join(difflib.unified_diff(
                                existing_value.splitlines(True),
                                value.splitlines(True))))

        else:
            for key in existing_configmap.data:
                if key not in data:
                    self._logger.debug('Existing configmap has extra %s', key)

        configmap = self._models.v1.ConfigMap(
                metadata=self._models.v1.ObjectMeta(
                    generateName=f'{name}-',
                    labels={'ingrate-name': name}),
                data=data)

        configmap = await self.create_configmap(namespace, configmap)

        self._logger.info('Created new configmap: %s', configmap.metadata.name)

        return configmap

    def init_deployment_metadata(self, deployment, *, name, configmap):
        deployment.metadata.name = name
        if deployment.metadata.annotations is None:
            deployment.metadata.annotations = {}
        deployment.metadata.annotations['ingrate-configmap-version'] = configmap.metadata.name

    def init_deployment(self, deployment, *, name, configmap):
        self.init_deployment_metadata(
                deployment,
                configmap=configmap,
                name=name)
        if deployment.spec.template.metadata is None:
            deployment.spec.template.metadata = self._models.v1.ObjectMeta()
        # Deployment selector will be set to match labels of deployment
        # template if no selector is provided.
        if deployment.spec.template.metadata.labels is None:
            deployment.spec.template.metadata.labels = {}
        deployment.spec.template.metadata.labels['ingrate-name'] = name

    async def replace_or_create_deployment(
            self, namespace, name, deployment, configmap):
        try:
            deployment = await self._v1beta1_api.replace_namespaced_deployment(
                    namespace, name, deployment)

            self._logger.info('Updated deployment')

        except AK8sNotFound:
            deployment = await self._v1beta1_api.create_namespaced_deployment(
                    namespace, deployment)

            self._logger.info('Created deployment')

        # Referencing the deployment in the configmap owner references
        # should cause the configmap to be cleaned up when the deployment
        # is removed.
        # TODO: Reference the replicaset created with this change, instead.
        await self.add_configmap_owner_ref(configmap, deployment)

        return deployment

    async def add_configmap_owner_ref(self, target, referrent):
        if target.metadata.ownerReferences and any(
                ref.uid==referrent.metadata.uid
                for ref in target.metadata.ownerReferences ):
            return

        if target.metadata.ownerReferences is None:
            target.metadata.ownerReferences = []

        target.metadata.ownerReferences.append(
                self._models.v1.OwnerReference(
                    apiVersion=referrent.apiVersion,
                    kind=referrent.kind,
                    name=referrent.metadata.name,
                    uid=referrent.metadata.uid))

        # TODO: Send a patch?
        # Where do I put this application/strategic-merge-patch+json ?
        # To do this, I need a type that somehow conveys the content-type
        # to AK8sClient.
        #
        # client considers types that body can be
        # client considers type that api can consume
        # client chooses one that is common
        # aiohttp payloads carry metadata, maybe just implement that
        # don't worry about actual content-type negotiation or agreement
        # if the caller provides the right type, it will work
        #
        # This would be an opportunity to do something like:
        #
        #     configmap_patch = StrategicMergePatch(
        #             configmap,
        #             'metadata/ownerReferences')
        #
        # Which is nice, because it makes it unnecessary to rebuild a
        # sparse configmap to serve as a patch.
        #
        #await self._v1_api.patch_namespaced_config_map(
        #        configmap.metadata.namespace,
        #        configmap.metadata.name,
        #        configmap_patch)

        return await self._v1_api.replace_namespaced_config_map(
                target.metadata.namespace,
                target.metadata.name,
                target)


if __name__ == '__main__':
    try:
        rc = asyncio.get_event_loop().run_until_complete(main()) or 0

    except KeyboardInterrupt as e:
        rc = 1

    exit(rc)
