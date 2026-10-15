"""
Agent to Agent v1 — acceptance demonstration.

Drives the real `relay` and `a2a` command-line interfaces end to end, as an
operator would, against a throwaway git copy of test-repository/.  The only
substitution is the worker: the real Claude Code CLI is replaced by the scripted
fake worker used by the test suite (tests/fake_worker.py), so the run is
deterministic and spends no API credit.

Every step states what must happen and checks it.  The script exits non-zero if
any expectation fails, and writes a Markdown transcript of every command, its
exit code and its output.

    python demo/acceptance_demo.py                       # transcript to a temp dir
    python demo/acceptance_demo.py --transcript PATH     # transcript to PATH

Nothing outside a fresh temporary directory is created or modified (apart from
Relay's own short-lived pytest cache directories, which it removes).
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HANG_SECONDS = 12  # must match the "hang" behaviour in tests/fake_worker.py
FAKE_WORKER = ROOT / "tests" / "fake_worker.py"
ISSUE = "https://github.com/craig-lunney/relay-prototype/issues/{n}"
EVIDENCE = "https://github.com/craig-lunney/relay-prototype/issues/{n}#issuecomment-demo"


def _force_remove(func, path, _exc_info):
    """rmtree helper: clear read-only flags (git objects on Windows), retry once."""
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


class Demo:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.repo = work / "target-repo"
        self.relay_db = work / "relay.db"
        self.a2a_db = work / "a2a.db"
        self.workspaces = work / "workspaces"
        self.bin = work / "bin"
        self.lines: list[str] = []
        self.results: list[tuple[str, bool]] = []
        self._task_n = 0
        self._setup()

    # ── environment ─────────────────────────────────────────────────────────
    def _setup(self) -> None:
        shutil.copytree(ROOT / "test-repository", self.repo,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        git = ["git", "-c", "user.email=demo@relay.local", "-c", "user.name=Demo"]
        for args in (["init", "-b", "main"], ["add", "."], ["commit", "-m", "baseline"]):
            subprocess.run(git + args, cwd=self.repo, check=True, capture_output=True)
        self.bin.mkdir()
        key = self.work / "fake-api-key.txt"
        key.write_text("sk-ant-demo-not-a-real-key", encoding="ascii")
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "RELAY_API_KEY_FILE": str(key),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def worker(self, mode: str) -> None:
        """Install the fake worker in 'claude' position with the given behaviour."""
        for old in self.bin.iterdir():
            old.unlink()
        if sys.platform == "win32":
            (self.bin / "claude.cmd").write_text(
                f'@"{sys.executable}" "{FAKE_WORKER}" {mode} %*\r\n', encoding="utf-8")
        else:
            shim = self.bin / "claude"
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_WORKER}" {mode} "$@"\n')
            shim.chmod(0o755)

    # ── transcript ──────────────────────────────────────────────────────────
    def section(self, title: str, purpose: str) -> None:
        self.lines += ["", f"## {title}", "", purpose, ""]
        print(f"\n== {title}")

    def run(self, tool: str, *argv: str, background: bool = False):
        db = ["--db", str(self.relay_db)] if tool == "relay" else \
             ["--db", str(self.a2a_db), "--relay-db", str(self.relay_db)]
        cmd = [sys.executable, "-m", tool, *db, *argv]
        shown = " ".join([tool, *argv]).replace(str(self.work), "<demo>")
        if background:
            self.lines += [f"```\n$ {shown}   (started in background)\n```"]
            return subprocess.Popen(cmd, cwd=str(ROOT), env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        r = subprocess.run(cmd, cwd=str(ROOT), env=self.env, capture_output=True,
                           text=True, timeout=300)
        out = (r.stdout + r.stderr).strip().replace(str(self.work), "<demo>")
        self.lines += [f"```\n$ {shown}\n{out}\n[exit {r.returncode}]\n```"]
        print(f"$ {shown}  -> exit {r.returncode}")
        return r

    def expect(self, what: str, ok: bool) -> None:
        self.results.append((what, ok))
        self.lines.append(f"- {'PASS' if ok else '**FAIL**'}: {what}")
        print(f"   {'PASS' if ok else 'FAIL'}: {what}")

    # ── relay helpers ───────────────────────────────────────────────────────
    def task_file(self, **overrides) -> Path:
        self._task_n += 1
        task = {
            "protocol_version": "0.1",
            "project_id": "acceptance-demo",
            "repository": str(self.repo),
            "base_ref": "main",
            "objective": "Fix the floor-division bug in calculate_discount_price.",
            "requirements": ["Change // to / in the discount_amount line"],
            "acceptance_criteria": ["All tests pass"],
            "requested_actions": ["read_repository", "modify_working_tree", "run_tests"],
            "forbidden_actions": ["push", "deploy", "merge", "modify_main", "modify_relay_policy"],
            "required_tests": ["test_calculator.py"],
            "approval_policy": "auto",
        }
        task.update(overrides)
        path = self.work / f"task-{self._task_n}.json"
        path.write_text(json.dumps(task, indent=2), encoding="utf-8")
        return path

    def create(self, **overrides):
        r = self.run("relay", "create-task", str(self.task_file(**overrides)))
        task_id = next((w for w in r.stdout.split() if w.startswith("TASK-")), None)
        return r, task_id

    def task_count(self) -> int:
        with closing(sqlite3.connect(self.relay_db)) as conn:
            return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    def wait_for_event(self, task_id: str, event_type: str, timeout: float):
        """Poll the audit log; return the wall-clock time the event was first seen."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with closing(sqlite3.connect(self.relay_db)) as conn:
                row = conn.execute("SELECT 1 FROM events WHERE task_id=? AND event_type=?",
                                   (task_id, event_type)).fetchone()
            if row:
                return time.time()
            time.sleep(0.2)
        return None

    def status(self, task_id: str) -> str:
        with closing(sqlite3.connect(self.relay_db)) as conn:
            return conn.execute("SELECT status FROM tasks WHERE task_id=?",
                                (task_id,)).fetchone()[0]

    def seconds_since_event(self, task_id: str, event_type: str) -> float | None:
        """Seconds between an audit event's recorded timestamp and now."""
        with closing(sqlite3.connect(self.relay_db)) as conn:
            row = conn.execute("SELECT timestamp FROM events WHERE task_id=? AND event_type=? "
                               "ORDER BY sequence LIMIT 1", (task_id, event_type)).fetchone()
        if not row:
            return None
        return (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds()

    def verification(self, task_id: str) -> dict:
        with closing(sqlite3.connect(self.relay_db)) as conn:
            row = conn.execute(
                "SELECT details FROM events WHERE task_id=? AND event_type='VERIFICATION_COMPLETED' "
                "ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def run_task(self, task_id: str, timeout: int = 60, background: bool = False):
        return self.run("relay", "run-task", task_id, "--workspace", str(self.workspaces),
                        "--timeout", str(timeout), "--keep-workspace", background=background)

    def a2a_task(self, tier: str, title: str):
        self._task_n += 1
        r = self.run("a2a", "create-task", "--title", title, "--tier", tier,
                     "--issue-url", ISSUE.format(n=self._task_n), "--actor", "craig")
        return r.stdout.split()[1] if r.returncode == 0 else None

    def advance(self, tid: str, state: str, actor: str, role: str, *extra: str):
        return self.run("a2a", "advance", tid, "--to", state, "--actor", actor,
                        "--role", role, *extra)


def scenarios(d: Demo) -> None:
    ev = EVIDENCE.format(n=1)

    # 1 ─────────────────────────────────────────────────────────────────────
    d.section("1. Successful task", "A correct fix is independently verified and COMPLETED.")
    d.worker("fix")
    r, ok_task = d.create()
    d.expect("task is validated and READY", r.returncode == 0 and "Status: READY" in r.stdout)
    r = d.run_task(ok_task)
    v = d.verification(ok_task)
    d.expect("run-task exits 0 and the task is COMPLETED",
             r.returncode == 0 and d.status(ok_task) == "COMPLETED")
    d.expect("verifier saw exactly calculator.py change; integrity checked; no protected violations",
             v.get("files_changed") == ["calculator.py"] and v.get("integrity_checked") is True
             and v.get("protected_violations") == [])
    d.expect('acceptance criterion "All tests pass" is PASS',
             v.get("acceptance_results") == {"All tests pass": "PASS"})
    r = d.run("relay", "audit", ok_task)
    d.expect("audit trail records creation, policy, execution, verification and completion",
             all(e in r.stdout for e in ("TASK_CREATED", "POLICY_CHECKED", "EXECUTION_STARTED",
                                         "VERIFICATION_COMPLETED", "TASK_COMPLETED")))

    # 2 ─────────────────────────────────────────────────────────────────────
    d.section("2. Test weakening is rejected",
              "The worker leaves the bug unfixed and redefines every test as `pass` "
              "with the same names (the audit's critical attack).")
    d.worker("neuter_redefine")
    _, weak = d.create()
    r = d.run_task(weak)
    v = d.verification(weak)
    d.expect("task is FAILED", r.returncode == 1 and d.status(weak) == "FAILED")
    d.expect("verifier reports the protected test file as modified and did not run it",
             any("test_calculator.py" in p for p in v.get("protected_violations", []))
             and v.get("test_exit_code") is None)

    d.section("3. Unchanged work and undeclared files are rejected", "")
    d.worker("noop")
    _, noop = d.create()
    d.run_task(noop)
    d.expect("a worker that changes nothing fails with no_op", d.status(noop) == "FAILED"
             and d.verification(noop).get("no_op") is True)
    d.worker("fix_new_file")
    _, extra = d.create()
    d.run_task(extra)
    v = d.verification(extra)
    d.expect("a correct fix plus an undeclared new file fails, and only because of that file",
             d.status(extra) == "FAILED" and v.get("unexpected_files") == ["NOTES.md"]
             and v.get("test_exit_code") == 0 and v.get("protected_violations") == [])

    # 4 ─────────────────────────────────────────────────────────────────────
    d.section("4. Forbidden capability", "A task that requests `push` is rejected by policy.")
    r, pushy = d.create(requested_actions=["read_repository", "modify_working_tree", "push"])
    with closing(sqlite3.connect(d.relay_db)) as conn:
        last = conn.execute("SELECT status FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
    d.expect("create-task exits 1 and the task is recorded as REJECTED",
             r.returncode == 1 and "REJECTED" in r.stderr and last[0] == "REJECTED")
    before = d.task_count()
    r = d.run("relay", "create-task", str(d.task_file(approved=True)))
    d.expect("a task file that tries to pre-approve itself is refused; no task is created",
             r.returncode == 1 and "unexpected field" in r.stderr and d.task_count() == before)

    # 5 ─────────────────────────────────────────────────────────────────────
    d.section("5. Manual approval gate", "A manual-policy task cannot run until a human approves it.")
    d.worker("fix")
    r, manual = d.create(approval_policy="manual")
    d.expect("task is BLOCKED", d.status(manual) == "BLOCKED")
    r = d.run_task(manual)
    d.expect("run-task refuses a BLOCKED task and changes nothing",
             r.returncode == 1 and d.status(manual) == "BLOCKED")
    d.run("relay", "approve-task", manual, "--actor", "craig", "--reason", "demo approval")
    r = d.run_task(manual)
    d.expect("after approval it runs and is COMPLETED", d.status(manual) == "COMPLETED")
    _, refused = d.create(approval_policy="manual")
    r = d.run("relay", "reject-task", refused, "--actor", "craig", "--reason", "not now")
    d.expect("a BLOCKED task can be rejected by a human (REJECTED, terminal)",
             r.returncode == 0 and d.status(refused) == "REJECTED")

    # 6 ─────────────────────────────────────────────────────────────────────
    d.section("6. Timeout and retry", "A hung worker times out; the operator retries.")
    d.worker("hang")
    _, slow = d.create()
    r = d.run_task(slow, timeout=3)
    worker_time = d.seconds_since_event(slow, "EXECUTION_STARTED")
    d.expect("task is TIMEOUT", r.returncode == 1 and d.status(slow) == "TIMEOUT")
    d.expect(f"the 3 s timeout is enforced: the worker tree is killed well before its "
             f"{HANG_SECONDS} s sleep ends",
             worker_time is not None and worker_time < HANG_SECONDS - 2)
    d.run("relay", "retry-task", slow, "--actor", "craig")
    d.expect("retry-task makes it READY", d.status(slow) == "READY")
    d.worker("fix")
    d.run_task(slow)
    d.expect("the retry completes", d.status(slow) == "COMPLETED")

    # 7 ─────────────────────────────────────────────────────────────────────
    d.section("7. Crash recovery",
              "Relay itself is killed mid-execution. `relay recover` detects it; the operator "
              "accepts recovery; the interrupted workspace is preserved; the task re-runs.")
    d.worker("hang")
    _, crash = d.create()
    proc = d.run_task(crash, timeout=60, background=True)
    started_at = d.wait_for_event(crash, "EXECUTION_STARTED", timeout=60)
    d.expect("the worker had really started when Relay was killed", started_at is not None)
    proc.kill()
    proc.wait()
    d.lines.append("```\n(relay run-task process killed while the worker was running)\n```")
    # The orphaned fake worker sleeps HANG_SECONDS from launch; wait until it has
    # certainly exited so its workspace is free to be moved aside.
    remaining = (started_at or time.time()) + HANG_SECONDS + 3 - time.time()
    time.sleep(max(0.0, remaining))
    r = d.run("relay", "recover")
    d.expect("recover reports the task and it is RECOVERY_REQUIRED",
             crash in r.stdout and d.status(crash) == "RECOVERY_REQUIRED")
    r = d.run("relay", "accept-recovery", crash, "--actor", "craig",
              "--workspace", str(d.workspaces))
    d.expect("accept-recovery preserves the interrupted workspace and makes the task READY",
             r.returncode == 0 and "Workspace preserved" in r.stdout and d.status(crash) == "READY")
    d.worker("fix")
    d.run_task(crash)
    d.expect("the re-run completes", d.status(crash) == "COMPLETED")

    # 8 ─────────────────────────────────────────────────────────────────────
    d.section("8. Criteria Relay cannot check need a human",
              "A criterion beyond 'the tests pass' is UNVERIFIED; the task waits in "
              "ACCEPTANCE_REQUIRED until acknowledged.")
    _, review = d.create(acceptance_criteria=[
        "All tests pass", "calculate_discount_price(99.99, 15.0) == 84.99"])
    d.run_task(review)
    v = d.verification(review)
    d.expect("verification passes but the task is ACCEPTANCE_REQUIRED",
             v.get("passed") is True and d.status(review) == "ACCEPTANCE_REQUIRED")
    d.expect("the numeric criterion is UNVERIFIED (not silently passed)",
             v.get("acceptance_results", {}).get(
                 "calculate_discount_price(99.99, 15.0) == 84.99") == "UNVERIFIED")
    r = d.run("relay", "status", review)
    d.expect("relay status lists the criterion awaiting human acknowledgement",
             "Awaiting human acknowledgement" in r.stdout
             and "calculate_discount_price(99.99, 15.0) == 84.99" in r.stdout)

    # 9 ─────────────────────────────────────────────────────────────────────
    d.section("9. A2A T2 walk-through (attested ledger)",
              "Every protocol step is attested by a role; review requires a COMPLETED Relay task.")
    t2 = d.a2a_task("T2", "Fix discount rounding")
    d.advance(t2, "SPECCING", "craig", "HUMAN")
    d.advance(t2, "AWAITING_GATE_1", "chatgpt", "CHATGPT", "--evidence", ev)
    r = d.advance(t2, "IN_IMPLEMENTATION", "claude", "CLAUDE")
    d.expect("Claude cannot approve Gate 1 (Human-only)",
             r.returncode == 1 and "may only be performed by HUMAN" in r.stderr)
    d.advance(t2, "IN_IMPLEMENTATION", "craig", "HUMAN")
    r = d.advance(t2, "UNDER_REVIEW", "claude", "CLAUDE", "--evidence", ev,
                  "--relay-task", review)
    d.expect("a Relay task still awaiting acceptance is not accepted as evidence",
             r.returncode == 1 and "ACCEPTANCE_REQUIRED" in r.stderr)
    r = d.advance(t2, "UNDER_REVIEW", "claude", "CLAUDE", "--evidence", ev,
                  "--relay-task", ok_task)
    d.expect("citing the COMPLETED Relay task from scenario 1 moves it to UNDER_REVIEW",
             r.returncode == 0)
    r = d.advance(t2, "AWAITING_GATE_2", "claude", "CHATGPT", "--evidence", ev)
    d.expect("the implementer cannot also act as reviewer",
             r.returncode == 1 and "two protocol roles" in r.stderr.replace("\n", " "))
    d.advance(t2, "AWAITING_GATE_2", "chatgpt", "CHATGPT", "--evidence", ev)
    d.advance(t2, "SHIPPED", "craig", "HUMAN", "--evidence", ev)
    r = d.advance(t2, "CLOSED", "craig", "HUMAN")
    d.expect("the T2 task reaches CLOSED", r.returncode == 0 and "[CLOSED]" in r.stdout)
    r = d.run("a2a", "get-task", t2)
    d.expect("get-task shows the attested history with the cited Relay task",
             f"relay task: {ok_task}" in r.stdout and "by chatgpt [CHATGPT]" in r.stdout)

    # 10 ────────────────────────────────────────────────────────────────────
    d.section("10. A2A T3 walk-through: human re-run",
              "A T3 task cannot ship until a human re-run is recorded (protocol §9.7).")
    d.run("relay", "acknowledge-criteria", review, "--actor", "craig",
          "--reason", "checked 84.99 by hand")
    d.expect("after human acknowledgement the scenario 8 task is COMPLETED",
             d.status(review) == "COMPLETED")
    t3 = d.a2a_task("T3", "Rotate crontab entry")
    d.advance(t3, "SPECCING", "craig", "HUMAN")
    d.advance(t3, "AWAITING_GATE_1", "chatgpt", "CHATGPT", "--evidence", ev)
    d.advance(t3, "IN_IMPLEMENTATION", "craig", "HUMAN")
    d.advance(t3, "UNDER_REVIEW", "claude", "CLAUDE", "--evidence", ev, "--relay-task", review)
    d.advance(t3, "AWAITING_GATE_2", "chatgpt", "CHATGPT", "--evidence", ev)
    r = d.advance(t3, "SHIPPED", "craig", "HUMAN", "--evidence", ev)
    d.expect("SHIPPED is refused without a recorded human re-run",
             r.returncode == 1 and "human re-run" in r.stderr)
    d.run("a2a", "record-human-verification", t3, "--actor", "craig",
          "--command", "python -m pytest -q test_calculator.py", "--output", "17 passed")
    r = d.advance(t3, "SHIPPED", "craig", "HUMAN", "--evidence", ev)
    d.expect("after the re-run is recorded, SHIPPED succeeds", r.returncode == 0)
    t2b = d.a2a_task("T2", "Adjust cron cadence")
    d.advance(t2b, "SPECCING", "craig", "HUMAN")
    d.advance(t2b, "AWAITING_GATE_1", "chatgpt", "CHATGPT", "--evidence", ev)
    r = d.run("a2a", "escalate-tier", t2b, "--actor", "craig",
              "--reason", "amendment adds EXCLUSIVE resource: crontab")
    d.expect("a T2 task is escalated to T3 at Gate 1 and the change is recorded",
             r.returncode == 0 and "now T3" in r.stdout)
    r = d.run("a2a", "list-tasks")
    d.expect("list-tasks shows every ledger task", all(t in r.stdout for t in (t2, t3, t2b)))
    r = d.run("a2a", "check")
    d.expect("the ledger is consistent", r.returncode == 0)

    with closing(sqlite3.connect(d.a2a_db)) as conn:
        conn.execute("UPDATE a2a_tasks SET state='CLOSED' WHERE task_id=?", (t3,))
        conn.commit()
    d.lines.append("```\n(raw SQL: UPDATE a2a_tasks SET state='CLOSED' ... bypassing the ledger)\n```")
    r = d.run("a2a", "check")
    d.expect("`a2a check` detects the out-of-ledger state change",
             r.returncode == 1 and "outside the ledger" in r.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--transcript", type=Path, default=None)
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="relay-demo-"))
    demo = Demo(work)
    started = datetime.now(timezone.utc)
    try:
        scenarios(demo)
    finally:
        passed = sum(ok for _, ok in demo.results)
        header = [
            "# Agent to Agent v1 — Acceptance Transcript",
            "",
            f"Generated {started.strftime('%Y-%m-%d %H:%M UTC')} by `demo/acceptance_demo.py` "
            f"on Python {sys.version.split()[0]} ({sys.platform}).",
            "",
            "Real `relay` and `a2a` CLIs; the Claude worker is the scripted fake worker "
            "(`tests/fake_worker.py`), so no API credit is used. `<demo>` is a fresh temp dir.",
            "",
            f"**Result: {passed}/{len(demo.results)} expectations met.**",
            "",
            "| Expectation | Result |",
            "|---|---|",
            *[f"| {w} | {'PASS' if ok else '**FAIL**'} |" for w, ok in demo.results],
        ]
        text = "\n".join(header + demo.lines) + "\n"
        out = args.transcript or (work / "acceptance-transcript.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"\n{passed}/{len(demo.results)} expectations met. Transcript: {out}")
        if args.transcript:
            shutil.rmtree(work, onerror=_force_remove)
            if work.exists():
                print(f"Note: could not fully remove the temp dir {work}")
    return 0 if passed == len(demo.results) else 1


if __name__ == "__main__":
    sys.exit(main())
