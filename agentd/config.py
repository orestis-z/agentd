from __future__ import annotations

import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml


VALID_NOTIFY_EVENTS = {"success", "failure"}
DEFAULT_SLACK_WEBHOOK_SECRET = "agentd-slack-webhook"


@dataclass
class NotifyConfig:
    on: list[str] = field(default_factory=lambda: ["failure"])
    slack_webhook_secret: str = DEFAULT_SLACK_WEBHOOK_SECRET


@dataclass
class TaskConfig:
    name: str
    prompt: str = ""
    skill: str | None = None
    skill_args: str | None = None
    system_prompt: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None
    max_budget_usd: float | None = None
    cwd: str | None = None
    model: str | None = None
    notify: NotifyConfig | None = None
    git_name: str | None = None
    git_email: str | None = None
    github_token_secret: str | None = None
    anthropic_base_url: str | None = None
    anthropic_api_key_secret: str | None = None
    anthropic_auth_token_secret: str | None = None
    ca_cert_secret: str | None = None
    vertex_project_id: str | None = None
    cloud_ml_region: str | None = None
    pre_run: str | None = None
    gpus: int | None = None
    gpu_type: str | None = None
    timeout: int | None = None
    schedule: str | None = None
    dispatched_by: str | None = None


def _github_blob_to_raw(url: str) -> str:
    return re.sub(
        r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)",
        r"https://raw.githubusercontent.com/\1/\2/\3",
        url,
    )


def resolve_skill(ref: str) -> str:
    if ref.startswith("http://") or ref.startswith("https://"):
        raw_url = _github_blob_to_raw(ref)
        with urllib.request.urlopen(raw_url) as resp:
            return resp.read().decode()

    ref_path = Path(os.path.expanduser(ref))
    if ref_path.is_file():
        return ref_path.read_text()

    skills_dir = Path(os.path.expanduser("~/.claude/skills"))
    skill_file = skills_dir / ref / "SKILL.md"
    if skill_file.is_file():
        return skill_file.read_text()

    raise ValueError(f"Cannot resolve skill: {ref}")


def load_task(path: str | Path) -> TaskConfig:
    path = Path(path)
    with path.open() as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    if "name" not in data:
        raise ValueError(f"{path}: missing required field 'name'")
    if "prompt" not in data and "skill" not in data:
        raise ValueError(f"{path}: requires 'prompt' or 'skill'")

    notify = None
    if "notify" in data:
        n = data["notify"]
        if not isinstance(n, dict):
            raise ValueError(f"{path}: notify must be a mapping")
        # YAML parses bare `on:` as boolean True — handle both keys
        on = n.get("on", n.get(True, ["failure"]))
        invalid = set(on) - VALID_NOTIFY_EVENTS
        if invalid:
            raise ValueError(f"{path}: invalid notify.on values: {invalid}")
        notify = NotifyConfig(
            on=on,
            slack_webhook_secret=n.get("slack_webhook_secret", DEFAULT_SLACK_WEBHOOK_SECRET),
        )

    skill_ref = data.get("skill")
    skill_args = data.get("skill_args")
    system_prompt = data.get("system_prompt")
    if skill_ref:
        skill_content = resolve_skill(skill_ref)
        if system_prompt:
            system_prompt = skill_content + "\n\n" + system_prompt
        else:
            system_prompt = skill_content

    prompt = data.get("prompt", "")
    if skill_args:
        prompt = f"{prompt}\n\nSkill arguments: {skill_args}"

    return TaskConfig(
        name=data["name"],
        prompt=prompt,
        skill=skill_ref,
        skill_args=skill_args,
        system_prompt=system_prompt,
        allowed_tools=data.get("allowed_tools", []),
        max_turns=data.get("max_turns"),
        max_budget_usd=data.get("max_budget_usd"),
        cwd=data.get("cwd"),
        model=data.get("model"),
        notify=notify,
        git_name=data.get("git_name"),
        git_email=data.get("git_email"),
        github_token_secret=data.get("github_token_secret"),
        anthropic_base_url=data.get("anthropic_base_url"),
        anthropic_api_key_secret=data.get("anthropic_api_key_secret"),
        anthropic_auth_token_secret=data.get("anthropic_auth_token_secret"),
        ca_cert_secret=data.get("ca_cert_secret"),
        vertex_project_id=data.get("vertex_project_id"),
        cloud_ml_region=data.get("cloud_ml_region"),
        pre_run=data.get("pre_run"),
        gpus=data.get("gpus"),
        gpu_type=data.get("gpu_type"),
        timeout=data.get("timeout"),
        schedule=data.get("schedule"),
        dispatched_by=data.get("dispatched_by"),
    )
