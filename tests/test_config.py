from __future__ import annotations

import pytest
from pathlib import Path
from agentd.config import load_task, NotifyConfig


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "task.yml"
    p.write_text(content)
    return p


def test_load_minimal(tmp_path):
    p = _write_yaml(tmp_path, "name: test\nprompt: do something\n")
    task = load_task(p)
    assert task.name == "test"
    assert task.prompt == "do something"
    assert task.notify is None
    assert task.allowed_tools == []


def test_load_full(tmp_path):
    p = _write_yaml(tmp_path, """
name: full
prompt: do all the things
system_prompt: be helpful
allowed_tools:
  - Bash
  - Read
max_turns: 10
max_budget_usd: 3.5
cwd: /workspace
""")
    task = load_task(p)
    assert task.name == "full"
    assert task.system_prompt == "be helpful"
    assert task.allowed_tools == ["Bash", "Read"]
    assert task.max_turns == 10
    assert task.max_budget_usd == 3.5
    assert task.cwd == "/workspace"


def test_load_notify_default_on(tmp_path):
    p = _write_yaml(tmp_path, """
name: notified
prompt: do something
notify:
  slack_webhook_env: MY_WEBHOOK
""")
    task = load_task(p)
    assert task.notify is not None
    assert task.notify.slack_webhook_env == "MY_WEBHOOK"
    assert task.notify.on == ["failure"]


def test_load_notify_custom_on(tmp_path):
    p = _write_yaml(tmp_path, """
name: notified
prompt: do something
notify:
  slack_webhook_env: MY_WEBHOOK
  on: [success, failure]
""")
    task = load_task(p)
    assert set(task.notify.on) == {"success", "failure"}


def test_load_notify_invalid_on(tmp_path):
    p = _write_yaml(tmp_path, """
name: bad
prompt: do something
notify:
  slack_webhook_env: MY_WEBHOOK
  on: [success, crash]
""")
    with pytest.raises(ValueError, match="invalid notify.on"):
        load_task(p)


def test_load_notify_missing_webhook(tmp_path):
    p = _write_yaml(tmp_path, """
name: bad
prompt: do something
notify:
  on: [failure]
""")
    with pytest.raises(ValueError, match="slack_webhook_env"):
        load_task(p)


def test_load_missing_name(tmp_path):
    p = _write_yaml(tmp_path, "prompt: do something\n")
    with pytest.raises(ValueError, match="name"):
        load_task(p)


def test_load_missing_prompt(tmp_path):
    p = _write_yaml(tmp_path, "name: test\n")
    with pytest.raises(ValueError, match="prompt"):
        load_task(p)


def test_load_infra_fields(tmp_path):
    p = _write_yaml(tmp_path, """
name: gpu-task
prompt: run eval
gpus: 4
gpu_type: h100
timeout: 3600
""")
    task = load_task(p)
    assert task.gpus == 4
    assert task.gpu_type == "h100"
    assert task.timeout == 3600


def test_load_infra_fields_default_none(tmp_path):
    p = _write_yaml(tmp_path, "name: test\nprompt: do something\n")
    task = load_task(p)
    assert task.gpus is None
    assert task.gpu_type is None
    assert task.timeout is None
