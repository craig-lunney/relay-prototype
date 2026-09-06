"""Tests for Relay SQLite state manager."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from relay.models import (
    AuditEvent,
    AuditEventType,
    Execution,
    ExecutionLease,
    ExecutionStatus,
    Task,
    TaskStatus,
)
from relay.state import InvalidTransitionError, StateManager


# ── Tasks ─────────────────────────────────────────────────────────────────────

def test_create_and_retrieve_task(tmp_db, sample_task):
    retrieved = tmp_db.get_task(sample_task.task_id)
    assert retrieved is not None
    assert retrieved.task_id == sample_task.task_id
    assert retrieved.status == TaskStatus.READY
    assert retrieved.project_id == sample_task.project_id


def test_get_nonexistent_task_returns_none(tmp_db):
    assert tmp_db.get_task("TASK-DOESNTEXIST") is None


def test_update_task_status(tmp_db, sample_task):
    tmp_db.update_task_status(sample_task.task_id, TaskStatus.RUNNING)
    task = tmp_db.get_task(sample_task.task_id)
    assert task.status == TaskStatus.RUNNING


def test_update_task_status_invalid_transition_raises(tmp_db, sample_task):
    with pytest.raises(InvalidTransitionError):
        # READY → COMPLETED is not a valid transition
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.COMPLETED, validate_transition=True
        )


def test_update_task_status_no_validation(tmp_db, sample_task):
    # validate_transition=False bypasses the check (for recovery/testing use)
    tmp_db.update_task_status(
        sample_task.task_id, TaskStatus.COMPLETED, validate_transition=False
    )
    task = tmp_db.get_task(sample_task.task_id)
    assert task.status == TaskStatus.COMPLETED


def test_increment_attempt_count(tmp_db, sample_task):
    tmp_db.increment_attempt_count(sample_task.task_id)
    tmp_db.increment_attempt_count(sample_task.task_id)
    task = tmp_db.get_task(sample_task.task_id)
    assert task.attempt_count == 2


def test_task_json_fields_roundtrip(tmp_db, sample_task):
    task = tmp_db.get_task(sample_task.task_id)
    assert task.requirements == sample_task.requirements
    assert task.acceptance_criteria == sample_task.acceptance_criteria
    assert task.requested_actions == sample_task.requested_actions
    assert task.forbidden_actions == sample_task.forbidden_actions
    assert task.required_tests == sample_task.required_tests


# ── Executions ────────────────────────────────────────────────────────────────

def _make_execution(task_id: str, workspace: str = "/tmp/ws") -> Execution:
    now = datetime.now(timezone.utc)
    return Execution(
        execution_id=Execution.new_id(),
        task_id=task_id,
        attempt_number=1,
        status=ExecutionStatus.RUNNING,
        workspace=workspace,
        worker_identity="test-worker",
        policy_version="0.1",
        capability_snapshot=["read_repository"],
        started_at=now,
    )


def test_create_and_retrieve_execution(tmp_db, sample_task):
    ex = _make_execution(sample_task.task_id)
    tmp_db.create_execution(ex)
    retrieved = tmp_db.get_execution(ex.execution_id)
    assert retrieved is not None
    assert retrieved.execution_id == ex.execution_id
    assert retrieved.status == ExecutionStatus.RUNNING


def test_update_execution(tmp_db, sample_task):
    ex = _make_execution(sample_task.task_id)
    tmp_db.create_execution(ex)
    ex.status = ExecutionStatus.COMPLETED
    ex.exit_code = 0
    ex.ended_at = datetime.now(timezone.utc)
    tmp_db.update_execution(ex)

    retrieved = tmp_db.get_execution(ex.execution_id)
    assert retrieved.status == ExecutionStatus.COMPLETED
    assert retrieved.exit_code == 0
    assert retrieved.ended_at is not None


def test_get_executions_for_task_ordered(tmp_db, sample_task):
    for i in range(3):
        ex = _make_execution(sample_task.task_id)
        ex.attempt_number = i + 1
        tmp_db.create_execution(ex)

    execs = tmp_db.get_executions_for_task(sample_task.task_id)
    assert len(execs) == 3
    assert [e.attempt_number for e in execs] == [1, 2, 3]


def test_execution_history_not_overwritten(tmp_db, sample_task):
    """Relay never overwrites past execution records."""
    ex1 = _make_execution(sample_task.task_id)
    ex1.attempt_number = 1
    tmp_db.create_execution(ex1)

    ex2 = _make_execution(sample_task.task_id)
    ex2.attempt_number = 2
    tmp_db.create_execution(ex2)

    execs = tmp_db.get_executions_for_task(sample_task.task_id)
    assert len(execs) == 2
    assert execs[0].execution_id == ex1.execution_id
    assert execs[1].execution_id == ex2.execution_id


# ── Leases ────────────────────────────────────────────────────────────────────

def _make_lease(execution_id: str, task_id: str, workspace: str) -> ExecutionLease:
    now = datetime.now(timezone.utc)
    return ExecutionLease(
        execution_id=execution_id,
        task_id=task_id,
        workspace=workspace,
        pid=12345,
        started_at=now,
        expires_at=now + timedelta(seconds=60),
        worker_identity="test-worker",
    )


def test_acquire_and_release_lease(tmp_db, sample_task):
    ex = _make_execution(sample_task.task_id, "/tmp/ws1")
    tmp_db.create_execution(ex)
    lease = _make_lease(ex.execution_id, sample_task.task_id, "/tmp/ws1")

    acquired = tmp_db.acquire_lease(lease)
    assert acquired is True

    active = tmp_db.get_active_lease("/tmp/ws1")
    assert active is not None
    assert active.execution_id == ex.execution_id

    tmp_db.release_lease(ex.execution_id)
    assert tmp_db.get_active_lease("/tmp/ws1") is None


def test_acquire_lease_fails_if_workspace_occupied(tmp_db, sample_task):
    """Two executions cannot simultaneously hold the same workspace lease."""
    ex1 = _make_execution(sample_task.task_id, "/tmp/same-ws")
    ex2 = _make_execution(sample_task.task_id, "/tmp/same-ws")
    tmp_db.create_execution(ex1)
    tmp_db.create_execution(ex2)

    lease1 = _make_lease(ex1.execution_id, sample_task.task_id, "/tmp/same-ws")
    lease2 = _make_lease(ex2.execution_id, sample_task.task_id, "/tmp/same-ws")

    assert tmp_db.acquire_lease(lease1) is True
    assert tmp_db.acquire_lease(lease2) is False  # blocked


def test_get_all_active_leases(tmp_db, sample_task):
    for ws in ("/tmp/ws-a", "/tmp/ws-b"):
        ex = _make_execution(sample_task.task_id, ws)
        tmp_db.create_execution(ex)
        tmp_db.acquire_lease(_make_lease(ex.execution_id, sample_task.task_id, ws))

    leases = tmp_db.get_all_active_leases()
    assert len(leases) == 2


# ── Events (append-only) ──────────────────────────────────────────────────────

def _make_event(task_id: str, event_type=AuditEventType.TASK_CREATED) -> AuditEvent:
    return AuditEvent(
        event_id=AuditEvent.new_id(),
        event_type=event_type,
        task_id=task_id,
        execution_id=None,
        timestamp=datetime.now(timezone.utc),
        actor="relay",
        details={"test": True},
    )


def test_append_and_retrieve_event(tmp_db, sample_task):
    event = _make_event(sample_task.task_id)
    tmp_db.append_event(event)

    events = tmp_db.get_events_for_task(sample_task.task_id)
    assert len(events) == 1
    assert events[0].event_type == AuditEventType.TASK_CREATED
    assert events[0].actor == "relay"


def test_events_are_ordered_by_sequence(tmp_db, sample_task):
    types = [
        AuditEventType.TASK_CREATED,
        AuditEventType.TASK_VALIDATED,
        AuditEventType.EXECUTION_STARTED,
    ]
    for t in types:
        tmp_db.append_event(_make_event(sample_task.task_id, t))

    events = tmp_db.get_events_for_task(sample_task.task_id)
    assert len(events) == 3
    seqs = [e.sequence for e in events]
    assert seqs == sorted(seqs)
    assert events[0].event_type == AuditEventType.TASK_CREATED
    assert events[2].event_type == AuditEventType.EXECUTION_STARTED


def test_events_sequence_is_globally_monotonic(tmp_db):
    """Sequences must never repeat across tasks."""
    task_a_id = "TASK-AAAAAAAA"
    task_b_id = "TASK-BBBBBBBB"

    # Create minimal task rows so FK constraint is satisfied
    for tid in (task_a_id, task_b_id):
        t = Task(
            task_id=tid, project_id="p", repository="/tmp/r",
            base_ref="main", working_ref="main",
            objective="o", requirements=[], acceptance_criteria=[],
            requested_actions=[], forbidden_actions=[], required_tests=[],
            approval_policy="auto",
        )
        tmp_db.create_task(t)

    tmp_db.append_event(_make_event(task_a_id))
    tmp_db.append_event(_make_event(task_b_id))
    tmp_db.append_event(_make_event(task_a_id))

    all_events = tmp_db.get_all_events()
    seqs = [e.sequence for e in all_events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # no duplicates


def test_event_count(tmp_db, sample_task):
    for _ in range(5):
        tmp_db.append_event(_make_event(sample_task.task_id))
    assert tmp_db.event_count() == 5
