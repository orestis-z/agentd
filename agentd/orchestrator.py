"""Bastion-level orchestrator for agentd tasks.

Long-lived daemon that watches a queue directory for task YAMLs, provisions
ephemeral pods with the right GPU count via launch.sh, runs agentd inside
them, copies logs out, and tears everything down.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agentd.config import load_task

NAMESPACE = os.environ.get("DEVENV_NAMESPACE", "machine-learning")
DEFAULT_GPU_TYPE = "a100"
DEFAULT_TIMEOUT = 3600
STALE_POD_AGE = 14400  # 4 hours


def _user() -> str:
    return os.environ.get("USER", os.environ.get("LOGNAME", "unknown")).lower()


def _instance_name(task_name: str) -> str:
    slug = task_name.lower().replace(" ", "-").replace("_", "-")
    return f"agentd-{slug}"


def pod_name(task_name: str) -> str:
    return f"devenv-{_user()}-{_instance_name(task_name)}"


def _run_cmd(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def provision_pod(task_name: str, gpus: int, gpu_type: str, devenv_dir: Path):
    instance = _instance_name(task_name)
    print(f"=== Provisioning pod {pod_name(task_name)} ({gpus}x {gpu_type}) ===")
    subprocess.run(
        [
            str(devenv_dir / "launch.sh"),
            "--name", instance,
            "--gpus", str(gpus),
            "--gpu-type", gpu_type,
            "--cluster",
        ],
        stdin=subprocess.DEVNULL,
        env={**os.environ, "DEVENV_CLAUDE_RUNNER": "1"},
    )
    _run_cmd([
        "oc", "wait", "--for=condition=Ready",
        f"pod/{pod_name(task_name)}", "-n", NAMESPACE, "--timeout=1800s",
    ])


def run_task_in_pod(task_path: Path, task_name: str, timeout: int) -> int:
    pod = pod_name(task_name)
    remote_task_dir = "/tmp/agentd-tasks"
    remote_task_path = f"{remote_task_dir}/{task_path.name}"

    _run_cmd([
        "oc", "exec", pod, "-n", NAMESPACE, "--",
        "bash", "-c",
        f"pip install git+https://github.com/orestis-z/agentd.git && "
        f"mkdir -p {remote_task_dir} /tmp/agentd-logs",
    ])

    _run_cmd([
        "oc", "cp", str(task_path),
        f"{NAMESPACE}/{pod}:{remote_task_path}",
    ])

    result = subprocess.run(
        [
            "oc", "exec", pod, "-n", NAMESPACE, "--",
            "bash", "-c",
            f"export AGENTD_LOG_DIR=/tmp/agentd-logs && "
            f"su -s /bin/bash claude-runner -c 'python -m agentd --task {remote_task_path}'",
        ],
        timeout=timeout,
    )
    return result.returncode


def copy_logs_out(task_name: str, log_dir: Path):
    pod = pod_name(task_name)
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        _run_cmd([
            "oc", "cp",
            f"{NAMESPACE}/{pod}:/tmp/agentd-logs/.",
            str(log_dir),
        ])
    except subprocess.CalledProcessError:
        print(f"WARNING: failed to copy logs from {pod}")


def teardown_pod(task_name: str, devenv_dir: Path):
    instance = _instance_name(task_name)
    pod = pod_name(task_name)
    print(f"=== Tearing down {pod} ===")

    try:
        subprocess.run(
            [str(devenv_dir / "launch.sh"), "--name", instance, "--down"],
            input=b"y\n",
            timeout=120,
        )
    except Exception:
        pass

    pvc = f"devenv-workspace-{_user()}-{instance}"
    try:
        subprocess.run(
            ["oc", "delete", "pvc", pvc, "-n", NAMESPACE, "--wait=false"],
            timeout=60,
        )
    except Exception:
        pass


def cleanup_stale_pods():
    prefix = f"devenv-{_user()}-agentd-"
    try:
        result = subprocess.run(
            [
                "oc", "get", "pods", "-n", NAMESPACE,
                "-o", "jsonpath={range .items[*]}{.metadata.name} {.metadata.creationTimestamp}{\"\\n\"}{end}",
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            name, ts = parts
            if not name.startswith(prefix):
                continue
            created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if age > STALE_POD_AGE:
                print(f"Deleting stale pod {name} (age {int(age)}s)")
                subprocess.run(
                    ["oc", "delete", "pod", name, "-n", NAMESPACE, "--wait=false"],
                )
    except Exception:
        pass


def process_task(
    task_path: Path,
    devenv_dir: Path,
    log_dir: Path,
    completed_dir: Path,
    failed_dir: Path,
):
    task = load_task(task_path)
    gpus = task.gpus or 1
    gpu_type = task.gpu_type or DEFAULT_GPU_TYPE
    timeout = task.timeout or DEFAULT_TIMEOUT

    print(f"\n=== Task: {task.name} | {gpus}x {gpu_type} | timeout {timeout}s ===")

    try:
        provision_pod(task.name, gpus, gpu_type, devenv_dir)
        exit_code = run_task_in_pod(task_path, task.name, timeout)
        copy_logs_out(task.name, log_dir)

        dest = completed_dir if exit_code == 0 else failed_dir
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(task_path), str(dest / task_path.name))

        status = "completed" if exit_code == 0 else "failed"
        print(f"=== Task {task.name} {status} (exit {exit_code}) ===")
    except Exception as e:
        print(f"ERROR: {task.name}: {e}")
        failed_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(task_path), str(failed_dir / task_path.name))
    finally:
        teardown_pod(task.name, devenv_dir)


_current_task: str | None = None
_devenv_dir: Path | None = None


def _register_signal_handlers():
    def _handler(signum, _frame):
        if _current_task and _devenv_dir:
            teardown_pod(_current_task, _devenv_dir)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def watch_queue(
    queue_dir: Path,
    devenv_dir: Path,
    log_dir: Path,
    poll_interval: int = 30,
):
    global _current_task, _devenv_dir
    _devenv_dir = devenv_dir

    completed_dir = queue_dir.parent / "completed"
    failed_dir = queue_dir.parent / "failed"

    _register_signal_handlers()
    cleanup_stale_pods()

    print(f"=== Orchestrator watching {queue_dir} (poll every {poll_interval}s) ===")

    while True:
        tasks = sorted(queue_dir.glob("*.yml"), key=lambda p: p.stat().st_mtime)
        if not tasks:
            time.sleep(poll_interval)
            continue

        task_path = tasks[0]
        try:
            task = load_task(task_path)
            _current_task = task.name
        except Exception as e:
            print(f"ERROR: invalid task {task_path}: {e}")
            failed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(task_path), str(failed_dir / task_path.name))
            continue

        process_task(task_path, devenv_dir, log_dir, completed_dir, failed_dir)
        _current_task = None


def main():
    global NAMESPACE

    parser = argparse.ArgumentParser(
        prog="agentd-orchestrate",
        description="Bastion-level orchestrator for agentd tasks",
    )
    parser.add_argument(
        "--queue", required=True, type=Path,
        help="Directory to watch for task YAML files",
    )
    parser.add_argument(
        "--devenv-dir", required=True, type=Path,
        help="Path to the devenv repo (contains launch.sh)",
    )
    parser.add_argument(
        "--log-dir", type=Path, default=Path("logs"),
        help="Directory for extracted run logs (default: ./logs)",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=30,
        help="Seconds between queue polls (default: 30)",
    )
    parser.add_argument(
        "--namespace", default=NAMESPACE,
        help=f"OpenShift namespace (default: {NAMESPACE})",
    )
    args = parser.parse_args()

    NAMESPACE = args.namespace

    lock_path = args.queue / ".orchestrator.lock"
    args.queue.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("ERROR: another orchestrator is already running")
        sys.exit(1)

    watch_queue(args.queue, args.devenv_dir, args.log_dir, args.poll_interval)


if __name__ == "__main__":
    main()
