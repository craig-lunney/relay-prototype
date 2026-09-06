"""
P1-B concurrency tests — atomic state and lease hardening.

These tests exercise actual thread-level concurrency, not sequential mocks.
Each test uses threading.Barrier to synchronise threads so they enter the
critical section simultaneously, maximising the chance of exposing races.

SQLite WAL mode serialises writers, but the UNIQUE partial index on
execution_leases(workspace) WHERE released=0 is the authoritative enforcer:
whichever writer commits first owns the lease; all others get IntegrityError.

Tests B1–B5 plus one explicit security invariant test.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from relay.audit import AuditLog
from relay.executor import ExecutionEngine
from relay.models import (
    ALLOWED_TRANSITIONS,
    Execution,
    ExecutionLease,
    ExecutionStatus,
    Task,
    TaskStatus,
)
from relay.policy import PolicyEngine
from relay.state import InvalidTransitionError, StateManager


def _now():
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


def _make_lease(execution_id: str, task_id: str, workspace: str) -> ExecutionLease:
    now = _now()
    return ExecutionLease(
        execution_id=execution_id,
        task_id=task_id,
        workspace=workspace,
        pid=12345,
        started_at=now,
        expires_at=now + timedelta(seconds=60),
        worker_identity="test-worker",
    )


# ── B1: Concurrent lease acquisition — same workspace ─────────────────────────

def test_b1_concurrent_lease_acquisition_same_workspace(tmp_db, sample_task, tmp_path):
    """
    Two threads simultaneously attempt to acquire a lease for the same workspace.
    Exactly one must succeed; the other must fail.
    Never two successes. Database remains consistent.
    Run 10 rounds to make the race meaningful.
    """
    workspace = str(tmp_path / "shared-ws")

    for round_num in range(10):
        results: list[tuple[Execution, bool]] = []  # (execution, acquired) pairs
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        executions = []
        for _ in range(2):
            ex = _make_execution(sample_task.task_id, workspace)
            tmp_db.create_execution(ex)
            executions.append(ex)

        def attempt(ex: Execution) -> None:
            lease = _make_lease(ex.execution_id, sample_task.task_id, workspace)
            barrier.wait()  # synchronise: both threads enter critical section together
            ok = tmp_db.acquire_lease(lease)
            with lock:
                results.append((ex, ok))  # keep execution paired with its result

        threads = [threading.Thread(target=attempt, args=(ex,)) for ex in executions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = sum(1 for _, ok in results if ok)
        assert successes == 1, (
            f"Round {round_num}: expected exactly 1 lease success, "
            f"got {successes}. Results: {[(ex.execution_id, ok) for ex, ok in results]}"
        )

        # Verify database state: exactly one active lease.
        active = tmp_db.get_active_lease(workspace)
        assert active is not None
        winners = [ex for ex, ok in results if ok]
        assert active.execution_id == winners[0].execution_id

        # Release the winning lease so the next round starts clean.
        tmp_db.release_lease(active.execution_id)


# ── B2: Concurrent acquisition — different workspaces ─────────────────────────

def test_b2_concurrent_different_workspaces(tmp_db, sample_task, tmp_path):
    """
    Two threads acquire leases for different workspaces concurrently.
    Both must succeed — independent workspaces must not block each other.
    """
    workspaces = [str(tmp_path / "ws-alpha"), str(tmp_path / "ws-beta")]
    results: dict[str, bool] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    executions = []
    for ws in workspaces:
        ex = _make_execution(sample_task.task_id, ws)
        tmp_db.create_execution(ex)
        executions.append((ws, ex))

    def attempt(ws: str, ex: Execution) -> None:
        lease = _make_lease(ex.execution_id, sample_task.task_id, ws)
        barrier.wait()
        ok = tmp_db.acquire_lease(lease)
        with lock:
            results[ws] = ok

    threads = [threading.Thread(target=attempt, args=(ws, ex)) for ws, ex in executions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results.values()), (
        f"Both workspaces should acquire leases independently; got: {results}"
    )
    assert len(results) == 2


# ── B3: Concurrent task status transition ─────────────────────────────────────

def test_b3_concurrent_task_status_transition(tmp_db, sample_task):
    """
    Two threads simultaneously attempt to transition the same task from READY → RUNNING.
    The resulting state must be RUNNING (valid), never an impossible state.
    Exactly one transition succeeds; the other raises InvalidTransitionError due to
    the CAS (compare-and-swap) WHERE clause detecting the concurrent modification.
    """
    outcomes: list[str] = []  # "ok" or "conflict"
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt_transition() -> None:
        try:
            barrier.wait()
            tmp_db.update_task_status(
                sample_task.task_id, TaskStatus.RUNNING, validate_transition=True
            )
            with lock:
                outcomes.append("ok")
        except InvalidTransitionError:
            with lock:
                outcomes.append("conflict")

    threads = [threading.Thread(target=attempt_transition) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Final task state must be a valid state (RUNNING).
    task = tmp_db.get_task(sample_task.task_id)
    assert task.status == TaskStatus.RUNNING, (
        f"Expected RUNNING, got {task.status}"
    )

    # At least one transition must have succeeded; the other may have conflicted.
    assert "ok" in outcomes, f"No successful transition: {outcomes}"

    # The ALLOWED_TRANSITIONS state machine must not have been violated.
    # READY → RUNNING is the only valid transition from READY.
    # RUNNING → RUNNING is invalid — so at most one "ok" is expected.
    ok_count = outcomes.count("ok")
    conflict_count = outcomes.count("conflict")
    assert ok_count <= 1, f"More than one transition succeeded: {outcomes}"
    assert ok_count + conflict_count == 2, f"Unexpected outcomes: {outcomes}"


# ── B4: Failed lease acquisition — no Claude, correct accounting ───────────────

def test_b4_failed_lease_acquisition(tmp_db, audit, policy, sample_task, tmp_path):
    """
    When the workspace is already leased, engine.run() must:
    - not launch Claude
    - return a FAILED execution (not raise)
    - not increment attempt_count
    - not change task status
    - leave the existing lease intact
    - record the refusal in the audit trail
    """
    engine = ExecutionEngine(tmp_db, audit, policy)
    workspace = engine._prepare_workspace(sample_task, tmp_path / "ws")

    # Pre-lease the workspace by another execution.
    now = _now()
    ex0 = _make_execution(sample_task.task_id, workspace)
    tmp_db.create_execution(ex0)
    lease0 = _make_lease(ex0.execution_id, sample_task.task_id, workspace)
    assert tmp_db.acquire_lease(lease0) is True

    # Attempt engine.run() on the pre-leased workspace.
    with patch.object(engine, "_prepare_workspace", return_value=workspace):
        with patch("subprocess.Popen") as mock_popen:
            result = engine.run(sample_task, tmp_path / "ws")

    # Claude was NOT launched.
    mock_popen.assert_not_called()

    # Execution recorded as FAILED with a descriptive error.
    assert result.status == ExecutionStatus.FAILED
    assert result.error is not None
    assert "lease" in result.error.lower()

    # Original lease intact — the blocking execution still owns the workspace.
    active = tmp_db.get_active_lease(workspace)
    assert active is not None
    assert active.execution_id == ex0.execution_id

    # attempt_count NOT incremented — Claude never ran.
    reloaded = tmp_db.get_task(sample_task.task_id)
    assert reloaded.attempt_count == 0

    # Task status unchanged — still READY.
    assert reloaded.status == TaskStatus.READY

    # Audit trail records the failure.
    events = tmp_db.get_events_for_task(sample_task.task_id)
    failed_events = [e for e in events if "FAILED" in e.event_type.value]
    assert failed_events, "No EXECUTION_FAILED audit event recorded"


# ── B5: Regression — all existing tests still pass ────────────────────────────
# Verified by running the full pytest suite: `pytest tests/ -v`
# This marker acknowledges the requirement explicitly.
# The 99 existing tests serve as B5.


# ── Security invariant: exactly one lease owner per workspace ─────────────────

def test_security_invariant_at_most_one_lease_owner(tmp_db, sample_task, tmp_path):
    """
    Security invariant: it must be impossible for two concurrent executions to
    both believe they own the same workspace lease.

    N_CONCURRENT threads race to acquire a single workspace lease.
    Verified across ROUNDS rounds to catch intermittent races.

    Tested at the StateManager/database boundary — not via mocked functions.
    """
    workspace = str(tmp_path / "invariant-ws")
    N_CONCURRENT = 8
    ROUNDS = 5

    for round_num in range(ROUNDS):
        successes: list[str] = []  # execution_ids that acquired the lease
        lock = threading.Lock()
        barrier = threading.Barrier(N_CONCURRENT)

        executions = []
        for _ in range(N_CONCURRENT):
            ex = _make_execution(sample_task.task_id, workspace)
            tmp_db.create_execution(ex)
            executions.append(ex)

        def race(ex: Execution) -> None:
            lease = _make_lease(ex.execution_id, sample_task.task_id, workspace)
            barrier.wait()  # all threads enter acquire_lease() simultaneously
            ok = tmp_db.acquire_lease(lease)
            if ok:
                with lock:
                    successes.append(ex.execution_id)

        threads = [threading.Thread(target=race, args=(ex,)) for ex in executions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # INVARIANT: exactly one execution owns the lease.
        assert len(successes) == 1, (
            f"Round {round_num}: security invariant violated — "
            f"{len(successes)} executions believe they own the same workspace. "
            f"Owners: {successes}"
        )

        # Database agrees: exactly one active lease for this workspace.
        active = tmp_db.get_active_lease(workspace)
        assert active is not None
        assert active.execution_id == successes[0], (
            f"Round {round_num}: database lease owner {active.execution_id!r} "
            f"does not match winner {successes[0]!r}"
        )

        # Release for the next round.
        tmp_db.release_lease(active.execution_id)
