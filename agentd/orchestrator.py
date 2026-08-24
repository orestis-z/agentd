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
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from pathlib import Path

import yaml
from croniter import croniter as Croniter

from agentd.config import load_task, resolve_skill

NAMESPACE = os.environ.get("DEVENV_NAMESPACE", "machine-learning")
AGENTD_NAMESPACE = "agentd"
DEFAULT_GPU_TYPE = "a100"
DEFAULT_TIMEOUT = None
STALE_POD_AGE = 14400  # 4 hours


def _user() -> str:
    return os.environ.get("USER", os.environ.get("LOGNAME", "unknown")).lower()


def _instance_name(task_name: str) -> str:
    return task_name.lower().replace(" ", "-").replace("_", "-")


def pod_name(task_name: str) -> str:
    return f"devenv-{_user()}-{_instance_name(task_name)}"


def _oc_login():
    server = os.environ.get("OC_LOGIN_SERVER", "")
    password = os.environ.get("OC_LOGIN_PASSWORD", "")
    if not server or not password:
        return False
    result = subprocess.run(
        ["oc", "login", server, "--insecure-skip-tls-verify",
         "-u", _user(), "-p", password],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("=== Re-authenticated with OpenShift ===")
        return True
    print(f"WARNING: oc login failed: {result.stderr.strip()}")
    return False


def _is_auth_error(exc):
    msg = str(exc).lower()
    stderr = ""
    if hasattr(exc, "stderr") and exc.stderr:
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        stderr = stderr.lower()
    return any(k in msg or k in stderr for k in (
        "unauthorized", "forbidden", "must be logged in",
        "token has expired", "login",
    ))


def _run_cmd(cmd, **kwargs):
    try:
        return subprocess.run(cmd, check=True, **kwargs)
    except subprocess.CalledProcessError as e:
        if _is_auth_error(e) and _oc_login():
            return subprocess.run(cmd, check=True, **kwargs)
        raise


def _read_k8s_secret(secret_name: str, key: str = "value") -> str:
    try:
        result = subprocess.run(
            ["oc", "get", "secret", secret_name, "-n", AGENTD_NAMESPACE,
             "-o", f"jsonpath={{.data.{key}}}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not result.stdout:
            return ""
        import base64
        return base64.b64decode(result.stdout).decode()
    except Exception:
        return ""


def _write_failure_log(log_dir: Path, task_name: str, error: str, task_data: dict | None = None):
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    log_path = log_dir / f"{run_id}.jsonl"
    start_record = {"event": "task_start", "task": task_name, "ts": now}
    if task_data:
        for k in ("model", "gpus", "gpu_type", "dispatched_by"):
            if task_data.get(k) is not None:
                start_record[k] = task_data[k]
    result_record = {
        "event": "task_result", "task": task_name, "ts": now,
        "is_error": True, "subtype": "orchestrator_error",
        "result": error, "error": error,
        "num_turns": 0, "total_cost_usd": 0, "duration_ms": 0,
    }
    with log_path.open("w") as f:
        f.write(json.dumps(start_record) + "\n")
        f.write(json.dumps(result_record) + "\n")


def _resolve_task_for_pod(task_path: Path) -> Path:
    """Resolve skill references on the bastion so the pod doesn't need them."""
    with task_path.open() as f:
        data = yaml.safe_load(f)
    skill_ref = data.get("skill")
    if not skill_ref:
        return task_path
    github_token = None
    if data.get("github_token_secret"):
        github_token = _read_k8s_secret(data["github_token_secret"])
    skill_content = resolve_skill(skill_ref, github_token=github_token)
    skill_content = f"Skill source: {skill_ref}\n\n{skill_content}"
    existing = data.get("system_prompt", "")
    data["system_prompt"] = (skill_content + "\n\n" + existing) if existing else skill_content
    data.setdefault("prompt", "Follow the instructions in the system prompt.")
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
    # launch.sh exits non-zero when tmux attach fails (expected: stdin is /dev/null).
    # The oc wait below is the real readiness check.
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

    ca_cert_path = "/tmp/agentd-custom-ca.pem"
    ca_cert_pem = ""
    if task.ca_cert_secret:
        ca_cert_pem = _read_k8s_secret(task.ca_cert_secret)

    setup_script = (
        "set -ex && "
        f"mkdir -p {remote_task_dir} /tmp/agentd-logs && chmod 1777 /tmp/agentd-logs && "
        "WS_GID=$(stat -c '%g' /workspace 2>/dev/null || echo 0) && "
        "{ getent group $WS_GID &>/dev/null || groupadd -g $WS_GID workspace; } && "
        "{ id claude-runner &>/dev/null 2>&1 || useradd -M -d /root -g $WS_GID -G 0 claude-runner; } && "
        "chmod g+rx /root && "
        "chmod -R g+rX /root/.claude 2>/dev/null || true && "
        "chmod -R g+rwX /root/.config /root/.cache /root/.triton 2>/dev/null || true && "
        "mkdir -p /root/.triton && chmod 1777 /root/.triton && "
        "chmod g+r /root/.claude/.credentials.json 2>/dev/null || true && "
        "chmod -R g+wX /workspace 2>/dev/null || true && "
        f"{git_config}"
        "python3 -c '"
        'import json, pathlib; '
        'p = pathlib.Path("/root/.claude.json"); '
        'data = json.loads(p.read_text()) if p.exists() else {}; '
        'data.setdefault("projects", {})["/workspace"] = {"hasTrustDialogAccepted": True}; '
        "p.write_text(json.dumps(data))'"
    )
    print("=== Running pod setup ===")
    result = subprocess.run(
        ["oc", "exec", pod, "-n", NAMESPACE, "--", "bash", "-c", setup_script],
        capture_output=True, text=True,
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"Pod setup failed (exit {result.returncode})")

    cwd = task.cwd or "/workspace"
    agentd_rules = (
        "# agentd rules (auto-injected)\n"
        "Do NOT use git worktrees or the EnterWorktree tool — work directly in the repository.\n"
        "Each Bash tool call runs in a fresh shell — environment variables, venv activation, "
        "and cd do not persist across calls. Use absolute paths and set variables within the same command.\n"
    )
    _run_cmd([
        "oc", "exec", pod, "-n", NAMESPACE, "--",
        "bash", "-c",
        f"mkdir -p {cwd}/.claude && "
        f"RULES={repr(agentd_rules)} && "
        f"if [ -f {cwd}/.claude/CLAUDE.md ]; then "
        f"  grep -q 'agentd rules' {cwd}/.claude/CLAUDE.md || "
        f"  printf '\\n%s' \"$RULES\" >> {cwd}/.claude/CLAUDE.md; "
        f"else "
        f"  printf '%s' \"$RULES\" > {cwd}/.claude/CLAUDE.md; "
        f"fi",
    ])

    if ca_cert_pem:
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as tmp:
            tmp.write(ca_cert_pem)
            tmp_path = tmp.name
        try:
            _run_cmd(["oc", "cp", tmp_path, f"{NAMESPACE}/{pod}:{ca_cert_path}"])
            _run_cmd(["oc", "exec", pod, "-n", NAMESPACE, "--", "chmod", "644", ca_cert_path])
        finally:
            os.unlink(tmp_path)

    resolved_task_path = _resolve_task_for_pod(task_path)
    _run_cmd([
        "oc", "cp", str(resolved_task_path),
        f"{NAMESPACE}/{pod}:{remote_task_path}",
    ])
    _run_cmd([
        "oc", "exec", pod, "-n", NAMESPACE, "--",
        "chmod", "644", remote_task_path,
    ])

    ui_url = os.environ.get("AGENTD_UI_URL", "")
    env_exports = "export AGENTD_LOG_DIR=/tmp/agentd-logs && "
    if ui_url:
        env_exports += f"export AGENTD_UI_URL='{ui_url}' && "
    if task.anthropic_base_url:
        api_key = ""
        if task.anthropic_api_key_secret:
            api_key = _read_k8s_secret(task.anthropic_api_key_secret)
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
    if ca_cert_pem:
        env_exports += f"export NODE_EXTRA_CA_CERTS='{ca_cert_path}' && "
    if task.anthropic_auth_token_secret:
        auth_token = _read_k8s_secret(task.anthropic_auth_token_secret)
        if auth_token:
            env_exports += f"export ANTHROPIC_AUTH_TOKEN='{auth_token}' && "
    if task.github_token_secret:
        token = _read_k8s_secret(task.github_token_secret)
        if token:
            env_exports += f"export GITHUB_TOKEN='{token}' && export GH_TOKEN='{token}' && "
    if task.notify and task.notify.slack_webhook_secret:
        webhook = _read_k8s_secret(task.notify.slack_webhook_secret)
        if webhook:
            env_exports += f"export AGENTD_SLACK_WEBHOOK='{webhook}' && "

    pre_run = ""
    if task.pre_run:
        pre_run = f"{task.pre_run} && "

    agent_cmd = f"source /root/.bashrc 2>/dev/null; {pre_run}python -m agentd --task {remote_task_path}"
    result = subprocess.run(
        [
            "oc", "exec", pod, "-n", NAMESPACE, "--",
            "bash", "-c",
            f"{env_exports}"
            f"sudo -E -u claude-runner bash -i -c {repr(agent_cmd)}",
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
            ["oc", "delete", "pvc", pvc, "-n", NAMESPACE, "--wait"],
            timeout=120,
        )
    except Exception:
        try:
            subprocess.run(
                ["oc", "patch", "pvc", pvc, "-n", NAMESPACE,
                 "-p", '{"metadata":{"finalizers":null}}'],
                timeout=30,
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
    task: "TaskConfig",
    devenv_dir: Path,
    log_dir: Path,
    completed_dir: Path,
    failed_dir: Path,
):
    gpus = task.gpus if task.gpus is not None else 1
    gpu_type = task.gpu_type or DEFAULT_GPU_TYPE
    timeout = task.timeout if task.timeout is not None else DEFAULT_TIMEOUT

    timeout_str = f"{timeout}s" if timeout else "none"
    print(f"\n=== Task: {task.name} | {gpus}x {gpu_type} | timeout {timeout_str} ===")

    with _active_lock:
        _active_tasks[task_path.name] = task.name

    running_marker = task_path.parent / f".running-{task_path.name}"
    try:
        shutil.copy2(str(task_path), str(running_marker))
        provision_pod(task.name, gpus, gpu_type, devenv_dir)
        exit_code = run_task_in_pod(task_path, task, timeout)

        dest = completed_dir if exit_code == 0 else failed_dir
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(task_path), str(dest / task_path.name))

        status = "completed" if exit_code == 0 else "failed"
        print(f"=== Task {task.name} {status} (exit {exit_code}) ===")
    except Exception as e:
        print(f"ERROR: {task.name}: {e}")
        _write_failure_log(log_dir, task.name, str(e), {
            "model": task.model, "gpus": task.gpus,
            "gpu_type": task.gpu_type, "dispatched_by": task.dispatched_by,
        })
        failed_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(task_path), str(failed_dir / task_path.name))
    finally:
        with _active_lock:
            _active_tasks.pop(task_path.name, None)
        copy_logs_out(task.name, log_dir)
        running_marker.unlink(missing_ok=True)
        resolved = task_path.parent / f".resolved-{task_path.name}"
        resolved.unlink(missing_ok=True)
        teardown_pod(task.name, devenv_dir)


_active_tasks: dict[str, str] = {}  # task_path.name -> task.name
_active_lock = threading.Lock()
_devenv_dir: Path | None = None


def _register_signal_handlers():
    def _handler(signum, _frame):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if _devenv_dir:
            with _active_lock:
                tasks = list(_active_tasks.values())
            for task_name in tasks:
                teardown_pod(task_name, _devenv_dir)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def watch_queue(
    queue_dir: Path,
    devenv_dir: Path,
    log_dir: Path,
    poll_interval: int = 30,
    schedule_dir: Path | None = None,
    max_parallel: int = 8,
):
    global _devenv_dir
    _devenv_dir = devenv_dir

    completed_dir = queue_dir.parent / "completed"
    failed_dir = queue_dir.parent / "failed"

    _register_signal_handlers()
    cleanup_stale_pods()

    for marker in queue_dir.glob(".running-*.yml"):
        print(f"=== Cleaning stale running marker: {marker.name} ===")
        marker.unlink()

    print(f"=== Orchestrator watching {queue_dir} (poll every {poll_interval}s, max parallel {max_parallel}) ===")
    if schedule_dir:
        print(f"=== Checking schedules in {schedule_dir} ===")

    futures: dict[str, Future] = {}

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        while True:
            done = [k for k, f in futures.items() if f.done()]
            for k in done:
                exc = futures[k].exception()
                if exc:
                    print(f"ERROR: task thread {k} raised: {exc}")
                del futures[k]

            if schedule_dir and schedule_dir.is_dir():
                check_schedules(schedule_dir, queue_dir)

            tasks = sorted(
                (p for p in queue_dir.glob("*.yml") if not p.name.startswith(".")),
                key=lambda p: p.stat().st_mtime,
            )

            for task_path in tasks:
                if len(futures) >= max_parallel:
                    break
                if task_path.name in futures:
                    continue

                try:
                    task = load_task(task_path)
                except Exception as e:
                    print(f"ERROR: invalid task {task_path}: {e}")
                    try:
                        with task_path.open() as f:
                            task_data = yaml.safe_load(f) or {}
                    except Exception:
                        task_data = {}
                    _write_failure_log(
                        log_dir, task_data.get("name", task_path.stem),
                        f"Invalid task: {e}", task_data,
                    )
                    failed_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(task_path), str(failed_dir / task_path.name))
                    continue

                future = pool.submit(
                    process_task, task_path, task, devenv_dir, log_dir,
                    completed_dir, failed_dir,
                )
                futures[task_path.name] = future

            time.sleep(poll_interval)


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
        "--max-parallel", type=int, default=8,
        help="Maximum concurrent tasks (default: 8)",
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
        max_parallel=args.max_parallel,
    )


if __name__ == "__main__":
    main()
