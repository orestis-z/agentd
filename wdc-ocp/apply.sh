#!/usr/bin/env bash
# Deploy agentd-ui. Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
NS=agentd-ui
HOST=agentd-ui.apps.oc-nm-upstream-wdc.washington.nmopenshift.com

echo "==> namespace + rbac"
oc apply -f 00-namespace.yaml
oc apply -f 01-rbac.yaml

echo "==> ghcr.io pull secret"
if [ -z "${GHCR_TOKEN:-}" ]; then
  echo "WARNING: GHCR_TOKEN not set — skipping pull secret (image pull will fail if secret absent)"
else
  oc create secret docker-registry ghcr-pull-secret -n "$NS" \
    --docker-server=ghcr.io \
    --docker-username="${GHCR_USER:-oauth2}" \
    --docker-password="${GHCR_TOKEN}" \
    --dry-run=client -o yaml | oc apply -f -
  oc secrets link agentd-ui ghcr-pull-secret --for=pull -n "$NS"
fi

echo "==> wildcard *.apps TLS for oauth-proxy (copied from openshift-ingress/router-certs-default)"
oc get secret router-certs-default -n openshift-ingress -o jsonpath='{.data.tls\.crt}' | base64 -d > /tmp/agentd-ui-wild.crt
oc get secret router-certs-default -n openshift-ingress -o jsonpath='{.data.tls\.key}' | base64 -d > /tmp/agentd-ui-wild.key
oc create secret tls agentd-ui-wildcard-tls -n "$NS" \
  --cert=/tmp/agentd-ui-wild.crt --key=/tmp/agentd-ui-wild.key \
  --dry-run=client -o yaml | oc apply -f -
rm -f /tmp/agentd-ui-wild.crt /tmp/agentd-ui-wild.key

echo "==> cookie secret (oauth-proxy session)"
oc create secret generic agentd-ui-cookie -n "$NS" \
  --from-literal=session_secret="$(openssl rand -hex 16)" \
  --dry-run=client -o yaml | oc apply -f -

echo "==> tasks ConfigMap"
oc apply -f 02-configmap-tasks.yaml

echo "==> shared PVC (queue + logs + schedules)"
oc apply -f 05-shared-pvc.yaml

echo "==> orchestrator RBAC"
oc apply -f 06-orchestrator-rbac.yaml

echo "==> devenv scripts ConfigMap"
oc create configmap devenv-scripts -n "$NS" \
  --from-file=launch.sh=devenv/launch.sh \
  --from-file=pod.yml=devenv/k8s/pod.yml \
  --from-file=headless-service.yml=devenv/k8s/headless-service.yml \
  --from-file=setup-repos.sh=devenv/k8s/setup-repos.sh \
  --dry-run=client -o yaml | oc apply -f -

echo "==> UI deployment"
oc apply -f 03-deployment.yaml

echo "==> orchestrator deployment"
oc apply -f 07-orchestrator-deployment.yaml
oc rollout status deploy/agentd-orchestrator -n "$NS" --timeout=300s || true
oc rollout restart deploy/agentd-ui -n "$NS" >/dev/null 2>&1 || true
oc rollout status deploy/agentd-ui -n "$NS" --timeout=180s || true

echo "==> public LoadBalancer service"
oc apply -f 04-service.yaml

echo "==> oauth-public IngressController (exposes cluster OAuth server externally)"
oc apply -f 08-oauth-public-ingresscontroller.yaml

echo "==> waiting for the VPC LoadBalancer to get an ingress address..."
for i in $(seq 1 60); do
  LB=$(oc get svc agentd-ui -n "$NS" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  [ -n "$LB" ] && break
  sleep 10
done

echo
if [ -n "${LB:-}" ]; then
  echo "==> LoadBalancer address: $LB"
  echo "==> NEXT: create public DNS record  $HOST  ->  $LB  (CNAME if hostname, A if IP)"
else
  echo "==> LB address not ready yet; re-check with:  oc get svc agentd-ui -n $NS -w"
fi
echo "==> done. URL (after DNS): https://$HOST"
