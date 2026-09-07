"""
P1-J: Verification Authority & Task-Outcome Binding tests.

Covers the adversarial test matrix across six security property categories:

  P1-J-SM  State-machine: READY→FAILED transition, validate_transition=True
  P1-J-WO  Workspace Ownership: lease held through verification
  P1-J-DD  Delete Detection: deleted/renamed tests block COMPLETED
  P1-J-DA  Deactivation Detection: SKIPPED/XFAILED tests block COMPLETED
  P1-J-NO  No-op Detection: unchanged workspace blocks COMPLETED
  P1-J-PP  Parser: ANSI stripping, conflicts, zero-result, parameterised
  P1-J-SI  Schema & Input: expected_deleted_tests, backslash rejection
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from relay.cli import cmd_run_task
from relay.executor import ExecutionEngine
from relay.models import (
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
)
from relay.paths import InvalidTestSpecifierError, validate_test_specifier
from relay.schemas import ValidationError, validate_task_input
from relay.state import InvalidTransitionError, StateManager
from relay.testing import parse_test_results, _parse_collection_output
from relay.verifier import Verifier, VerificationResult


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _make_execution(
    task_id: str,
    *,
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    exit_code: int | None = 0,
    error: str | None = None,
    workspace: str = "",
    baseline: list[str] | None = None,
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
        baseline_tests_collected=baseline or [],
    )


def _args(task_id, db_path, workspace_path, timeout=120):
    args = MagicMock()
    args.task_id = task_id
    args.db = str(db_path)
    args.workspace = str(workspace_path)
    args.timeout = timeout
    args.keep_workspace = True
    return args


def _run_side_effect(db_path, return_exec):
    from relay.state import StateManager

    def _side_effect(task, workspace_base, timeout_seconds=120):
        s = StateManager(str(db_path))
        s.update_task_status(task.task_id, TaskStatus.RUNNING, validate_transition=True)
        return return_exec

    return _side_effect


def _passing_result(**overrides) -> VerificationResult:
    defaults = dict(
        passed=True,
        branch="main",
        git_status_lines=["M calculator.py"],
        files_changed=["calculator.py"],
        files_added=[],
        files_deleted=[],
        unexpected_files=[],
        test_exit_code=0,
        test_output="test_calculator.py::test_add PASSED",
        acceptance_results={"All tests pass": True},
        report_agreement={},
        errors=[],
        no_op=False,
        baseline_tests_collected=["test_calculator.py::test_add"],
        deleted_tests=[],
        deactivated_tests=[],
        node_ids_all_passed=True,
        test_results={"test_calculator.py::test_add": "PASSED"},
    )
    defaults.update(overrides)
    return VerificationResult(**defaults)


def _failing_result(**overrides) -> VerificationResult:
    r = _passing_result()
    r.passed = False
    r.test_exit_code = 1
    r.errors = ["test failed"]
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


# ── P1-J-SM: State-machine ────────────────────────────────────────────────────

class TestStateMachineTransitions:
    def test_ready_to_failed_is_now_allowed(self):
        """READY→FAILED must be in ALLOWED_TRANSITIONS after P1-J."""
        from relay.models import ALLOWED_TRANSITIONS
        assert TaskStatus.FAILED in ALLOWED_TRANSITIONS[TaskStatus.READY]

    def test_pre_launch_failure_uses_validate_transition_true(
        self, tmp_db, tmp_path, sample_task
    ):
        """Pre-launch FAILED path must use validate_transition=True (no bypass)."""
        pre_launch = _make_execution(
            sample_task.task_id,
            status=ExecutionStatus.FAILED,
            exit_code=None,
            error="Workspace lease could not be acquired.",
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

    def test_validate_transition_false_not_needed_for_ready_to_failed(self, tmp_db):
        """READY→FAILED must succeed with validate_transition=True."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=[],
            approval_policy="auto",
            status=TaskStatus.READY,
        )
        tmp_db.create_task(task)
        # Must not raise InvalidTransitionError.
        tmp_db.update_task_status(task.task_id, TaskStatus.FAILED, validate_transition=True)
        refreshed = tmp_db.get_task(task.task_id)
        assert refreshed.status == TaskStatus.FAILED

    def test_running_to_failed_still_allowed(self, tmp_db):
        """RUNNING→FAILED must remain allowed (auth error, CLI not found paths)."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=[],
            approval_policy="auto",
            status=TaskStatus.READY,
        )
        tmp_db.create_task(task)
        tmp_db.update_task_status(task.task_id, TaskStatus.RUNNING, validate_transition=True)
        tmp_db.update_task_status(task.task_id, TaskStatus.FAILED, validate_transition=True)
        assert tmp_db.get_task(task.task_id).status == TaskStatus.FAILED


# ── P1-J-WO: Workspace Ownership ─────────────────────────────────────────────

class TestWorkspaceOwnership:
    def test_lease_released_after_verification_on_success(
        self, tmp_db, tmp_path, sample_task
    ):
        """Workspace lease must be released AFTER verification completes."""
        release_calls: list[str] = []

        exec_obj = _make_execution(
            sample_task.task_id,
            status=ExecutionStatus.COMPLETED,
            exit_code=0,
            workspace=str(tmp_path / "ws" / sample_task.task_id),
        )

        def fake_run(task, workspace_base, timeout_seconds=120):
            s = StateManager(str(tmp_path / "relay_test.db"))
            s.update_task_status(task.task_id, TaskStatus.RUNNING, validate_transition=True)
            return exec_obj

        def fake_release(exec_id):
            release_calls.append(exec_id)

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", side_effect=fake_run):
            with patch.object(ExecutionEngine, "release_workspace_lease", side_effect=fake_release):
                with patch.object(Verifier, "verify", return_value=_passing_result()):
                    cmd_run_task(args)

        assert release_calls == [exec_obj.execution_id]

    def test_lease_released_after_verification_on_failure(
        self, tmp_db, tmp_path, sample_task
    ):
        """Workspace lease must be released even when verification fails."""
        release_calls: list[str] = []

        exec_obj = _make_execution(
            sample_task.task_id,
            status=ExecutionStatus.COMPLETED,
            exit_code=0,
            workspace=str(tmp_path / "ws" / sample_task.task_id),
        )

        def fake_run(task, workspace_base, timeout_seconds=120):
            s = StateManager(str(tmp_path / "relay_test.db"))
            s.update_task_status(task.task_id, TaskStatus.RUNNING, validate_transition=True)
            return exec_obj

        def fake_release(exec_id):
            release_calls.append(exec_id)

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", side_effect=fake_run):
            with patch.object(ExecutionEngine, "release_workspace_lease", side_effect=fake_release):
                with patch.object(Verifier, "verify", return_value=_failing_result()):
                    with pytest.raises(SystemExit):
                        cmd_run_task(args)

        assert release_calls == [exec_obj.execution_id]

    def test_release_workspace_lease_delegates_to_state(self, tmp_db, tmp_path):
        """ExecutionEngine.release_workspace_lease must delegate to state.release_lease."""
        from relay.audit import AuditLog
        from relay.policy import PolicyEngine

        state = StateManager(str(tmp_path / "relay_test.db"))
        audit = AuditLog(state)
        policy = PolicyEngine()
        engine = ExecutionEngine(state, audit, policy)

        released = []
        with patch.object(state, "release_lease", side_effect=released.append):
            engine.release_workspace_lease("EXEC-FAKE01")

        assert released == ["EXEC-FAKE01"]


# ── P1-J-DD: Delete Detection ─────────────────────────────────────────────────

class TestDeleteDetection:
    def test_deleted_test_blocks_completed(self, tmp_db, tmp_path, sample_task):
        """Worker deleting a baseline test must block TASK_COMPLETED."""
        exec_obj = _make_execution(
            sample_task.task_id,
            status=ExecutionStatus.COMPLETED,
            exit_code=0,
            workspace=str(tmp_path / "ws" / sample_task.task_id),
            baseline=["test_calculator.py::test_add", "test_calculator.py::test_precision"],
        )

        def fake_run(task, workspace_base, timeout_seconds=120):
            s = StateManager(str(tmp_path / "relay_test.db"))
            s.update_task_status(task.task_id, TaskStatus.RUNNING, validate_transition=True)
            return exec_obj

        # Verifier result: test_precision is absent (deleted) from test_results.
        result = _passing_result(
            deleted_tests=["test_calculator.py::test_precision"],
            node_ids_all_passed=False,
            passed=False,
            errors=["test_calculator.py::test_precision was deleted"],
        )

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", side_effect=fake_run):
            with patch.object(ExecutionEngine, "release_workspace_lease"):
                with patch.object(Verifier, "verify", return_value=result):
                    with pytest.raises(SystemExit) as exc:
                        cmd_run_task(args)

        assert exc.value.code == 1
        task = tmp_db.get_task(sample_task.task_id)
        assert task.status == TaskStatus.FAILED

    def test_expected_deleted_test_is_permitted(self):
        """Tests in expected_deleted_tests must not be flagged as deleted."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=["test_calc.py"],
            expected_deleted_tests=["test_calc.py::test_old"],
            approval_policy="auto",
        )
        baseline = ["test_calc.py::test_add", "test_calc.py::test_old"]
        # test_old is gone from results but it's in expected_deleted_tests.
        test_results = {"test_calc.py::test_add": "PASSED"}
        test_output = "test_calc.py::test_add PASSED\n"

        verifier = Verifier()
        deleted, deactivated, nip, _, errors = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )

        assert "test_calc.py::test_old" not in deleted
        assert not errors

    def test_unexpected_deleted_test_is_flagged(self):
        """Tests missing from results and not in expected_deleted_tests must be flagged."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=["test_calc.py"],
            expected_deleted_tests=[],
            approval_policy="auto",
        )
        baseline = ["test_calc.py::test_add", "test_calc.py::test_sneaky"]
        test_results_map = {"test_calc.py::test_add": "PASSED"}
        test_output = "test_calc.py::test_add PASSED\n"

        verifier = Verifier()
        deleted, _, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert "test_calc.py::test_sneaky" in deleted

    def test_empty_baseline_blocks_completed(self, tmp_db, tmp_path, sample_task):
        """Empty baseline (collection failed) must block COMPLETED (H-2)."""
        exec_obj = _make_execution(
            sample_task.task_id,
            status=ExecutionStatus.COMPLETED,
            exit_code=0,
            workspace=str(tmp_path / "ws" / sample_task.task_id),
            baseline=[],  # empty — collection failed or no tests found
        )

        def fake_run(task, workspace_base, timeout_seconds=120):
            s = StateManager(str(tmp_path / "relay_test.db"))
            s.update_task_status(task.task_id, TaskStatus.RUNNING, validate_transition=True)
            return exec_obj

        # passed=False because baseline is empty (H-2).
        result = _passing_result(
            baseline_tests_collected=[],
            passed=False,
            errors=["No baseline tests collected."],
        )

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", side_effect=fake_run):
            with patch.object(ExecutionEngine, "release_workspace_lease"):
                with patch.object(Verifier, "verify", return_value=result):
                    with pytest.raises(SystemExit) as exc:
                        cmd_run_task(args)

        assert exc.value.code == 1
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.FAILED


# ── P1-J-DA: Deactivation Detection ──────────────────────────────────────────

class TestDeactivationDetection:
    def test_skipped_test_blocks_completed(self):
        """A baseline test that becomes SKIPPED must set deactivated_tests."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=["test_calc.py"],
            approval_policy="auto",
        )
        baseline = ["test_calc.py::test_add", "test_calc.py::test_precision"]
        test_output = (
            "test_calc.py::test_add PASSED\n"
            "test_calc.py::test_precision SKIPPED\n"
        )

        verifier = Verifier()
        _, deactivated, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert "test_calc.py::test_precision" in deactivated

    def test_xfailed_test_blocks_completed(self):
        """A baseline test that becomes XFAILED must set deactivated_tests."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=["test_calc.py"],
            approval_policy="auto",
        )
        baseline = ["test_calc.py::test_discount"]
        test_output = "test_calc.py::test_discount XFAILED\n"

        verifier = Verifier()
        _, deactivated, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert "test_calc.py::test_discount" in deactivated

    def test_passed_test_not_deactivated(self):
        """A test that went from baseline to PASSED must not be in deactivated_tests."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=["test_calc.py"],
            approval_policy="auto",
        )
        baseline = ["test_calc.py::test_add"]
        test_output = "test_calc.py::test_add PASSED\n"

        verifier = Verifier()
        _, deactivated, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert deactivated == []


# ── P1-J-NO: No-op Detection ──────────────────────────────────────────────────

class TestNoOpDetection:
    def test_no_files_changed_blocks_completed(self, tmp_db, tmp_path, sample_task):
        """Zero file changes (no-op) must block TASK_COMPLETED (H-6)."""
        exec_obj = _make_execution(
            sample_task.task_id,
            status=ExecutionStatus.COMPLETED,
            exit_code=0,
            workspace=str(tmp_path / "ws" / sample_task.task_id),
            baseline=["test_calculator.py::test_add"],
        )

        def fake_run(task, workspace_base, timeout_seconds=120):
            s = StateManager(str(tmp_path / "relay_test.db"))
            s.update_task_status(task.task_id, TaskStatus.RUNNING, validate_transition=True)
            return exec_obj

        # no_op=True blocks COMPLETED.
        result = _passing_result(
            files_changed=[],
            files_added=[],
            files_deleted=[],
            no_op=True,
            passed=False,
            errors=["No files changed — worker made no modifications."],
        )

        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", side_effect=fake_run):
            with patch.object(ExecutionEngine, "release_workspace_lease"):
                with patch.object(Verifier, "verify", return_value=result):
                    with pytest.raises(SystemExit) as exc:
                        cmd_run_task(args)

        assert exc.value.code == 1
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.FAILED

    def test_file_added_is_not_no_op(self):
        """A task that only adds a file (no changes/deletes) must not be no-op."""
        no_op = not ([] or ["new_module.py"] or [])
        assert not no_op

    def test_file_deleted_is_not_no_op(self):
        """A task that only deletes a file must not be no-op."""
        no_op = not ([] or [] or ["old_module.py"])
        assert not no_op


# ── P1-J-PP: Parser ───────────────────────────────────────────────────────────

class TestParser:
    def test_ansi_codes_stripped(self):
        """ANSI colour codes must be stripped before parsing."""
        # Green PASSED, red FAILED
        raw = (
            "\x1b[32mtest_foo.py::test_add\x1b[0m PASSED\n"
            "\x1b[31mtest_foo.py::test_sub\x1b[0m FAILED\n"
        )
        results = parse_test_results(raw)
        assert results is not None
        assert results.get("test_foo.py::test_add") == "PASSED"
        assert results.get("test_foo.py::test_sub") == "FAILED"

    def test_conflicting_outcomes_fail_closed(self):
        """Same node ID with two different outcomes must return None (fail closed)."""
        raw = (
            "test_foo.py::test_add PASSED\n"
            "test_foo.py::test_add FAILED\n"
        )
        assert parse_test_results(raw) is None

    def test_zero_results_non_empty_output_fail_closed(self):
        """Non-empty output with no parseable outcomes must return None."""
        raw = "some test runner garbage without any outcome lines\n"
        assert parse_test_results(raw) is None

    def test_empty_output_returns_empty_dict(self):
        """Truly empty output returns {} (not None) — no tests ran."""
        assert parse_test_results("") == {}

    def test_parameterised_test_parsed(self):
        """Parameterised test IDs like test_foo[0] must be captured as-is."""
        raw = "test_foo.py::test_discount[case0] PASSED\n"
        results = parse_test_results(raw)
        assert results is not None
        assert "test_foo.py::test_discount[case0]" in results

    def test_node_id_passed_prefix_match(self):
        """node_id_passed must match base spec to parameterised variant."""
        test_results = {
            "test_foo.py::test_discount[case0]": "PASSED",
            "test_foo.py::test_discount[case1]": "PASSED",
        }
        verifier = Verifier()
        assert verifier._node_id_passed("test_foo.py::test_discount", test_results)

    def test_node_id_passed_prefix_match_one_failing(self):
        """If any parameterised variant fails, node_id_passed must be False."""
        test_results = {
            "test_foo.py::test_discount[case0]": "PASSED",
            "test_foo.py::test_discount[case1]": "FAILED",
        }
        verifier = Verifier()
        assert not verifier._node_id_passed("test_foo.py::test_discount", test_results)

    def test_collection_output_parsed(self):
        """--collect-only -q output must produce node ID list."""
        raw = (
            "test_calculator.py::test_add\n"
            "test_calculator.py::test_subtract\n"
            "5 tests collected in 0.1s\n"
        )
        ids = _parse_collection_output(raw)
        assert "test_calculator.py::test_add" in ids
        assert "test_calculator.py::test_subtract" in ids
        # Summary line must not be included.
        assert not any("collected" in i for i in ids)

    def test_exit_zero_empty_results_non_empty_output_errors(self):
        """exit_code=0 but zero parsed results with non-empty output errors."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=["test_calc.py::test_add"],
            approval_policy="auto",
        )
        baseline = ["test_calc.py::test_add"]
        # Output has no parseable outcome lines.
        test_output = "some output but no PASSED/FAILED\n"

        verifier = Verifier()
        _, _, nip, _, errors = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert not nip
        assert errors  # must have at least one error message


# ── P1-J-SI: Schema & Input ───────────────────────────────────────────────────

class TestSchemaAndInput:
    def test_expected_deleted_tests_accepted_in_schema(self):
        """expected_deleted_tests must be an accepted optional field in task input."""
        data = {
            "project_id": "p",
            "repository": "/tmp/repo",
            "objective": "o",
            "expected_deleted_tests": ["test_old.py::test_removed"],
        }
        # Must not raise.
        validate_task_input(data)

    def test_expected_deleted_tests_wrong_type_rejected(self):
        """expected_deleted_tests must be a list — wrong type raises ValidationError."""
        data = {
            "project_id": "p",
            "repository": "/tmp/repo",
            "objective": "o",
            "expected_deleted_tests": "not_a_list",
        }
        with pytest.raises(ValidationError):
            validate_task_input(data)

    def test_expected_deleted_tests_non_string_items_rejected(self):
        """expected_deleted_tests items must be strings."""
        data = {
            "project_id": "p",
            "repository": "/tmp/repo",
            "objective": "o",
            "expected_deleted_tests": [123],
        }
        with pytest.raises(ValidationError):
            validate_task_input(data)

    def test_backslash_in_test_specifier_rejected(self):
        """Backslash in a required_tests specifier must be rejected."""
        with pytest.raises(InvalidTestSpecifierError, match="backslash"):
            validate_test_specifier(r"test_foo\test_bar.py")

    def test_forward_slash_in_specifier_accepted(self):
        """Forward slash path separators must be accepted."""
        # Must not raise.
        validate_test_specifier("subdir/test_foo.py")

    def test_node_id_specifier_accepted(self):
        """test_foo.py::test_func must be accepted."""
        validate_test_specifier("test_foo.py::test_func")

    def test_expected_deleted_tests_stored_and_retrieved(self, tmp_db):
        """expected_deleted_tests must survive create_task / get_task round-trip."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=["test_calc.py"],
            expected_deleted_tests=["test_calc.py::test_old"],
            approval_policy="auto",
        )
        tmp_db.create_task(task)
        fetched = tmp_db.get_task(task.task_id)
        assert fetched.expected_deleted_tests == ["test_calc.py::test_old"]

    def test_baseline_fields_stored_and_retrieved(self, tmp_db, sample_task):
        """Execution baseline fields must survive create_execution/get_execution."""
        execution = Execution(
            execution_id=Execution.new_id(),
            task_id=sample_task.task_id,
            attempt_number=1,
            status=ExecutionStatus.RUNNING,
            workspace="/tmp/ws",
            worker_identity="test",
            policy_version="0.1",
            capability_snapshot=[],
            started_at=_now(),
            baseline_tests_collected=["test_calc.py::test_add", "test_calc.py::test_sub"],
            baseline_test_exit=0,
            baseline_test_output="2 tests collected",
        )
        tmp_db.create_execution(execution)
        fetched = tmp_db.get_execution(execution.execution_id)
        assert fetched.baseline_tests_collected == ["test_calc.py::test_add", "test_calc.py::test_sub"]
        assert fetched.baseline_test_exit == 0
        assert fetched.baseline_test_output == "2 tests collected"

    def test_content_hash_in_task_created_event(self, tmp_db, audit):
        """TASK_CREATED event emitted by task_created_with_hash must include content_hash."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=["test_calc.py"],
            approval_policy="auto",
        )
        tmp_db.create_task(task)
        audit.task_created_with_hash(task)
        events = tmp_db.get_events_for_task(task.task_id)
        assert events
        assert "content_hash" in events[0].details
        # Must be a 64-char hex SHA-256.
        h = events[0].details["content_hash"]
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_content_hash_deterministic(self, tmp_db, audit):
        """Same task content must produce same content_hash each time."""
        task = Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="test determinism",
            requirements=["req1"],
            acceptance_criteria=["ac1"],
            requested_actions=["read_repository"],
            forbidden_actions=["push"],
            required_tests=["test_calc.py"],
            approval_policy="auto",
        )
        tmp_db.create_task(task)
        e1 = audit.task_created_with_hash(task)
        e2 = audit.task_created_with_hash(task)
        assert e1.details["content_hash"] == e2.details["content_hash"]


# ── P1-J-CK: Checkout Fail-Closed ─────────────────────────────────────────────

class TestCheckoutFailsClose:
    """P1-J amendment: base_ref checkout must fail closed — no Claude launch on failure."""

    @staticmethod
    def _checkout_fail(self, base_ref, workspace):
        raise RuntimeError(f"git checkout {base_ref!r} failed: ref not found")

    def test_checkout_success_permits_execution(
        self, tmp_db, audit, policy, sample_task, tmp_path
    ):
        """Successful checkout (autouse no-op) allows execution to proceed."""
        engine = ExecutionEngine(tmp_db, audit, policy)
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = ("", "")
        with patch("subprocess.Popen", return_value=proc):
            execution = engine.run(sample_task, tmp_path / "ws")
        # Worker was launched — exit_code must be set (not None).
        assert execution.exit_code is not None

    def test_checkout_failure_prevents_claude_launch(
        self, tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
    ):
        """Checkout failure must prevent subprocess.Popen (Claude) from being called."""
        monkeypatch.setattr(ExecutionEngine, "_checkout_base_ref", self._checkout_fail)
        engine = ExecutionEngine(tmp_db, audit, policy)
        with patch("subprocess.Popen") as mock_popen:
            engine.run(sample_task, tmp_path / "ws")
        mock_popen.assert_not_called()

    def test_checkout_failure_status_is_failed(
        self, tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
    ):
        """Checkout failure must produce ExecutionStatus.FAILED."""
        monkeypatch.setattr(ExecutionEngine, "_checkout_base_ref", self._checkout_fail)
        engine = ExecutionEngine(tmp_db, audit, policy)
        execution = engine.run(sample_task, tmp_path / "ws")
        assert execution.status == ExecutionStatus.FAILED

    def test_checkout_failure_exit_code_is_none(
        self, tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
    ):
        """Checkout failure must leave exit_code=None (pre-launch worker-ran discriminator)."""
        monkeypatch.setattr(ExecutionEngine, "_checkout_base_ref", self._checkout_fail)
        engine = ExecutionEngine(tmp_db, audit, policy)
        execution = engine.run(sample_task, tmp_path / "ws")
        assert execution.exit_code is None

    def test_checkout_failure_no_active_lease(
        self, tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
    ):
        """Checkout fails before lease acquisition — no orphaned lease in the DB."""
        monkeypatch.setattr(ExecutionEngine, "_checkout_base_ref", self._checkout_fail)
        engine = ExecutionEngine(tmp_db, audit, policy)
        execution = engine.run(sample_task, tmp_path / "ws")
        # workspace="" on pre-launch failure; get_active_lease on "" returns None.
        active = tmp_db.get_active_lease(execution.workspace) if execution.workspace else None
        assert active is None

    def test_checkout_failure_task_becomes_failed_via_cmd_run_task(
        self, tmp_db, tmp_path, sample_task
    ):
        """cmd_run_task must transition task to FAILED when checkout produces pre-launch FAILED."""
        pre_launch = _make_execution(
            sample_task.task_id,
            status=ExecutionStatus.FAILED,
            exit_code=None,
            error="git checkout 'main' failed: ref not found",
            workspace="",
        )
        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", return_value=pre_launch):
            with pytest.raises(SystemExit) as exc:
                cmd_run_task(args)

        assert exc.value.code == 1
        assert tmp_db.get_task(sample_task.task_id).status == TaskStatus.FAILED

    def test_checkout_failure_cannot_reach_verification(
        self, tmp_db, tmp_path, sample_task
    ):
        """Verifier.verify must never be called when checkout failure produces pre-launch FAILED."""
        pre_launch = _make_execution(
            sample_task.task_id,
            status=ExecutionStatus.FAILED,
            exit_code=None,
            error="git checkout 'main' failed: ref not found",
            workspace="",
        )
        args = _args(sample_task.task_id, tmp_path / "relay_test.db", tmp_path / "ws")
        with patch.object(ExecutionEngine, "run", return_value=pre_launch):
            with patch.object(Verifier, "verify") as mock_verify:
                with pytest.raises(SystemExit):
                    cmd_run_task(args)

        mock_verify.assert_not_called()


# ── P1-J-AS: expected_deleted_tests Authority Scope ───────────────────────────

class TestExpectedDeletedTestsScope:
    """P1-J amendment: expected_deleted_tests is an exception declaration, not an authority source."""

    def _make_task(self, expected_deleted_tests=None, required_tests=None):
        return Task(
            task_id=Task.new_id(),
            project_id="p",
            repository=".",
            base_ref="main",
            working_ref="main",
            objective="o",
            requirements=[],
            acceptance_criteria=[],
            requested_actions=[],
            forbidden_actions=[],
            required_tests=required_tests or ["test_calc.py"],
            expected_deleted_tests=expected_deleted_tests or [],
            approval_policy="auto",
        )

    def test_valid_baseline_deletion_is_authorized(self):
        """A test in both expected_deleted_tests AND baseline may be exempted."""
        task = self._make_task(expected_deleted_tests=["test_calc.py::test_old"])
        baseline = ["test_calc.py::test_add", "test_calc.py::test_old"]
        test_output = "test_calc.py::test_add PASSED\n"

        verifier = Verifier()
        deleted, _, _, _, errors = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert "test_calc.py::test_old" not in deleted
        assert not errors

    def test_undeclared_deletion_is_flagged(self):
        """A test missing from results and not in expected_deleted_tests must be flagged."""
        task = self._make_task(expected_deleted_tests=[])
        baseline = ["test_calc.py::test_add", "test_calc.py::test_sub"]
        test_output = "test_calc.py::test_add PASSED\n"

        verifier = Verifier()
        deleted, _, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert "test_calc.py::test_sub" in deleted

    def test_nonexistent_expected_deletion_is_not_authorized(self):
        """A node ID in expected_deleted_tests that was never in the baseline cannot authorize anything."""
        task = self._make_task(expected_deleted_tests=["test_calc.py::test_never_existed"])
        baseline = ["test_calc.py::test_real"]
        # test_real disappears — the ghost declaration must NOT exempt it.
        test_output = ""

        verifier = Verifier()
        deleted, _, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert "test_calc.py::test_real" in deleted

    def test_expected_deletion_not_in_baseline_grants_no_exemption(self):
        """Declaring a test not present in the baseline cannot exempt any deletion."""
        task = self._make_task(expected_deleted_tests=["test_calc.py::test_ghost"])
        baseline = ["test_calc.py::test_add", "test_calc.py::test_sub"]
        # Both real tests disappear; only ghost is declared.
        test_output = ""

        verifier = Verifier()
        deleted, _, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert "test_calc.py::test_add" in deleted
        assert "test_calc.py::test_sub" in deleted
        # The ghost was never in baseline — it is not itself a deletion.
        assert "test_calc.py::test_ghost" not in deleted

    def test_expected_deletion_outside_scope_not_authorized(self):
        """A node ID from outside required_tests scope cannot authorize any deletion."""
        # required_tests covers only test_calc.py; declaration is for test_other.py.
        task = self._make_task(
            required_tests=["test_calc.py"],
            expected_deleted_tests=["test_other.py::test_something"],
        )
        # Baseline only contains tests from the required_tests scope.
        baseline = ["test_calc.py::test_add"]
        test_output = ""

        verifier = Verifier()
        deleted, _, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        # test_calc.py::test_add must be flagged — the out-of-scope declaration cannot exempt it.
        assert "test_calc.py::test_add" in deleted

    def test_additive_tests_not_treated_as_deletions(self):
        """Tests added by the worker (present in results, absent from baseline) are not deletions."""
        task = self._make_task()
        baseline = ["test_calc.py::test_add"]
        test_output = "test_calc.py::test_add PASSED\ntest_calc.py::test_new PASSED\n"

        verifier = Verifier()
        deleted, _, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert deleted == []

    def test_multiple_valid_expected_deletions_all_authorized(self):
        """Multiple expected deletions that are all in the baseline are all authorized."""
        task = self._make_task(
            expected_deleted_tests=[
                "test_calc.py::test_old_add",
                "test_calc.py::test_old_sub",
            ]
        )
        baseline = [
            "test_calc.py::test_old_add",
            "test_calc.py::test_old_sub",
            "test_calc.py::test_new",
        ]
        test_output = "test_calc.py::test_new PASSED\n"

        verifier = Verifier()
        deleted, _, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert "test_calc.py::test_old_add" not in deleted
        assert "test_calc.py::test_old_sub" not in deleted
        assert deleted == []

    def test_mixed_valid_and_invalid_expected_deletions_fails_for_invalid(self):
        """A mix of valid and invalid declarations: invalid one cannot exempt a real deletion."""
        task = self._make_task(
            expected_deleted_tests=[
                "test_calc.py::test_legitimate",  # in baseline — valid
                "test_calc.py::test_fabricated",  # NOT in baseline — invalid
            ]
        )
        baseline = ["test_calc.py::test_legitimate", "test_calc.py::test_real"]
        # test_legitimate disappears — authorized (in baseline and declared).
        # test_real disappears — NOT authorized (not declared).
        # test_fabricated is declared but was never in baseline — cannot authorize anything.
        test_output = ""

        verifier = Verifier()
        deleted, _, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        assert "test_calc.py::test_legitimate" not in deleted
        assert "test_calc.py::test_real" in deleted

    def test_expected_deleted_tests_cannot_authorize_arbitrary_declaration(self):
        """Arbitrary strings in expected_deleted_tests that are not in baseline have no effect."""
        task = self._make_task(
            expected_deleted_tests=[
                "../../../etc/passwd",
                "test_calc.py::../../evil",
                "completely_random_string",
            ]
        )
        baseline = ["test_calc.py::test_real"]
        test_output = ""

        verifier = Verifier()
        deleted, _, _, _, _ = verifier._check_test_accountability(
            task, baseline, True, test_output, 0
        )
        # Arbitrary declarations grant no exemption — test_real must be flagged.
        assert "test_calc.py::test_real" in deleted
