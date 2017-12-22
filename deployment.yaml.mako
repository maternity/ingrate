apiVersion: extensions/v1beta1
kind: Deployment
metadata: {}
spec:
  #selector: {}

  replicas: 1
  strategy:
    type: RollingUpdate
    rollingUpdate:
        maxSurge: 1
        maxUnavailable: 0

  template:
    #metadata: {}
    spec:
      # TODO: Not hardcode this
      # :; kubectl create serviceaccount foo
      # :; kubectl create role ingress-secret-reader --resource=secret --verb=get
      # :; kubectl create rolebinding --serviceaccount default:foo --role ingress-secret-reader foo-is-an-ingress-secret-reader
      serviceAccountName: foo
      initContainers:
      - name: secret-loader
        # Need basic shell, kubectl, jq, coreutils.
        image: quay.io/maternity/k8s-util:1
        volumeMounts:
        - name: certs
          mountPath: /usr/local/etc/haproxy/certs
        command:
        - /bin/sh
        - -c
        - |
<%text>
          cd /usr/local/etc/haproxy/certs

          # Generate a totally legit fallback cert so haproxy will start.
          openssl req -new -x509 -newkey rsa:1024 -subj '/CN=*' -nodes \
              -keyout zz-fallback -out zz-fallback

          # We aren't going to copy the secrets verbatim into the deployment,
          # instead we'll reference the location of the secret, along with the
          # current version so the deployment can be invalidated if secrets are
          # changed.
          for secret; do
            # jq may be optional, but I couldn't figure out how to get at
            # .data["tls.crt"] with jsonpath or go-template.
            (
            set -- $secret
            ns=$1
            sn=$2
            out=${ns}__${sn}
            kubectl get secret -n $ns $sn -o json |
              jq -r '.data."tls.crt", .data."tls.key"' |
              # NOTE: coreutils base64 handles this (more than one stream) fine
              base64 -d >$out
            # Don't leave an empty file, or haproxy won't start.  In some cases
            # we might prefer the pod to fail (when another one is still
            # running, that might have a good cert still).  Other times, we'll
            # want it to run without, and the operator can iterate to get the
            # missing pieces in place.
            grep -q '^-----BEGIN' $out || rm -f $out
            )
          done
          ls -ltr
</%text>
        - emptiness # $0
% for (ns,name),secret in sorted(secrets.items()):
        - ${secret.metadata.namespace} ${secret.metadata.name} ${secret.metadata.resourceVersion}
% endfor
      containers:
      - name: haproxy
        image: haproxy:1.8.1
        volumeMounts:
        - name: config
          mountPath: /usr/local/etc/haproxy
        - name: certs
          mountPath: /usr/local/etc/haproxy/certs
        readinessProbe:
          tcpSocket:
            port: 80
        lifecycle:
          preStop:
            exec:
              command:
              # graceful shutdown
              - /bin/sh
              - -c
              - 'kill -USR1 1'
        terminationGracePeriodSeconds: ${60*60}
      volumes:
      - name: config
        projected:
          sources:
          - configMap:
              name: ${configmap.metadata.name}
              items:
              - path: haproxy.cfg
                key: haproxy.cfg
      - name: certs
