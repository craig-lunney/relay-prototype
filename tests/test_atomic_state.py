"""Focused tests for StateManager atomic operations (P1-K Step 2).

Tests cover:
  1. Successful accept_recovery_atomic
  2. Successful retry_task_atomic
  3. Successful stale-lease recovery (mark_stale_lease_atomic)
  4. RUNNING → RECOVERY_REQUIRED via mark_stale_lease_atomic
  5. VERIFYING → RECOVERY_REQUIRED via mark_stale_lease_atomic
  6. Stale lease on a task that must NOT be transitioned
  7. State + audit rollback together when the transaction fails
  8. Concurrent/duplicate protection
  9. No independent commit inside atomic operations (proved by rollback tests)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from relay.models import (
    AuditEvent,
    AuditEventType,
    Execution,
    ExecutionLease,
    ExecutionStatus,
    TaskStatus,
)
from relay.state import InvalidTransitionError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_execution(task_id: str, workspace: str) -> Execution:
    return Execution(
        execution_id=Execution.new_id(),
        task_id=task_id,
        attempt_number=1,
        status=ExecutionStatus.RUNNING,
        workspace=workspace,
        worker_identity="test-worker",
        policy_version="0.1",
        capability_snapshot=[],
        started_at=_now(),
    )


def _make_expired_lease(execution: Execution) -> ExecutionLease:
    now = _now()
    return ExecutionLease(
        execution_id=execution.execution_id,
        task_id=execution.task_id,
        workspace=execution.workspace,
        pid=99999,
        started_at=now - timedelta(seconds=300),
        expires_at=now - timedelta(seconds=100),
        worker_identity=execution.worker_identity,
    )


def _setup_stale(db, task_id: str) -> Execution:
    """Create a RUNNING execution + active (expired) lease for task_id."""
    ws = f"/tmp/stale-{task_id}"
    ex = _make_execution(task_id, ws)
    db.create_execution(ex)
    db.acquire_lease(_make_expired_lease(ex))
    return ex


# ── accept_recovery_atomic ────────────────────────────────────────────────────

class TestAcceptRecoveryAtomic:
    def test_transitions_to_ready(self, tmp_db, sample_task):
        """RECOVERY_REQUIRED → READY, RECOVERY_ACCEPTED event, single call."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
        )
        tmp_db.accept_recovery_atomic(sample_task.task_id)

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.READY

    def test_emits_recovery_accepted_event(self, tmp_db, sample_task):
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
        )
        tmp_db.accept_recovery_atomic(sample_task.task_id)

        types = [e.event_type for e in tmp_db.get_events_for_task(sample_task.task_id)]
        assert AuditEventType.RECOVERY_ACCEPTED in types

    def test_emits_exactly_one_event(self, tmp_db, sample_task):
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
        )
        before = tmp_db.event_count()
        tmp_db.accept_recovery_atomic(sample_task.task_id)
        assert tmp_db.event_count() == before + 1

    def test_wrong_state_raises(self, tmp_db, sample_task):
        """Fails when task is not RECOVERY_REQUIRED (sample_task starts READY)."""
        with pytest.raises(InvalidTransitionError, match="RECOVERY_REQUIRED"):
            tmp_db.accept_recovery_atomic(sample_task.task_id)

    def test_duplicate_call_raises(self, tmp_db, sample_task):
        """Second call fails because task is now READY, not RECOVERY_REQUIRED."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
        )
        tmp_db.accept_recovery_atomic(sample_task.task_id)
        with pytest.raises(InvalidTransitionError):
            tmp_db.accept_recovery_atomic(sample_task.task_id)

    def test_rollback_on_event_failure(self, tmp_db, sample_task, monkeypatch):
        """If the audit INSERT fails, the task-status UPDATE must be rolled back too."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
        )

        def _fail(cls):
            raise RuntimeError("forced failure — rollback test")

        monkeypatch.setattr(AuditEvent, "new_id", classmethod(_fail))

        with pytest.raises(RuntimeError, match="forced failure"):
            tmp_db.accept_recovery_atomic(sample_task.task_id)

        # State + event must both have been rolled back (proves single transaction)
        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.RECOVERY_REQUIRED
        assert tmp_db.event_count() == 0


# ── retry_task_atomic ─────────────────────────────────────────────────────────

class TestRetryTaskAtomic:
    def test_transitions_to_ready(self, tmp_db, sample_task):
        """TIMEOUT → READY, TASK_RETRIED event."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=False
        )
        tmp_db.retry_task_atomic(sample_task.task_id)

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.READY

    def test_emits_task_retried_event(self, tmp_db, sample_task):
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=False
        )
        tmp_db.retry_task_atomic(sample_task.task_id)

        types = [e.event_type for e in tmp_db.get_events_for_task(sample_task.task_id)]
        assert AuditEventType.TASK_RETRIED in types

    def test_emits_exactly_one_event(self, tmp_db, sample_task):
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=False
        )
        before = tmp_db.event_count()
        tmp_db.retry_task_atomic(sample_task.task_id)
        assert tmp_db.event_count() == before + 1

    def test_wrong_state_raises(self, tmp_db, sample_task):
        """Fails when task is not TIMEOUT (sample_task starts READY)."""
        with pytest.raises(InvalidTransitionError, match="TIMEOUT"):
            tmp_db.retry_task_atomic(sample_task.task_id)

    def test_exhausted_attempts_raises(self, tmp_db, sample_task):
        """Fails when all allowed attempts are exhausted."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=False
        )
        for _ in range(sample_task.max_execution_attempts):
            tmp_db.increment_attempt_count(sample_task.task_id)

        with pytest.raises(InvalidTransitionError, match="attempt"):
            tmp_db.retry_task_atomic(sample_task.task_id)

    def test_duplicate_call_raises(self, tmp_db, sample_task):
        """Second call fails once task has been transitioned to READY."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=False
        )
        tmp_db.retry_task_atomic(sample_task.task_id)
        with pytest.raises(InvalidTransitionError):
            tmp_db.retry_task_atomic(sample_task.task_id)

    def test_rollback_on_event_failure(self, tmp_db, sample_task, monkeypatch):
        """If the audit INSERT fails, the TIMEOUT → READY update must be rolled back."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=False
        )

        def _fail(cls):
            raise RuntimeError("forced failure — rollback test")

        monkeypatch.setattr(AuditEvent, "new_id", classmethod(_fail))

        with pytest.raises(RuntimeError, match="forced failure"):
            tmp_db.retry_task_atomic(sample_task.task_id)

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.TIMEOUT
        assert tmp_db.event_count() == 0


# ── mark_stale_lease_atomic ───────────────────────────────────────────────────

class TestMarkStaleLeaseAtomic:
    # 4. RUNNING → RECOVERY_REQUIRED
    def test_running_task_transitions_to_recovery_required(self, tmp_db, sample_task):
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RUNNING, validate_transition=False
        )
        ex = _setup_stale(tmp_db, sample_task.task_id)

        tmp_db.mark_stale_lease_atomic(ex.execution_id)

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.RECOVERY_REQUIRED

    # 5. VERIFYING → RECOVERY_REQUIRED
    def test_verifying_task_transitions_to_recovery_required(self, tmp_db, sample_task):
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.VERIFYING, validate_transition=False
        )
        ex = _setup_stale(tmp_db, sample_task.task_id)

        tmp_db.mark_stale_lease_atomic(ex.execution_id)

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.RECOVERY_REQUIRED

    # 3. Full stale-lease recovery (execution, lease, task, event)
    def test_full_stale_lease_recovery(self, tmp_db, sample_task):
        """execution → INTERRUPTED, lease released, task → RECOVERY_REQUIRED, event emitted."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RUNNING, validate_transition=False
        )
        ex = _setup_stale(tmp_db, sample_task.task_id)

        tmp_db.mark_stale_lease_atomic(ex.execution_id)

        saved_ex = tmp_db.get_execution(ex.execution_id)
        assert saved_ex.status == ExecutionStatus.INTERRUPTED
        assert saved_ex.ended_at is not None

        assert tmp_db.get_active_lease(ex.workspace) is None

        types = [e.event_type for e in tmp_db.get_events_for_task(sample_task.task_id)]
        assert AuditEventType.RECOVERY_SCAN_STALE_LEASE in types

    # 6. Stale lease with task that must NOT transition
    def test_completed_task_not_transitioned(self, tmp_db, sample_task):
        """Terminal-state tasks must not be transitioned by a stale-lease scan."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.COMPLETED, validate_transition=False
        )
        ex = _setup_stale(tmp_db, sample_task.task_id)

        tmp_db.mark_stale_lease_atomic(ex.execution_id)

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.COMPLETED  # unchanged

        # Audit event still emitted to record the stale lease was detected
        types = [e.event_type for e in tmp_db.get_events_for_task(sample_task.task_id)]
        assert AuditEventType.RECOVERY_SCAN_STALE_LEASE in types

    def test_already_recovery_required_task_not_double_transitioned(self, tmp_db, sample_task):
        """Task already in RECOVERY_REQUIRED is not re-transitioned."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
        )
        ex = _setup_stale(tmp_db, sample_task.task_id)

        tmp_db.mark_stale_lease_atomic(ex.execution_id)

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.RECOVERY_REQUIRED  # unchanged, not reset

    # 8. Idempotency / concurrent-duplicate protection
    def test_idempotent_on_repeated_calls(self, tmp_db, sample_task):
        """Repeated calls must not raise; state stays consistent."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RUNNING, validate_transition=False
        )
        ex = _setup_stale(tmp_db, sample_task.task_id)

        tmp_db.mark_stale_lease_atomic(ex.execution_id)  # first call
        tmp_db.mark_stale_lease_atomic(ex.execution_id)  # second — must not raise

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.RECOVERY_REQUIRED  # not re-transitioned back

        saved_ex = tmp_db.get_execution(ex.execution_id)
        assert saved_ex.status == ExecutionStatus.INTERRUPTED  # not double-changed

    def test_event_emitted_on_each_idempotent_call(self, tmp_db, sample_task):
        """Each call — even idempotent ones — emits an audit event for traceability."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RUNNING, validate_transition=False
        )
        ex = _setup_stale(tmp_db, sample_task.task_id)

        tmp_db.mark_stale_lease_atomic(ex.execution_id)
        tmp_db.mark_stale_lease_atomic(ex.execution_id)

        types = [e.event_type for e in tmp_db.get_events_for_task(sample_task.task_id)]
        stale_events = [t for t in types if t == AuditEventType.RECOVERY_SCAN_STALE_LEASE]
        assert len(stale_events) == 2

    # 7. Rollback: state + audit roll back together (also proves 9 — no independent commit)
    def test_rollback_on_event_failure(self, tmp_db, sample_task, monkeypatch):
        """If the audit INSERT fails, execution, lease, and task changes are all rolled back."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RUNNING, validate_transition=False
        )
        ex = _setup_stale(tmp_db, sample_task.task_id)

        def _fail(cls):
            raise RuntimeError("forced failure — rollback test")

        monkeypatch.setattr(AuditEvent, "new_id", classmethod(_fail))

        with pytest.raises(RuntimeError, match="forced failure"):
            tmp_db.mark_stale_lease_atomic(ex.execution_id)

        # All state changes must be rolled back (proves single-transaction atomicity)
        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.RUNNING  # not RECOVERY_REQUIRED

        saved_ex = tmp_db.get_execution(ex.execution_id)
        assert saved_ex.status == ExecutionStatus.RUNNING  # not INTERRUPTED

        assert tmp_db.get_active_lease(ex.workspace) is not None  # not released

        assert tmp_db.event_count() == 0
