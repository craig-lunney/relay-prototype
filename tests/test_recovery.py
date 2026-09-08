"""Tests for the recovery engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from relay.models import (
    AuditEventType,
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
    # scan() now delegates to mark_stale_lease_atomic(), which emits
    # RECOVERY_SCAN_STALE_LEASE (not the old RECOVERY_REQUIRED event that
    # _transition_to_recovery used to emit via audit.recovery_required()).
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
    event_types = [e.event_type for e in events]
    assert AuditEventType.RECOVERY_SCAN_STALE_LEASE in event_types


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


# ── C8: Recovery boundary regression ─────────────────────────────────────────

def test_c8_restart_blocked_by_workspace_lease_not_repository(tmp_db, audit, sample_task, tmp_path):
    """
    C8 regression: can_safely_restart() must check the task's WORKSPACE lease,
    not task.repository.

    Pre-fix bug: the check called get_active_lease(task.repository). Leases are
    issued on WORKSPACE copies (e.g. workspace_base/task_id), never on the source
    repository path. That call always returned None, so the guard was ineffective —
    a task with an active workspace lease could be incorrectly declared safe to restart.

    This test creates an active lease on the WORKSPACE path (distinct from the
    repository path), sets the task to RECOVERY_REQUIRED, and verifies that
    can_safely_restart() correctly returns (False, ...) because the workspace lease
    is detected via get_active_leases_for_task().
    """
    # Set task to RECOVERY_REQUIRED (the only valid input for can_safely_restart)
    tmp_db.update_task_status(
        sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
    )

    # Create an execution whose workspace is NOT the same as task.repository
    workspace = str(tmp_path / "ws" / sample_task.task_id)
    now = _now()
    exec_ = Execution(
        execution_id=Execution.new_id(),
        task_id=sample_task.task_id,
        attempt_number=1,
        status=ExecutionStatus.INTERRUPTED,
        workspace=workspace,
        worker_identity="test-worker",
        policy_version="0.1",
        capability_snapshot=[],
        started_at=now - timedelta(seconds=60),
    )
    tmp_db.create_execution(exec_)

    # Acquire a lease on the workspace path (not task.repository)
    lease = ExecutionLease(
        execution_id=exec_.execution_id,
        task_id=sample_task.task_id,
        workspace=workspace,
        pid=12345,
        started_at=now - timedelta(seconds=60),
        expires_at=now + timedelta(seconds=300),
        worker_identity="test-worker",
    )
    tmp_db.acquire_lease(lease)

    # Confirm the repository path has NO lease (pre-fix check would see None and
    # incorrectly allow restart)
    assert tmp_db.get_active_lease(sample_task.repository) is None

    # The fixed check must find the workspace lease and block restart
    engine = RecoveryEngine(tmp_db, audit)
    can_restart, reason = engine.can_safely_restart(sample_task.task_id)

    assert not can_restart, (
        "Expected restart to be blocked by active workspace lease, but it was allowed"
    )
    assert "lease" in reason.lower(), f"Expected 'lease' in reason: {reason!r}"
    assert workspace in reason or exec_.execution_id in reason


# ── P1-K: scan/engine integration tests ──────────────────────────────────────
#
# These tests verify the new scan() behaviour after P1-K Step 3:
#   - scan() delegates to mark_stale_lease_atomic() (atomic, not _transition_to_recovery)
#   - RUNNING and VERIFYING tasks are both treated as recoverable
#   - Live worker + valid lease is not treated as stale
#   - Dead PID with unexpired lease is still stale
#   - READY task stale leases are cleaned up without transitioning the task
#   - Scan is idempotent; concurrent scans produce only one authority transition

def _make_expired_lease_direct(tmp_db, execution_id: str) -> None:
    """Force a lease into expired state via direct DB update (test helper)."""
    tmp_db._conn().execute(
        "UPDATE execution_leases SET expires_at=? WHERE execution_id=?",
        ((_now() - timedelta(seconds=60)).isoformat(), execution_id),
    )
    tmp_db._conn().commit()


def test_p1k_scan_verifying_transitions_to_recovery_required(tmp_db, audit, sample_task):
    """Stale lease on a VERIFYING task must move it to RECOVERY_REQUIRED."""
    task, execution = _setup_running_task(tmp_db, sample_task.task_id, "/tmp/p1k-verify-ws")
    tmp_db.update_task_status(
        sample_task.task_id, TaskStatus.VERIFYING, validate_transition=False
    )
    _make_expired_lease_direct(tmp_db, execution.execution_id)

    engine = RecoveryEngine(tmp_db, audit)
    requiring_recovery = engine.scan()

    assert sample_task.task_id in requiring_recovery
    task_after = tmp_db.get_task(sample_task.task_id)
    assert task_after.status == TaskStatus.RECOVERY_REQUIRED

    events = tmp_db.get_events_for_task(sample_task.task_id)
    assert any(e.event_type == AuditEventType.RECOVERY_SCAN_STALE_LEASE for e in events)


def test_p1k_scan_live_worker_unexpired_lease_no_recovery(tmp_db, audit, sample_task, monkeypatch):
    """Live worker with a valid lease must not trigger recovery."""
    _setup_running_task(tmp_db, sample_task.task_id, "/tmp/p1k-live-ws")
    # Lease is unexpired (default from _make_lease).

    monkeypatch.setattr(RecoveryEngine, "_worker_is_alive", lambda self, lease: True)

    requiring_recovery = RecoveryEngine(tmp_db, audit).scan()

    assert requiring_recovery == []
    task_after = tmp_db.get_task(sample_task.task_id)
    assert task_after.status == TaskStatus.RUNNING  # unchanged

    events = tmp_db.get_events_for_task(sample_task.task_id)
    assert not any(e.event_type == AuditEventType.RECOVERY_SCAN_STALE_LEASE for e in events)


def test_p1k_scan_dead_pid_unexpired_lease_triggers_recovery(tmp_db, audit, sample_task, monkeypatch):
    """Dead worker PID with an unexpired lease must still trigger recovery."""
    task, execution = _setup_running_task(tmp_db, sample_task.task_id, "/tmp/p1k-dead-ws")
    # Lease is unexpired — only the PID is dead.

    monkeypatch.setattr(RecoveryEngine, "_worker_is_alive", lambda self, lease: False)

    requiring_recovery = RecoveryEngine(tmp_db, audit).scan()

    assert sample_task.task_id in requiring_recovery
    task_after = tmp_db.get_task(sample_task.task_id)
    assert task_after.status == TaskStatus.RECOVERY_REQUIRED


def test_p1k_scan_stale_ready_lease_releases_lease_preserves_task(tmp_db, audit, sample_task):
    """Stale lease on a READY task: lease released, task status unchanged, not in requiring_recovery."""
    # sample_task starts in READY state.
    ex = _make_running_execution(sample_task.task_id, "/tmp/p1k-ready-ws")
    tmp_db.create_execution(ex)
    tmp_db.acquire_lease(_make_lease(ex, expired=True))

    engine = RecoveryEngine(tmp_db, audit)
    requiring_recovery = engine.scan()

    task_after = tmp_db.get_task(sample_task.task_id)
    assert task_after.status == TaskStatus.READY  # unchanged

    assert tmp_db.get_active_lease(ex.workspace) is None  # lease released

    assert sample_task.task_id not in requiring_recovery

    events = tmp_db.get_events_for_task(sample_task.task_id)
    assert any(e.event_type == AuditEventType.RECOVERY_SCAN_STALE_LEASE for e in events)


def test_p1k_scan_already_recovery_required_idempotent(tmp_db, audit, sample_task):
    """Task already in RECOVERY_REQUIRED with a stale lease: scan is idempotent on task status."""
    task, execution = _setup_running_task(tmp_db, sample_task.task_id, "/tmp/p1k-idem-ws")
    # Simulate a previous scan having already moved the task to RECOVERY_REQUIRED.
    tmp_db.update_task_status(
        sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
    )
    _make_expired_lease_direct(tmp_db, execution.execution_id)

    requiring_recovery = RecoveryEngine(tmp_db, audit).scan()

    # Task stays RECOVERY_REQUIRED and is NOT re-added to requiring_recovery
    task_after = tmp_db.get_task(sample_task.task_id)
    assert task_after.status == TaskStatus.RECOVERY_REQUIRED
    assert sample_task.task_id not in requiring_recovery


def test_p1k_scan_repeated_no_duplicate_transition(tmp_db, audit, sample_task):
    """Repeated scans must not produce duplicate state transitions or duplicate audit events."""
    task, execution = _setup_running_task(tmp_db, sample_task.task_id, "/tmp/p1k-repeat-ws")
    _make_expired_lease_direct(tmp_db, execution.execution_id)

    engine = RecoveryEngine(tmp_db, audit)
    result1 = engine.scan()
    result2 = engine.scan()  # lease already released — no active leases found

    assert result1 == [sample_task.task_id]
    assert result2 == []

    task_after = tmp_db.get_task(sample_task.task_id)
    assert task_after.status == TaskStatus.RECOVERY_REQUIRED

    events = tmp_db.get_events_for_task(sample_task.task_id)
    stale_events = [e for e in events if e.event_type == AuditEventType.RECOVERY_SCAN_STALE_LEASE]
    assert len(stale_events) == 1  # exactly one, not two


def test_p1k_scan_concurrent_only_one_authority_transition(tmp_db, audit, sample_task):
    """Two engine instances on the same DB produce only one authority transition."""
    task, execution = _setup_running_task(tmp_db, sample_task.task_id, "/tmp/p1k-conc-ws")
    _make_expired_lease_direct(tmp_db, execution.execution_id)

    engine1 = RecoveryEngine(tmp_db, audit)
    engine2 = RecoveryEngine(tmp_db, audit)

    result1 = engine1.scan()  # First: task → RECOVERY_REQUIRED, lease released
    result2 = engine2.scan()  # Second: no active leases found

    assert result1 == [sample_task.task_id]
    assert result2 == []

    task_after = tmp_db.get_task(sample_task.task_id)
    assert task_after.status == TaskStatus.RECOVERY_REQUIRED

    events = tmp_db.get_events_for_task(sample_task.task_id)
    stale_events = [e for e in events if e.event_type == AuditEventType.RECOVERY_SCAN_STALE_LEASE]
    assert len(stale_events) == 1  # only one authority transition


def test_p1k_scan_uses_mark_stale_lease_atomic(tmp_db, audit, sample_task, monkeypatch):
    """scan() must call mark_stale_lease_atomic() and emit RECOVERY_SCAN_STALE_LEASE,
    not the old _transition_to_recovery path which emitted RECOVERY_REQUIRED."""
    task, execution = _setup_running_task(tmp_db, sample_task.task_id, "/tmp/p1k-atomic-ws")
    _make_expired_lease_direct(tmp_db, execution.execution_id)

    atomic_calls: list[str] = []
    original = tmp_db.mark_stale_lease_atomic

    def _recording(execution_id: str, actor: str = "relay") -> None:
        atomic_calls.append(execution_id)
        return original(execution_id, actor)

    monkeypatch.setattr(tmp_db, "mark_stale_lease_atomic", _recording)

    RecoveryEngine(tmp_db, audit).scan()

    # The atomic method was called with the stale execution_id
    assert execution.execution_id in atomic_calls

    # The event type is RECOVERY_SCAN_STALE_LEASE (atomic path),
    # not RECOVERY_REQUIRED (old _transition_to_recovery path).
    events = tmp_db.get_events_for_task(sample_task.task_id)
    event_types = {e.event_type for e in events}
    assert AuditEventType.RECOVERY_SCAN_STALE_LEASE in event_types
    assert AuditEventType.RECOVERY_REQUIRED not in event_types
