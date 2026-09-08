"""Focused tests for P1-K CLI commands: accept-recovery and retry-task."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from relay.cli import cmd_accept_recovery, cmd_retry_task
from relay.models import (
    AuditEventType,
    Execution,
    ExecutionLease,
    ExecutionStatus,
    TaskStatus,
)
from relay.state import StateManager


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _make_execution(task_id: str, workspace: str, status=ExecutionStatus.INTERRUPTED) -> Execution:
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


# ── accept-recovery ───────────────────────────────────────────────────────────

class TestAcceptRecovery:
    def _put_in_recovery(self, tmp_db, sample_task) -> None:
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.RECOVERY_REQUIRED,
            validate_transition=False,
        )

    def test_transitions_to_ready(self, tmp_db, sample_task, tmp_path):
        """Happy path: RECOVERY_REQUIRED → READY."""
        self._put_in_recovery(tmp_db, sample_task)
        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
            workspace=str(tmp_path / "ws"),
        )
        cmd_accept_recovery(args)

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.READY

    def test_emits_recovery_accepted_audit_event(self, tmp_db, sample_task, tmp_path):
        self._put_in_recovery(tmp_db, sample_task)
        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
            workspace=str(tmp_path / "ws"),
        )
        cmd_accept_recovery(args)

        types = [e.event_type for e in tmp_db.get_events_for_task(sample_task.task_id)]
        assert AuditEventType.RECOVERY_ACCEPTED in types

    def test_task_not_found_exits_1(self, tmp_db, tmp_path, capsys):
        args = _make_args(
            db=tmp_db.db_path,
            task_id="TASK-NOTEXIST",
            actor="ops",
            workspace=str(tmp_path / "ws"),
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_accept_recovery(args)
        assert exc_info.value.code == 1
        assert "not found" in capsys.readouterr().err

    def test_wrong_status_exits_1(self, tmp_db, sample_task, tmp_path, capsys):
        """sample_task starts READY — not RECOVERY_REQUIRED."""
        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
            workspace=str(tmp_path / "ws"),
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_accept_recovery(args)
        assert exc_info.value.code == 1
        assert "RECOVERY_REQUIRED" in capsys.readouterr().err

    def test_attempts_exhausted_exits_1(self, tmp_db, sample_task, tmp_path, capsys):
        self._put_in_recovery(tmp_db, sample_task)
        for _ in range(sample_task.max_execution_attempts):
            tmp_db.increment_attempt_count(sample_task.task_id)

        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
            workspace=str(tmp_path / "ws"),
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_accept_recovery(args)
        assert exc_info.value.code == 1
        assert "attempt" in capsys.readouterr().err.lower()

    def test_active_lease_blocks_recovery(self, tmp_db, sample_task, tmp_path, capsys):
        self._put_in_recovery(tmp_db, sample_task)

        ws = str(tmp_path / "ws" / sample_task.task_id)
        ex = _make_execution(sample_task.task_id, ws)
        tmp_db.create_execution(ex)
        tmp_db.acquire_lease(_make_active_lease(ex))

        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
            workspace=str(tmp_path / "ws"),
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_accept_recovery(args)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "lease" in err.lower()

    def test_workspace_renamed_when_interrupted_execution_exists(
        self, tmp_db, sample_task, tmp_path
    ):
        """Interrupted workspace is renamed to <task_id>.interrupted.<exec_id[:8]>."""
        self._put_in_recovery(tmp_db, sample_task)

        workspace_base = tmp_path / "ws"
        workspace_base.mkdir()
        current_ws = workspace_base / sample_task.task_id
        current_ws.mkdir()
        (current_ws / "work.py").write_text("# interrupted work")

        ex = _make_execution(sample_task.task_id, str(current_ws))
        tmp_db.create_execution(ex)

        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
            workspace=str(workspace_base),
        )
        cmd_accept_recovery(args)

        assert not current_ws.exists()
        renamed = workspace_base / f"{sample_task.task_id}.interrupted.{ex.execution_id[:8]}"
        assert renamed.exists()
        assert (renamed / "work.py").exists()

    def test_missing_workspace_does_not_fail(self, tmp_db, sample_task, tmp_path):
        """No workspace directory: silently skip rename, still transition to READY."""
        self._put_in_recovery(tmp_db, sample_task)

        workspace_base = tmp_path / "ws"
        workspace_base.mkdir()
        # task workspace deliberately not created

        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
            workspace=str(workspace_base),
        )
        cmd_accept_recovery(args)

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.READY

    def test_no_interrupted_execution_skips_rename(self, tmp_db, sample_task, tmp_path):
        """No interrupted executions: workspace is left in place, DB still transitions."""
        self._put_in_recovery(tmp_db, sample_task)

        workspace_base = tmp_path / "ws"
        workspace_base.mkdir()
        current_ws = workspace_base / sample_task.task_id
        current_ws.mkdir()

        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
            workspace=str(workspace_base),
        )
        cmd_accept_recovery(args)

        assert current_ws.exists()
        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.READY

    def test_db_failure_after_rename_leaves_task_recovery_required(
        self, tmp_db, sample_task, tmp_path, monkeypatch
    ):
        """DB failure after filesystem rename: task stays RECOVERY_REQUIRED; rename is not rolled back."""
        self._put_in_recovery(tmp_db, sample_task)

        workspace_base = tmp_path / "ws"
        workspace_base.mkdir()
        current_ws = workspace_base / sample_task.task_id
        current_ws.mkdir()

        ex = _make_execution(sample_task.task_id, str(current_ws))
        tmp_db.create_execution(ex)

        def _fail(self, task_id, actor="relay"):
            raise RuntimeError("forced DB failure")

        monkeypatch.setattr(StateManager, "accept_recovery_atomic", _fail)

        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
            workspace=str(workspace_base),
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_accept_recovery(args)
        assert exc_info.value.code == 1

        # Task unchanged in DB (atomic method never ran)
        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.RECOVERY_REQUIRED

        # Filesystem rename already happened (not rolled back)
        renamed = workspace_base / f"{sample_task.task_id}.interrupted.{ex.execution_id[:8]}"
        assert renamed.exists()

    def test_most_recent_interrupted_execution_used_for_rename(
        self, tmp_db, sample_task, tmp_path
    ):
        """When multiple interrupted executions exist, the most recent suffix is used."""
        self._put_in_recovery(tmp_db, sample_task)

        workspace_base = tmp_path / "ws"
        workspace_base.mkdir()
        current_ws = workspace_base / sample_task.task_id
        current_ws.mkdir()

        ex1 = _make_execution(sample_task.task_id, str(current_ws))
        ex2 = _make_execution(sample_task.task_id, str(current_ws))
        tmp_db.create_execution(ex1)
        tmp_db.create_execution(ex2)

        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
            workspace=str(workspace_base),
        )
        cmd_accept_recovery(args)

        renamed = workspace_base / f"{sample_task.task_id}.interrupted.{ex2.execution_id[:8]}"
        assert renamed.exists()


# ── retry-task ────────────────────────────────────────────────────────────────

class TestRetryTask:
    def _put_in_timeout(self, tmp_db, sample_task) -> None:
        tmp_db.update_task_status(
            sample_task.task_id, TaskStatus.TIMEOUT,
            validate_transition=False,
        )

    def test_transitions_to_ready(self, tmp_db, sample_task):
        self._put_in_timeout(tmp_db, sample_task)
        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
        )
        cmd_retry_task(args)

        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.READY

    def test_emits_task_retried_audit_event(self, tmp_db, sample_task):
        self._put_in_timeout(tmp_db, sample_task)
        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
        )
        cmd_retry_task(args)

        types = [e.event_type for e in tmp_db.get_events_for_task(sample_task.task_id)]
        assert AuditEventType.TASK_RETRIED in types

    def test_task_not_found_exits_1(self, tmp_db, capsys):
        args = _make_args(
            db=tmp_db.db_path,
            task_id="TASK-NOTEXIST",
            actor="ops",
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_retry_task(args)
        assert exc_info.value.code == 1
        assert "not found" in capsys.readouterr().err

    def test_wrong_status_exits_1(self, tmp_db, sample_task, capsys):
        """sample_task starts READY — not TIMEOUT."""
        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_retry_task(args)
        assert exc_info.value.code == 1
        assert "TIMEOUT" in capsys.readouterr().err

    def test_attempts_exhausted_exits_1(self, tmp_db, sample_task, capsys):
        self._put_in_timeout(tmp_db, sample_task)
        for _ in range(sample_task.max_execution_attempts):
            tmp_db.increment_attempt_count(sample_task.task_id)

        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_retry_task(args)
        assert exc_info.value.code == 1
        assert "attempt" in capsys.readouterr().err.lower()

    def test_active_lease_blocks_retry(self, tmp_db, sample_task, tmp_path, capsys):
        self._put_in_timeout(tmp_db, sample_task)

        ws = str(tmp_path / "ws" / sample_task.task_id)
        ex = _make_execution(sample_task.task_id, ws)
        tmp_db.create_execution(ex)
        tmp_db.acquire_lease(_make_active_lease(ex))

        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_retry_task(args)
        assert exc_info.value.code == 1
        assert "lease" in capsys.readouterr().err.lower()

    def test_no_workspace_manipulation(self, tmp_db, sample_task, tmp_path):
        """retry-task must not touch the filesystem at all."""
        self._put_in_timeout(tmp_db, sample_task)

        ws = tmp_path / "ws" / sample_task.task_id
        ws.mkdir(parents=True)
        (ws / "marker.txt").write_text("untouched")

        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
        )
        cmd_retry_task(args)

        assert ws.exists()
        assert (ws / "marker.txt").read_text() == "untouched"

    def test_duplicate_call_raises_on_second(self, tmp_db, sample_task, capsys):
        """Second retry-task call fails: task is now READY, not TIMEOUT."""
        self._put_in_timeout(tmp_db, sample_task)
        args = _make_args(
            db=tmp_db.db_path,
            task_id=sample_task.task_id,
            actor="ops",
        )
        cmd_retry_task(args)

        with pytest.raises(SystemExit) as exc_info:
            cmd_retry_task(args)
        assert exc_info.value.code == 1
        assert "TIMEOUT" in capsys.readouterr().err
