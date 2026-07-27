from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

run_id: str | None = None
_log_file = None  # temporary: file-based logging until Loki is set up

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9_]{36,}"),
    re.compile(r"ghs_[A-Za-z0-9_]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
    re.compile(r"gcloud\s+.*print-access-token"),
]


def _scan(text: str) -> str | None:
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group()[:20] + "..."
    return None


def _log(record: dict) -> None:
    record["ts"] = datetime.now(timezone.utc).isoformat()
    if run_id is not None:
        record.setdefault("run_id", run_id)
    line = json.dumps(record, default=str)
    print(line, file=sys.stderr, flush=True)
    # temporary: file-based logging until Loki is set up
    if _log_file is not None:
        _log_file.write(line + "\n")
        _log_file.flush()


async def security_hook(input_data, tool_use_id=None, context=None):
    tool_name = getattr(input_data, "tool_name", None) or input_data.get("tool_name", "")
    tool_input = getattr(input_data, "tool_input", None) or input_data.get("tool_input", {})

    text_to_scan = ""
    if tool_name == "Bash":
        text_to_scan = tool_input.get("command", "")
    elif tool_name in ("Write", "Edit"):
        text_to_scan = tool_input.get("content", "") + tool_input.get("new_string", "")

    if text_to_scan:
        found = _scan(text_to_scan)
        if found:
            reason = f"Blocked: secret pattern detected ({found})"
            _log({"event": "security_deny", "tool": tool_name, "reason": reason})
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

    return {}


async def logging_hook(input_data, tool_use_id=None, context=None):
    tool_name = getattr(input_data, "tool_name", None) or input_data.get("tool_name", "")
    tool_input = getattr(input_data, "tool_input", None) or input_data.get("tool_input", {})
    session_id = getattr(input_data, "session_id", None) or input_data.get("session_id", "")

    summary = ""
    if tool_name == "Bash":
        summary = tool_input.get("command", "")[:200]
    elif tool_name in ("Read", "Glob", "Grep"):
        summary = str(tool_input)[:200]
    elif tool_name in ("Write", "Edit"):
        path = tool_input.get("file_path", "")
        summary = path

    _log({
        "event": "tool_use",
        "tool": tool_name,
        "input": summary,
        "session_id": session_id,
    })

    return {}
