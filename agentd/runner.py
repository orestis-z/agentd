from __future__ import annotations

import argparse
import asyncio
import json
import sys

from claude_agent_sdk import ClaudeAgentOptions, query

from agentd.config import load_task
from agentd.hooks import logging_hook, security_hook


def _log(record: dict) -> None:
    from agentd.hooks import _log as _do_log
    _do_log(record)


async def run(task_path: str, dry_run: bool = False) -> int:
    task = load_task(task_path)

    if dry_run:
        print(json.dumps(task.__dict__, indent=2, default=str))
        return 0

    _log({"event": "task_start", "task": task.name, "prompt": task.prompt[:200]})

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
    )
    if task.system_prompt:
        options.system_prompt = task.system_prompt
    if task.allowed_tools:
        options.allowed_tools = task.allowed_tools
    if task.max_turns is not None:
        options.max_turns = task.max_turns
    if task.max_budget_usd is not None:
        options.max_budget_usd = task.max_budget_usd
    if task.cwd:
        options.cwd = task.cwd
    options.hooks = {
        "PreToolUse": [{"callback": security_hook}],
        "PostToolUse": [{"callback": logging_hook}],
    }

    exit_code = 2
    async for msg in query(prompt=task.prompt, options=options):
        if hasattr(msg, "result"):
            _log({
                "event": "task_result",
                "task": task.name,
                "is_error": getattr(msg, "is_error", False),
                "subtype": getattr(msg, "subtype", None),
                "num_turns": getattr(msg, "num_turns", None),
                "total_cost_usd": getattr(msg, "total_cost_usd", None),
                "duration_ms": getattr(msg, "duration_ms", None),
            })
            print(msg.result)
            exit_code = 1 if getattr(msg, "is_error", False) else 0

    return exit_code


def main():
    parser = argparse.ArgumentParser(
        prog="agentd",
        description="Headless AI agent runner for scheduled coding tasks",
    )
    parser.add_argument("--task", required=True, help="Path to task YAML file")
    parser.add_argument("--dry-run", action="store_true", help="Print config without running")
    args = parser.parse_args()

    sys.exit(asyncio.run(run(args.task, args.dry_run)))


if __name__ == "__main__":
    main()
