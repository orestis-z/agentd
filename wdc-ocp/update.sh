#!/usr/bin/env bash
# Quick update: rebuild UI image, push, and restart both deployments.
# For full infra changes (RBAC, PVCs, ConfigMaps), use apply.sh instead.
set -euo pipefail

IMAGE="ghcr.io/${GHCR_OWNER:-orestis-z}/agentd-ui:latest"
NS=agentd
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UI_DIR="$SCRIPT_DIR/../ui"

echo "==> building UI image"
docker build -t "$IMAGE" "$UI_DIR"

echo "==> pushing $IMAGE"
docker push "$IMAGE"

echo "==> restarting orchestrator (re-installs agentd from main)"
oc rollout restart deploy/agentd-orchestrator -n "$NS"

echo "==> restarting UI (pulls new image)"
oc rollout restart deploy/agentd-ui -n "$NS"

echo "==> waiting for rollouts"
oc rollout status deploy/agentd-orchestrator -n "$NS" --timeout=300s
oc rollout status deploy/agentd-ui -n "$NS" --timeout=180s

echo "==> done"
