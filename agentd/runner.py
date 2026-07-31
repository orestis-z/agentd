from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import uuid

from claude_agent_sdk import ClaudeAgentOptions, query

from agentd.config import load_task
import agentd.hooks as hooks
from agentd.hooks import logging_hook, security_hook
from agentd.notify import post_slack


def _log(record: dict) -> None:
    from agentd.hooks import _log as _do_log
    _do_log(record)


def _notify(task, exit_code: int, result_text: str, metadata: dict) -> None:
    if not task.notify:
        return
    webhook_url = os.environ.get(task.notify.slack_webhook_env)
    if not webhook_url:
        _log({
            "event": "notify_skip",
            "reason": f"env var {task.notify.slack_webhook_env} not set",
        })
        return

    if exit_code == 0 and "success" in task.notify.on:
        post_slack(webhook_url, task.name, "SUCCESS", result_text or "Task completed.", metadata)
    elif exit_code != 0 and "failure" in task.notify.on:
        detail = result_text or f"Task failed with exit code {exit_code}."
        post_slack(webhook_url, task.name, "FAILURE", detail, metadata)


async def run(task_path: str, dry_run: bool = False) -> int:
    task = load_task(task_path)

    if dry_run:
        print(json.dumps(dataclasses.asdict(task), indent=2, default=str))
        return 0

    hooks.run_id = str(uuid.uuid4())

    # temporary: file-based logging until Loki is set up
    log_dir = os.environ.get("AGENTD_LOG_DIR", os.path.join(os.getcwd(), "logs"))
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{hooks.run_id}.jsonl")
    hooks._log_file = open(log_path, "w")

    try:
        _log({
            "event": "task_start",
            "task": task.name,
            "prompt": task.prompt[:200],
            "model": task.model,
            "gpus": task.gpus,
            "gpu_type": task.gpu_type,
            "max_turns": task.max_turns,
            "max_budget_usd": task.max_budget_usd,
        })

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
        if task.model:
            options.model = task.model
        options.hooks = {
            "PreToolUse": [{"callback": security_hook}],
            "PostToolUse": [{"callback": logging_hook}],
        }

        exit_code = 2
        result_text = ""
        result_metadata = {}

        prompt = task.prompt or "Follow the instructions in the system prompt."
        try:
            async for msg in query(prompt=prompt, options=options):
                if hasattr(msg, "result"):
                    result_metadata = {
                        "num_turns": getattr(msg, "num_turns", None),
                        "total_cost_usd": getattr(msg, "total_cost_usd", None),
                        "duration_ms": getattr(msg, "duration_ms", None),
                    }
                    result_text = msg.result
                    _log({
                        "event": "task_result",
                        "task": task.name,
                        "is_error": getattr(msg, "is_error", False),
                        "subtype": getattr(msg, "subtype", None),
                        "result": result_text,
                        **result_metadata,
                    })
                    print(result_text)
                    exit_code = 1 if getattr(msg, "is_error", False) else 0
        except Exception as exc:
            import traceback
            error_text = traceback.format_exc()
            _log({
                "event": "task_result",
                "task": task.name,
                "is_error": True,
                "subtype": "crash",
                "error": str(exc),
                "traceback": error_text,
                **result_metadata,
            })
            result_text = result_text or str(exc)
            print(error_text, file=sys.stderr)
            exit_code = 2

        _notify(task, exit_code, result_text, result_metadata)
        return exit_code
    finally:
        hooks._log_file.close()
        hooks._log_file = None


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
