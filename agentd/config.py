from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


VALID_NOTIFY_EVENTS = {"success", "failure"}


@dataclass
class NotifyConfig:
    slack_webhook_env: str
    on: list[str] = field(default_factory=lambda: ["failure"])


@dataclass
class TaskConfig:
    name: str
    prompt: str
    system_prompt: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None
    max_budget_usd: float | None = None
    cwd: str | None = None
    notify: NotifyConfig | None = None


def load_task(path: str | Path) -> TaskConfig:
    path = Path(path)
    with path.open() as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    for key in ("name", "prompt"):
        if key not in data:
            raise ValueError(f"{path}: missing required field '{key}'")

    notify = None
    if "notify" in data:
        n = data["notify"]
        if not isinstance(n, dict) or "slack_webhook_env" not in n:
            raise ValueError(f"{path}: notify requires 'slack_webhook_env'")
        # YAML parses bare `on:` as boolean True — handle both keys
        on = n.get("on", n.get(True, ["failure"]))
        invalid = set(on) - VALID_NOTIFY_EVENTS
        if invalid:
            raise ValueError(f"{path}: invalid notify.on values: {invalid}")
        notify = NotifyConfig(slack_webhook_env=n["slack_webhook_env"], on=on)

    return TaskConfig(
        name=data["name"],
        prompt=data["prompt"],
        system_prompt=data.get("system_prompt"),
        allowed_tools=data.get("allowed_tools", []),
        max_turns=data.get("max_turns"),
        max_budget_usd=data.get("max_budget_usd"),
        cwd=data.get("cwd"),
        notify=notify,
    )
