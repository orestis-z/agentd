from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from agentd.orchestrator import _instance_name, _user, pod_name


def test_instance_name():
    assert _instance_name("eval-model") == "agentd-eval-model"
    assert _instance_name("fix flaky test") == "agentd-fix-flaky-test"
    assert _instance_name("my_task") == "agentd-my-task"


def test_pod_name():
    with patch.dict(os.environ, {"USER": "alice"}):
        assert pod_name("eval-model") == "devenv-alice-agentd-eval-model"


def test_pod_name_uppercase_user():
    with patch.dict(os.environ, {"USER": "Alice"}):
        assert pod_name("eval") == "devenv-alice-agentd-eval"


def test_process_task_moves_to_completed(tmp_path):
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    completed_dir = tmp_path / "completed"
    failed_dir = tmp_path / "failed"
    log_dir = tmp_path / "logs"

    task_yaml = queue_dir / "test-task.yml"
    task_yaml.write_text("name: test\nprompt: do something\ngpus: 1\n")

    with patch("agentd.orchestrator.provision_pod"), \
         patch("agentd.orchestrator.run_task_in_pod", return_value=0), \
         patch("agentd.orchestrator.copy_logs_out"), \
         patch("agentd.orchestrator.teardown_pod"):
        from agentd.orchestrator import process_task
        process_task(task_yaml, Path("/fake/devenv"), log_dir, completed_dir, failed_dir)

    assert not task_yaml.exists()
    assert (completed_dir / "test-task.yml").exists()


def test_process_task_moves_to_failed(tmp_path):
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    completed_dir = tmp_path / "completed"
    failed_dir = tmp_path / "failed"
    log_dir = tmp_path / "logs"

    task_yaml = queue_dir / "test-task.yml"
    task_yaml.write_text("name: test\nprompt: do something\ngpus: 1\n")

    with patch("agentd.orchestrator.provision_pod"), \
         patch("agentd.orchestrator.run_task_in_pod", return_value=1), \
         patch("agentd.orchestrator.copy_logs_out"), \
         patch("agentd.orchestrator.teardown_pod"):
        from agentd.orchestrator import process_task
        process_task(task_yaml, Path("/fake/devenv"), log_dir, completed_dir, failed_dir)

    assert not task_yaml.exists()
    assert (failed_dir / "test-task.yml").exists()


def test_process_task_moves_to_failed_on_exception(tmp_path):
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    completed_dir = tmp_path / "completed"
    failed_dir = tmp_path / "failed"
    log_dir = tmp_path / "logs"

    task_yaml = queue_dir / "test-task.yml"
    task_yaml.write_text("name: test\nprompt: do something\ngpus: 1\n")

    with patch("agentd.orchestrator.provision_pod", side_effect=RuntimeError("pod failed")), \
         patch("agentd.orchestrator.teardown_pod"):
        from agentd.orchestrator import process_task
        process_task(task_yaml, Path("/fake/devenv"), log_dir, completed_dir, failed_dir)

    assert not task_yaml.exists()
    assert (failed_dir / "test-task.yml").exists()
