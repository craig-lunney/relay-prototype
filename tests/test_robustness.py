"""
Batch 3 — robustness fixes from the v1 completion audit.

  E-7   A task left in VERIFYING (or RUNNING) with no lease was invisible to
        `relay recover` and had no way out.
  E-9   The workspace was deleted and re-copied *before* the lease was taken,
        so a concurrent run could destroy a workspace another run owned.
  E-10  The lease covered only the worker timeout; a long verification could
        outlive it and be declared stale by a concurrent `relay recover`.
  E-12  base_ref reached `git checkout` unvalidated, without `--`, and with the
        operator's full environment.
  E-13  required_tests path containment was checked only after baseline
        collection had already run pytest on the specifiers.
  --    `accept-recovery` only warned when it could not move the interrupted
        workspace aside, then proceeded anyway.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from relay.cli import cmd_accept_recovery, cmd_run_task
from relay.executor import (
    VERIFICATION_LEASE_SECONDS,
    ExecutionEngine,
    _worker_environment,
)
from relay.models import (
    AuditEventType,
    Execution,
    ExecutionLease,
    ExecutionStatus,
    TaskStatus,
)
from relay.policy import PolicyEngine
from relay.recovery import RecoveryEngine
from relay.verifier import Verifier

# Captured at import time, before conftest's autouse fixtures replace them.
_REAL_CHECKOUT_BASE_REF = ExecutionEngine._checkout_base_ref
_REAL_CAPTURE_BASELINE = ExecutionEngine._capture_baseline


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _advance(state, task_id, *statuses):
    for s in statuses:
        state.update_task_status(task_id, s, validate_transition=True)


def _execution(task, workspace, status=ExecutionStatus.RUNNING, exit_code=None):
    return Execution(
        execution_id=Execution.new_id(), task_id=task.task_id, attempt_number=1,
        status=status, workspace=str(workspace), worker_identity="test",
        policy_version="0.1", capability_snapshot=[], started_at=_now(),
        exit_code=exit_code,
    )


def _lease(execution, workspace, pid=None, expires_in=600):
    return ExecutionLease(
        execution_id=execution.execution_id, task_id=execution.task_id,
        workspace=str(workspace), pid=pid if pid is not None else os.getpid(),
        started_at=_now(), expires_at=_now() + timedelta(seconds=expires_in),
        worker_identity="test",
    )


def _args(task_id, db, workspace, **extra):
    args = MagicMock()
    args.task_id = task_id
    args.db = str(db)
    args.workspace = str(workspace)
    args.timeout = 60
    args.keep_workspace = True
    args.actor = "craig"
    for k, v in extra.items():
        setattr(args, k, v)
    return args


# ── E-7: orphaned RUNNING / VERIFYING tasks ───────────────────────────────────

class TestOrphanedTasks:
    @pytest.mark.parametrize("final", [TaskStatus.RUNNING, TaskStatus.VERIFYING])
    def test_recover_finds_task_with_no_lease(self, tmp_db, audit, sample_task, final):
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING)
        if final == TaskStatus.VERIFYING:
            _advance(tmp_db, sample_task.task_id, TaskStatus.VERIFYING)
        assert tmp_db.get_all_active_leases() == []

        found = RecoveryEngine(tmp_db, audit).scan()

        assert found == [sample_task.task_id]
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RECOVERY_REQUIRED
        events = [e.event_type for e in tmp_db.get_events_for_task(sample_task.task_id)]
        assert AuditEventType.RECOVERY_REQUIRED in events

    def test_orphan_execution_marked_interrupted(self, tmp_db, audit, sample_task, tmp_path):
        execution = _execution(sample_task, tmp_path / "ws")
        tmp_db.create_execution(execution)
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING)
        RecoveryEngine(tmp_db, audit).scan()
        assert tmp_db.get_execution(execution.execution_id).status == ExecutionStatus.INTERRUPTED

    def test_running_task_with_live_lease_is_left_alone(
        self, tmp_db, audit, sample_task, tmp_path
    ):
        execution = _execution(sample_task, tmp_path / "ws")
        tmp_db.create_execution(execution)
        assert tmp_db.acquire_lease(_lease(execution, tmp_path / "ws"))
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING)

        assert RecoveryEngine(tmp_db, audit).scan() == []
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RUNNING

    def test_orphan_mark_is_refused_while_a_lease_is_active(
        self, tmp_db, sample_task, tmp_path
    ):
        execution = _execution(sample_task, tmp_path / "ws")
        tmp_db.create_execution(execution)
        tmp_db.acquire_lease(_lease(execution, tmp_path / "ws"))
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING)
        assert tmp_db.mark_orphaned_task_atomic(sample_task.task_id, "test") is False
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RUNNING

    def test_repeated_scans_are_idempotent(self, tmp_db, audit, sample_task):
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING)
        engine = RecoveryEngine(tmp_db, audit)
        assert engine.scan() == [sample_task.task_id]
        assert engine.scan() == []
        events = [e for e in tmp_db.get_events_for_task(sample_task.task_id)
                  if e.event_type == AuditEventType.RECOVERY_REQUIRED]
        assert len(events) == 1


class TestVerificationAborted:
    def test_verifier_exception_leaves_task_recoverable(self, tmp_db, sample_task, tmp_path):
        workspace = tmp_path / "ws" / sample_task.task_id
        workspace.mkdir(parents=True)

        def fake_run(self, task, workspace_base, timeout_seconds=120):
            execution = _execution(task, workspace, ExecutionStatus.COMPLETED, exit_code=0)
            tmp_db.create_execution(execution)
            assert tmp_db.acquire_lease(_lease(execution, workspace))
            tmp_db.update_task_status(task.task_id, TaskStatus.RUNNING)
            return execution

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", fake_run), \
             patch.object(Verifier, "verify", side_effect=RuntimeError("git missing")):
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)

        assert exc.value.code == 1
        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.RECOVERY_REQUIRED
        assert "git missing" in (task.error or "")
        assert tmp_db.get_active_leases_for_task(sample_task.task_id) == []

    def test_keyboard_interrupt_during_verification_is_reraised_and_recorded(
        self, tmp_db, sample_task, tmp_path
    ):
        workspace = tmp_path / "ws" / sample_task.task_id
        workspace.mkdir(parents=True)

        def fake_run(self, task, workspace_base, timeout_seconds=120):
            execution = _execution(task, workspace, ExecutionStatus.COMPLETED, exit_code=0)
            tmp_db.create_execution(execution)
            assert tmp_db.acquire_lease(_lease(execution, workspace))
            tmp_db.update_task_status(task.task_id, TaskStatus.RUNNING)
            return execution

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", fake_run), \
             patch.object(Verifier, "verify", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                cmd_run_task(args)

        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RECOVERY_REQUIRED
        assert tmp_db.get_active_leases_for_task(sample_task.task_id) == []


# ── E-10: lease covers verification ───────────────────────────────────────────

class TestLeaseRenewal:
    def test_renew_extends_active_lease(self, tmp_db, sample_task, tmp_path):
        execution = _execution(sample_task, tmp_path / "ws")
        tmp_db.create_execution(execution)
        tmp_db.acquire_lease(_lease(execution, tmp_path / "ws", expires_in=5))
        new_expiry = _now() + timedelta(seconds=VERIFICATION_LEASE_SECONDS)
        assert tmp_db.renew_lease(execution.execution_id, new_expiry) is True
        lease = tmp_db.get_active_leases_for_task(sample_task.task_id)[0]
        assert lease.expires_at >= new_expiry - timedelta(seconds=1)

    def test_renew_refused_for_released_lease(self, tmp_db, sample_task, tmp_path):
        execution = _execution(sample_task, tmp_path / "ws")
        tmp_db.create_execution(execution)
        tmp_db.acquire_lease(_lease(execution, tmp_path / "ws"))
        tmp_db.release_lease(execution.execution_id)
        assert tmp_db.renew_lease(execution.execution_id, _now()) is False

    def test_run_task_renews_lease_before_verifying(self, tmp_db, sample_task, tmp_path):
        workspace = tmp_path / "ws" / sample_task.task_id
        workspace.mkdir(parents=True)
        seen = {}

        def fake_run(self, task, workspace_base, timeout_seconds=120):
            execution = _execution(task, workspace, ExecutionStatus.COMPLETED, exit_code=0)
            tmp_db.create_execution(execution)
            assert tmp_db.acquire_lease(_lease(execution, workspace, expires_in=1))
            tmp_db.update_task_status(task.task_id, TaskStatus.RUNNING)
            return execution

        def fake_verify(self, task, execution, expected_new_files=None):
            seen["lease"] = tmp_db.get_active_leases_for_task(task.task_id)[0]
            raise RuntimeError("stop here")

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", fake_run), \
             patch.object(Verifier, "verify", fake_verify):
            with pytest.raises(SystemExit):
                cmd_run_task(args)
        remaining = (seen["lease"].expires_at - _now()).total_seconds()
        assert remaining > VERIFICATION_LEASE_SECONDS - 30

    def test_lost_lease_prevents_verification(self, tmp_db, sample_task, tmp_path):
        """If recovery reclaimed the lease while the worker ran, run-task must not
        verify or complete the task."""
        workspace = tmp_path / "ws" / sample_task.task_id
        workspace.mkdir(parents=True)

        def fake_run(self, task, workspace_base, timeout_seconds=120):
            execution = _execution(task, workspace, ExecutionStatus.COMPLETED, exit_code=0)
            tmp_db.create_execution(execution)
            assert tmp_db.acquire_lease(_lease(execution, workspace))
            tmp_db.update_task_status(task.task_id, TaskStatus.RUNNING)
            tmp_db.mark_stale_lease_atomic(execution.execution_id)  # concurrent recover
            return execution

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", fake_run), \
             patch.object(Verifier, "verify") as verify:
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)
        assert exc.value.code == 1
        verify.assert_not_called()
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RECOVERY_REQUIRED


# ── E-9: lease before destructive workspace preparation ───────────────────────

class TestLeaseBeforePrepare:
    def test_competing_run_does_not_destroy_leased_workspace(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        base = tmp_path / "ws"
        workspace = (base / sample_task.task_id).resolve()
        workspace.mkdir(parents=True)
        marker = workspace / "owner-marker.txt"
        marker.write_text("owned by the first run")
        holder = _execution(sample_task, workspace)
        tmp_db.create_execution(holder)
        assert tmp_db.acquire_lease(_lease(holder, workspace))

        with patch("subprocess.Popen") as popen:
            execution = ExecutionEngine(tmp_db, audit, policy).run(sample_task, base)

        assert execution.status == ExecutionStatus.FAILED
        assert "lease" in (execution.error or "").lower()
        popen.assert_not_called()
        assert marker.read_text() == "owned by the first run"
        assert tmp_db.get_task(sample_task.task_id).attempt_count == 0

    def test_prepare_failure_after_lease_releases_it(
        self, tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
    ):
        def checkout_fail(self, base_ref, workspace):
            raise RuntimeError("git checkout 'main' failed")

        monkeypatch.setattr(ExecutionEngine, "_checkout_base_ref", checkout_fail)
        execution = ExecutionEngine(tmp_db, audit, policy).run(sample_task, tmp_path / "ws")
        assert execution.status == ExecutionStatus.FAILED
        assert execution.exit_code is None
        assert tmp_db.get_active_leases_for_task(sample_task.task_id) == []
        task = tmp_db.get_task(sample_task.task_id)
        assert task.attempt_count == 0
        assert task.status == TaskStatus.READY


# ── E-12: base_ref ────────────────────────────────────────────────────────────

class TestBaseRef:
    @pytest.mark.parametrize("ref", ["-x", "--orphan=evil", "main;rm", "a b", "a..b",
                                     "", "ref\n", "@{-1}", "main~1^"])
    def test_policy_rejects_unsafe_base_ref(self, sample_task, ref):
        sample_task.base_ref = ref
        result = PolicyEngine().validate_task(sample_task)
        assert result.allowed is False
        assert "base_ref" in result.reason

    @pytest.mark.parametrize("ref", ["main", "release/1.2", "v1.0.0", "feature_x-2"])
    def test_policy_accepts_plain_refs(self, sample_task, ref):
        sample_task.base_ref = ref
        assert PolicyEngine().validate_task(sample_task).allowed is True

    def test_checkout_uses_separator_and_curated_env(self, tmp_db, audit, policy, tmp_path):
        ws = tmp_path / "ws"
        (ws / ".git").mkdir(parents=True)
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["env"] = kwargs.get("env")
            out = str(ws) if cmd[:2] == ["git", "rev-parse"] else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")

        with patch("subprocess.run", fake_run):
            _REAL_CHECKOUT_BASE_REF(ExecutionEngine(tmp_db, audit, policy), "main", str(ws))
        assert seen["cmd"] == ["git", "checkout", "main", "--"]
        assert seen["env"] == _worker_environment()


class TestGitRootGuard:
    """Batch 5 review B1: git must never act on a repository enclosing the workspace."""

    def _git(self, *args, cwd):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       cwd=str(cwd), check=True, capture_output=True, text=True)

    def test_non_root_repository_is_refused_and_enclosing_repo_untouched(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        outer = tmp_path / "outer"
        target = outer / "not-a-repo"
        target.mkdir(parents=True)
        (target / "test_x.py").write_text("def test_x():\n    pass\n")
        self._git("init", "-b", "main", cwd=outer)
        self._git("add", ".", cwd=outer)
        self._git("commit", "-m", "init", cwd=outer)
        self._git("checkout", "-b", "feature", cwd=outer)

        sample_task.repository = str(target)

        engine = ExecutionEngine(tmp_db, audit, policy)
        with patch.object(ExecutionEngine, "_checkout_base_ref", _REAL_CHECKOUT_BASE_REF), \
             patch("subprocess.Popen") as popen:
            execution = engine.run(sample_task, outer / "workspaces")

        assert execution.status == ExecutionStatus.FAILED
        assert "not the root of a git repository" in execution.error
        popen.assert_not_called()
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=str(outer),
                                capture_output=True, text=True).stdout.strip()
        assert branch == "feature", "the enclosing repository's branch must not change"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows .cmd process-tree behaviour")
    def test_timeout_kills_the_whole_worker_tree(self, tmp_path):
        """Batch 5 review S1: killing a .cmd shim must also end the real worker,
        otherwise communicate() waits for it and --timeout is not enforced."""
        import time

        from relay.executor import _kill_process_tree

        shim = tmp_path / "worker.cmd"
        shim.write_text(f'@"{sys.executable}" -c "import time; time.sleep(30)"\r\n')
        proc = subprocess.Popen([str(shim)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        time.sleep(1.0)
        started = time.monotonic()
        _kill_process_tree(proc)
        proc.communicate(timeout=20)
        assert time.monotonic() - started < 10


# ── E-13: required_tests containment before baseline collection ──────────────

class TestRequiredTestsBeforeBaseline:
    def test_escaping_specifier_is_not_collected(self, tmp_db, audit, policy, sample_task,
                                                 tmp_path, monkeypatch):
        sample_task.required_tests = ["../outside_test.py"]
        execution = _execution(sample_task, sample_task.repository)
        tmp_db.create_execution(execution)
        engine = ExecutionEngine(tmp_db, audit, policy)
        with patch("relay.testing.collect_tests") as collect:
            _REAL_CAPTURE_BASELINE(engine, sample_task, execution, sample_task.repository)
        collect.assert_not_called()
        assert execution.baseline_tests_collected == []
        assert "outside" in execution.baseline_test_output


# ── accept-recovery must not proceed if the workspace cannot be moved ─────────

class TestAcceptRecoveryRename:
    def test_rename_failure_aborts_without_state_change(self, tmp_db, sample_task, tmp_path):
        base = tmp_path / "ws"
        (base / sample_task.task_id).mkdir(parents=True)
        execution = _execution(sample_task, base / sample_task.task_id)
        tmp_db.create_execution(execution)
        tmp_db.acquire_lease(_lease(execution, base / sample_task.task_id, pid=0, expires_in=-5))
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING)
        tmp_db.mark_stale_lease_atomic(execution.execution_id)
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RECOVERY_REQUIRED

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", base)
        with patch.object(Path, "rename", side_effect=PermissionError("in use")):
            with pytest.raises(SystemExit) as exc:
                cmd_accept_recovery(args)
        assert exc.value.code == 1
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RECOVERY_REQUIRED


# ── Independent-review findings on Batch 3 ────────────────────────────────────

class TestReviewFindings:
    def test_losing_lease_race_via_cli_changes_nothing(self, tmp_db, sample_task, tmp_path):
        """B2: a second run-task that cannot get the lease must not delete the
        holder's workspace, fail the shared task, or touch the holder's lease."""
        base = tmp_path / "ws"
        workspace = (base / sample_task.task_id).resolve()
        workspace.mkdir(parents=True)
        marker = workspace / "owner-marker.txt"
        marker.write_text("owned by the first run")
        holder = _execution(sample_task, workspace)
        tmp_db.create_execution(holder)
        assert tmp_db.acquire_lease(_lease(holder, workspace))

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", base,
                     keep_workspace=False)
        with patch("subprocess.Popen") as popen:
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)

        assert exc.value.code == 1
        popen.assert_not_called()
        assert marker.read_text() == "owned by the first run"
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.READY
        [active] = tmp_db.get_active_leases_for_task(sample_task.task_id)
        assert active.execution_id == holder.execution_id

    def test_prepare_failure_leaves_no_partial_workspace(
        self, tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
    ):
        def checkout_fail(self, base_ref, workspace):
            raise RuntimeError("git checkout failed")

        monkeypatch.setattr(ExecutionEngine, "_checkout_base_ref", checkout_fail)
        execution = ExecutionEngine(tmp_db, audit, policy).run(sample_task, tmp_path / "ws")
        assert execution.workspace == ""
        assert not (tmp_path / "ws" / sample_task.task_id).exists()

    def test_renewed_lease_is_not_reclaimed_by_a_stale_scan(self, tmp_db, sample_task, tmp_path):
        """S1: recovery decided staleness from an old snapshot; the run renewed
        the lease in between.  The reclaim must not happen."""
        execution = _execution(sample_task, tmp_path / "ws")
        tmp_db.create_execution(execution)
        stale = _lease(execution, tmp_path / "ws", expires_in=-5)
        tmp_db.acquire_lease(stale)
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING)
        observed = tmp_db.get_active_leases_for_task(sample_task.task_id)[0].expires_at

        assert tmp_db.renew_lease(execution.execution_id, _now() + timedelta(seconds=300))
        handled = tmp_db.mark_stale_lease_atomic(
            execution.execution_id, expected_expires_at=observed
        )

        assert handled is False
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RUNNING
        assert tmp_db.get_active_leases_for_task(sample_task.task_id)
        assert tmp_db.get_execution(execution.execution_id).status == ExecutionStatus.RUNNING

    def test_unrenewed_stale_lease_is_still_reclaimed(self, tmp_db, audit, sample_task, tmp_path):
        execution = _execution(sample_task, tmp_path / "ws")
        tmp_db.create_execution(execution)
        tmp_db.acquire_lease(_lease(execution, tmp_path / "ws", expires_in=-5))
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING)
        assert RecoveryEngine(tmp_db, audit).scan() == [sample_task.task_id]
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RECOVERY_REQUIRED

    def test_verifier_system_exit_zero_still_exits_nonzero(self, tmp_db, sample_task, tmp_path):
        workspace = tmp_path / "ws" / sample_task.task_id
        workspace.mkdir(parents=True)

        def fake_run(self, task, workspace_base, timeout_seconds=120):
            execution = _execution(task, workspace, ExecutionStatus.COMPLETED, exit_code=0)
            tmp_db.create_execution(execution)
            assert tmp_db.acquire_lease(_lease(execution, workspace))
            tmp_db.update_task_status(task.task_id, TaskStatus.RUNNING)
            return execution

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", fake_run), \
             patch.object(Verifier, "verify", side_effect=SystemExit(0)):
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)
        assert exc.value.code == 1
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RECOVERY_REQUIRED

    def test_accept_recovery_preserves_workspace_after_aborted_verification(
        self, tmp_db, sample_task, tmp_path
    ):
        """A task orphaned by an aborted verification has a COMPLETED execution;
        its workspace (which the operator was told to review) must be moved
        aside, not left for the next run to delete."""
        base = tmp_path / "ws"
        workspace = base / sample_task.task_id
        workspace.mkdir(parents=True)
        (workspace / "work.py").write_text("x = 1\n")
        execution = _execution(sample_task, workspace, ExecutionStatus.COMPLETED, exit_code=0)
        tmp_db.create_execution(execution)
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING, TaskStatus.VERIFYING)
        assert tmp_db.mark_orphaned_task_atomic(sample_task.task_id, "verification aborted")

        cmd_accept_recovery(_args(sample_task.task_id, tmp_path / "relay_test.db", base))

        moved = base / f"{sample_task.task_id}.interrupted.{execution.execution_id[:8]}"
        assert (moved / "work.py").exists()
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.READY

    def test_orphan_event_names_the_latest_execution(self, tmp_db, audit, sample_task, tmp_path):
        execution = _execution(sample_task, tmp_path / "ws")
        tmp_db.create_execution(execution)
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING)
        RecoveryEngine(tmp_db, audit).scan()
        [event] = [e for e in tmp_db.get_events_for_task(sample_task.task_id)
                   if e.event_type == AuditEventType.RECOVERY_REQUIRED]
        assert event.execution_id == execution.execution_id

    def test_valid_specifier_still_reaches_collection(
        self, tmp_db, audit, policy, sample_task
    ):
        execution = _execution(sample_task, sample_task.repository)
        tmp_db.create_execution(execution)
        engine = ExecutionEngine(tmp_db, audit, policy)
        _REAL_CAPTURE_BASELINE(engine, sample_task, execution, sample_task.repository)
        assert execution.baseline_test_exit == 0
        assert "test_calculator.py::test_add" in execution.baseline_tests_collected


class TestOwnershipRaces:
    """S3 (second review pass): an attempt that does not own the task must never
    change the task's state or touch a workspace."""

    def test_stale_run_after_task_moved_on_changes_nothing(self, tmp_db, sample_task, tmp_path):
        """A run that read READY, but acquires the lease after another run already
        finished (task ACCEPTANCE_REQUIRED, workspace kept for review)."""
        base = tmp_path / "ws"
        workspace = (base / sample_task.task_id).resolve()
        workspace.mkdir(parents=True)
        marker = workspace / "kept-for-review.txt"
        marker.write_text("review me")
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING, TaskStatus.VERIFYING,
                 TaskStatus.ACCEPTANCE_REQUIRED)

        stale_view = tmp_db.get_task(sample_task.task_id)
        stale_view.status = TaskStatus.READY
        engine_task = stale_view
        from relay.audit import AuditLog
        engine = ExecutionEngine(tmp_db, AuditLog(tmp_db), PolicyEngine())
        with patch("subprocess.Popen") as popen:
            execution = engine.run(engine_task, base)

        popen.assert_not_called()
        assert execution.ownership_denied is True
        assert execution.workspace == ""
        assert marker.read_text() == "review me"
        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.ACCEPTANCE_REQUIRED
        assert task.attempt_count == 0
        assert tmp_db.get_active_leases_for_task(sample_task.task_id) == []

    def test_pre_launch_failure_does_not_fail_a_task_another_run_claimed(
        self, tmp_db, sample_task, tmp_path
    ):
        """Run A fails before claiming; meanwhile run B claimed the task (RUNNING).
        A's CLI must not move B's live task to FAILED."""
        def fake_run(self, task, workspace_base, timeout_seconds=120):
            # Another run (B) claims the task while A is failing pre-launch.
            tmp_db.update_task_status(task.task_id, TaskStatus.RUNNING)
            return _execution(task, "", ExecutionStatus.FAILED)

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", fake_run):
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)
        assert exc.value.code == 1
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RUNNING

    def test_own_claimed_pre_launch_failure_still_fails_the_task(
        self, tmp_db, sample_task, tmp_path
    ):
        def fake_run(self, task, workspace_base, timeout_seconds=120):
            tmp_db.update_task_status(task.task_id, TaskStatus.RUNNING)
            execution = _execution(task, "", ExecutionStatus.FAILED)
            execution.task_claimed = True
            execution.error = "Authentication not configured"
            return execution

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", fake_run):
            with pytest.raises(SystemExit):
                cmd_run_task(args)
        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.FAILED
        assert "Authentication" in task.error

    def test_compare_and_set_refuses_wrong_expected_status(self, tmp_db, sample_task):
        _advance(tmp_db, sample_task.task_id, TaskStatus.RUNNING)
        assert tmp_db.compare_and_set_status(
            sample_task.task_id, TaskStatus.READY, TaskStatus.FAILED) is False
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.RUNNING
        assert tmp_db.compare_and_set_status(
            sample_task.task_id, TaskStatus.RUNNING, TaskStatus.FAILED) is True
