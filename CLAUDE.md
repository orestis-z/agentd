# agentd

Headless AI agent runner using the Claude Agent SDK. Runs scheduled, unsupervised coding tasks on OpenShift.

## Setup

Python 3.10+. Install deps:

```bash
pip install -e .
```

## Running

```bash
agentd --task tasks/example-code-review.yml
agentd --task tasks/example-fix-test.yml --dry-run
```

Requires Vertex AI auth (`CLAUDE_CODE_USE_VERTEX=1`) or `ANTHROPIC_API_KEY`.

## Tests

```bash
pip install -e ".[test]"
pytest tests/ -v
```

## Structure

- `agentd/runner.py` — CLI entry point, async query loop
- `agentd/config.py` — task YAML loader + validation
- `agentd/hooks.py` — PreToolUse security scanner, PostToolUse JSONL logger
- `agentd/orchestrator.py` — bastion-level daemon: queue watching, pod lifecycle, log extraction
- `tasks/` — task definitions (YAML)
- `k8s/` — CronJob, NetworkPolicy, and UI deployment manifests
