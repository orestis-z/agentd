#!/usr/bin/env bash
# Deploy agentd. Idempotent — safe to re-run.
# Cluster-admin resources (namespace, ClusterRoleBindings, IngressController, TLS cert)
# are applied by bootstrap-admin.sh. This script handles everything else.
set -euo pipefail
cd "$(dirname "$0")"
NS=agentd
HOST=agentd.apps.oc-nm-upstream-wdc.washington.nmopenshift.com

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

echo "==> cookie secret (oauth-proxy session)"
oc create secret generic agentd-ui-cookie -n "$NS" \
  --from-literal=session_secret="$(openssl rand -hex 16)" \
  --dry-run=client -o yaml | oc apply -f -

echo "==> tasks ConfigMap"
oc apply -f 02-configmap-tasks.yaml

echo "==> shared PVC (queue + logs + schedules)"
oc apply -f 05-shared-pvc.yaml

echo "==> devenv scripts ConfigMap"
DEVENV_DIR="${DEVENV_DIR:-$(cd "$(dirname "$0")/../.." && pwd)/devenv}"
if [ ! -f "$DEVENV_DIR/launch.sh" ]; then
  echo "ERROR: devenv repo not found at $DEVENV_DIR"
  echo "  Set DEVENV_DIR to the path of your devenv checkout"
  exit 1
fi
oc create configmap devenv-scripts -n "$NS" \
  --from-file=launch.sh="$DEVENV_DIR/launch.sh" \
  --from-file=pod.yml="$DEVENV_DIR/k8s/pod.yml" \
  --from-file=headless-service.yml="$DEVENV_DIR/k8s/headless-service.yml" \
  --from-file=setup-repos.sh="$DEVENV_DIR/k8s/setup-repos.sh" \
  --dry-run=client -o yaml | oc apply -f -

echo "==> UI deployment"
oc apply -f 03-deployment.yaml

echo "==> orchestrator deployment"
oc apply -f 07-orchestrator-deployment.yaml
oc rollout status deploy/agentd-orchestrator -n "$NS" --timeout=300s || true
oc rollout restart deploy/agentd-ui -n "$NS" >/dev/null 2>&1 || true
oc rollout status deploy/agentd-ui -n "$NS" --timeout=180s || true

echo "==> service + passthrough route"
oc apply -f 04-service.yaml

echo "==> done. URL: https://$HOST"
