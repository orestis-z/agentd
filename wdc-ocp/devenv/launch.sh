#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$SCRIPT_DIR"

# Auto-update
_old_head=$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null)
GIT_TERMINAL_PROMPT=0 git -C "$SCRIPT_DIR" pull --ff-only --quiet 2>/dev/null || true
if [ "$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null)" != "$_old_head" ]; then
    echo "devenv updated — re-running with latest version..."
    exec "$0" "$@"
fi

# Derive repo owner from git remote for image references
DEVENV_OWNER=$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null | sed -n 's|.*github.com[:/]\([^/]*\)/devenv.*|\1|p')
DEVENV_OWNER="${DEVENV_OWNER:-neuralmagic}"
export DEVENV_OWNER

ACTION=""
MODE=""
GPU_COUNT=1
GPU_TYPE=""
INSTANCE=""
FAST_STORAGE=""
WORKER=""
HEAD_INSTANCE=""
IMAGE_TAG="latest"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --down|--restart) ACTION="$1" ;;
        --cluster) MODE="cluster" ;;
        --local) MODE="local" ;;
        --gpus) GPU_COUNT="$2"; shift ;;
        --gpu-type) GPU_TYPE="$2"; shift ;;
        --name) INSTANCE="$2"; shift ;;
        --fast-storage) FAST_STORAGE=1 ;;
        --worker)
            WORKER=1
            if [[ -n "${2:-}" && "$2" != --* ]]; then
                HEAD_INSTANCE="$2"; shift
            fi
            ;;
        --tag) IMAGE_TAG="$2"; shift ;;
        *)
            echo "Usage: devenv [--restart|--down] [--cluster|--local] [--gpus N] [--gpu-type h100|a100] [--name INSTANCE] [--fast-storage] [--worker [HEAD_INSTANCE]] [--tag TAG]"
            exit 1
            ;;
    esac
    shift
done

GPU_TYPE="${GPU_TYPE:-a100}"

export USER="${USER:-$(whoami)}"; USER="${USER,,}"
export GPU_COUNT
export INSTANCE
export IMAGE_TAG

# Auto-detect mode
if [ -z "$MODE" ]; then
    if command -v oc &>/dev/null; then
        MODE="cluster"
    elif command -v docker &>/dev/null || command -v podman &>/dev/null; then
        MODE="local"
    else
        echo "ERROR: Neither oc nor docker/podman found"
        exit 1
    fi
fi

resolve_fast_storage_class() {
    echo "lvms-$(echo "$GPU_TYPE" | tr '[:upper:]' '[:lower:]')-tier1-storage"
}

# ── Cluster mode ──
if [ "$MODE" = "cluster" ]; then
    NAMESPACE="machine-learning"
    POD_NAME="devenv-${USER}${INSTANCE:+-$INSTANCE}"
    if [ -n "$WORKER" ]; then
        REPOS_PVC="devenv-workspace-${USER}${HEAD_INSTANCE:+-$HEAD_INSTANCE}"
    else
        REPOS_PVC="devenv-workspace-${USER}${INSTANCE:+-$INSTANCE}"
    fi
    VSCODE_SUBPATH="vscode${INSTANCE:+-$INSTANCE}"
    export POD_NAME REPOS_PVC VSCODE_SUBPATH
    DEVENV_CLUSTER=$(oc whoami --show-server 2>/dev/null | sed -n 's|.*api\.\([^.]*\)\..*|\1|p')
    export DEVENV_CLUSTER

    if ! oc whoami &>/dev/null 2>&1; then
        echo "ERROR: Not logged into OpenShift. Run: oc login <cluster-url>"
        exit 1
    fi

    if [ -n "$WORKER" ] && [ -z "$INSTANCE" ]; then
        echo "ERROR: --worker requires --name to identify the worker pod"
        exit 1
    fi

    # Create headless service for inter-pod DNS (idempotent)
    envsubst < "$SCRIPT_DIR/k8s/headless-service.yml" | oc apply -f - 2>/dev/null || true

    case "$ACTION" in
        --down)
            read -r -p "Delete pod $POD_NAME? [Y/n] " confirm
            if [ "${confirm,,}" = "n" ]; then exit 0; fi
            echo "Deleting pod $POD_NAME..."
            oc delete pod "$POD_NAME" -n "$NAMESPACE" 2>/dev/null || true
            echo "PVCs retained. To free storage, delete unused PVCs:"
            oc get pvc -n "$NAMESPACE" --no-headers 2>/dev/null | awk -v user="$USER" -v inst="${INSTANCE:+-$INSTANCE}" \
                '$1 ~ "devenv-(workspace|fast)-" user inst "$" {print "  oc delete pvc " $1}'
            exit 0
            ;;
        --restart)
            read -r -p "Restart pod $POD_NAME? [Y/n] " confirm
            if [ "${confirm,,}" = "n" ]; then exit 0; fi
            echo "Restarting pod $POD_NAME..."
            oc delete pod "$POD_NAME" -n "$NAMESPACE" 2>/dev/null || true
            sleep 2
            ;;
    esac

    # Create repos PVC if needed
    if ! oc get pvc "$REPOS_PVC" -n "$NAMESPACE" &>/dev/null; then
        echo "Creating repos PVC $REPOS_PVC..."
        cat <<PVCEOF | oc apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${REPOS_PVC}
  namespace: ${NAMESPACE}
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ocs-storagecluster-cephfs-tier2
  resources:
    requests:
      storage: 50Gi
PVCEOF
        echo "Waiting for PVC to bind..."
        if ! oc wait --for=jsonpath='{.status.phase}'=Bound pvc/"$REPOS_PVC" -n "$NAMESPACE" --timeout=120s; then
            echo "ERROR: PVC $REPOS_PVC failed to bind — the storage provisioner may be down."
            echo "  Check: oc describe pvc $REPOS_PVC -n $NAMESPACE"
            exit 1
        fi
        echo "Cloning repos onto $REPOS_PVC..."
        DEVENV_OWNER="${DEVENV_OWNER:-neuralmagic}" "$SCRIPT_DIR/k8s/setup-repos.sh"
    fi

    # Create fast storage PVC if requested
    if [ -n "$FAST_STORAGE" ]; then
        FAST_PVC="devenv-fast-${USER}${INSTANCE:+-$INSTANCE}"
        if ! oc get pvc "$FAST_PVC" -n "$NAMESPACE" &>/dev/null; then
            FAST_CLASS=$(resolve_fast_storage_class)
            echo "Creating fast storage PVC $FAST_PVC ($FAST_CLASS)..."
            cat <<FASTEOF | oc apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${FAST_PVC}
  namespace: ${NAMESPACE}
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: ${FAST_CLASS}
  resources:
    requests:
      storage: 200Gi
FASTEOF
        fi
    fi

    # Create pod if not running
    if ! oc get pod "$POD_NAME" -n "$NAMESPACE" &>/dev/null; then
        echo "Creating $POD_NAME with $GPU_COUNT GPU(s) on $GPU_TYPE..."
        POD_YAML=$(envsubst < "$SCRIPT_DIR/k8s/pod.yml")
        if [ -n "$FAST_STORAGE" ]; then
            FAST_PVC="devenv-fast-${USER}${INSTANCE:+-$INSTANCE}"
            POD_YAML=$(echo "$POD_YAML" | sed "s/volumeMounts:/volumeMounts:\\n        - name: fast\\n          mountPath: \/data\/fast/")
            POD_YAML=$(echo "$POD_YAML" | sed "s/  restartPolicy:/    - name: fast\\n      persistentVolumeClaim:\\n        claimName: ${FAST_PVC}\\n  restartPolicy:/")
        fi
        # Inject S3 object store credentials (shared cluster bucket)
        POD_YAML=$(echo "$POD_YAML" | sed "s/^      env:/      env:\\n        - name: AWS_ACCESS_KEY_ID\\n          valueFrom:\\n            secretKeyRef:\\n              name: ml-object-store\\n              key: AWS_ACCESS_KEY_ID\\n        - name: AWS_SECRET_ACCESS_KEY\\n          valueFrom:\\n            secretKeyRef:\\n              name: ml-object-store\\n              key: AWS_SECRET_ACCESS_KEY\\n        - name: S3_ENDPOINT_URL\\n          value: \"http:\/\/rook-ceph-rgw-ocs-storagecluster-cephobjectstore.openshift-storage.svc\"/")
        NODE_ROLE="up-$(echo "$GPU_TYPE" | tr '[:upper:]' '[:lower:]')mcp"
        POD_YAML=$(echo "$POD_YAML" | sed "s/  restartPolicy:/  nodeSelector:\\n    node-role.kubernetes.io\/$NODE_ROLE: \"\"\\n  restartPolicy:/")
        # Ray cluster setup
        HEAD_POD="devenv-${USER}${HEAD_INSTANCE:+-$HEAD_INSTANCE}"
        HEAD_DNS="${HEAD_POD}.devenv-svc-${USER}.${NAMESPACE}.svc.cluster.local"
        if [ -n "$WORKER" ]; then
            # Worker: join the head's Ray cluster, disable interactive tty
            POD_YAML=$(echo "$POD_YAML" | sed "s/^      env:/      env:\\n        - name: DEVENV_RAY_HEAD_ADDR\\n          value: \"${HEAD_DNS}\"/")
            POD_YAML=$(echo "$POD_YAML" | sed '/stdin: true/d; /tty: true/d')
        else
            # Head: start Ray head node
            POD_YAML=$(echo "$POD_YAML" | sed "s/^      env:/      env:\\n        - name: DEVENV_RAY_HEAD\\n          value: \"1\"/")
        fi
        # Set hostname to pod name for DNS resolution via headless service
        POD_YAML=$(echo "$POD_YAML" | sed "s/  restartPolicy:/  hostname: ${POD_NAME}\\n  subdomain: devenv-svc-${USER}\\n  restartPolicy:/")
        echo "$POD_YAML" | oc apply -f -
    fi

    # Wait for pod to be ready
    ready=$(oc get pod "$POD_NAME" -n "$NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    if [ "$ready" != "True" ]; then
        echo "Waiting for pod to be ready..."
        last_event=""
        last_status=""
        fail_count=0
        while true; do
            phase=$(oc get pod "$POD_NAME" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)
            ready=$(oc get pod "$POD_NAME" -n "$NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
            if [ "$ready" = "True" ]; then
                break
            fi
            if [ "$phase" = "Failed" ]; then
                echo "ERROR: Pod failed to start"
                oc describe pod "$POD_NAME" -n "$NAMESPACE" | tail -10
                exit 1
            fi
            container_status=$(oc get pod "$POD_NAME" -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].state}' 2>/dev/null | grep -oP '"reason":"[^"]*"' | head -1 | sed 's/"reason":"//;s/"//' || true)
            status_msg="${phase}${container_status:+ ($container_status)}"
            if [ -n "$status_msg" ] && [ "$status_msg" != "$last_status" ]; then
                echo "  $status_msg"
                last_status="$status_msg"
            fi
            event=$(oc get events -n "$NAMESPACE" --field-selector "involvedObject.name=$POD_NAME" --sort-by='.lastTimestamp' -o jsonpath='{.items[-1:].message}' 2>/dev/null || true)
            if [ -n "$event" ] && [ "$event" != "$last_event" ]; then
                echo "  $event"
                last_event="$event"
            fi
            scheduled=$(oc get pod "$POD_NAME" -n "$NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].status}' 2>/dev/null || true)
            sched_reason=$(oc get pod "$POD_NAME" -n "$NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].reason}' 2>/dev/null || true)
            if [ "$scheduled" = "False" ] && [ "$sched_reason" = "Unschedulable" ]; then
                fail_count=$((fail_count + 1))
                if [ "$fail_count" -ge 3 ]; then
                    sched_msg=$(oc get pod "$POD_NAME" -n "$NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].message}' 2>/dev/null)
                    echo "ERROR: Pod cannot be scheduled — $sched_msg"
                    if echo "$sched_msg" | grep -qi "PersistentVolumeClaim"; then
                        echo "  A PVC may not have bound. Check: oc get pvc -n $NAMESPACE | grep Pending"
                    fi
                    echo "  Delete the pod with: devenv${INSTANCE:+ --name $INSTANCE} --down"
                    exit 1
                fi
            else
                fail_count=0
            fi
            sleep 3
        done
    fi

    if [ -n "$WORKER" ]; then
        echo "Worker $POD_NAME is ready (Ray worker joined head at $HEAD_POD)"
        exit 0
    fi

    echo "Attaching to $POD_NAME (tmux)..."
    exec oc exec -it "$POD_NAME" -n "$NAMESPACE" -- tmux new-session -A -s main
fi

# ── Local mode (docker run) ──

IMAGE="ghcr.io/${DEVENV_OWNER}/devenv:${IMAGE_TAG}"
CONTAINER_NAME="devenv-${USER}${INSTANCE:+-$INSTANCE}"

# HF cache: prefer shared cache, fall back to personal
if [ -d "/mnt/data/engine/hf_cache" ]; then
    HF_CACHE_DIR="/mnt/data/engine/hf_cache"
elif [ -d "$HOME/hf_hub" ]; then
    HF_CACHE_DIR="$HOME/hf_hub"
else
    HF_CACHE_DIR="$HOME/.cache/huggingface"
    mkdir -p "$HF_CACHE_DIR"
fi

# Check that required repos exist
for repo in compressed-tensors llm-compressor speculators vllm; do
    if [ ! -d "$HOME/repos/$repo" ]; then
        echo "WARNING: $HOME/repos/$repo not found — container expects it mounted"
    fi
done

case "$ACTION" in
    --down)
        read -r -p "Stop and remove $CONTAINER_NAME? [Y/n] " confirm
        if [ "${confirm,,}" = "n" ]; then exit 0; fi
        echo "Stopping $CONTAINER_NAME..."
        docker stop "$CONTAINER_NAME" 2>/dev/null || true
        docker rm "$CONTAINER_NAME" 2>/dev/null || true
        exit 0
        ;;
    --restart)
        read -r -p "Restart $CONTAINER_NAME? [Y/n] " confirm
        if [ "${confirm,,}" = "n" ]; then exit 0; fi
        echo "Restarting $CONTAINER_NAME..."
        docker stop "$CONTAINER_NAME" 2>/dev/null || true
        docker rm "$CONTAINER_NAME" 2>/dev/null || true
        ;;
esac

# Start container if not running
if ! docker container inspect "$CONTAINER_NAME" > /dev/null 2>&1 || \
   [ "$(docker container inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" != "true" ]; then
    docker rm "$CONTAINER_NAME" 2>/dev/null || true
    echo "Pulling latest image..."
    docker pull "$IMAGE" || true
    echo "Starting $CONTAINER_NAME..."
    docker run -d \
        --name "$CONTAINER_NAME" \
        -e DEVENV_INSTANCE="$INSTANCE" \
        --gpus all \
        --network host \
        --shm-size=64g \
        -v "$HOME/repos/compressed-tensors:/workspace/compressed-tensors" \
        -v "$HOME/repos/llm-compressor:/workspace/llm-compressor" \
        -v "$HOME/repos/speculators:/workspace/speculators" \
        -v "$HOME/repos/vllm:/workspace/vllm" \
        -v "$HF_CACHE_DIR:/root/.cache/huggingface" \
        -v devenv-pip-cache:/pip-cache \
        -v "$HOME/.config/gcloud:/root/.config/gcloud" \
        -v "$HOME/.config/gh:/root/.config/gh" \
        -v "$HOME/.config/bk.yaml:/root/.config/bk.yaml" \
        "$IMAGE"
fi

echo "Attaching to $CONTAINER_NAME (tmux)..."
exec docker exec -it "$CONTAINER_NAME" tmux new-session -A -s main
