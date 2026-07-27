# agentd

Headless AI agent runner for scheduled, unsupervised coding tasks. Wraps the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) with task definitions, security hooks, and K8s deployment manifests.

## Install

```bash
pip install -e .
```

## Usage

```bash
# Run a task
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
```

Required fields: `name`, `prompt`.

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

## K8s deployment

Deploy as a CronJob on OpenShift:

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

Tool invocations are logged as JSONL to stderr. The final result goes to stdout. On OpenShift, both are picked up by the cluster log aggregator.

## Architecture

See the [research & plan document](https://gist.github.com/orestis-z/e5a3cd26c0d5c59d31c1e4368dbbf2bd) for full rationale, tool evaluations, and security design.
