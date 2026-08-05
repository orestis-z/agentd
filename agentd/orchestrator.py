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

import yaml
from croniter import croniter as Croniter

from agentd.config import load_task, resolve_skill

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


def _resolve_task_for_pod(task_path: Path) -> Path:
    """Resolve skill references on the bastion so the pod doesn't need them."""
    with task_path.open() as f:
        data = yaml.safe_load(f)
    skill_ref = data.get("skill")
    if not skill_ref:
        return task_path
    skill_content = resolve_skill(skill_ref)
    existing = data.get("system_prompt", "")
    data["system_prompt"] = (skill_content + "\n\n" + existing) if existing else skill_content
    del data["skill"]
    skill_args = data.pop("skill_args", None)
    if skill_args:
        data["prompt"] = data.get("prompt", "") + f"\n\nSkill arguments: {skill_args}"
    resolved = task_path.parent / f".resolved-{task_path.name}"
    with resolved.open("w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return resolved


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
    )
    _run_cmd([
        "oc", "wait", "--for=condition=Ready",
        f"pod/{pod_name(task_name)}", "-n", NAMESPACE, "--timeout=1800s",
    ])


def run_task_in_pod(task_path: Path, task: "TaskConfig", timeout: int) -> int:
    pod = pod_name(task.name)
    remote_task_dir = "/tmp/agentd-tasks"
    remote_task_path = f"{remote_task_dir}/{task_path.name}"

    git_config = ""
    if task.git_name:
        git_config += f"git config --global user.name '{task.git_name}' && "
    if task.git_email:
        git_config += f"git config --global user.email '{task.git_email}' && "

    _run_cmd([
        "oc", "exec", pod, "-n", NAMESPACE, "--",
        "bash", "-c",
        "set -e && "
        "pip install git+https://github.com/orestis-z/agentd.git && "
        f"mkdir -p {remote_task_dir} /tmp/agentd-logs && chmod 1777 /tmp/agentd-logs && "
        "WS_GID=$(stat -c '%g' /workspace 2>/dev/null || echo 0) && "
        "getent group $WS_GID &>/dev/null || groupadd -g $WS_GID workspace && "
        "id claude-runner &>/dev/null 2>&1 || useradd -M -d /root -g $WS_GID -G 0 claude-runner && "
        "chmod g+rx /root && "
        "chmod -R g+rX /root/.config /root/.claude /root/.cache 2>/dev/null || true && "
        "chmod g+r /root/.claude/.credentials.json 2>/dev/null || true && "
        f"{git_config}"
        "python3 -c '"
        'import json, pathlib; '
        'p = pathlib.Path("/root/.claude.json"); '
        'data = json.loads(p.read_text()) if p.exists() else {}; '
        'data.setdefault("projects", {})["/workspace"] = {"hasTrustDialogAccepted": True}; '
        "p.write_text(json.dumps(data))'",
    ])

    resolved_task_path = _resolve_task_for_pod(task_path)
    _run_cmd([
        "oc", "cp", str(resolved_task_path),
        f"{NAMESPACE}/{pod}:{remote_task_path}",
    ])
    _run_cmd([
        "oc", "exec", pod, "-n", NAMESPACE, "--",
        "chmod", "644", remote_task_path,
    ])

    env_exports = "export AGENTD_LOG_DIR=/tmp/agentd-logs && "
    if task.anthropic_base_url:
        api_key = ""
        if task.anthropic_api_key_env:
            api_key = os.environ.get(task.anthropic_api_key_env, "")
        env_exports += f"export ANTHROPIC_BASE_URL='{task.anthropic_base_url}' && "
        env_exports += f"export ANTHROPIC_API_KEY='{api_key or 'dummy'}' && "
        env_exports += "unset CLAUDE_CODE_USE_VERTEX CLOUD_ML_REGION ANTHROPIC_VERTEX_PROJECT_ID && "
    else:
        project_id = task.vertex_project_id or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
        region = task.cloud_ml_region or os.environ.get("CLOUD_ML_REGION", "")
        use_vertex = os.environ.get("CLAUDE_CODE_USE_VERTEX", "")
        if use_vertex:
            env_exports += f"export CLAUDE_CODE_USE_VERTEX='{use_vertex}' && "
        if region:
            env_exports += f"export CLOUD_ML_REGION='{region}' && "
        if project_id:
            env_exports += f"export ANTHROPIC_VERTEX_PROJECT_ID='{project_id}' && "
    if task.github_token_env:
        token = os.environ.get(task.github_token_env, "")
        if token:
            env_exports += f"export GITHUB_TOKEN='{token}' && "
    if task.notify and task.notify.slack_webhook_env:
        webhook = os.environ.get(task.notify.slack_webhook_env, "")
        if webhook:
            env_exports += f"export {task.notify.slack_webhook_env}='{webhook}' && "

    result = subprocess.run(
        [
            "oc", "exec", pod, "-n", NAMESPACE, "--",
            "bash", "-c",
            f"{env_exports}"
            f"sudo -E -u claude-runner python -m agentd --task {remote_task_path}",
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


def check_schedules(schedule_dir: Path, queue_dir: Path):
    now = datetime.now(timezone.utc)

    for path in schedule_dir.glob("*.yml"):
        if path.name.startswith("."):
            continue
        try:
            with path.open() as f:
                data = yaml.safe_load(f)
        except Exception:
            continue

        schedule = data.get("schedule")
        if not schedule:
            continue

        slug = path.stem
        last_file = schedule_dir / f".last-{slug}"
        queue_path = queue_dir / path.name
        running_marker = queue_dir / f".running-{path.name}"
        if queue_path.exists() or running_marker.exists():
            continue

        if last_file.exists():
            try:
                base = datetime.fromisoformat(last_file.read_text().strip())
            except ValueError:
                base = now
        else:
            # First seen — record creation time so cron runs from here
            last_file.write_text(now.isoformat())
            print(f"=== Registered new schedule: {data.get('name', slug)} ({schedule}) ===")
            continue

        try:
            next_run = Croniter(schedule, base).get_next(datetime)
        except Exception:
            print(f"WARNING: invalid cron expression in {path}: {schedule}")
            continue

        if now >= next_run:
            task_data = dict(data)
            task_data.pop("schedule", None)
            queue_dir.mkdir(parents=True, exist_ok=True)
            with queue_path.open("w") as f:
                yaml.dump(task_data, f, default_flow_style=False, sort_keys=False)

            last_file.write_text(now.isoformat())
            print(f"=== Enqueued scheduled task: {data.get('name', slug)} ===")


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
    gpus = task.gpus if task.gpus is not None else 1
    gpu_type = task.gpu_type or DEFAULT_GPU_TYPE
    timeout = task.timeout if task.timeout is not None else DEFAULT_TIMEOUT

    print(f"\n=== Task: {task.name} | {gpus}x {gpu_type} | timeout {timeout}s ===")

    running_marker = task_path.parent / f".running-{task_path.name}"
    try:
        shutil.copy2(str(task_path), str(running_marker))
        provision_pod(task.name, gpus, gpu_type, devenv_dir)
        exit_code = run_task_in_pod(task_path, task, timeout)
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
        running_marker.unlink(missing_ok=True)
        resolved = task_path.parent / f".resolved-{task_path.name}"
        resolved.unlink(missing_ok=True)
        teardown_pod(task.name, devenv_dir)


_current_task: str | None = None
_devenv_dir: Path | None = None


def _register_signal_handlers():
    def _handler(signum, _frame):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
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
    schedule_dir: Path | None = None,
):
    global _current_task, _devenv_dir
    _devenv_dir = devenv_dir

    completed_dir = queue_dir.parent / "completed"
    failed_dir = queue_dir.parent / "failed"

    _register_signal_handlers()
    cleanup_stale_pods()

    print(f"=== Orchestrator watching {queue_dir} (poll every {poll_interval}s) ===")
    if schedule_dir:
        print(f"=== Checking schedules in {schedule_dir} ===")

    while True:
        if schedule_dir and schedule_dir.is_dir():
            check_schedules(schedule_dir, queue_dir)

        tasks = sorted(
            (p for p in queue_dir.glob("*.yml") if not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
        )
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
        "--schedule-dir", type=Path, default=None,
        help="Directory with scheduled task YAMLs (optional)",
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

    watch_queue(
        args.queue, args.devenv_dir, args.log_dir, args.poll_interval,
        schedule_dir=args.schedule_dir,
    )


if __name__ == "__main__":
    main()
