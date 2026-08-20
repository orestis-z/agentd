#!/usr/bin/env bash
# One-time bootstrap — requires cluster-admin.
# Creates the namespace, ClusterRoleBindings, IngressController, and TLS cert.
# After this, a namespace admin can run apply.sh for everything else.
set -euo pipefail
cd "$(dirname "$0")"
NS=agentd

echo "==> namespace"
oc apply -f 00-namespace.yaml

echo "==> RBAC (ClusterRoleBindings for UI ServiceAccount)"
oc apply -f 01-rbac.yaml

echo "==> orchestrator RBAC (cross-namespace role in machine-learning)"
oc apply -f 06-orchestrator-rbac.yaml

echo "==> wildcard *.apps TLS for oauth-proxy"
oc get secret router-certs-default -n openshift-ingress \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > /tmp/agentd-wild.crt
oc get secret router-certs-default -n openshift-ingress \
  -o jsonpath='{.data.tls\.key}' | base64 -d > /tmp/agentd-wild.key
oc create secret tls agentd-ui-wildcard-tls -n "$NS" \
  --cert=/tmp/agentd-wild.crt --key=/tmp/agentd-wild.key \
  --dry-run=client -o yaml | oc apply -f -
rm -f /tmp/agentd-wild.crt /tmp/agentd-wild.key

echo "==> oauth-public IngressController"
oc apply -f 08-oauth-public-ingresscontroller.yaml

echo "==> done. A namespace admin can now run ./apply.sh"
