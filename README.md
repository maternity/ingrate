## Ingrate ingress controller

Ingrate is an haproxy based kubernetes ingress controller.


## Configuration

### Roles/Clusterroles

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ingrate:controller
  labels:
    maternity.io/ingrate: 'true'
rules:
- apiGroups:
  - extensions
  - apps
  resources:
  - ingresses
  verbs:
  - list
  - watch
- apiGroups:
  - extensions
  - apps
  resources:
  - ingresses/status
  verbs:
  - update
- apiGroups:
  - ""
  resources:
  - secrets
  verbs:
  - get
  - watch
- apiGroups:
  - ""
  resources:
  - services
  verbs:
  - get
  - watch
  - list
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ingrate:controller
  namespace: kube-system
  labels:
    maternity.io/ingrate: 'true'
rules:
- apiGroups:
  - ""
  resources:
  - services
  verbs:
  - get
  - watch
  - list
- apiGroups:
  - ""
  resources:
  - configmaps
  verbs:
  - create
  - update
  - get
- apiGroups:
  - extensions
  - apps
  resources:
  - deployments
  verbs:
  - create
  - update
  - get
  - watch
- apiGroups:
  - extensions
  - apps
  resources:
  - deployments/status
  verbs:
  - update
- apiGroups:
  - extensions
  - apps
  resources:
  - replicasets
  verbs:
  - list
  - watch
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ingrate:proxy
  labels:
    maternity.io/ingrate: 'true'
rules:
- apiGroups:
  - ""
  resources:
  - secrets
  verbs:
  - get
```

### Running an ingrate instance

```bash
NAME=whatnow

# Create controller serviceaccount
:; kubectl create -n kube-system sa ingrate-$NAME-controller
:; kubectl create -n kube-system \
        rolebinding ingrate:controller:ingrate-$NAME-controller \
        --serviceaccount kube-system:ingrate-$NAME-controller \
        --role ingrate:controller
:; kubectl create -n kube-system \
        clusterrolebinding ingrate:controller:ingrate-$NAME-controller \
        --serviceaccount kube-system:ingrate-$NAME-controller \
        --clusterrole ingrate:controller

# Create proxy serviceaccount
:; kubectl create -n kube-system sa ingrate-$NAME-proxy
:; kubectl create -n kube-system \
        clusterrolebinding ingrate:proxy:ingrate-$NAME-proxy \
        --serviceaccount kube-system:ingrate-$NAME-proxy \
        --clusterrole ingrate:proxy

# Create config for volume overlay (optional)
:; kubectl create -n kube-system cm ingrate-$NAME \
        --from-file=haproxy.cfg.mako \
        --from-file=deployment.yaml.mako \

# TODO: add overrides to mount template overlays
# Run ingrate controller
:; kubectl run -n kube-system ingrate-$NAME \
        --serviceaccount ingrate-$NAME-controller \
        --image quay.io/maternity/ingrate:0.1.1 \
        -- -v kube-system $NAME
```
