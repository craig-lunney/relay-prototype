"""Tests for the recovery engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from relay.models import (
    Execution,
    ExecutionLease,
    ExecutionStatus,
    Task,
    TaskStatus,
)
from relay.recovery import RecoveryEngine


def _now():
    return datetime.now(timezone.utc)


def _make_running_execution(task_id: str, workspace: str = "/tmp/ws") -> Execution:
    return Execution(
        execution_id=Execution.new_id(),
        task_id=task_id,
        attempt_number=1,
        status=ExecutionStatus.RUNNING,
        workspace=workspace,
        worker_identity="test-worker/host/1234",
        policy_version="0.1",
        capability_snapshot=["read_repository"],
        started_at=_now(),
    )


def _make_lease(
    execution: Execution, expired: bool = False, dead_pid: bool = False
) -> ExecutionLease:
    now = _now()
    return ExecutionLease(
        execution_id=execution.execution_id,
        task_id=execution.task_id,
        workspace=execution.workspace,
        pid=99998 if dead_pid else 12345,
        started_at=now - timedelta(seconds=200 if expired else 10),
        expires_at=now - timedelta(seconds=100) if expired else now + timedelta(seconds=120),
        worker_identity=execution.worker_identity,
    )


def _setup_running_task(tmp_db, task_id: str, workspace: str) -> tuple[Task, Execution]:
    """Create a task in RUNNING state with an active execution and lease."""
    task = tmp_db.get_task(task_id)
    tmp_db.update_task_status(task_id, TaskStatus.RUNNING, validate_transition=False)

    execution = _make_running_execution(task_id, workspace)
    tmp_db.create_execution(execution)

    lease = _make_lease(execution)
    tmp_db.acquire_lease(lease)

    return task, execution


# ── Classification ────────────────────────────────────────────────────────────

def test_classify_completed_execution(tmp_db, audit, sample_task):
    ex = _make_running_execution(sample_task.task_id)
    ex.status = ExecutionStatus.COMPLETED
    tmp_db.create_execution(ex)

    engine = RecoveryEngine(tmp_db, audit)
    assert engine.classify_execution(ex.execution_id) == "COMPLETED"


def test_classify_failed_execution(tmp_db, audit, sample_task):
    ex = _make_running_execution(sample_task.task_id)
    ex.status = ExecutionStatus.FAILED
    tmp_db.create_execution(ex)

    engine = RecoveryEngine(tmp_db, audit)
    assert engine.classify_execution(ex.execution_id) == "FAILED"


def test_classify_timeout_execution(tmp_db, audit, sample_task):
    ex = _make_running_execution(sample_task.task_id)
    ex.status = ExecutionStatus.TIMEOUT
    tmp_db.create_execution(ex)

    engine = RecoveryEngine(tmp_db, audit)
    assert engine.classify_execution(ex.execution_id) == "TIMEOUT"


def test_classify_interrupted_execution(tmp_db, audit, sample_task):
    ex = _make_running_execution(sample_task.task_id)
    ex.status = ExecutionStatus.INTERRUPTED
    tmp_db.create_execution(ex)

    engine = RecoveryEngine(tmp_db, audit)
    assert engine.classify_execution(ex.execution_id) == "INTERRUPTED"


def test_classify_still_running_is_interrupted(tmp_db, audit, sample_task):
    """An execution still in RUNNING state when we classify it = interrupted."""
    ex = _make_running_execution(sample_task.task_id)
    tmp_db.create_execution(ex)

    engine = RecoveryEngine(tmp_db, audit)
    assert engine.classify_execution(ex.execution_id) == "INTERRUPTED"


def test_classify_unknown_execution(tmp_db, audit):
    engine = RecoveryEngine(tmp_db, audit)
    assert engine.classify_execution("EXEC-DOESNTEXIST") == "UNKNOWN"


# ── Expired lease detection ───────────────────────────────────────────────────

def test_scan_detects_expired_lease(tmp_db, audit, sample_task):
    task, execution = _setup_running_task(tmp_db, sample_task.task_id, "/tmp/scan-ws")

    # Make the lease expired
    from relay.models import ExecutionLease
    now = _now()
    expired_lease = ExecutionLease(
        execution_id=execution.execution_id,
        task_id=sample_task.task_id,
        workspace="/tmp/scan-ws",
        pid=99999,
        started_at=now - timedelta(seconds=300),
        expires_at=now - timedelta(seconds=100),  # expired
        worker_identity="test",
    )
    # Release the old lease and insert an expired one (simulate via direct DB update)
    tmp_db.release_lease(execution.execution_id)
    tmp_db._conn().execute(
        "UPDATE execution_leases SET released=0, expires_at=? WHERE execution_id=?",
        (expired_lease.expires_at.isoformat(), execution.execution_id),
    )
    tmp_db._conn().commit()

    engine = RecoveryEngine(tmp_db, audit)
    requiring_recovery = engine.scan()

    assert sample_task.task_id in requiring_recovery
    task_after = tmp_db.get_task(sample_task.task_id)
    assert task_after.status == TaskStatus.RECOVERY_REQUIRED


def test_scan_no_active_leases_returns_empty(tmp_db, audit):
    engine = RecoveryEngine(tmp_db, audit)
    result = engine.scan()
    assert result == []


# ── RECOVERY_REQUIRED → READY restart ────────────────────────────────────────

def test_can_safely_restart_recovery_required_task(tmp_db, audit, sample_task):
    tmp_db.update_task_status(
        sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
    )
    engine = RecoveryEngine(tmp_db, audit)
    can_restart, reason = engine.can_safely_restart(sample_task.task_id)
    assert can_restart, reason


def test_cannot_restart_ready_task(tmp_db, audit, sample_task):
    engine = RecoveryEngine(tmp_db, audit)
    can_restart, reason = engine.can_safely_restart(sample_task.task_id)
    assert not can_restart
    assert "RECOVERY_REQUIRED" in reason


def test_cannot_restart_if_attempts_exhausted(tmp_db, audit, sample_task):
    tmp_db.update_task_status(
        sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
    )
    for _ in range(sample_task.max_execution_attempts):
        tmp_db.increment_attempt_count(sample_task.task_id)

    engine = RecoveryEngine(tmp_db, audit)
    can_restart, reason = engine.can_safely_restart(sample_task.task_id)
    assert not can_restart
    assert "attempt" in reason.lower()


def test_recovery_required_emits_audit_event(tmp_db, audit, sample_task):
    task, execution = _setup_running_task(tmp_db, sample_task.task_id, "/tmp/rec-ws")

    now = _now()
    tmp_db._conn().execute(
        "UPDATE execution_leases SET expires_at=? WHERE execution_id=?",
        ((now - timedelta(seconds=60)).isoformat(), execution.execution_id),
    )
    tmp_db._conn().commit()

    engine = RecoveryEngine(tmp_db, audit)
    engine.scan()

    events = tmp_db.get_events_for_task(sample_task.task_id)
    event_types = [e.event_type.value for e in events]
    assert "RECOVERY_REQUIRED" in event_types


# ── Interrupted execution state ───────────────────────────────────────────────

def test_interrupted_execution_marked_interrupted(tmp_db, audit, sample_task):
    task, execution = _setup_running_task(tmp_db, sample_task.task_id, "/tmp/int-ws")

    now = _now()
    tmp_db._conn().execute(
        "UPDATE execution_leases SET expires_at=? WHERE execution_id=?",
        ((now - timedelta(seconds=60)).isoformat(), execution.execution_id),
    )
    tmp_db._conn().commit()

    engine = RecoveryEngine(tmp_db, audit)
    engine.scan()

    saved_exec = tmp_db.get_execution(execution.execution_id)
    assert saved_exec.status == ExecutionStatus.INTERRUPTED
    assert saved_exec.error is not None
    assert saved_exec.ended_at is not None
