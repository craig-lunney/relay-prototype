"""P1-K Step 5: Pre-launch failure handling — focused tests.

Verifies the invariants introduced in Step 5:
  A. Auth failure → execution FAILED in DB atomically (no phantom RUNNING)
  B. Auth failure → no EXECUTION_STARTED event (Claude never ran)
  C. Auth failure → EXECUTION_FAILED event present
  D. Auth failure → exit_code is None (pre-launch discriminator preserved)
  E. FileNotFoundError → execution FAILED in DB atomically
  F. FileNotFoundError → no EXECUTION_STARTED event
  G. FileNotFoundError → EXECUTION_FAILED event present
  H. Atomicity: if event INSERT fails inside pre_launch_failed_atomic,
     the execution status UPDATE is also rolled back (single transaction)
  I. Successful launch → EXECUTION_STARTED IS emitted (regression guard)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from relay.executor import ExecutionEngine
from relay.models import AuditEvent, AuditEventType, ExecutionStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def _event_types(tmp_db, task_id):
    return [e.event_type for e in tmp_db.get_events_for_task(task_id)]


# ── A: Auth failure — no phantom RUNNING execution ───────────────────────────

def test_A_auth_failure_execution_failed_in_db(
    tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
):
    """Auth failure commits execution FAILED atomically — DB never shows phantom RUNNING."""
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)
    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=MagicMock()):
        execution = engine.run(sample_task, tmp_path / "ws")

    saved = tmp_db.get_execution(execution.execution_id)
    assert saved is not None
    assert saved.status == ExecutionStatus.FAILED


# ── B: Auth failure — no EXECUTION_STARTED event ────────────────────────────

def test_B_auth_failure_no_execution_started_event(
    tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
):
    """Auth failure must not produce EXECUTION_STARTED — Claude never started."""
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)
    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=MagicMock()):
        engine.run(sample_task, tmp_path / "ws")

    assert AuditEventType.EXECUTION_STARTED not in _event_types(tmp_db, sample_task.task_id)


# ── C: Auth failure — EXECUTION_FAILED event present ────────────────────────

def test_C_auth_failure_emits_execution_failed_event(
    tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
):
    """Auth failure must emit an EXECUTION_FAILED audit event."""
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)
    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=MagicMock()):
        engine.run(sample_task, tmp_path / "ws")

    assert AuditEventType.EXECUTION_FAILED in _event_types(tmp_db, sample_task.task_id)


# ── D: Auth failure — exit_code is None ─────────────────────────────────────

def test_D_auth_failure_exit_code_is_none(
    tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
):
    """Auth failure must leave exit_code=None — the pre-launch discriminator."""
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)
    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=MagicMock()):
        execution = engine.run(sample_task, tmp_path / "ws")

    saved = tmp_db.get_execution(execution.execution_id)
    assert saved.exit_code is None


# ── E: FileNotFoundError — no phantom RUNNING execution ─────────────────────

def test_E_file_not_found_execution_failed_in_db(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """Popen FileNotFoundError commits execution FAILED atomically — no phantom RUNNING."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=FileNotFoundError("claude: not found")):
        execution = engine.run(sample_task, tmp_path / "ws")

    saved = tmp_db.get_execution(execution.execution_id)
    assert saved is not None
    assert saved.status == ExecutionStatus.FAILED


# ── F: FileNotFoundError — no EXECUTION_STARTED event ───────────────────────

def test_F_file_not_found_no_execution_started_event(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """FileNotFoundError must not produce EXECUTION_STARTED — Claude never ran."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=FileNotFoundError("claude: not found")):
        engine.run(sample_task, tmp_path / "ws")

    assert AuditEventType.EXECUTION_STARTED not in _event_types(tmp_db, sample_task.task_id)


# ── G: FileNotFoundError — EXECUTION_FAILED event present ───────────────────

def test_G_file_not_found_emits_execution_failed_event(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """FileNotFoundError must emit an EXECUTION_FAILED audit event."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=FileNotFoundError("claude: not found")):
        engine.run(sample_task, tmp_path / "ws")

    assert AuditEventType.EXECUTION_FAILED in _event_types(tmp_db, sample_task.task_id)


# ── H: Atomicity — rollback on event INSERT failure ─────────────────────────

def test_H_pre_launch_failed_atomic_rollback(
    tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
):
    """If the event INSERT fails inside pre_launch_failed_atomic, the execution
    status UPDATE is rolled back too — proving single-transaction atomicity."""
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)

    def _fail(cls):
        raise RuntimeError("forced event INSERT failure — rollback test")

    monkeypatch.setattr(AuditEvent, "new_id", classmethod(_fail))

    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=MagicMock()):
        with pytest.raises(RuntimeError, match="forced event INSERT failure"):
            engine.run(sample_task, tmp_path / "ws")

    # Execution was created as RUNNING before the atomic call.
    # The UPDATE inside pre_launch_failed_atomic rolled back — status must still be RUNNING.
    executions = tmp_db.get_executions_for_task(sample_task.task_id)
    assert executions, "Expected at least one execution to have been created"
    assert any(e.status == ExecutionStatus.RUNNING for e in executions), (
        "No RUNNING execution found — the rolled-back UPDATE must not have persisted."
    )

    # The EXECUTION_FAILED event INSERT was also rolled back.
    assert AuditEventType.EXECUTION_FAILED not in _event_types(tmp_db, sample_task.task_id)


# ── I: Successful launch — EXECUTION_STARTED is emitted ─────────────────────

def test_I_successful_launch_emits_execution_started(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """When Claude launches (Popen succeeds), EXECUTION_STARTED must be in the audit trail."""
    proc = MagicMock()
    proc.communicate.return_value = ("", "")
    proc.returncode = 0

    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", return_value=proc):
        engine.run(sample_task, tmp_path / "ws")

    assert AuditEventType.EXECUTION_STARTED in _event_types(tmp_db, sample_task.task_id)
