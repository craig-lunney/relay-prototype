"""P1-K Step 6: Full crash/recovery integration tests.

Exercises end-to-end lifecycle scenarios across real crash/recovery boundaries.
Uses real StateManager, SQLite, workspace filesystem, lease handling,
RecoveryEngine, and ExecutionEngine. Only the Claude subprocess boundary
is mocked to deterministically simulate launch, timeout, interruption, or crash.

Scenarios:
  1. Crash while RUNNING → scan → RECOVERY_REQUIRED → accept_recovery → READY
  2. Pre-launch OS crash (no EXECUTION_STARTED) → scan → RECOVERY_REQUIRED
  3. Interrupted workspace preserved + renamed by accept-recovery (real filesystem)
  4. TIMEOUT → retry → READY; subsequent execution gets fresh workspace/identity
  5. Recovery + retry: execution history preserved, attempt counts correct
  6. Active vs stale lease effects on can_safely_restart
  7. Terminal states cannot be reopened by recovery mechanisms
  8. Duplicate/concurrent recovery rejected by CAS protection
  9. Full audit trail reflects the crash-recover-retry lifecycle
"""
from __future__ import annotations

import argparse
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from relay.cli import cmd_accept_recovery, cmd_retry_task
from relay.executor import ExecutionEngine
from relay.models import (
    AuditEventType,
    Execution,
    ExecutionLease,
    ExecutionStatus,
    TaskStatus,
)
from relay.recovery import RecoveryEngine
from relay.state import InvalidTransitionError


# ── Shared helpers ────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_types(tmp_db, task_id) -> list[AuditEventType]:
    return [e.event_type for e in tmp_db.get_events_for_task(task_id)]


def _make_running_execution(task_id: str, workspace: str) -> Execution:
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


def _make_active_lease(execution: Execution) -> ExecutionLease:
    return ExecutionLease(
        execution_id=execution.execution_id,
        task_id=execution.task_id,
        workspace=execution.workspace,
        pid=99999,
        started_at=_now(),
        expires_at=_now() + timedelta(hours=1),
        worker_identity=execution.worker_identity,
    )


def _expire_lease(tmp_db, execution_id: str) -> None:
    """Force an active lease to appear expired (direct DB update; test use only)."""
    tmp_db._conn().execute(
        "UPDATE execution_leases SET expires_at=? WHERE execution_id=? AND released=0",
        ((_now() - timedelta(seconds=120)).isoformat(), execution_id),
    )
    tmp_db._conn().commit()


def _setup_crashed_execution(tmp_db, task_id: str, workspace: str) -> Execution:
    """Simulate the DB state left after an OS-level crash mid-execution.

    Replicates what engine.run() committed before the process died:
    execution created as RUNNING, lease acquired, task RUNNING, attempt_count
    incremented. No EXECUTION_STARTED event — the crash happened before Popen.
    """
    tmp_db.update_task_status(task_id, TaskStatus.RUNNING, validate_transition=False)
    ex = _make_running_execution(task_id, workspace)
    tmp_db.create_execution(ex)
    tmp_db.acquire_lease(_make_active_lease(ex))
    tmp_db.increment_attempt_count(task_id)
    return ex


def _accept_args(tmp_db, task_id: str, workspace_base: str) -> argparse.Namespace:
    return argparse.Namespace(
        db=tmp_db.db_path,
        task_id=task_id,
        actor="ops",
        workspace=workspace_base,
    )


def _retry_args(tmp_db, task_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        db=tmp_db.db_path,
        task_id=task_id,
        actor="ops",
    )


def _timeout_popen(*args, **kwargs):
    proc = MagicMock()
    calls = [0]
    def communicate(timeout=None, input=None):
        calls[0] += 1
        if calls[0] == 1:
            raise subprocess.TimeoutExpired(args[0] if args else "cmd", timeout or 1)
        return ("", "")
    proc.communicate.side_effect = communicate
    proc.returncode = -1
    return proc


def _success_popen(*args, **kwargs):
    proc = MagicMock()
    proc.communicate.return_value = ("", "")
    proc.returncode = 0
    return proc


# ══ Scenario 1: Crash while RUNNING ══════════════════════════════════════════

class TestScenario1CrashWhileRunning:
    """Full cycle: RUNNING + stale lease → scan → RECOVERY_REQUIRED → accept_recovery → READY."""

    def _crash(self, tmp_db, task_id, workspace):
        ex = _setup_crashed_execution(tmp_db, task_id, workspace)
        _expire_lease(tmp_db, ex.execution_id)
        return ex

    def test_scan_transitions_to_recovery_required(self, tmp_db, audit, sample_task, tmp_path):
        ws = str(tmp_path / "crashed")
        ex = self._crash(tmp_db, sample_task.task_id, ws)

        RecoveryEngine(tmp_db, audit).scan()

        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RECOVERY_REQUIRED

    def test_execution_marked_interrupted(self, tmp_db, audit, sample_task, tmp_path):
        ws = str(tmp_path / "crashed")
        ex = self._crash(tmp_db, sample_task.task_id, ws)

        RecoveryEngine(tmp_db, audit).scan()

        saved = tmp_db.get_execution(ex.execution_id)
        assert saved.status == ExecutionStatus.INTERRUPTED
        assert saved.ended_at is not None

    def test_lease_released_by_scan(self, tmp_db, audit, sample_task, tmp_path):
        ws = str(tmp_path / "crashed")
        ex = self._crash(tmp_db, sample_task.task_id, ws)

        RecoveryEngine(tmp_db, audit).scan()

        assert tmp_db.get_active_lease(ws) is None

    def test_accept_recovery_transitions_to_ready(self, tmp_db, audit, sample_task, tmp_path):
        ws = str(tmp_path / "crashed")
        self._crash(tmp_db, sample_task.task_id, ws)
        RecoveryEngine(tmp_db, audit).scan()

        tmp_db.accept_recovery_atomic(sample_task.task_id, actor="ops")

        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.READY

    def test_execution_history_preserved_after_recovery(self, tmp_db, audit, sample_task, tmp_path):
        ws = str(tmp_path / "crashed")
        ex = self._crash(tmp_db, sample_task.task_id, ws)
        RecoveryEngine(tmp_db, audit).scan()
        tmp_db.accept_recovery_atomic(sample_task.task_id)

        executions = tmp_db.get_executions_for_task(sample_task.task_id)
        assert len(executions) == 1
        assert executions[0].execution_id == ex.execution_id
        assert executions[0].status == ExecutionStatus.INTERRUPTED

    def test_audit_trail_contains_stale_lease_and_recovery_accepted(
        self, tmp_db, audit, sample_task, tmp_path
    ):
        ws = str(tmp_path / "crashed")
        self._crash(tmp_db, sample_task.task_id, ws)
        RecoveryEngine(tmp_db, audit).scan()
        tmp_db.accept_recovery_atomic(sample_task.task_id)

        types = _event_types(tmp_db, sample_task.task_id)
        assert AuditEventType.RECOVERY_SCAN_STALE_LEASE in types
        assert AuditEventType.RECOVERY_ACCEPTED in types


# ══ Scenario 2: Pre-launch OS crash (no EXECUTION_STARTED) ═══════════════════

class TestScenario2PreLaunchCrash:
    """OS crash before Popen: RUNNING execution, no EXECUTION_STARTED, scan recovers."""

    def test_no_execution_started_before_crash(self, tmp_db, audit, sample_task, tmp_path):
        """The crashed execution has no EXECUTION_STARTED — process never ran."""
        ws = str(tmp_path / "pre_launch")
        _setup_crashed_execution(tmp_db, sample_task.task_id, ws)

        # No EXECUTION_STARTED — Popen was never called
        assert AuditEventType.EXECUTION_STARTED not in _event_types(tmp_db, sample_task.task_id)

    def test_scan_detects_and_recovers_pre_launch_crash(
        self, tmp_db, audit, sample_task, tmp_path
    ):
        """Recovery scanner handles the RUNNING+lease state left by a pre-launch crash."""
        ws = str(tmp_path / "pre_launch")
        ex = _setup_crashed_execution(tmp_db, sample_task.task_id, ws)
        _expire_lease(tmp_db, ex.execution_id)

        RecoveryEngine(tmp_db, audit).scan()

        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RECOVERY_REQUIRED
        assert tmp_db.get_execution(ex.execution_id).status == ExecutionStatus.INTERRUPTED

    def test_no_execution_started_after_scan(self, tmp_db, audit, sample_task, tmp_path):
        """Scan must not inject a spurious EXECUTION_STARTED into the audit trail."""
        ws = str(tmp_path / "pre_launch")
        ex = _setup_crashed_execution(tmp_db, sample_task.task_id, ws)
        _expire_lease(tmp_db, ex.execution_id)

        RecoveryEngine(tmp_db, audit).scan()

        # Only the stale-lease scan event should exist — no EXECUTION_STARTED
        assert AuditEventType.EXECUTION_STARTED not in _event_types(tmp_db, sample_task.task_id)
        assert AuditEventType.RECOVERY_SCAN_STALE_LEASE in _event_types(tmp_db, sample_task.task_id)

    def test_recovery_cycle_completes_after_pre_launch_crash(
        self, tmp_db, audit, sample_task, tmp_path
    ):
        """Full recovery cycle (scan → accept_recovery) completes after a pre-launch crash."""
        ws = str(tmp_path / "pre_launch")
        ex = _setup_crashed_execution(tmp_db, sample_task.task_id, ws)
        _expire_lease(tmp_db, ex.execution_id)

        RecoveryEngine(tmp_db, audit).scan()
        tmp_db.accept_recovery_atomic(sample_task.task_id)

        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.READY
        # Still no EXECUTION_STARTED — it should never appear for this execution
        assert AuditEventType.EXECUTION_STARTED not in _event_types(tmp_db, sample_task.task_id)


# ══ Scenario 3: Interrupted workspace preserved through recovery ═══════════════

class TestScenario3WorkspacePreservation:
    """Interrupted workspace is preserved/renamed by accept-recovery (real filesystem)."""

    def _setup(self, tmp_db, sample_task, tmp_path):
        workspace_base = tmp_path / "ws"
        workspace_base.mkdir()
        workspace = workspace_base / sample_task.task_id
        workspace.mkdir()
        (workspace / "partial.py").write_text("# incomplete work")
        ex = _setup_crashed_execution(tmp_db, sample_task.task_id, str(workspace))
        _expire_lease(tmp_db, ex.execution_id)
        return workspace_base, workspace, ex

    def test_workspace_renamed_to_interrupted_path(
        self, tmp_db, audit, sample_task, tmp_path
    ):
        """accept-recovery renames workspace to <task_id>.interrupted.<exec_id[:8]>."""
        workspace_base, workspace, ex = self._setup(tmp_db, sample_task, tmp_path)
        RecoveryEngine(tmp_db, audit).scan()

        cmd_accept_recovery(_accept_args(tmp_db, sample_task.task_id, str(workspace_base)))

        renamed = workspace_base / f"{sample_task.task_id}.interrupted.{ex.execution_id[:8]}"
        assert renamed.exists(), "Forensic workspace must be preserved under renamed path"
        assert not workspace.exists(), "Original workspace must be vacated for next run"

    def test_workspace_contents_preserved_in_renamed_directory(
        self, tmp_db, audit, sample_task, tmp_path
    ):
        """Forensic files inside the workspace survive the rename."""
        workspace_base, workspace, ex = self._setup(tmp_db, sample_task, tmp_path)
        RecoveryEngine(tmp_db, audit).scan()

        cmd_accept_recovery(_accept_args(tmp_db, sample_task.task_id, str(workspace_base)))

        renamed = workspace_base / f"{sample_task.task_id}.interrupted.{ex.execution_id[:8]}"
        assert (renamed / "partial.py").exists()
        assert (renamed / "partial.py").read_text() == "# incomplete work"

    def test_task_ready_after_recovery_with_workspace_rename(
        self, tmp_db, audit, sample_task, tmp_path
    ):
        """Task reaches READY after the full scan + workspace-rename + accept cycle."""
        workspace_base, workspace, ex = self._setup(tmp_db, sample_task, tmp_path)
        RecoveryEngine(tmp_db, audit).scan()

        cmd_accept_recovery(_accept_args(tmp_db, sample_task.task_id, str(workspace_base)))

        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.READY


# ══ Scenario 4: TIMEOUT → retry → READY; new execution gets fresh workspace ═══

class TestScenario4TimeoutAndRetry:
    """TIMEOUT execution: retry path works; subsequent execution is independent."""

    def _run_timeout(self, engine, sample_task, workspace_base):
        with patch("subprocess.Popen", side_effect=_timeout_popen):
            return engine.run(sample_task, workspace_base, timeout_seconds=1)

    def test_timeout_execution_retry_transitions_to_ready(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        engine = ExecutionEngine(tmp_db, audit, policy)
        ex = self._run_timeout(engine, sample_task, tmp_path / "ws")

        assert ex.status == ExecutionStatus.TIMEOUT

        engine.release_workspace_lease(ex.execution_id)
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=True
        )

        tmp_db.retry_task_atomic(sample_task.task_id, actor="ops")

        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.READY

    def test_timeout_execution_preserved_in_history_after_retry(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        engine = ExecutionEngine(tmp_db, audit, policy)
        ex = self._run_timeout(engine, sample_task, tmp_path / "ws")

        engine.release_workspace_lease(ex.execution_id)
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=True
        )
        tmp_db.retry_task_atomic(sample_task.task_id)

        saved = tmp_db.get_execution(ex.execution_id)
        assert saved.status == ExecutionStatus.TIMEOUT
        assert saved.exit_code == -1  # timeout sentinel

    def test_retry_new_execution_gets_different_identity(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        """After retry, the next engine.run() produces a distinct execution."""
        engine = ExecutionEngine(tmp_db, audit, policy)
        ex1 = self._run_timeout(engine, sample_task, tmp_path / "ws")

        engine.release_workspace_lease(ex1.execution_id)
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=True
        )
        tmp_db.retry_task_atomic(sample_task.task_id)

        task2 = tmp_db.get_task(sample_task.task_id)
        with patch("subprocess.Popen", side_effect=_success_popen):
            ex2 = engine.run(task2, tmp_path / "ws")

        assert ex2.execution_id != ex1.execution_id
        assert ex2.status == ExecutionStatus.COMPLETED

    def test_both_executions_in_history_after_retry(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        engine = ExecutionEngine(tmp_db, audit, policy)
        ex1 = self._run_timeout(engine, sample_task, tmp_path / "ws")

        engine.release_workspace_lease(ex1.execution_id)
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=True
        )
        tmp_db.retry_task_atomic(sample_task.task_id)

        task2 = tmp_db.get_task(sample_task.task_id)
        with patch("subprocess.Popen", side_effect=_success_popen):
            ex2 = engine.run(task2, tmp_path / "ws")

        all_executions = tmp_db.get_executions_for_task(sample_task.task_id)
        assert len(all_executions) == 2
        statuses = {e.status for e in all_executions}
        assert ExecutionStatus.TIMEOUT in statuses
        assert ExecutionStatus.COMPLETED in statuses

    def test_attempt_count_correct_after_retry_and_new_execution(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        engine = ExecutionEngine(tmp_db, audit, policy)
        ex1 = self._run_timeout(engine, sample_task, tmp_path / "ws")

        engine.release_workspace_lease(ex1.execution_id)
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=True
        )
        tmp_db.retry_task_atomic(sample_task.task_id)

        task2 = tmp_db.get_task(sample_task.task_id)
        with patch("subprocess.Popen", side_effect=_success_popen):
            engine.run(task2, tmp_path / "ws")

        task_final = tmp_db.get_task(sample_task.task_id)
        assert task_final.attempt_count == 2


# ══ Scenario 5: Recovery + retry preserves history and attempt counts ══════════

class TestScenario5MultiAttemptHistory:
    """After crash + recovery + retry: history preserved, attempt counts correct."""

    def _crash_and_recover(self, tmp_db, audit, sample_task, tmp_path):
        """Simulate crash → scan → accept_recovery → READY."""
        ws = str(tmp_path / "crashed_ws")
        ex1 = _setup_crashed_execution(tmp_db, sample_task.task_id, ws)
        _expire_lease(tmp_db, ex1.execution_id)
        RecoveryEngine(tmp_db, audit).scan()
        tmp_db.accept_recovery_atomic(sample_task.task_id)
        return ex1

    def test_prior_execution_still_in_history_after_second_run(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        ex1 = self._crash_and_recover(tmp_db, audit, sample_task, tmp_path)

        task2 = tmp_db.get_task(sample_task.task_id)
        engine = ExecutionEngine(tmp_db, audit, policy)
        with patch("subprocess.Popen", side_effect=_success_popen):
            ex2 = engine.run(task2, tmp_path / "ws2")

        all_executions = tmp_db.get_executions_for_task(sample_task.task_id)
        exec_ids = {e.execution_id for e in all_executions}
        assert ex1.execution_id in exec_ids
        assert ex2.execution_id in exec_ids

    def test_interrupted_status_preserved_in_history(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        ex1 = self._crash_and_recover(tmp_db, audit, sample_task, tmp_path)

        task2 = tmp_db.get_task(sample_task.task_id)
        engine = ExecutionEngine(tmp_db, audit, policy)
        with patch("subprocess.Popen", side_effect=_success_popen):
            engine.run(task2, tmp_path / "ws2")

        assert tmp_db.get_execution(ex1.execution_id).status == ExecutionStatus.INTERRUPTED

    def test_second_execution_has_distinct_identity(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        ex1 = self._crash_and_recover(tmp_db, audit, sample_task, tmp_path)

        task2 = tmp_db.get_task(sample_task.task_id)
        engine = ExecutionEngine(tmp_db, audit, policy)
        with patch("subprocess.Popen", side_effect=_success_popen):
            ex2 = engine.run(task2, tmp_path / "ws2")

        assert ex2.execution_id != ex1.execution_id

    def test_attempt_count_after_crash_recovery_and_second_execution(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        """attempt_count reflects both the crashed and successful execution."""
        self._crash_and_recover(tmp_db, audit, sample_task, tmp_path)

        # Crash used attempt 1; accept_recovery_atomic does not reset the count.
        task2 = tmp_db.get_task(sample_task.task_id)
        assert task2.attempt_count == 1

        engine = ExecutionEngine(tmp_db, audit, policy)
        with patch("subprocess.Popen", side_effect=_success_popen):
            engine.run(task2, tmp_path / "ws2")

        task_final = tmp_db.get_task(sample_task.task_id)
        assert task_final.attempt_count == 2


# ══ Scenario 6: Lease cleanup and recovery protection ═════════════════════════

class TestScenario6LeaseProtection:
    """Active leases block recovery; stale leases released by scan enable recovery."""

    def test_active_lease_blocks_can_safely_restart(self, tmp_db, audit, sample_task, tmp_path):
        """An active lease on the execution workspace prevents safe restart."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
        )
        ws = str(tmp_path / "active_ws")
        ex = _make_running_execution(sample_task.task_id, ws)
        tmp_db.create_execution(ex)
        tmp_db.acquire_lease(_make_active_lease(ex))

        engine = RecoveryEngine(tmp_db, audit)
        can_restart, reason = engine.can_safely_restart(sample_task.task_id)

        assert not can_restart
        assert "lease" in reason.lower()

    def test_stale_lease_scan_releases_blocking_lease(self, tmp_db, audit, sample_task, tmp_path):
        """Expired lease is released by scan, unblocking safe restart."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RECOVERY_REQUIRED, validate_transition=False
        )
        ws = str(tmp_path / "stale_ws")
        ex = _make_running_execution(sample_task.task_id, ws)
        tmp_db.create_execution(ex)
        tmp_db.acquire_lease(_make_active_lease(ex))
        _expire_lease(tmp_db, ex.execution_id)

        # Before scan: lease blocks restart
        engine = RecoveryEngine(tmp_db, audit)
        can_before, _ = engine.can_safely_restart(sample_task.task_id)
        assert not can_before

        # Scan releases the stale lease
        engine.scan()

        # After scan: lease is gone, restart is safe
        can_after, reason = engine.can_safely_restart(sample_task.task_id)
        assert can_after, reason

    def test_live_lease_protects_running_execution(
        self, tmp_db, audit, sample_task, tmp_path, monkeypatch
    ):
        """A live worker with valid lease is not interrupted by the recovery scanner."""
        ws = str(tmp_path / "live_ws")
        ex = _setup_crashed_execution(tmp_db, sample_task.task_id, ws)
        # Lease is NOT expired (active, worker alive)
        monkeypatch.setattr(RecoveryEngine, "_worker_is_alive", lambda self, lease: True)

        requiring_recovery = RecoveryEngine(tmp_db, audit).scan()

        assert sample_task.task_id not in requiring_recovery
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RUNNING
        assert tmp_db.get_active_lease(ws) is not None  # lease intact


# ══ Scenario 7: Terminal states cannot be reopened ════════════════════════════

class TestScenario7TerminalStateIsolation:
    """Terminal-state tasks are not modified by crash/recovery mechanisms."""

    @pytest.mark.parametrize("terminal_status", [
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.REJECTED,
    ])
    def test_terminal_task_not_reopened_by_scan(
        self, tmp_db, audit, sample_task, tmp_path, terminal_status
    ):
        """Recovery scan must not transition a terminal task to RECOVERY_REQUIRED."""
        tmp_db.update_task_status(
            sample_task.task_id, terminal_status, validate_transition=False
        )
        ws = str(tmp_path / "terminal_ws")
        ex = _make_running_execution(sample_task.task_id, ws)
        tmp_db.create_execution(ex)
        tmp_db.acquire_lease(_make_active_lease(ex))
        _expire_lease(tmp_db, ex.execution_id)

        RecoveryEngine(tmp_db, audit).scan()

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == terminal_status, (
            f"Terminal task {terminal_status.value} must not be reopened by scan"
        )

    @pytest.mark.parametrize("terminal_status", [
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    ])
    def test_accept_recovery_fails_on_terminal_task(
        self, tmp_db, sample_task, terminal_status
    ):
        """accept_recovery_atomic must raise on a task that is not RECOVERY_REQUIRED."""
        tmp_db.update_task_status(
            sample_task.task_id, terminal_status, validate_transition=False
        )

        with pytest.raises(InvalidTransitionError):
            tmp_db.accept_recovery_atomic(sample_task.task_id)

    def test_retry_task_fails_on_completed_task(self, tmp_db, sample_task):
        """retry_task_atomic must raise on a COMPLETED task."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.COMPLETED, validate_transition=False
        )

        with pytest.raises(InvalidTransitionError):
            tmp_db.retry_task_atomic(sample_task.task_id)


# ══ Scenario 8: Duplicate/concurrent recovery rejected ════════════════════════

class TestScenario8CASProtection:
    """CAS guards prevent double recovery/retry after the first succeeds."""

    def test_second_accept_recovery_rejected_after_first_succeeds(
        self, tmp_db, audit, sample_task, tmp_path
    ):
        """Second accept_recovery_atomic raises because task is now READY."""
        ws = str(tmp_path / "cas_ws")
        ex = _setup_crashed_execution(tmp_db, sample_task.task_id, ws)
        _expire_lease(tmp_db, ex.execution_id)
        RecoveryEngine(tmp_db, audit).scan()

        # First accept succeeds
        tmp_db.accept_recovery_atomic(sample_task.task_id)
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.READY

        # Second accept must fail — task is READY, not RECOVERY_REQUIRED
        with pytest.raises(InvalidTransitionError):
            tmp_db.accept_recovery_atomic(sample_task.task_id)

    def test_second_retry_task_rejected_after_first_succeeds(self, tmp_db, sample_task):
        """Second retry_task_atomic raises because task is now READY."""
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=False
        )

        tmp_db.retry_task_atomic(sample_task.task_id)
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.READY

        with pytest.raises(InvalidTransitionError):
            tmp_db.retry_task_atomic(sample_task.task_id)

    def test_concurrent_scan_produces_single_recovery_transition(
        self, tmp_db, audit, sample_task, tmp_path
    ):
        """Two scan() calls on the same execution produce exactly one RECOVERY_REQUIRED."""
        ws = str(tmp_path / "concurrent_ws")
        ex = _setup_crashed_execution(tmp_db, sample_task.task_id, ws)
        _expire_lease(tmp_db, ex.execution_id)

        engine1 = RecoveryEngine(tmp_db, audit)
        engine2 = RecoveryEngine(tmp_db, audit)

        result1 = engine1.scan()
        result2 = engine2.scan()  # lease already released — no active leases found

        assert sample_task.task_id in result1
        assert result2 == []

        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RECOVERY_REQUIRED

        stale_events = [
            e for e in tmp_db.get_events_for_task(sample_task.task_id)
            if e.event_type == AuditEventType.RECOVERY_SCAN_STALE_LEASE
        ]
        assert len(stale_events) == 1


# ══ Scenario 9: Audit trail reflects crash-recover-retry lifecycle ═════════════

class TestScenario9AuditTrail:
    """The full event sequence accurately reflects what happened at each boundary."""

    def test_full_crash_recovery_audit_sequence(self, tmp_db, audit, sample_task, tmp_path):
        """Full audit trail: crash → scan → RECOVERY_REQUIRED → accept_recovery → READY."""
        ws = str(tmp_path / "audit_ws")
        _setup_crashed_execution(tmp_db, sample_task.task_id, ws)
        _expire_lease(tmp_db, tmp_db.get_executions_for_task(sample_task.task_id)[0].execution_id)

        RecoveryEngine(tmp_db, audit).scan()
        tmp_db.accept_recovery_atomic(sample_task.task_id)

        types = _event_types(tmp_db, sample_task.task_id)
        assert AuditEventType.RECOVERY_SCAN_STALE_LEASE in types
        assert AuditEventType.RECOVERY_ACCEPTED in types
        # No EXECUTION_STARTED — crash happened before Popen
        assert AuditEventType.EXECUTION_STARTED not in types

    def test_timeout_retry_audit_sequence(self, tmp_db, audit, policy, sample_task, tmp_path):
        """Audit trail for TIMEOUT + retry contains EXECUTION_STARTED (process ran)."""
        engine = ExecutionEngine(tmp_db, audit, policy)
        with patch("subprocess.Popen", side_effect=_timeout_popen):
            ex = engine.run(sample_task, tmp_path / "ws", timeout_seconds=1)

        engine.release_workspace_lease(ex.execution_id)
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=True
        )
        tmp_db.retry_task_atomic(sample_task.task_id, actor="ops")

        types = _event_types(tmp_db, sample_task.task_id)
        # Popen succeeded → EXECUTION_STARTED was emitted
        assert AuditEventType.EXECUTION_STARTED in types
        assert AuditEventType.EXECUTION_TIMEOUT in types
        assert AuditEventType.TASK_RETRIED in types

    def test_no_execution_started_for_pre_launch_failure_via_executor(
        self, tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
    ):
        """Auth failure via executor: no EXECUTION_STARTED in audit (Step 5 invariant)."""
        monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)
        engine = ExecutionEngine(tmp_db, audit, policy)
        with patch("subprocess.Popen", side_effect=MagicMock()):
            engine.run(sample_task, tmp_path / "ws")

        types = _event_types(tmp_db, sample_task.task_id)
        assert AuditEventType.EXECUTION_STARTED not in types
        assert AuditEventType.EXECUTION_FAILED in types

    def test_full_recovery_plus_retry_audit_spans_both_executions(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        """Audit trail spans the full crash-recover-timeout-retry cycle."""
        ws = str(tmp_path / "full_ws")
        ex1 = _setup_crashed_execution(tmp_db, sample_task.task_id, ws)
        _expire_lease(tmp_db, ex1.execution_id)
        RecoveryEngine(tmp_db, audit).scan()
        tmp_db.accept_recovery_atomic(sample_task.task_id)

        task2 = tmp_db.get_task(sample_task.task_id)
        engine = ExecutionEngine(tmp_db, audit, policy)
        with patch("subprocess.Popen", side_effect=_timeout_popen):
            ex2 = engine.run(task2, tmp_path / "ws2", timeout_seconds=1)

        engine.release_workspace_lease(ex2.execution_id)
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT, validate_transition=True
        )
        tmp_db.retry_task_atomic(sample_task.task_id, actor="ops")

        types = _event_types(tmp_db, sample_task.task_id)
        # First execution (crash): no EXECUTION_STARTED
        # Recovery events
        assert AuditEventType.RECOVERY_SCAN_STALE_LEASE in types
        assert AuditEventType.RECOVERY_ACCEPTED in types
        # Second execution (timeout): Popen ran → EXECUTION_STARTED present
        assert AuditEventType.EXECUTION_STARTED in types
        assert AuditEventType.EXECUTION_TIMEOUT in types
        # Retry
        assert AuditEventType.TASK_RETRIED in types
