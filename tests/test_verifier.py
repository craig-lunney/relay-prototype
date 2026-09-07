"""Tests for the independent verification layer."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from relay.models import Execution, ExecutionStatus, ImplementationReport, Task, TaskStatus
from relay.verifier import Verifier


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=T", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _make_execution(task_id: str, workspace: str) -> Execution:
    from datetime import datetime, timezone
    return Execution(
        execution_id=Execution.new_id(),
        task_id=task_id,
        attempt_number=1,
        status=ExecutionStatus.COMPLETED,
        workspace=workspace,
        worker_identity="test",
        policy_version="0.1",
        capability_snapshot=[],
        started_at=datetime.now(timezone.utc),
    )


def _make_task(repo_path: str) -> Task:
    return Task(
        task_id=Task.new_id(),
        project_id="test",
        repository=repo_path,
        base_ref="main",
        working_ref="main",
        objective="test",
        requirements=[],
        acceptance_criteria=["All tests pass"],
        requested_actions=["read_repository", "modify_working_tree", "run_tests"],
        forbidden_actions=["push"],
        required_tests=["test_calculator.py"],
        approval_policy="auto",
        status=TaskStatus.READY,
    )


# ── Branch detection ──────────────────────────────────────────────────────────

def test_verifier_detects_branch(test_repo):
    verifier = Verifier()
    task = _make_task(str(test_repo))
    execution = _make_execution(task.task_id, str(test_repo))
    result = verifier.verify(task, execution)
    # Branch should be detected (may be 'master' or 'main' depending on git config)
    assert result.branch != "UNKNOWN"


# ── Git status inspection ─────────────────────────────────────────────────────

def test_verifier_detects_modified_file(test_repo):
    # Apply the fix to calculator.py
    calc = test_repo / "calculator.py"
    content = calc.read_text(encoding="utf-8")
    fixed = content.replace(
        "discount_amount = price * discount_percent // 100",
        "discount_amount = price * discount_percent / 100",
    )
    calc.write_text(fixed, encoding="utf-8")

    verifier = Verifier()
    task = _make_task(str(test_repo))
    execution = _make_execution(task.task_id, str(test_repo))
    result = verifier.verify(task, execution)

    assert "calculator.py" in result.files_changed or len(result.git_status_lines) > 0


def test_verifier_detects_new_file(test_repo):
    (test_repo / "new_module.py").write_text("# new\n")

    verifier = Verifier()
    task = _make_task(str(test_repo))
    execution = _make_execution(task.task_id, str(test_repo))
    result = verifier.verify(task, execution)

    added_or_changed = result.files_added + result.files_changed + result.git_status_lines
    assert any("new_module" in f for f in added_or_changed)


# ── Test execution ────────────────────────────────────────────────────────────

def test_verifier_runs_tests_and_captures_exit_code(test_repo):
    """The verifier runs tests independently, not via Claude's report."""
    # Inject a controlled failure so the verifier has a non-zero exit code to detect.
    calc = test_repo / "calculator.py"
    calc.write_text(calc.read_text(encoding="utf-8").replace("return a + b", "return a + b + 999"), encoding="utf-8")

    verifier = Verifier()
    task = _make_task(str(test_repo))
    execution = _make_execution(task.task_id, str(test_repo))
    result = verifier.verify(task, execution)

    assert result.test_exit_code is not None
    assert result.test_exit_code != 0  # broken add() causes test_add to fail
    assert result.test_output != ""


def test_verifier_passes_after_fix(test_repo):
    """After applying the bug fix, the verifier should report all tests passing."""
    calc = test_repo / "calculator.py"
    content = calc.read_text(encoding="utf-8")
    fixed = content.replace(
        "discount_amount = price * discount_percent // 100",
        "discount_amount = price * discount_percent / 100",
    )
    calc.write_text(fixed, encoding="utf-8")

    verifier = Verifier()
    task = _make_task(str(test_repo))
    execution = _make_execution(task.task_id, str(test_repo))
    result = verifier.verify(task, execution)

    assert result.test_exit_code == 0
    assert result.passed


def test_verifier_no_required_tests_skips_test_run(test_repo):
    verifier = Verifier()
    task = _make_task(str(test_repo))
    task.required_tests = []
    execution = _make_execution(task.task_id, str(test_repo))
    result = verifier.verify(task, execution)
    assert result.test_exit_code is None
    assert result.test_output == ""


# ── Report vs reality comparison ──────────────────────────────────────────────

def test_verifier_flags_false_completed_claim(test_repo):
    """
    Claude says COMPLETED but tests fail → report_agreement shows disagreement.
    Relay never accepts COMPLETED based solely on Claude's report.
    """
    # Inject a controlled failure so the verifier has failing tests to catch.
    calc = test_repo / "calculator.py"
    calc.write_text(calc.read_text(encoding="utf-8").replace("return a + b", "return a + b + 999"), encoding="utf-8")

    false_report = ImplementationReport.from_dict({
        "protocol_version": "0.1",
        "message_type": "IMPLEMENTATION_REPORT",
        "task_id": "TASK-FAKE0001",
        "execution_id": "EXEC-FAKE0001",
        "status": "COMPLETED",  # lying — bug is not fixed
        "summary": "All done",
        "files_changed": [],
        "files_added": [],
        "files_deleted": [],
        "tests_run": ["test_calculator.py"],
        "risks": [],
        "uncertainties": [],
        "blockers": [],
        "recommended_next_action": "none",
    })

    verifier = Verifier()
    task = _make_task(str(test_repo))
    execution = _make_execution(task.task_id, str(test_repo))
    execution.report = false_report

    result = verifier.verify(task, execution)

    # Tests should still fail (bug not fixed) regardless of what report says
    assert result.test_exit_code != 0
    assert not result.passed
    # The disagreement should be captured
    assert result.report_agreement.get("completed_claim_verified") is False


def test_verifier_report_agreement_when_no_report(test_repo):
    verifier = Verifier()
    task = _make_task(str(test_repo))
    execution = _make_execution(task.task_id, str(test_repo))
    execution.report = None

    result = verifier.verify(task, execution)
    assert result.report_agreement.get("report_present") is False


# ── Unexpected file detection ─────────────────────────────────────────────────

def test_verifier_flags_unexpected_files(test_repo):
    (test_repo / "unexpected_file.py").write_text("# should not be here\n")

    verifier = Verifier()
    task = _make_task(str(test_repo))
    task.acceptance_criteria = []
    task.required_tests = []
    execution = _make_execution(task.task_id, str(test_repo))

    result = verifier.verify(task, execution, expected_new_files=["expected_file.py"])
    assert "unexpected_file.py" in result.unexpected_files


def test_verifier_allows_expected_new_files(test_repo):
    (test_repo / "expected_file.py").write_text("# expected\n")

    verifier = Verifier()
    task = _make_task(str(test_repo))
    task.required_tests = []
    execution = _make_execution(task.task_id, str(test_repo))

    result = verifier.verify(task, execution, expected_new_files=["expected_file.py"])
    assert "expected_file.py" not in result.unexpected_files
