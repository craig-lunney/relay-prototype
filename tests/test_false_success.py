"""
Regression tests for the false-success authority flaw fix (P1-I post-mortem).

INVARIANT: TASK_COMPLETED requires BOTH:
  (1) worker ran — exit_code is not None, and
  (2) independent verification passed.

Pre-launch failures (exit_code is None — Claude was never launched) must be
routed directly to TASK_FAILED without calling the verifier. Calling the verifier
on an unchanged baseline workspace produces a false TASK_COMPLETED whenever the
existing tests already pass.

Root cause (EXEC-4B0F0A87): RELAY_API_KEY_FILE was unset; executor failed closed
before launching Claude; exit_code was never set; the original code routed ALL
FAILED executions to the verifier; the baseline workspace (with the fix already
applied) passed 17/17 tests; TASK_COMPLETED was recorded despite zero agent work.

These tests cover four scenarios:
  A — pre-launch failure:   verifier NOT called, task FAILED
  B — post-launch failure:  verifier IS called (Claude may have done partial work)
  C — Claude COMPLETED:     verifier called, task COMPLETED only if it passes
  D — auth failure (models EXEC-4B0F0A87 exactly): verifier NOT called, error preserved
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from tests.conftest import hold_worker_lease

from relay.cli import cmd_run_task
from relay.executor import ExecutionEngine
from relay.models import (
    Execution,
    ExecutionStatus,
    TaskStatus,
)
from relay.verifier import Verifier, VerificationResult


def _now():
    return datetime.now(timezone.utc)


def _controlled_execution(
    task_id: str,
    *,
    status: ExecutionStatus,
    exit_code,
    error: str | None = None,
    workspace: str = "",
) -> Execution:
    return Execution(
        execution_id=Execution.new_id(),
        task_id=task_id,
        attempt_number=1,
        status=status,
        workspace=workspace,
        worker_identity="test-worker",
        policy_version="0.1",
        capability_snapshot=[],
        started_at=_now(),
        ended_at=_now(),
        exit_code=exit_code,
        error=error,
    )


def _args(task_id, db_path, workspace_path, timeout=120):
    """Build a minimal argparse-style namespace for cmd_run_task."""
    from unittest.mock import MagicMock
    args = MagicMock()
    args.task_id = task_id
    args.db = str(db_path)
    args.workspace = str(workspace_path)
    args.timeout = timeout
    args.keep_workspace = True  # avoid cleanup side-effects in tests
    return args


def _run_side_effect(db_path, return_exec):
    """
    Return a side_effect function for patching ExecutionEngine.run.

    The real executor transitions the task to RUNNING before launching Claude.
    This side_effect mirrors that: it transitions the task to RUNNING, then
    returns the controlled execution. This keeps cmd_run_task's initial READY
    check happy while ensuring subsequent RUNNING→VERIFYING transitions are valid.
    """
    from relay.state import StateManager

    def _side_effect(task, workspace_base, timeout_seconds=120):
        s = StateManager(str(db_path))
        s.update_task_status(task.task_id, TaskStatus.RUNNING, validate_transition=True)
        hold_worker_lease(db_path, return_exec)
        return return_exec

    return _side_effect


def _passing_result() -> VerificationResult:
    return VerificationResult(
        passed=True,
        branch="main",
        git_status_lines=[],
        files_changed=[],
        files_added=[],
        files_deleted=[],
        unexpected_files=[],
        test_exit_code=0,
        test_output="1 passed",
        acceptance_results={"All tests pass": True},
        report_agreement={},
        errors=[],
    )


def _failing_result() -> VerificationResult:
    return VerificationResult(
        passed=False,
        branch="main",
        git_status_lines=[],
        files_changed=[],
        files_added=[],
        files_deleted=[],
        unexpected_files=[],
        test_exit_code=1,
        test_output="1 failed",
        acceptance_results={"All tests pass": False},
        report_agreement={},
        errors=["test_calculator.py::test_discount FAILED"],
    )


# ── Scenario A: Pre-launch failure — verifier must NOT be called ──────────────

def test_a_pre_launch_failure_skips_verifier(tmp_db, tmp_path, sample_task):
    """
    Pre-launch failure (exit_code=None, status=FAILED): worker was never launched.
    The verifier must not be called — the workspace is an unchanged baseline copy
    and verifying it would produce a false TASK_COMPLETED if existing tests pass.
    """
    pre_launch = _controlled_execution(
        sample_task.task_id,
        status=ExecutionStatus.FAILED,
        exit_code=None,
        error="Pre-launch failure: workspace lease could not be acquired.",
    )
    args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")

    with patch.object(ExecutionEngine, "run", return_value=pre_launch):
        with patch.object(Verifier, "verify") as mock_verify:
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)

    mock_verify.assert_not_called()
    assert exc.value.code == 1
    task = tmp_db.get_task(sample_task.task_id)
    assert task.status == TaskStatus.FAILED


def test_a_pre_launch_failure_preserves_error(tmp_db, tmp_path, sample_task):
    """
    The error message from the pre-launch failure must be recorded on the task,
    not silently discarded.
    """
    error_msg = "Execution blocked: manual approval policy requires human sign-off."
    pre_launch = _controlled_execution(
        sample_task.task_id,
        status=ExecutionStatus.FAILED,
        exit_code=None,
        error=error_msg,
    )
    args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")

    with patch.object(ExecutionEngine, "run", return_value=pre_launch):
        with patch.object(Verifier, "verify"):
            with pytest.raises(SystemExit):
                cmd_run_task(args)

    task = tmp_db.get_task(sample_task.task_id)
    assert task.status == TaskStatus.FAILED
    assert "manual approval" in task.error


def test_a_pre_launch_no_error_field_uses_default(tmp_db, tmp_path, sample_task):
    """
    If the pre-launch execution has no error string, the task still transitions to
    FAILED with a non-empty reason rather than a blank error field.
    """
    pre_launch = _controlled_execution(
        sample_task.task_id,
        status=ExecutionStatus.FAILED,
        exit_code=None,
        error=None,
    )
    args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")

    with patch.object(ExecutionEngine, "run", return_value=pre_launch):
        with patch.object(Verifier, "verify"):
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)

    assert exc.value.code == 1
    task = tmp_db.get_task(sample_task.task_id)
    assert task.status == TaskStatus.FAILED
    assert task.error  # non-empty default reason


# ── Scenario B: Post-launch failure — verifier IS called ──────────────────────

def test_b_post_launch_failure_calls_verifier(tmp_db, tmp_path, sample_task):
    """
    Claude ran and exited non-zero (exit_code=1, status=FAILED, worker_ran=True).
    The verifier must be called — Claude may have done useful partial work whose
    result should be independently assessed, not discarded.
    """
    post_launch = _controlled_execution(
        sample_task.task_id,
        status=ExecutionStatus.FAILED,
        exit_code=1,
        error="Claude exited with code 1.",
    )
    args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")

    # Use side_effect so engine.run() also transitions the task to RUNNING, as
    # the real executor would before launching Claude (READY→RUNNING in the DB).
    with patch.object(ExecutionEngine, "run", side_effect=_run_side_effect(tmp_path / "relay_test.db", post_launch)):
        with patch.object(Verifier, "verify", return_value=_failing_result()) as mock_verify:
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)

    mock_verify.assert_called_once()
    assert exc.value.code == 1
    task = tmp_db.get_task(sample_task.task_id)
    assert task.status == TaskStatus.FAILED


def test_b_post_launch_failure_exit_code_nonzero_is_discriminator(tmp_db, tmp_path, sample_task):
    """
    exit_code != None is the sole discriminator for worker_ran.
    An exit_code of 2 (not just 1) must also route through the verifier.
    """
    post_launch = _controlled_execution(
        sample_task.task_id,
        status=ExecutionStatus.FAILED,
        exit_code=2,
        error="Claude exited with code 2.",
    )
    args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")

    with patch.object(ExecutionEngine, "run", side_effect=_run_side_effect(tmp_path / "relay_test.db", post_launch)):
        with patch.object(Verifier, "verify", return_value=_failing_result()) as mock_verify:
            with pytest.raises(SystemExit):
                cmd_run_task(args)

    mock_verify.assert_called_once()


# ── Scenario C: Claude COMPLETED — verifier called, COMPLETED only if it passes ─

def test_c_completed_execution_calls_verifier(tmp_db, tmp_path, sample_task):
    """
    Claude COMPLETED (exit_code=0). The verifier must be called. TASK_COMPLETED is
    only recorded after independent verification passes — never solely on Claude's claim.
    """
    completed = _controlled_execution(
        sample_task.task_id,
        status=ExecutionStatus.COMPLETED,
        exit_code=0,
    )
    args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")

    with patch.object(ExecutionEngine, "run", side_effect=_run_side_effect(tmp_path / "relay_test.db", completed)):
        with patch.object(Verifier, "verify", return_value=_passing_result()) as mock_verify:
            cmd_run_task(args)  # must not sys.exit on success

    mock_verify.assert_called_once()
    task = tmp_db.get_task(sample_task.task_id)
    assert task.status == TaskStatus.COMPLETED


def test_c_completed_execution_verification_failure_blocks_task_completed(tmp_db, tmp_path, sample_task):
    """
    Claude says COMPLETED but the verifier catches a lie — task must NOT reach
    TASK_COMPLETED. This is the core claim-vs-reality check.
    """
    completed = _controlled_execution(
        sample_task.task_id,
        status=ExecutionStatus.COMPLETED,
        exit_code=0,
    )
    args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")

    with patch.object(ExecutionEngine, "run", side_effect=_run_side_effect(tmp_path / "relay_test.db", completed)):
        with patch.object(Verifier, "verify", return_value=_failing_result()) as mock_verify:
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)

    mock_verify.assert_called_once()
    assert exc.value.code == 1
    task = tmp_db.get_task(sample_task.task_id)
    assert task.status == TaskStatus.FAILED
    assert task.status != TaskStatus.COMPLETED


# ── Scenario D: Auth failure path (models EXEC-4B0F0A87) ─────────────────────

def test_d_auth_failure_skips_verifier(tmp_db, tmp_path, sample_task):
    """
    Exact reproduction of the false-success event EXEC-4B0F0A87:
    RELAY_API_KEY_FILE was not set; executor failed closed; exit_code stayed None;
    the original code routed the FAILED execution to the verifier; baseline tests
    passed; TASK_COMPLETED was incorrectly recorded.

    With the fix, a pre-launch FAILED execution with auth-error text and
    exit_code=None must NOT call the verifier, and the task must be FAILED.
    """
    auth_error_msg = (
        "Authentication not configured: RELAY_API_KEY_FILE is not set. "
        "Set it to the path of the Anthropic API key file before running relay. "
        "Claude cannot authenticate without it."
    )
    auth_failure = _controlled_execution(
        sample_task.task_id,
        status=ExecutionStatus.FAILED,
        exit_code=None,  # Claude was never launched
        error=auth_error_msg,
    )
    args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")

    with patch.object(ExecutionEngine, "run", return_value=auth_failure):
        with patch.object(Verifier, "verify") as mock_verify:
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)

    mock_verify.assert_not_called()
    assert exc.value.code == 1

    task = tmp_db.get_task(sample_task.task_id)
    assert task.status == TaskStatus.FAILED
    assert "Authentication not configured" in (task.error or "")


def test_d_auth_failure_never_reaches_task_completed(tmp_db, tmp_path, sample_task):
    """
    Even if baseline tests would pass (simulated by a would-be passing verifier),
    an auth failure must not result in TASK_COMPLETED. This directly tests the
    scenario that caused the false-success in the first integration attempt.
    """
    auth_failure = _controlled_execution(
        sample_task.task_id,
        status=ExecutionStatus.FAILED,
        exit_code=None,
        error="Authentication not configured: RELAY_API_KEY_FILE is not set.",
    )
    args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")

    # Even if we patched verify to return passing, the gate must prevent the call.
    with patch.object(ExecutionEngine, "run", return_value=auth_failure):
        with patch.object(Verifier, "verify", return_value=_passing_result()):
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)

    assert exc.value.code == 1
    task = tmp_db.get_task(sample_task.task_id)
    assert task.status != TaskStatus.COMPLETED
    assert task.status == TaskStatus.FAILED
