## Inputs:
## - ingresses
## - secrets
## - services

## Things we do that don't map well into k8s ingress model
## - Error pages
## - Dumb redirects (e.g. www.maternityneighbourhood.com -> maternityneighborhood.com)
## - Session stickiness

global
    # TODO: Need a container syslog or something
    #log /dev/log local0
    maxconn 10000
    ssl-default-bind-options no-sslv3 no-tls-tickets
    # Cipher list from https://starefossen.github.io/post/2015/01/26/securing-ssl-in-haproxy/
    ssl-default-bind-ciphers ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES256-GCM-SHA384:DHE-RSA-AES128-GCM-SHA256:DHE-DSS-AES128-GCM-SHA256:kEDH+AESGCM:ECDHE-RSA-AES128-SHA256:ECDHE-ECDSA-AES128-SHA256:ECDHE-RSA-AES128-SHA:ECDHE-ECDSA-AES128-SHA:ECDHE-RSA-AES256-SHA384:ECDHE-ECDSA-AES256-SHA384:ECDHE-RSA-AES256-SHA:ECDHE-ECDSA-AES256-SHA:DHE-RSA-AES128-SHA256:DHE-RSA-AES128-SHA:DHE-DSS-AES128-SHA256:DHE-RSA-AES256-SHA256:DHE-DSS-AES256-SHA:DHE-RSA-AES256-SHA:AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA256:AES256-SHA256:AES128-SHA:AES256-SHA:AES:CAMELLIA:DES-CBC3-SHA:!aNULL:!eNULL:!EXPORT:!DES:!RC4:!MD5:!PSK:!aECDH:!EDH-DSS-DES-CBC3-SHA:!EDH-RSA-DES-CBC3-SHA:!KRB5-DES-CBC3-SHA


defaults
    log global
    mode http
    option httplog
    option dontlognull
    timeout connect 3s

    timeout client 60s
    timeout server 60s

    retries 3
    option redispatch
    #errorfile 408 /etc/haproxy/errors/408.http
    #errorfile 500 /etc/haproxy/errors/500.http
    #errorfile 502 /etc/haproxy/errors/502.http
    #errorfile 503 /etc/haproxy/errors/503.http
    #errorfile 504 /etc/haproxy/errors/504.http

frontend frontend
    bind :80 accept-proxy
    bind :443 ssl crt /usr/local/etc/haproxy/certs accept-proxy
    mode http

    #option httpclose
    option http-server-close
    option forwardfor
    http-request set-header X-Forwarded-Proto https if { ssl_fc }

    # Legacy ACLs
    #acl proxy_https hdr(x-forwarded-proto) https

    # Dynamic path ACLs
<%

  # Flatten the set of paths.
  paths = {
    path.path
    for ing in ingresses if ing.spec.rules
    for rule in ing.spec.rules if rule.http
    for path in rule.http.paths if path.path }

  # Generate stable, unique acl ids for each unique path prefix.
  from hashlib import sha1
  path_acls = {
    path: f'path_{sha1(path.encode()).digest().hex()[:6]}'
    for path in set(paths) }
%>\
% for path in paths:
    acl ${path_acls[path]} path_beg ${path}
% endfor

    redirect scheme https if !{ ssl_fc }

<%
  rule_paths = [
    (ing, rule, path)
    for ing in ingresses if ing.spec.rules
    for rule in ing.spec.rules if rule.http
    for path in rule.http.paths
  ]
%>\
% for ing, rule, path in rule_paths:
%   if rule.host is not None and path.path is not None:
    use_backend svc_${ing.metadata.namespace}_${path.backend.serviceName}_${path.backend.servicePort} if { hdr_dom(host) ${rule.host} } ${path_acls[path.path]} # path ${path.path}
%   elif rule.host is None and path.path is not None:
    use_backend svc_${ing.metadata.namespace}_${path.backend.serviceName}_${path.backend.servicePort} if ${path_acls[path.path]} # path ${path.path}
%   elif rule.host is not None and path.path is None:
    use_backend svc_${ing.metadata.namespace}_${path.backend.serviceName}_${path.backend.servicePort} if { hdr_dom(host) ${rule.host} }
%   else:
## This case seems to be possible, but redundant.
<%    raise NotImplementedError('rules with no path and no host could just specify Ingress.spec.backend') %>
%   endif
% endfor
% for ing in ingresses:
%   if ing.spec.backend is not None:
    default_backend svc_${ing.metadata.namespace}_${ing.spec.backend.serviceName}_${ing.spec.backend.servicePort}
%   endif
% endfor


<%
  backends = {
    *( (ing.metadata.namespace, path.backend.serviceName, path.backend.servicePort)
    for ing in ingresses if ing.spec.rules
    for rule in ing.spec.rules if rule.http
    for path in rule.http.paths ),
    *( (ing.metadata.namespace, ing.spec.backend.serviceName, ing.spec.backend.servicePort)
    for ing in ingresses if ing.spec.backend ) }
%>
## Sort to avoid churn.
% for svc_ns, svc_name, svc_port in sorted(backends):

<%
    service = services[svc_ns, svc_name]
    release_cookie = service.metadata.annotations and service.metadata.annotations.get('ingrate.maternity.io/release-cookie')
    default_release = service.metadata.annotations and service.metadata.annotations.get('ingrate.maternity.io/release')
    release_services = release_map.get((service.metadata.namespace, service.metadata.name))
%>\
backend svc_${svc_ns}_${svc_name}_${svc_port}
    #TODO: confirm clusterIP is set?
    balance roundrobin
%   if release_cookie:
    cookie ${release_cookie} indirect preserve
%   endif

%   if release_services is not None:
%     for rsvc_name in sorted(release_services):
<%      rservice = services[svc_ns, rsvc_name] %>\
    server ${svc_ns}_${rsvc_name}_${svc_port} ${rservice.spec.clusterIP}:${svc_port} cookie ${rsvc_name} weight ${100 if rsvc_name == default_release else 0}
%     endfor
%   else:
    server ${svc_ns}_${svc_name}_${svc_port} ${service.spec.clusterIP}:${svc_port}
%   endif\

% endfor
