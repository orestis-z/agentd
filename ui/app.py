from __future__ import annotations

import json
import os
import shutil
import glob
from pathlib import Path

from datetime import datetime, timezone

from flask import Flask, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "agentd-ui-dev-key")

LOG_DIR = os.environ.get("AGENTD_LOG_DIR", "./logs")
TASK_DIR = os.environ.get("AGENTD_TASK_DIR", "./tasks")
QUEUE_DIR = os.environ.get("AGENTD_QUEUE_DIR", "./queue")


def _format_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.total_seconds() < 60:
            return "just now"
        if delta.total_seconds() < 3600:
            mins = int(delta.total_seconds() / 60)
            return f"{mins}m ago"
        if delta.total_seconds() < 86400:
            hours = int(delta.total_seconds() / 3600)
            return f"{hours}h ago"
        if delta.days < 7:
            return dt.strftime("%a %H:%M")
        return dt.strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        return ts


def _read_first_last(path: str) -> tuple[dict | None, dict | None]:
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return None, None
    first = json.loads(lines[0]) if lines else None
    merged = None
    for line in lines:
        parsed = json.loads(line)
        if parsed.get("event") == "task_result":
            if merged is None:
                merged = parsed
            else:
                for k, v in parsed.items():
                    if v is not None:
                        merged[k] = v
    return first, merged


def _read_all_events(path: str) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []


def _list_runs() -> list[dict]:
    runs = []

    for path in sorted(glob.glob(os.path.join(QUEUE_DIR, "*.yml")), reverse=True):
        from agentd.config import load_task
        try:
            task = load_task(path)
            task_name = task.name
        except Exception:
            task_name = Path(path).stem
        runs.append({
            "run_id": None,
            "run_id_short": "-",
            "task": task_name,
            "started": "",
            "status": "queued",
            "cost": None,
            "turns": None,
            "duration": None,
        })

    for path in sorted(glob.glob(os.path.join(LOG_DIR, "*.jsonl")), reverse=True):
        run_id = Path(path).stem
        first, last = _read_first_last(path)
        if not first:
            continue
        run = {
            "run_id": run_id,
            "run_id_short": run_id[:8],
            "task": first.get("task", "unknown"),
            "started": _format_ts(first.get("ts", "")),
            "status": "running",
            "cost": None,
            "turns": None,
            "duration": None,
        }
        if last:
            run["status"] = "failed" if last.get("is_error") else "success"
            run["cost"] = last.get("total_cost_usd")
            run["turns"] = last.get("num_turns")
            if last.get("duration_ms") is not None:
                run["duration"] = f"{last['duration_ms'] / 60_000:.1f}m"
        runs.append(run)
    return runs


def _list_tasks() -> list[str]:
    tasks = []
    for path in sorted(glob.glob(os.path.join(TASK_DIR, "*.yml"))):
        tasks.append(Path(path).name)
    return tasks


@app.route("/")
def index():
    runs = _list_runs()
    task_names = sorted(set(r["task"] for r in runs))
    return render_template("runs.html", runs=runs, tasks=_list_tasks(), task_names=task_names)


@app.route("/launch", methods=["POST"])
def launch():
    task_name = request.form.get("task")
    if not task_name:
        flash("No task selected.", "error")
        return redirect(url_for("index"))

    task_path = os.path.join(TASK_DIR, task_name)
    if not os.path.isfile(task_path):
        flash(f"Task file not found: {task_name}", "error")
        return redirect(url_for("index"))

    os.makedirs(QUEUE_DIR, exist_ok=True)
    shutil.copy2(task_path, os.path.join(QUEUE_DIR, task_name))
    flash(f"Queued: {task_name}", "success")
    return redirect(url_for("index"))


@app.route("/delete/<run_id>", methods=["POST"])
def delete(run_id):
    log_path = os.path.join(LOG_DIR, f"{run_id}.jsonl")
    if os.path.isfile(log_path):
        os.remove(log_path)
        flash(f"Deleted run {run_id[:8]}", "success")
    else:
        flash("Run not found.", "error")
    return redirect(url_for("index"))


@app.route("/run/<run_id>")
def run_detail(run_id):
    log_path = os.path.join(LOG_DIR, f"{run_id}.jsonl")
    if not os.path.isfile(log_path):
        flash("Run not found.", "error")
        return redirect(url_for("index"))

    events = _read_all_events(log_path)
    first = events[0] if events else {}

    result = None
    for e in events:
        if e.get("event") == "task_result":
            if result is None:
                result = e
            else:
                for k, v in e.items():
                    if v is not None:
                        result[k] = v

    status = "running"
    if result:
        status = "failed" if result.get("is_error") else "success"

    run_info = {
        "run_id": run_id,
        "run_id_short": run_id[:8],
        "task": first.get("task", "unknown"),
        "started": _format_ts(first.get("ts", "")),
        "started_raw": first.get("ts", ""),
        "status": status,
        "model": first.get("model"),
        "gpus": first.get("gpus"),
        "gpu_type": first.get("gpu_type"),
        "max_turns": first.get("max_turns"),
        "max_budget_usd": first.get("max_budget_usd"),
        "cost": result.get("total_cost_usd") if result else None,
        "turns": result.get("num_turns") if result else None,
        "duration": f"{result['duration_ms'] / 60_000:.1f}m" if result and result.get("duration_ms") else None,
    }

    timeline = []
    for e in events:
        ev = e.get("event")
        if ev in ("tool_use", "security_deny"):
            timeline.append({
                "ts": _format_ts(e.get("ts", "")),
                "event": ev,
                "tool": e.get("tool", ""),
                "detail": e.get("input", "") if ev == "tool_use" else e.get("reason", ""),
            })

    error_info = None
    if result and result.get("is_error"):
        error_info = {
            "error": result.get("error", "Unknown error"),
            "traceback": result.get("traceback"),
        }

    result_text = result.get("result") if result else None

    return render_template(
        "run_detail.html",
        run=run_info,
        timeline=timeline,
        error_info=error_info,
        result_text=result_text,
    )


if __name__ == "__main__":
    os.makedirs(LOG_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
