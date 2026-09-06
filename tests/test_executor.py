"""Tests for the execution engine."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from relay.executor import ExecutionEngine
from relay.models import ExecutionStatus, Task, TaskStatus
from relay.audit import AuditLog
from relay.policy import PolicyEngine
from relay.state import StateManager


def _make_engine(tmp_db, audit, policy):
    return ExecutionEngine(tmp_db, audit, policy)


# ── Workspace preparation ─────────────────────────────────────────────────────

def test_prepare_workspace_copies_repository(tmp_db, audit, policy, sample_task, tmp_path):
    engine = _make_engine(tmp_db, audit, policy)
    workspace = engine._prepare_workspace(sample_task, tmp_path / "ws")
    assert Path(workspace).exists()
    assert (Path(workspace) / "calculator.py").exists()
    assert (Path(workspace) / "test_calculator.py").exists()


def test_prepare_workspace_overwrites_stale_copy(tmp_db, audit, policy, sample_task, tmp_path):
    engine = _make_engine(tmp_db, audit, policy)
    ws_base = tmp_path / "ws"
    workspace = engine._prepare_workspace(sample_task, ws_base)
    # Pollute the workspace
    (Path(workspace) / "stale_file.txt").write_text("stale")
    # Re-prepare should give a clean copy
    workspace2 = engine._prepare_workspace(sample_task, ws_base)
    assert not (Path(workspace2) / "stale_file.txt").exists()


# ── Concurrent lease prevention ───────────────────────────────────────────────

def test_second_execution_blocked_by_active_lease(tmp_db, audit, policy, sample_task, tmp_path):
    """Two executions cannot run on the same workspace simultaneously."""
    engine = _make_engine(tmp_db, audit, policy)
    workspace = engine._prepare_workspace(sample_task, tmp_path / "ws")

    from relay.models import Execution, ExecutionLease
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    ex = Execution(
        execution_id=Execution.new_id(),
        task_id=sample_task.task_id,
        attempt_number=1,
        status=ExecutionStatus.RUNNING,
        workspace=workspace,
        worker_identity="other-worker",
        policy_version="0.1",
        capability_snapshot=[],
        started_at=now,
    )
    tmp_db.create_execution(ex)
    lease = ExecutionLease(
        execution_id=ex.execution_id,
        task_id=sample_task.task_id,
        workspace=workspace,
        pid=99999,
        started_at=now,
        expires_at=now + timedelta(seconds=120),
        worker_identity="other-worker",
    )
    tmp_db.acquire_lease(lease)

    # Attempt a second run on the same workspace
    with pytest.raises(RuntimeError, match="active lease"):
        # We need to mock _prepare_workspace to return the same workspace
        with patch.object(engine, "_prepare_workspace", return_value=workspace):
            engine.run(sample_task, tmp_path / "ws")


# ── Max attempts ──────────────────────────────────────────────────────────────

def test_exceeding_max_attempts_raises(tmp_db, audit, policy, sample_task, tmp_path):
    sample_task.attempt_count = sample_task.max_execution_attempts
    tmp_db.update_task_status(
        sample_task.task_id, TaskStatus.RUNNING, validate_transition=False
    )
    # Manually set attempt_count in db
    for _ in range(sample_task.max_execution_attempts):
        tmp_db.increment_attempt_count(sample_task.task_id)

    # Reload
    reloaded = tmp_db.get_task(sample_task.task_id)
    engine = _make_engine(tmp_db, audit, policy)

    with pytest.raises(RuntimeError, match="exhausted"):
        engine.run(reloaded, tmp_path / "ws")


# ── Timeout handling ──────────────────────────────────────────────────────────

def _make_timeout_popen(*args, **kwargs):
    """Popen mock that times out on first communicate() then returns empty on second."""
    proc = MagicMock()
    call_count = [0]

    def communicate(timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise subprocess.TimeoutExpired(args[0] if args else "cmd", timeout or 1)
        return ("", "")  # called after proc.kill()

    proc.communicate.side_effect = communicate
    proc.returncode = -1
    return proc


def test_timeout_sets_execution_status(tmp_db, audit, policy, sample_task, tmp_path):
    engine = _make_engine(tmp_db, audit, policy)

    with patch("subprocess.Popen", side_effect=_make_timeout_popen):
        execution = engine.run(sample_task, tmp_path / "ws", timeout_seconds=1)

    assert execution.status == ExecutionStatus.TIMEOUT
    assert execution.error is not None
    assert "timed out" in execution.error.lower()


def test_timeout_is_persisted_to_db(tmp_db, audit, policy, sample_task, tmp_path):
    engine = _make_engine(tmp_db, audit, policy)

    with patch("subprocess.Popen", side_effect=_make_timeout_popen):
        execution = engine.run(sample_task, tmp_path / "ws", timeout_seconds=1)

    saved = tmp_db.get_execution(execution.execution_id)
    assert saved.status == ExecutionStatus.TIMEOUT
    assert saved.ended_at is not None


def test_timeout_emits_audit_event(tmp_db, audit, policy, sample_task, tmp_path):
    engine = _make_engine(tmp_db, audit, policy)

    with patch("subprocess.Popen", side_effect=_make_timeout_popen):
        execution = engine.run(sample_task, tmp_path / "ws", timeout_seconds=1)

    events = tmp_db.get_events_for_task(sample_task.task_id)
    event_types = [e.event_type.value for e in events]
    assert "EXECUTION_TIMEOUT" in event_types


def test_timeout_ceiling_enforced(tmp_db, audit, policy, sample_task, tmp_path):
    """Requesting timeout > MAX_TIMEOUT_SECONDS is silently clamped."""
    from relay.executor import MAX_TIMEOUT_SECONDS
    engine = _make_engine(tmp_db, audit, policy)

    captured_timeout = []
    call_tracker = [0]

    def mock_popen(*args, **kwargs):
        proc = MagicMock()
        def communicate(timeout=None):
            call_tracker[0] += 1
            if call_tracker[0] == 1:
                captured_timeout.append(timeout)
                raise subprocess.TimeoutExpired("cmd", timeout)
            return ("", "")
        proc.communicate.side_effect = communicate
        return proc

    with patch("subprocess.Popen", side_effect=mock_popen):
        engine.run(sample_task, tmp_path / "ws", timeout_seconds=99999)

    assert captured_timeout[0] <= MAX_TIMEOUT_SECONDS


# ── Successful execution ──────────────────────────────────────────────────────

def _make_success_popen(report_json: dict):
    """Popen mock that returns a successful execution with a report."""
    def factory(*args, **kwargs):
        proc = MagicMock()
        proc.communicate.return_value = (json.dumps(report_json), "")
        proc.returncode = 0
        return proc
    return factory


def test_successful_execution_parses_report(tmp_db, audit, policy, sample_task, tmp_path):
    report = {
        "protocol_version": "0.1",
        "message_type": "IMPLEMENTATION_REPORT",
        "task_id": sample_task.task_id,
        "execution_id": "EXEC-TESTTEST",
        "status": "COMPLETED",
        "summary": "Fixed the bug",
        "files_changed": ["calculator.py"],
        "files_added": [],
        "files_deleted": [],
        "tests_run": ["test_calculator.py"],
        "risks": [],
        "uncertainties": [],
        "blockers": [],
        "recommended_next_action": "none",
    }
    engine = _make_engine(tmp_db, audit, policy)

    with patch("subprocess.Popen", side_effect=_make_success_popen(report)):
        execution = engine.run(sample_task, tmp_path / "ws")

    assert execution.status == ExecutionStatus.COMPLETED
    assert execution.report is not None
    # Report is evidence, not authoritative — it's stored but not acted on without verification
    assert execution.report.status == "COMPLETED"


def test_failed_execution_preserves_stdout_stderr(tmp_db, audit, policy, sample_task, tmp_path):
    """A failed execution retains enough state for recovery analysis."""
    def mock_popen(*args, **kwargs):
        proc = MagicMock()
        proc.communicate.return_value = ("some stdout", "some stderr error")
        proc.returncode = 1
        return proc

    engine = _make_engine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=mock_popen):
        execution = engine.run(sample_task, tmp_path / "ws")

    assert execution.status == ExecutionStatus.FAILED
    saved = tmp_db.get_execution(execution.execution_id)
    assert saved.stdout == "some stdout"
    assert saved.stderr == "some stderr error"
    assert saved.exit_code == 1
    assert saved.ended_at is not None
    assert saved.started_at is not None


# ── Lease lifecycle ───────────────────────────────────────────────────────────

def test_lease_released_after_successful_execution(tmp_db, audit, policy, sample_task, tmp_path):
    report = {
        "protocol_version": "0.1",
        "message_type": "IMPLEMENTATION_REPORT",
        "task_id": sample_task.task_id,
        "execution_id": "X",
        "status": "COMPLETED",
        "summary": "done",
        "files_changed": [], "files_added": [], "files_deleted": [],
        "tests_run": [], "risks": [], "uncertainties": [], "blockers": [],
        "recommended_next_action": "none",
    }
    engine = _make_engine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=_make_success_popen(report)):
        execution = engine.run(sample_task, tmp_path / "ws")

    # Lease should be released even after successful completion
    active = tmp_db.get_active_lease(execution.workspace)
    assert active is None


def test_lease_released_after_timeout(tmp_db, audit, policy, sample_task, tmp_path):
    engine = _make_engine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=_make_timeout_popen):
        execution = engine.run(sample_task, tmp_path / "ws", timeout_seconds=1)

    active = tmp_db.get_active_lease(execution.workspace)
    assert active is None
