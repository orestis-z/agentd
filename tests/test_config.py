from __future__ import annotations

import pytest
from pathlib import Path
from agentd.config import load_task, resolve_skill, _github_blob_to_raw, NotifyConfig, DEFAULT_SLACK_WEBHOOK_SECRET


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
  slack_webhook_secret: my-webhook
""")
    task = load_task(p)
    assert task.notify is not None
    assert task.notify.slack_webhook_secret == "my-webhook"
    assert task.notify.on == ["failure"]


def test_load_notify_default_webhook_secret(tmp_path):
    p = _write_yaml(tmp_path, """
name: notified
prompt: do something
notify:
  on: [failure]
""")
    task = load_task(p)
    assert task.notify is not None
    assert task.notify.slack_webhook_secret == DEFAULT_SLACK_WEBHOOK_SECRET
    assert task.notify.on == ["failure"]


def test_load_notify_custom_on(tmp_path):
    p = _write_yaml(tmp_path, """
name: notified
prompt: do something
notify:
  slack_webhook_secret: my-webhook
  on: [success, failure]
""")
    task = load_task(p)
    assert set(task.notify.on) == {"success", "failure"}


def test_load_notify_invalid_on(tmp_path):
    p = _write_yaml(tmp_path, """
name: bad
prompt: do something
notify:
  on: [success, crash]
""")
    with pytest.raises(ValueError, match="invalid notify.on"):
        load_task(p)


def test_load_missing_name(tmp_path):
    p = _write_yaml(tmp_path, "prompt: do something\n")
    with pytest.raises(ValueError, match="name"):
        load_task(p)


def test_load_missing_prompt_and_skill(tmp_path):
    p = _write_yaml(tmp_path, "name: test\n")
    with pytest.raises(ValueError, match="prompt.*skill"):
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


def test_load_skill_only_no_prompt(tmp_path):
    skill_file = tmp_path / "my_skill.md"
    skill_file.write_text("You are a code reviewer.")
    p = _write_yaml(tmp_path, f"""
name: skill-only
skill: {skill_file}
""")
    task = load_task(p)
    assert task.name == "skill-only"
    assert task.prompt == ""
    assert task.system_prompt == "You are a code reviewer."


def test_load_skill_local(tmp_path):
    skill_file = tmp_path / "my_skill.md"
    skill_file.write_text("You are a code reviewer.")
    p = _write_yaml(tmp_path, f"""
name: skill-test
prompt: review this PR
skill: {skill_file}
""")
    task = load_task(p)
    assert task.skill == str(skill_file)
    assert task.system_prompt == "You are a code reviewer."


def test_load_skill_prepends_to_system_prompt(tmp_path):
    skill_file = tmp_path / "my_skill.md"
    skill_file.write_text("You are a code reviewer.")
    p = _write_yaml(tmp_path, f"""
name: skill-test
prompt: review this PR
skill: {skill_file}
system_prompt: Be concise.
""")
    task = load_task(p)
    assert task.system_prompt == "You are a code reviewer.\n\nBe concise."


def test_load_skill_args(tmp_path):
    p = _write_yaml(tmp_path, """
name: autopilot
prompt: run autopilot
skill_args: --days 120
""")
    task = load_task(p)
    assert "Skill arguments: --days 120" in task.prompt


def test_load_skill_args_not_present(tmp_path):
    p = _write_yaml(tmp_path, """
name: test
prompt: do something
""")
    task = load_task(p)
    assert task.prompt == "do something"
    assert task.skill_args is None


def test_github_blob_to_raw():
    url = "https://github.com/vllm-project/speculators/blob/main/.claude/skills/pr-review/SKILL.md"
    raw = _github_blob_to_raw(url)
    assert raw == "https://raw.githubusercontent.com/vllm-project/speculators/main/.claude/skills/pr-review/SKILL.md"


def test_github_blob_to_raw_passthrough():
    url = "https://example.com/some/file.md"
    assert _github_blob_to_raw(url) == url


def test_resolve_skill_local_file(tmp_path):
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("skill content")
    assert resolve_skill(str(skill_file)) == "skill content"


def test_resolve_skill_named(tmp_path, monkeypatch):
    skills_dir = tmp_path / ".claude" / "skills" / "my-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("named skill content")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_skill("my-skill") == "named skill content"


def test_resolve_skill_not_found():
    with pytest.raises(ValueError, match="Cannot resolve skill"):
        resolve_skill("nonexistent-skill-xyz-12345")
