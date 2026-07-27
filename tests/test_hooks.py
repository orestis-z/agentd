from __future__ import annotations

import json
import io
import pytest
from agentd.hooks import _scan, _log, security_hook


def test_scan_aws_key():
    assert _scan("AKIAIOSFODNN7EXAMPLE") is not None


def test_scan_github_pat():
    assert _scan("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl") is not None


def test_scan_private_key():
    assert _scan("-----BEGIN RSA PRIVATE KEY-----") is not None


def test_scan_clean():
    assert _scan("echo hello world") is None


def test_scan_gcloud_token():
    assert _scan("gcloud auth print-access-token") is not None


@pytest.mark.asyncio
async def test_security_hook_blocks_secret():
    result = await security_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "echo AKIAIOSFODNN7EXAMPLE"},
    })
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_security_hook_allows_clean():
    result = await security_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
    })
    assert result == {}


@pytest.mark.asyncio
async def test_security_hook_blocks_write_secret():
    result = await security_hook({
        "tool_name": "Write",
        "tool_input": {"content": "key = 'sk-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef'", "file_path": "test.py"},
    })
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_log_includes_run_id(capsys):
    import agentd.hooks as hooks
    old_run_id = hooks.run_id
    try:
        hooks.run_id = "test-run-123"
        _log({"event": "test"})
        line = capsys.readouterr().err.strip()
        record = json.loads(line)
        assert record["run_id"] == "test-run-123"
        assert record["event"] == "test"
        assert "ts" in record
    finally:
        hooks.run_id = old_run_id


def test_log_writes_to_file():
    import agentd.hooks as hooks
    old_log_file = hooks._log_file
    old_run_id = hooks.run_id
    try:
        buf = io.StringIO()
        hooks._log_file = buf
        hooks.run_id = "file-test"
        _log({"event": "file_test"})
        line = buf.getvalue().strip()
        record = json.loads(line)
        assert record["event"] == "file_test"
        assert record["run_id"] == "file-test"
    finally:
        hooks._log_file = old_log_file
        hooks.run_id = old_run_id
