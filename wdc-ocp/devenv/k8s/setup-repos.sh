#!/bin/bash
set -e

USER="${USER:-$(whoami)}"; USER="${USER,,}"
NAMESPACE="machine-learning"
PVC_NAME="${REPOS_PVC:-devenv-workspace-${USER}}"
POD_NAME="devenv-setup-repos-${USER}"

REPOS=(
    "https://github.com/vllm-project/compressed-tensors.git"
    "https://github.com/vllm-project/llm-compressor.git"
    "https://github.com/vllm-project/speculators.git"
    "https://github.com/vllm-project/vllm.git"
)

echo "Creating setup pod to clone repos onto PVC ${PVC_NAME}..."

# Check PVC exists
if ! oc get pvc "$PVC_NAME" -n "$NAMESPACE" > /dev/null 2>&1; then
    echo "ERROR: PVC $PVC_NAME not found. Create PVCs first:"
    echo "  export USER=$USER && envsubst < k8s/pvc.yml | oc apply -f -"
    exit 1
fi

# Build clone commands
CLONE_CMDS=""
for repo in "${REPOS[@]}"; do
    repo_name=$(basename "$repo" .git)
    CLONE_CMDS="${CLONE_CMDS}if [ ! -d /workspace/${repo_name} ]; then git clone ${repo} /workspace/${repo_name}; else echo '${repo_name} already exists, pulling...'; git -C /workspace/${repo_name} pull --ff-only || true; fi; "
done

# Delete leftover setup pod if it exists
oc delete pod "$POD_NAME" -n "$NAMESPACE" --ignore-not-found 2>/dev/null

# Create a temporary pod to clone repos
cat <<EOF | oc apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${POD_NAME}
  namespace: ${NAMESPACE}
spec:
  containers:
    - name: setup
      image: ghcr.io/${DEVENV_OWNER:-neuralmagic}/devenv:latest
      command: ["/bin/bash", "-c", "${CLONE_CMDS} echo 'Done.' && sleep 5"]
      volumeMounts:
        - name: repos
          mountPath: /workspace
  volumes:
    - name: repos
      persistentVolumeClaim:
        claimName: ${PVC_NAME}
  restartPolicy: Never
EOF

echo "Waiting for pod to start (pulling image if needed)..."
last_event=""
wait_start=$SECONDS
while true; do
    phase=$(oc get pod "$POD_NAME" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null)
    case "$phase" in
        Running|Succeeded|Failed)
            break ;;
        *)
            if [ $(( SECONDS - wait_start )) -ge 120 ]; then
                echo "ERROR: Setup pod stuck — PVC may not have bound. Check:"
                echo "  oc describe pvc $PVC_NAME -n $NAMESPACE"
                oc delete pod "$POD_NAME" -n "$NAMESPACE" --ignore-not-found 2>/dev/null
                exit 1
            fi
            event=$(oc get events -n "$NAMESPACE" --field-selector "involvedObject.name=$POD_NAME" --sort-by='.lastTimestamp' -o jsonpath='{.items[-1:].message}' 2>/dev/null)
            if [ -n "$event" ] && [ "$event" != "$last_event" ]; then
                echo "  $event"
                last_event="$event"
            fi
            sleep 3 ;;
    esac
done

echo "Streaming logs..."
oc logs -f "$POD_NAME" -n "$NAMESPACE" || true
oc wait --for=jsonpath='{.status.phase}'=Succeeded pod/"$POD_NAME" -n "$NAMESPACE" --timeout=600s

echo "Cleaning up setup pod..."
oc delete pod "$POD_NAME" -n "$NAMESPACE"

echo "Repos cloned onto PVC ${PVC_NAME}."
