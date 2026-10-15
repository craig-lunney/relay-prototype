"""
Agent to Agent v1 — one real Claude run through Relay.

The acceptance demo (demo/acceptance_demo.py) uses a scripted fake worker.
This script performs the single REAL run: the actual Claude Code CLI fixes the
deliberate bug in a throwaway git copy of test-repository/, under Relay's
normal controls, and Relay verifies the result independently.

It spends a small amount of Anthropic API credit. Run it only when you mean to.

    python demo/real_run.py                       # key file: ~/.relay_api_key.txt
    python demo/real_run.py --key-file PATH       # or a key file elsewhere
    python demo/real_run.py --report PATH         # where to write the Markdown report

Requirements: the `claude` CLI on PATH and a key file containing one line,
your Anthropic API key. The key is passed to Claude through Relay's
apiKeyHelper; this script never reads or prints it.

The task is `sample_task.json` unchanged except for its repository path. Its
numeric acceptance criteria are not checked by Relay automatically, so a
successful run ends in ACCEPTANCE_REQUIRED: you check them and run
`relay acknowledge-criteria` yourself. This script never acknowledges for you.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="One real Claude run through Relay.")
    parser.add_argument("--key-file", type=Path, default=Path.home() / ".relay_api_key.txt")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    if not args.key_file.is_file():
        print(f"Key file not found: {args.key_file}. Save your Anthropic API key there "
              "(one line) or pass --key-file.", file=sys.stderr)
        return 2
    if shutil.which("claude") is None and shutil.which("claude.cmd") is None:
        print("The `claude` CLI is not on PATH.", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="relay-real-"))
    repo = work / "target-repo"
    shutil.copytree(ROOT / "test-repository", repo,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"))
    git = ["git", "-c", "user.email=operator@relay.local", "-c", "user.name=Operator"]
    for a in (["init", "-b", "main"], ["add", "."], ["commit", "-m", "baseline"]):
        subprocess.run(git + a, cwd=repo, check=True, capture_output=True)
    task = json.loads((ROOT / "sample_task.json").read_text(encoding="utf-8"))
    task["repository"] = str(repo)
    task_file = work / "task.json"
    task_file.write_text(json.dumps(task, indent=2), encoding="utf-8")
    db = work / "relay.db"
    env = {**os.environ, "RELAY_API_KEY_FILE": str(args.key_file), "PYTHONDONTWRITEBYTECODE": "1"}

    def relay(*argv):
        r = subprocess.run([sys.executable, "-m", "relay", "--db", str(db), *argv],
                           cwd=str(ROOT), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        out = (r.stdout + r.stderr).strip().replace(str(work), "<work>")
        print(f"$ relay {' '.join(argv)}".replace(str(work), "<work>"))
        print(out)
        print(f"[exit {r.returncode}]\n")
        return r, out

    log = []
    r, out = relay("create-task", str(task_file))
    log.append(("create-task", r.returncode, out))
    with closing(sqlite3.connect(db)) as conn:
        task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
    r, out = relay("run-task", task_id, "--workspace", str(work / "ws"),
                   "--timeout", str(args.timeout), "--keep-workspace")
    log.append(("run-task", r.returncode, out))
    for cmd in (["status", task_id], ["audit", task_id], ["audit", "--verify"]):
        r, out = relay(*cmd)
        log.append((" ".join(cmd), r.returncode, out))

    with closing(sqlite3.connect(db)) as conn:
        status = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
        ex = conn.execute("SELECT exit_code, error, stdout FROM executions WHERE task_id=? "
                          "ORDER BY attempt_number DESC LIMIT 1", (task_id,)).fetchone()
        ver = conn.execute("SELECT details FROM events WHERE task_id=? AND "
                           "event_type='VERIFICATION_COMPLETED' ORDER BY sequence DESC LIMIT 1",
                           (task_id,)).fetchone()
    verification = json.loads(ver[0]) if ver else {}
    cost = None
    try:
        cost = json.loads(ex[2].strip().splitlines()[-1]).get("total_cost_usd")
    except (ValueError, IndexError, AttributeError):
        pass
    diff = ""
    if (work / "ws" / task_id).exists():
        diff = subprocess.run(["git", "diff"], cwd=work / "ws" / task_id, capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout or ""

    lines = [
        "# Agent to Agent v1 — real Claude run",
        "",
        f"Run at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} with the real "
        "Claude Code CLI as the Relay worker. `<work>` is a fresh temp dir.",
        "",
        f"- **Task:** `{task_id}` (sample_task.json)",
        f"- **Final status:** `{status}`",
        f"- **Worker exit code:** `{ex[0]}`" + (f" · error: {ex[1]}" if ex[1] else ""),
        f"- **API cost reported by Claude:** {'$%.4f' % cost if cost is not None else 'not reported'}",
        f"- **Verification passed:** `{verification.get('passed')}`",
        f"- **Files changed (Relay's manifest):** `{verification.get('files_changed')}`",
        f"- **Protected violations:** `{verification.get('protected_violations')}`",
        f"- **Acceptance verdicts:** `{verification.get('acceptance_results')}`",
        "",
        "## What the worker changed",
        "",
        "```diff",
        diff.strip() or "(no diff)",
        "```",
        "",
        "## Commands",
        "",
    ]
    for name, code, out in log:
        lines += [f"```\n$ relay {name}\n{out}\n[exit {code}]\n```", ""]
    if status == "ACCEPTANCE_REQUIRED":
        lines += ["## Your step", "",
                  "Check the UNVERIFIED criteria in the kept workspace, then run "
                  f"`relay --db <work>/relay.db acknowledge-criteria {task_id} --actor <you>` "
                  "(or `--reject`).", ""]
    report = args.report or (work / "real-run-report.md")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"Status: {status}. Report: {report}. Work dir kept for inspection: {work}")
    return 0 if status in ("COMPLETED", "ACCEPTANCE_REQUIRED") else 1


if __name__ == "__main__":
    sys.exit(main())
