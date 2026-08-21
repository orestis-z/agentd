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
    webhook_url = os.environ.get("AGENTD_SLACK_WEBHOOK")
    if not webhook_url:
        _log({
            "event": "notify_skip",
            "reason": "AGENTD_SLACK_WEBHOOK env var not set",
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
        task_dict = dataclasses.asdict(task)
        task_dict.pop("notify", None)
        task_dict["task"] = task_dict.pop("name")
        _log({"event": "task_start", **task_dict})

        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
        )
        agentd_preamble = (
            "You are running as an automated agent (agentd). "
            "Do NOT use git worktrees or EnterWorktree — work directly in the repository. "
            "Each Bash tool call runs in a fresh shell — use absolute paths for Python "
            "and set environment variables within the same command, not across calls."
        )
        if task.system_prompt:
            options.system_prompt = agentd_preamble + "\n\n" + task.system_prompt
        else:
            options.system_prompt = agentd_preamble
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
        total_turns = 0
        total_cost = 0.0
        total_duration = 0

        prompt = task.prompt or "Follow the instructions in the system prompt."

        try:
            async for msg in query(prompt=prompt, options=options):
                content = getattr(msg, "content", None)
                if content is not None and isinstance(content, list):
                    total_turns += 1
                    text_parts = []
                    tool_calls = []
                    for block in content:
                        cls = type(block).__name__
                        if cls == "TextBlock" or (isinstance(block, dict) and block.get("type") == "text"):
                            text_parts.append(getattr(block, "text", "") or (block.get("text", "") if isinstance(block, dict) else ""))
                        elif cls == "ToolUseBlock" or (isinstance(block, dict) and block.get("type") == "tool_use"):
                            tool_calls.append({
                                "tool": getattr(block, "name", "") or (block.get("name", "") if isinstance(block, dict) else ""),
                                "input": getattr(block, "input", None) or (block.get("input") if isinstance(block, dict) else None),
                            })
                    _log({
                        "event": "assistant_message",
                        "turn": total_turns,
                        "text": "\n".join(text_parts) if text_parts else None,
                        "tool_calls": tool_calls or None,
                    })
                elif hasattr(msg, "result"):
                    total_cost += getattr(msg, "total_cost_usd", None) or 0.0
                    total_duration += getattr(msg, "duration_ms", None) or 0
                    result_text = msg.result
                    exit_code = 1 if getattr(msg, "is_error", False) else 0
                    _log({
                        "event": "task_result",
                        "task": task.name,
                        "is_error": getattr(msg, "is_error", False),
                        "subtype": getattr(msg, "subtype", None),
                        "result": result_text,
                        "num_turns": total_turns,
                        "total_cost_usd": total_cost,
                        "duration_ms": total_duration,
                    })
                    print(result_text)
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
                "num_turns": total_turns,
                "total_cost_usd": total_cost,
                "duration_ms": total_duration,
            })
            result_text = result_text or str(exc)
            print(error_text, file=sys.stderr)
            exit_code = 2

        result_metadata = {
            "num_turns": total_turns,
            "total_cost_usd": total_cost,
            "duration_ms": total_duration,
        }
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
