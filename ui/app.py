from __future__ import annotations

import json
import os
import shutil
import glob
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "agentd-ui-dev-key")

LOG_DIR = os.environ.get("AGENTD_LOG_DIR", "./logs")
TASK_DIR = os.environ.get("AGENTD_TASK_DIR", "./tasks")
QUEUE_DIR = os.environ.get("AGENTD_QUEUE_DIR", "./queue")


def _read_first_last(path: str) -> tuple[dict | None, dict | None]:
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return None, None
    first = json.loads(lines[0]) if lines else None
    last = None
    for line in reversed(lines):
        parsed = json.loads(line)
        if parsed.get("event") == "task_result":
            last = parsed
            break
    return first, last


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
            "started": first.get("ts", ""),
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


@app.route("/ask/<run_id>", methods=["GET", "POST"])
def ask(run_id):
    log_path = os.path.join(LOG_DIR, f"{run_id}.jsonl")
    if not os.path.isfile(log_path):
        flash("Run not found.", "error")
        return redirect(url_for("index"))

    first, last = _read_first_last(log_path)
    run_info = {
        "run_id": run_id,
        "run_id_short": run_id[:8],
        "task": first.get("task", "unknown") if first else "unknown",
        "started": first.get("ts", "") if first else "",
    }

    answer = None
    question = None
    if request.method == "POST":
        question = request.form.get("question", "").strip()
        if question:
            answer = _ask_claude(log_path, question)

    return render_template("ask.html", run=run_info, question=question, answer=answer)


def _ask_claude(log_path: str, question: str) -> str:
    with open(log_path) as f:
        logs = f.read()

    max_log_chars = 100_000
    if len(logs) > max_log_chars:
        logs = logs[:max_log_chars] + "\n... (truncated)"

    prompt = (
        f"You are analyzing logs from an AI agent run. "
        f"Answer the user's question based on these JSONL logs.\n\n"
        f"## Logs\n```\n{logs}\n```\n\n"
        f"## Question\n{question}"
    )

    try:
        import asyncio
        from claude_agent_sdk import ClaudeAgentOptions, query

        async def _query():
            options = ClaudeAgentOptions(
                permission_mode="bypassPermissions",
                allowed_tools=[],
                max_turns=1,
            )
            async for msg in query(prompt=prompt, options=options):
                if hasattr(msg, "result"):
                    return msg.result
            return "No response from Claude."

        return asyncio.run(_query())
    except Exception as exc:
        return f"Error querying Claude: {exc}"


if __name__ == "__main__":
    os.makedirs(LOG_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
