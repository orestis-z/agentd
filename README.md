# agentd

Headless AI agent runner for scheduled, unsupervised coding tasks. Wraps the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) with task definitions, security hooks, and K8s deployment manifests.

## Quickstart (bastion)

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

# Clone and install
git clone https://github.com/orestis-z/agentd.git
cd agentd
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e .

# Verify
agentd --task tasks/example-code-review.yml --dry-run

# Run the orchestrator (watches queue/, provisions pods, runs tasks)
mkdir -p queue
agentd-orchestrate --queue ./queue/ --devenv-dir ~/repos/devenv &

# Enqueue a task
cp tasks/example-gpu-eval.yml queue/
# The orchestrator will pick it up, spin up a pod, run the task, and tear down
```

Prerequisites:
- `oc` logged into the OpenShift cluster (`oc login ...`)
- [devenv](https://github.com/neuralmagic/devenv) repo cloned (for `launch.sh`)
- Vertex AI auth (`CLAUDE_CODE_USE_VERTEX=1`) or `ANTHROPIC_API_KEY` set

## Install

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e .
```

## Usage

```bash
# Run a task directly (inside a pod or locally)
agentd --task tasks/example-code-review.yml

# Dry run (print parsed config, don't execute)
agentd --task tasks/example-fix-test.yml --dry-run
```

## Task format

Tasks are YAML files:

```yaml
name: fix-flaky-test
prompt: |
  Find and fix flaky tests. Run pytest to verify. Commit the fix.
system_prompt: |                    # optional
  You are a developer working on speculators.
allowed_tools:                      # optional (defaults to all)
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
max_turns: 50                       # optional
max_budget_usd: 5.0                 # optional
cwd: /workspace/speculators         # optional
gpus: 4                             # optional (orchestrator)
gpu_type: h100                      # optional (orchestrator)
timeout: 3600                       # optional (orchestrator, seconds)
```

Required fields: `name`, `prompt`. The `gpus`, `gpu_type`, and `timeout` fields are used by the orchestrator to provision pods with the right resources.

## Security

A PreToolUse hook scans every tool invocation for secret patterns before execution:

- AWS access keys (`AKIA...`)
- GitHub PATs (`ghp_...`)
- API keys (`sk-...`)
- PEM private keys
- GCP token extraction commands

Matched invocations are denied. Extend `SECRET_PATTERNS` in `agentd/hooks.py`.

## Notifications

Tasks can optionally post to Slack on success, failure, or both:

```yaml
notify:
  slack_webhook_env: SLACK_WEBHOOK_URL   # env var name holding the webhook URL
  on: [success, failure]                  # default: [failure]
```

- `slack_webhook_env` — name of the environment variable containing the Slack webhook URL (not the URL itself)
- `on` — when to notify: `success`, `failure`, or both (default: `[failure]`)

On success, the agent's final result is posted. On failure, the error details are posted. Cost, turns, and duration metadata are included automatically.

Set the env var in your shell or K8s Secret:

```bash
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
agentd --task tasks/autopilot.yml
```

## vLLM model swap

Point at a local vLLM instance instead of Claude via Vertex AI:

```bash
ANTHROPIC_BASE_URL=http://vllm-svc:8000 \
ANTHROPIC_API_KEY=dummy \
ANTHROPIC_DEFAULT_SONNET_MODEL=my-model \
agentd --task tasks/fix-test.yml
```

The model must support tool calling (`--enable-auto-tool-choice`).

## Orchestrator

A bastion-level daemon that watches a queue directory for task YAMLs and manages the full pod lifecycle per task:

```bash
agentd-orchestrate --queue ./queue/ --devenv-dir ~/repos/devenv
```

For each queued task, the orchestrator:

1. Provisions an ephemeral pod via `launch.sh` with the GPU count/type from the task YAML
2. Copies the task YAML into the pod and runs `agentd` inside it
3. Extracts logs via `oc cp` before teardown
4. Deletes the pod and its workspace PVC to ensure a clean state

Options:

```
--queue DIR          Directory to watch for task YAML files
--devenv-dir DIR     Path to the devenv repo (contains launch.sh)
--log-dir DIR        Directory for extracted run logs (default: ./logs)
--poll-interval SEC  Seconds between queue polls (default: 30)
--namespace NS       OpenShift namespace (default: machine-learning)
```

Drop a task YAML into the queue directory to enqueue it. The orchestrator picks up the oldest file first and processes tasks sequentially. Completed tasks are moved to `completed/`, failed ones to `failed/`.

## K8s deployment

### CronJob (standalone)

Deploy individual tasks as CronJobs on OpenShift:

```bash
# Create task ConfigMap
oc create configmap agentd-tasks --from-file=tasks/ -n machine-learning

# Deploy CronJob
export TASK_NAME=code-review SCHEDULE="0 9 * * 1-5" NAMESPACE=machine-learning USER=$(whoami)
envsubst < k8s/cronjob.yml | oc apply -f -

# Apply network policy
oc apply -f k8s/networkpolicy.yml -n machine-learning
```

## Logging

Tool invocations are logged as JSONL to stderr and to per-run log files at `AGENTD_LOG_DIR/<run_id>.jsonl` (default: `./logs/`). Each run gets a UUID `run_id` included in every log line.

Set `AGENTD_LOG_DIR` to control where log files are written:

```bash
export AGENTD_LOG_DIR=/workspace/agentd-logs
agentd --task tasks/fix-test.yml
```

> **Note:** File-based logging is temporary until Loki is set up on the cluster. Once Loki is live, pod stderr will be the primary log source and file logging can be removed.

## Web UI

A lightweight Flask UI for managing runs and querying logs.

### Local

```bash
cd ui
pip install flask claude-agent-sdk pyyaml
AGENTD_LOG_DIR=../logs AGENTD_TASK_DIR=../tasks python app.py
```

Open `http://localhost:5000`.

### Docker

```bash
docker build -t agentd-ui ui/
docker run -p 5000:5000 \
  -v ./logs:/data/logs -e AGENTD_LOG_DIR=/data/logs \
  -v ./tasks:/tasks -e AGENTD_TASK_DIR=/tasks \
  agentd-ui
```

### OpenShift

```bash
export NAMESPACE=machine-learning
envsubst < k8s/ui-deployment.yml | oc apply -f -
```

### Features

- **Run list** — view past and active runs with status, cost, and duration
- **Launch** — start a new run from the available task definitions
- **Ask why** — ask questions about a run's logs (powered by Claude)

## Architecture

See the [research & plan document](https://gist.github.com/orestis-z/e5a3cd26c0d5c59d31c1e4368dbbf2bd) for full rationale, tool evaluations, and security design.
