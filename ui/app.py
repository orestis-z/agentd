from __future__ import annotations

import json
import os
import shutil
import glob
from pathlib import Path

from datetime import datetime, timezone

import yaml
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

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

    for path in glob.glob(os.path.join(QUEUE_DIR, "*.yml")):
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
            "started_raw": "",
            "status": "queued",
            "cost": None,
            "turns": None,
            "duration": None,
        })

    for path in glob.glob(os.path.join(LOG_DIR, "*.jsonl")):
        run_id = Path(path).stem
        first, last = _read_first_last(path)
        if not first:
            continue
        run = {
            "run_id": run_id,
            "run_id_short": run_id[:8],
            "task": first.get("task", "unknown"),
            "started": _format_ts(first.get("ts", "")),
            "started_raw": first.get("ts", ""),
            "model": first.get("model"),
            "gpus": first.get("gpus"),
            "gpu_type": first.get("gpu_type"),
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

    runs.sort(key=lambda r: r.get("started_raw") or "", reverse=True)
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


@app.route("/api/template/<filename>")
def api_template(filename):
    task_path = os.path.join(TASK_DIR, filename)
    if not os.path.isfile(task_path):
        return jsonify({}), 404
    with open(task_path) as f:
        data = yaml.safe_load(f) or {}
    return jsonify(data)


@app.route("/launch", methods=["POST"])
def launch():
    name = request.form.get("name", "").strip()
    prompt = request.form.get("prompt", "").strip()
    skill = request.form.get("skill", "").strip()
    if not name or (not prompt and not skill):
        flash("Name and either prompt or skill are required.", "error")
        return redirect(url_for("index"))

    task_data = {"name": name}
    if prompt:
        task_data["prompt"] = prompt
    if skill:
        task_data["skill"] = skill
        skill_args = request.form.get("skill_args", "").strip()
        if skill_args:
            task_data["skill_args"] = skill_args

    model = request.form.get("model", "").strip()
    if model:
        task_data["model"] = model

    system_prompt = request.form.get("system_prompt", "").strip()
    if system_prompt:
        task_data["system_prompt"] = system_prompt

    try:
        gpus = int(request.form.get("gpus", "0"))
        task_data["gpus"] = gpus
    except ValueError:
        pass

    gpu_type = request.form.get("gpu_type", "").strip()
    if gpu_type:
        task_data["gpu_type"] = gpu_type

    try:
        max_turns = request.form.get("max_turns", "").strip()
        if max_turns:
            task_data["max_turns"] = int(max_turns)
    except ValueError:
        pass

    try:
        max_budget = request.form.get("max_budget_usd", "").strip()
        if max_budget:
            task_data["max_budget_usd"] = float(max_budget)
    except ValueError:
        pass

    cwd = request.form.get("cwd", "").strip()
    if cwd:
        task_data["cwd"] = cwd

    allowed_tools = request.form.get("allowed_tools", "").strip()
    if allowed_tools:
        task_data["allowed_tools"] = [t.strip() for t in allowed_tools.split(",") if t.strip()]

    try:
        timeout = request.form.get("timeout", "").strip()
        if timeout:
            task_data["timeout"] = int(timeout)
    except ValueError:
        pass

    git_name = request.form.get("git_name", "").strip()
    if git_name:
        task_data["git_name"] = git_name

    git_email = request.form.get("git_email", "").strip()
    if git_email:
        task_data["git_email"] = git_email

    github_token_env = request.form.get("github_token_env", "").strip()
    if github_token_env:
        task_data["github_token_env"] = github_token_env

    anthropic_base_url = request.form.get("anthropic_base_url", "").strip()
    if anthropic_base_url:
        task_data["anthropic_base_url"] = anthropic_base_url

    slug = name.lower().replace(" ", "-").replace("_", "-")
    filename = f"{slug}.yml"
    os.makedirs(QUEUE_DIR, exist_ok=True)
    with open(os.path.join(QUEUE_DIR, filename), "w") as f:
        yaml.dump(task_data, f, default_flow_style=False, sort_keys=False)

    flash(f"Queued: {name}", "success")
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
        elif ev == "assistant_message":
            text = e.get("text") or ""
            tool_calls = e.get("tool_calls") or []
            tools_summary = ", ".join(tc["tool"] for tc in tool_calls)
            timeline.append({
                "ts": _format_ts(e.get("ts", "")),
                "event": ev,
                "tool": f"Turn {e.get('turn', '?')}",
                "detail": text[:300] if text else f"[{tools_summary}]" if tools_summary else "",
            })

    error_info = None
    if result and result.get("is_error"):
        error_info = {
            "error": result.get("error", "Unknown error"),
            "traceback": result.get("traceback"),
        }

    result_text = result.get("result") if result else None

    task_config = None
    if first.get("event") == "task_start":
        tc = {k: v for k, v in first.items() if k not in ("event", "ts", "run_id") and v is not None}
        if tc:
            task_config = json.dumps(tc, indent=2)

    raw_events = []
    for e in events:
        raw_events.append({
            "ts": e.get("ts", ""),
            "event": e.get("event", "unknown"),
            "json": json.dumps(e, indent=2),
        })

    return render_template(
        "run_detail.html",
        run=run_info,
        task_config=task_config,
        timeline=timeline,
        error_info=error_info,
        result_text=result_text,
        raw_events=raw_events,
    )


if __name__ == "__main__":
    os.makedirs(LOG_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
