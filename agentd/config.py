from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class TaskConfig:
    name: str
    prompt: str
    system_prompt: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None
    max_budget_usd: float | None = None
    cwd: str | None = None


def load_task(path: str | Path) -> TaskConfig:
    path = Path(path)
    with path.open() as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    for key in ("name", "prompt"):
        if key not in data:
            raise ValueError(f"{path}: missing required field '{key}'")

    return TaskConfig(
        name=data["name"],
        prompt=data["prompt"],
        system_prompt=data.get("system_prompt"),
        allowed_tools=data.get("allowed_tools", []),
        max_turns=data.get("max_turns"),
        max_budget_usd=data.get("max_budget_usd"),
        cwd=data.get("cwd"),
    )
