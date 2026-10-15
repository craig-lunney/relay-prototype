"""
Batch 6 — tamper-evident Relay audit log (hash chain).

Each event stores the previous event's hash and a hash over its own content.
`relay audit --verify` re-computes the chain and reports edits, deletions,
insertions, re-ordering and unhashed rows, and prints the head hash so it can
be recorded independently.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from relay.audit import AuditLog
from relay.models import AuditEventType, TaskStatus
from relay.state import GENESIS_HASH, StateManager

ROOT = Path(__file__).parent.parent


def _raw(db, sql, *params):
    conn = sqlite3.connect(str(db))
    conn.execute(sql, params)
    conn.commit()
    conn.close()


@pytest.fixture
def chained(tmp_db, sample_task):
    """A database with events from both write paths: append_event and an
    *_atomic method."""
    audit = AuditLog(tmp_db)
    audit.task_created(sample_task.task_id, "p", "r")
    audit.policy_checked(sample_task.task_id, True, "ok")
    tmp_db.update_task_status(sample_task.task_id, TaskStatus.RUNNING)
    tmp_db.update_task_status(sample_task.task_id, TaskStatus.TIMEOUT)
    tmp_db.retry_task_atomic(sample_task.task_id, actor="craig")
    audit.task_validated(sample_task.task_id)
    return tmp_db


def test_intact_chain_verifies(chained):
    problems, count, head = chained.verify_audit_chain()
    assert problems == []
    assert count == 4
    assert head != GENESIS_HASH and len(head) == 64


def test_every_write_path_links_to_the_previous_event(chained):
    rows = sqlite3.connect(chained.db_path).execute(
        "SELECT sequence, prev_hash, hash FROM events ORDER BY sequence").fetchall()
    assert rows[0][1] == GENESIS_HASH
    for (_, _, prev_hash), (_, link, _) in zip(rows, rows[1:]):
        assert link == prev_hash


@pytest.mark.parametrize("sql,message", [
    ("UPDATE events SET actor='mallory' WHERE sequence=2", "edited"),
    ("UPDATE events SET details='{}' WHERE sequence=1", "edited"),
    ("DELETE FROM events WHERE sequence=2", "sequence gap"),
    ("UPDATE events SET hash=NULL, prev_hash=NULL WHERE sequence=3", "no hash"),
])
def test_tampering_is_detected(chained, sql, message):
    _raw(chained.db_path, sql)
    problems, _, _ = chained.verify_audit_chain()
    assert any(message in p for p in problems), problems


def test_reordered_events_are_detected(chained):
    _raw(chained.db_path, "UPDATE events SET sequence=-1 WHERE sequence=2")
    _raw(chained.db_path, "UPDATE events SET sequence=2 WHERE sequence=3")
    _raw(chained.db_path, "UPDATE events SET sequence=3 WHERE sequence=-1")
    problems, _, _ = chained.verify_audit_chain()
    assert any("broken" in p or "edited" in p for p in problems)


def test_new_events_continue_the_chain_after_reopen(chained, sample_task):
    reopened = StateManager(chained.db_path)
    AuditLog(reopened).task_validated(sample_task.task_id)
    problems, count, _ = reopened.verify_audit_chain()
    assert problems == [] and count == 5


def _legacy_db(tmp_path, rows=2):
    """A database as written before Batch 6: user_version 0, unhashed events."""
    db = tmp_path / "legacy.db"
    state = StateManager(db)
    state._conn().close()
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version = 0")
    for seq in range(1, rows + 1):
        conn.execute("INSERT INTO events (event_id, sequence, event_type, task_id, "
                     "execution_id, timestamp, actor, details) VALUES (?,?,?,?,?,?,?,?)",
                     (f"EVT-L{seq}", seq, "TASK_CREATED", "TASK-OLD", None,
                      "2026-09-01T00:00:00+00:00", "relay", "{}"))
    conn.commit()
    conn.close()
    return db


def test_legacy_database_is_chained_once(tmp_path):
    db = _legacy_db(tmp_path)
    state = StateManager(db)
    problems, count, _ = state.verify_audit_chain()
    assert problems == []
    assert count == 3
    marker = state.get_all_events()[-1]
    assert marker.event_type == AuditEventType.AUDIT_CHAIN_STARTED
    assert marker.details == {"legacy_events_chained": 2}
    StateManager(db)
    assert len(StateManager(db).get_all_events()) == 3, "chaining must happen once"


def test_wiping_hashes_and_reopening_does_not_launder_tampering(chained):
    """Review finding: the migration is one-time, so NULLing every hash and
    reopening must leave the log reported as broken, not silently re-chained."""
    _raw(chained.db_path, "UPDATE events SET actor='mallory' WHERE sequence=2")
    _raw(chained.db_path, "UPDATE events SET hash=NULL, prev_hash=NULL")
    reopened = StateManager(chained.db_path)
    problems, count, _ = reopened.verify_audit_chain()
    assert count == 4, "no AUDIT_CHAIN_STARTED may be added on reopen"
    assert any("no hash" in p for p in problems)


def test_open_does_not_need_the_write_lock(chained):
    """Read-only commands must keep working while another process writes."""
    blocker = sqlite3.connect(chained.db_path, timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        reader = StateManager(chained.db_path)
        problems, count, _ = reader.verify_audit_chain()
        assert problems == [] and count == 4
    finally:
        blocker.rollback()
        blocker.close()


def test_empty_log_verifies(tmp_path):
    problems, count, head = StateManager(tmp_path / "empty.db").verify_audit_chain()
    assert (problems, count, head) == ([], 0, GENESIS_HASH)


def test_second_chain_started_marker_is_flagged(tmp_path):
    db = _legacy_db(tmp_path)
    state = StateManager(db)
    from relay.state import _insert_chained_event
    conn = state._conn()
    with conn:
        _insert_chained_event(conn, "EVT-FAKE", "AUDIT_CHAIN_STARTED", "-", None,
                              "2026-10-16T00:00:00+00:00", "relay",
                              '{"legacy_events_chained": 3}')
    problems, _, _ = state.verify_audit_chain()
    assert any("second AUDIT_CHAIN_STARTED" in p for p in problems)


def test_atomic_paths_all_link_into_the_chain(tmp_db, sample_task, tmp_path):
    from relay.models import Execution, ExecutionStatus
    from datetime import datetime, timezone
    ex = Execution(execution_id=Execution.new_id(), task_id=sample_task.task_id,
                   attempt_number=1, status=ExecutionStatus.RUNNING,
                   workspace=str(tmp_path / "ws"), worker_identity="t",
                   policy_version="0.1", capability_snapshot=[],
                   started_at=datetime.now(timezone.utc))
    tmp_db.create_execution(ex)
    AuditLog(tmp_db).task_created(sample_task.task_id, "p", "r")
    tmp_db.pre_launch_failed_atomic(ex.execution_id, "auth")
    tmp_db.update_task_status(sample_task.task_id, TaskStatus.RUNNING)
    tmp_db.mark_orphaned_task_atomic(sample_task.task_id, "orphan")
    tmp_db.accept_recovery_atomic(sample_task.task_id, actor="craig")
    problems, count, _ = tmp_db.verify_audit_chain()
    assert problems == [] and count == 4


def test_unhashed_row_after_the_chain_is_never_repaired(chained):
    _raw(chained.db_path,
         "INSERT INTO events (event_id, sequence, event_type, task_id, execution_id, "
         "timestamp, actor, details) VALUES ('EVT-FORGED', 99, 'TASK_COMPLETED', "
         "'TASK-X', NULL, '2026-10-16T00:00:00+00:00', 'relay', '{}')")
    reopened = StateManager(chained.db_path)
    problems, _, _ = reopened.verify_audit_chain()
    assert any("no hash" in p for p in problems)


def _relay(*argv):
    return subprocess.run([sys.executable, "-m", "relay", *argv], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=60)


def test_cli_verify_reports_intact_and_broken_chains(chained):
    r = _relay("--db", chained.db_path, "audit", "--verify")
    assert r.returncode == 0
    assert "Audit chain intact: 4 events." in r.stdout and "Head hash:" in r.stdout
    _raw(chained.db_path, "UPDATE events SET actor='mallory' WHERE sequence=1")
    r = _relay("--db", chained.db_path, "audit", "--verify")
    assert r.returncode == 1
    assert "BROKEN" in r.stderr and "#1" in r.stderr
