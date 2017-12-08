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

          for nsqsn; do
            # jq may be optional, but I couldn't figure out how to get at
            # .data["tls.crt"] with jsonpath or go-template.
            kubectl get secret $${nsqsn#*/} -n $${nsqsn%%/*} -o json |
              jq '.data["tls.crt"], .data["tls.key"]' |
              # NOTE: coreutils base64 handles this (more than one stream) fine
              base64 -d >$${nsqsn//\//__}
          done
          ls -ltr
</%text>
        - emptiness # $0
<%
  secrets = [
    (ing.metadata.namespace, tls.secretName)
    for ing in ingresses if ing.spec.tls
    for tls in ing.spec.tls ]
%>
% for ns, sn in secrets:
        - ${ns}/${sn}
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
