from __future__ import annotations

import json
import urllib.request
import urllib.error

from agentd.hooks import _log


def post_slack(
    webhook_url: str,
    task_name: str,
    status: str,
    detail: str,
    metadata: dict | None = None,
) -> None:
    emoji = ":white_check_mark:" if status == "SUCCESS" else ":x:"
    detail = detail[:2900]

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} agentd: {task_name} — {status}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": detail},
        },
    ]

    if metadata:
        parts = []
        if metadata.get("total_cost_usd") is not None:
            parts.append(f"Cost: ${metadata['total_cost_usd']:.2f}")
        if metadata.get("num_turns") is not None:
            parts.append(f"Turns: {metadata['num_turns']}")
        if metadata.get("duration_ms") is not None:
            mins = metadata["duration_ms"] / 60_000
            parts.append(f"Duration: {mins:.1f}m")
        if parts:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " | ".join(parts)}],
            })

    payload = json.dumps({"blocks": blocks}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        _log({"event": "notify_error", "error": str(exc)})
