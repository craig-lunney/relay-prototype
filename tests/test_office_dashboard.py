"""
Batch 7 — read-only office dashboard (python -m office).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from a2a.models import A2AProtocolState as S, A2ATask, A2ATaskTier
from a2a.state import A2AStateManager
from office.dashboard import render, snapshot
from relay.audit import AuditLog
from relay.models import Task, TaskStatus
from relay.state import StateManager
from tests.a2a_support import fake_relay_lookup, transition

ROOT = Path(__file__).parent.parent
URL = "https://github.com/org/repo/issues/9"
HOSTILE = '<script>alert("x")</script> & "quotes"'


def _relay_task(state: StateManager, status: TaskStatus, objective="Fix the bug") -> str:
    task = Task(task_id=Task.new_id(), project_id="p", repository=".", base_ref="main",
                working_ref="main", objective=objective, requirements=[],
                acceptance_criteria=[], requested_actions=[], forbidden_actions=[],
                required_tests=[], approval_policy="auto", status=status)
    state.create_task(task)
    return task.task_id


@pytest.fixture
def dbs(tmp_path):
    relay_db, a2a_db = tmp_path / "relay.db", tmp_path / "a2a.db"
    rs = StateManager(relay_db)
    audit = AuditLog(rs)
    blocked = _relay_task(rs, TaskStatus.BLOCKED, objective=HOSTILE)
    accepting = _relay_task(rs, TaskStatus.ACCEPTANCE_REQUIRED)
    done = _relay_task(rs, TaskStatus.COMPLETED)
    audit.task_created(blocked, "p", ".")
    audit.verification_completed(accepting, "EXEC-1", True, {
        "files_changed": ["calculator.py"], "protected_violations": [],
        "acceptance_results": {"All tests pass": "PASS", "Docs updated": "UNVERIFIED"}})
    audit.task_completed(done)

    sm = A2AStateManager(a2a_db, relay_status_lookup=fake_relay_lookup)
    t3 = A2ATask(task_id=A2ATask.new_id(), title=HOSTILE, tier=A2ATaskTier.T3,
                 github_issue_url=URL)
    sm.create_task(t3, created_by="craig")
    for step in (S.SPECCING, S.AWAITING_GATE_1, S.IN_IMPLEMENTATION, S.UNDER_REVIEW,
                 S.AWAITING_GATE_2):
        transition(sm, t3.task_id, step)
    for conn in (rs._conn(), sm._conn()):
        conn.close()
    rs._local.conn = None
    sm._local.conn = None
    return {"relay": relay_db, "a2a": a2a_db, "blocked": blocked, "accepting": accepting,
            "t3": t3.task_id}


def test_waiting_on_you_lists_every_decision_with_its_command(dbs):
    snap = snapshot(dbs["relay"], dbs["a2a"])
    by_id = {w["id"]: w for w in snap.waiting}
    assert "approve-task" in by_id[dbs["blocked"]]["command"]
    assert "acknowledge-criteria" in by_id[dbs["accepting"]]["command"]
    t3 = by_id[dbs["t3"]]
    assert "re-run" in t3["what"] and "record-human-verification" in t3["command"]
    assert len(snap.waiting) == 3


def test_verification_summary_and_unverified_criteria(dbs):
    snap = snapshot(dbs["relay"], dbs["a2a"])
    accepting = next(t for t in snap.relay_tasks if t["id"] == dbs["accepting"])
    assert accepting["passed"] is True
    assert accepting["changed"] == ["calculator.py"]
    assert accepting["unverified"] == ["Docs updated"]


def test_database_content_is_escaped(dbs):
    page = render(snapshot(dbs["relay"], dbs["a2a"]), dbs["relay"], dbs["a2a"])
    assert "<script>" not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &quot;quotes&quot;" in page


def test_databases_are_never_modified(dbs):
    before = {k: (dbs[k].read_bytes(), dbs[k].stat().st_mtime_ns) for k in ("relay", "a2a")}
    render(snapshot(dbs["relay"], dbs["a2a"]), dbs["relay"], dbs["a2a"])
    after = {k: (dbs[k].read_bytes(), dbs[k].stat().st_mtime_ns) for k in ("relay", "a2a")}
    assert before == after


def test_missing_databases_are_reported_not_created(tmp_path):
    relay_db, a2a_db = tmp_path / "none.db", tmp_path / "none2.db"
    page = render(snapshot(relay_db, a2a_db), relay_db, a2a_db)
    assert "No Relay database" in page and "No A2A database" in page
    assert not relay_db.exists() and not a2a_db.exists()


def test_audit_chain_status_shown_and_tampering_flagged(dbs):
    snap = snapshot(dbs["relay"], dbs["a2a"])
    assert snap.chain["problems"] == [] and snap.chain["count"] == 3
    conn = sqlite3.connect(str(dbs["relay"]))
    conn.execute("UPDATE events SET actor='mallory' WHERE sequence=1")
    conn.commit()
    conn.close()
    page = render(snapshot(dbs["relay"], dbs["a2a"]), dbs["relay"], dbs["a2a"])
    assert "Audit chain: 1 problem(s)" in page and "#1 edited" in page


def test_ledger_drift_is_flagged(dbs):
    conn = sqlite3.connect(str(dbs["a2a"]))
    conn.execute("UPDATE a2a_tasks SET state='SHIPPED' WHERE task_id=?", (dbs["t3"],))
    conn.commit()
    conn.close()
    snap = snapshot(dbs["relay"], dbs["a2a"])
    assert any("differs" in p for p in snap.ledger_problems)


def test_cli_writes_the_page(dbs, tmp_path):
    out = tmp_path / "office.html"
    r = subprocess.run([sys.executable, "-m", "office", "--relay-db", str(dbs["relay"]),
                        "--a2a-db", str(dbs["a2a"]), "--out", str(out),
                        "--intray", str(tmp_path / "intray")],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "waiting on you: 3" in r.stdout
    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and "Waiting on you" in page
    assert "<script" not in page.lower()


def test_odd_verification_details_do_not_crash(tmp_path):
    relay_db = tmp_path / "relay.db"
    rs = StateManager(relay_db)
    task_id = _relay_task(rs, TaskStatus.ACCEPTANCE_REQUIRED)
    AuditLog(rs).verification_completed(task_id, "EXEC-1", True, {
        "files_changed": 5, "protected_violations": 7, "acceptance_results": [1]})
    rs._conn().close()
    rs._local.conn = None
    snap = snapshot(relay_db, tmp_path / "a2a.db")
    task = snap.relay_tasks[0]
    assert (task["changed"], task["violations"], task["unverified"]) == ([], [], [])
    render(snap, relay_db, tmp_path / "a2a.db")
