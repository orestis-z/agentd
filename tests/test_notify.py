from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

from agentd.notify import post_slack


@patch("agentd.notify.urllib.request.urlopen")
def test_post_slack_success(mock_urlopen):
    post_slack(
        "https://hooks.slack.com/test",
        "my-task",
        "SUCCESS",
        "All good.",
        {"total_cost_usd": 1.5, "num_turns": 10, "duration_ms": 60000},
    )
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    payload = json.loads(req.data)
    assert payload["blocks"][0]["text"]["text"].startswith(":white_check_mark:")
    assert "my-task" in payload["blocks"][0]["text"]["text"]
    assert "All good." in payload["blocks"][1]["text"]["text"]
    assert len(payload["blocks"]) == 3  # header + section + context


@patch("agentd.notify.urllib.request.urlopen")
def test_post_slack_failure(mock_urlopen):
    post_slack(
        "https://hooks.slack.com/test",
        "broken-task",
        "FAILURE",
        "Something went wrong.",
    )
    req = mock_urlopen.call_args[0][0]
    payload = json.loads(req.data)
    assert ":x:" in payload["blocks"][0]["text"]["text"]
    assert len(payload["blocks"]) == 2  # no metadata context block


@patch("agentd.notify.urllib.request.urlopen", side_effect=Exception("network error"))
def test_post_slack_handles_error(mock_urlopen):
    post_slack("https://hooks.slack.com/test", "task", "SUCCESS", "ok")


@patch("agentd.notify.urllib.request.urlopen")
def test_post_slack_truncates(mock_urlopen):
    long_detail = "x" * 5000
    post_slack("https://hooks.slack.com/test", "task", "SUCCESS", long_detail)
    req = mock_urlopen.call_args[0][0]
    payload = json.loads(req.data)
    assert len(payload["blocks"][1]["text"]["text"]) == 2900
