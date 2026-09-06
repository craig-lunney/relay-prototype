"""
P1-D required_tests hardening tests.

Tests D1–D9 verify that required_tests entries are treated as data,
not as shell commands or arbitrary pytest expressions. The subprocess
invocation uses shell=False and the '--' separator; these tests also
verify the validation layer that provides defence-in-depth.

D1  Legitimate test path is accepted and executes.
D2  Legitimate pytest node ID (file::test_name) passes validation.
D3  Shell '&' injection is rejected.
D4  Shell ';' injection is rejected.
D5  Shell '|' injection is rejected.
D6  Option injection with '-' prefix is rejected.
D7  Path traversal '../' is rejected against workspace boundary.
D8  Absolute path outside workspace is rejected.
D9  Subprocess command uses shell=False and '--' separator.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from relay.models import Execution, ExecutionStatus, Task, TaskStatus
from relay.paths import (
    InvalidTestSpecifierError,
    validate_required_tests,
    validate_test_specifier,
)
from relay.policy import PolicyEngine
from relay.verifier import Verifier


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_task(repo: str, required_tests: list[str]) -> Task:
    return Task(
        task_id=Task.new_id(),
        project_id="test",
        repository=repo,
        base_ref="main",
        working_ref="main",
        objective="test",
        requirements=[],
        acceptance_criteria=[],
        requested_actions=["read_repository", "modify_working_tree", "run_tests"],
        forbidden_actions=["push"],
        required_tests=required_tests,
        approval_policy="auto",
        status=TaskStatus.READY,
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


# ── D1: Legitimate test path ──────────────────────────────────────────────────

def test_d1_legitimate_test_path_accepted(test_repo):
    """A valid required_tests file path passes validation and executes."""
    validate_test_specifier("test_calculator.py")
    validate_test_specifier("test_calculator.py", workspace=str(test_repo))

    verifier = Verifier()
    task = _make_task(str(test_repo), required_tests=["test_calculator.py"])
    execution = _make_execution(task.task_id, str(test_repo))
    result = verifier.verify(task, execution)

    # test_exit_code is set (tests ran), not skipped
    assert result.test_exit_code is not None
    assert "test_calculator" in result.test_output


# ── D2: Legitimate pytest node ID ────────────────────────────────────────────

def test_d2_node_id_form_accepted(test_repo):
    """file.py::test_name form passes validation (supported pytest node ID)."""
    validate_test_specifier("test_calculator.py::test_add_integers")
    validate_test_specifier(
        "test_calculator.py::test_add_integers", workspace=str(test_repo)
    )


def test_d2_nested_node_id_accepted():
    """Class::method node ID also passes validation."""
    validate_test_specifier("tests/test_state.py::TestClass::test_method")


# ── D3: Shell '&' injection ───────────────────────────────────────────────────

def test_d3_ampersand_injection_rejected():
    """'&' in a test specifier is rejected (shell AND operator)."""
    with pytest.raises(InvalidTestSpecifierError, match="metachar"):
        validate_test_specifier("tests/test_x.py & whoami")


def test_d3_ampersand_in_node_id_rejected():
    """'&' embedded anywhere in the specifier is caught."""
    with pytest.raises(InvalidTestSpecifierError, match="metachar"):
        validate_test_specifier("test.py::test_name & evil")


def test_d3_policy_rejects_ampersand_in_required_tests(test_repo):
    """PolicyEngine.validate_task() rejects a task with '&' in required_tests."""
    policy = PolicyEngine()
    task = _make_task(str(test_repo), required_tests=["tests/test_x.py & whoami"])
    result = policy.validate_task(task)
    assert not result.allowed
    assert "required_tests" in result.reason.lower() or "specifier" in result.reason.lower()


def test_d3_verifier_rejects_ampersand(test_repo):
    """Verifier._run_tests() rejects '&' in a specifier and returns no exit code."""
    verifier = Verifier()
    task = _make_task(str(test_repo), required_tests=["tests/test_x.py & whoami"])
    execution = _make_execution(task.task_id, str(test_repo))

    with patch("subprocess.run") as mock_run:
        result = verifier.verify(task, execution)

    # subprocess.run must NOT be called for the test phase — specifier was rejected
    # (git calls may occur, but the pytest call must not)
    pytest_calls = [
        c for c in mock_run.call_args_list
        if c.args and "pytest" in str(c.args[0])
    ]
    assert not pytest_calls, "subprocess.run should not be called for pytest on a rejected specifier"
    assert result.test_exit_code is None
    assert any("rejected" in e.lower() or "specifier" in e.lower() for e in result.errors)


# ── D4: Shell ';' injection ───────────────────────────────────────────────────

def test_d4_semicolon_injection_rejected():
    """';' in a test specifier is rejected (shell command separator)."""
    with pytest.raises(InvalidTestSpecifierError, match="metachar"):
        validate_test_specifier("tests/test_x.py; whoami")


# ── D5: Shell '|' injection ───────────────────────────────────────────────────

def test_d5_pipe_injection_rejected():
    """'|' in a test specifier is rejected (shell pipe operator)."""
    with pytest.raises(InvalidTestSpecifierError, match="metachar"):
        validate_test_specifier("tests/test_x.py | whoami")


# ── D6: Option injection with '-' ─────────────────────────────────────────────

def test_d6_dash_prefix_rejected():
    """A specifier starting with '-' is rejected (would be a pytest option)."""
    with pytest.raises(InvalidTestSpecifierError, match="option"):
        validate_test_specifier("-x")


def test_d6_double_dash_option_rejected():
    """A specifier starting with '--' is rejected."""
    with pytest.raises(InvalidTestSpecifierError, match="option"):
        validate_test_specifier("--collect-only")


def test_d6_policy_rejects_option_injection(test_repo):
    """Policy layer catches option-injection attempt in required_tests."""
    policy = PolicyEngine()
    task = _make_task(str(test_repo), required_tests=["-x", "test_calculator.py"])
    result = policy.validate_task(task)
    assert not result.allowed
    assert "required_tests" in result.reason.lower() or "option" in result.reason.lower()


# ── D7: Path traversal ────────────────────────────────────────────────────────

def test_d7_dotdot_traversal_rejected_with_workspace(tmp_path):
    """'../' traversal escaping workspace is rejected when workspace is given."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(InvalidTestSpecifierError, match="outside"):
        validate_test_specifier("../evil_test.py", workspace=str(workspace))


def test_d7_deep_traversal_rejected_with_workspace(tmp_path):
    """Multi-level traversal escaping workspace is rejected."""
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)

    with pytest.raises(InvalidTestSpecifierError, match="outside"):
        validate_test_specifier("tests/../../evil_test.py", workspace=str(workspace))


def test_d7_traversal_no_workspace_passthrough():
    """Without workspace context (policy time), traversal is syntactically valid — boundary
    check is deferred to verifier which has the workspace."""
    # This should NOT raise at policy time — path boundary check needs a workspace
    result = validate_test_specifier("../some_test.py")
    assert result == "../some_test.py"


# ── D8: Absolute path outside workspace ──────────────────────────────────────

def test_d8_absolute_path_outside_workspace_rejected(tmp_path):
    """An absolute path outside the workspace is rejected when workspace is given."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outside = tmp_path / "outside" / "test_evil.py"

    with pytest.raises(InvalidTestSpecifierError, match="outside"):
        validate_test_specifier(str(outside), workspace=str(workspace))


def test_d8_system_path_rejected(tmp_path):
    """A system path (e.g. Windows system dir or /etc) is rejected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    import sys
    # Use a path that is guaranteed to be outside any tmp_path workspace
    system_candidate = Path(sys.executable).parent

    with pytest.raises(InvalidTestSpecifierError, match="outside"):
        validate_test_specifier(str(system_candidate), workspace=str(workspace))


# ── D9: Subprocess uses shell=False and '--' separator ───────────────────────

def test_d9_subprocess_uses_list_not_string(test_repo):
    """
    The actual subprocess.run call for test execution passes a list of arguments,
    never a shell string. shell=False is the default and is not overridden.
    This guarantees that shell metacharacters cannot be interpreted.
    """
    verifier = Verifier()
    task = _make_task(str(test_repo), required_tests=["test_calculator.py"])
    execution = _make_execution(task.task_id, str(test_repo))

    captured_calls: list = []

    original_run = subprocess.run

    def capturing_run(cmd, **kwargs):
        captured_calls.append((cmd, kwargs))
        # Only intercept pytest calls — let git calls through
        if isinstance(cmd, list) and "pytest" in str(cmd):
            result = MagicMock()
            result.returncode = 0
            result.stdout = "1 passed"
            result.stderr = ""
            return result
        return original_run(cmd, **kwargs)

    with patch("subprocess.run", side_effect=capturing_run):
        verifier.verify(task, execution)

    pytest_calls = [
        (cmd, kwargs) for cmd, kwargs in captured_calls
        if isinstance(cmd, list) and "pytest" in str(cmd)
    ]
    assert pytest_calls, "Expected at least one pytest subprocess.run call"

    for cmd, kwargs in pytest_calls:
        # Must be a list — never a string
        assert isinstance(cmd, list), f"Command must be a list, got {type(cmd)}: {cmd!r}"
        # Must not use shell=True
        assert not kwargs.get("shell", False), "shell=True must never be used"
        # Must contain '--' separator before test specifiers
        assert "--" in cmd, f"'--' separator must be present in command: {cmd}"
        # Test specifiers must appear after '--'
        sep_idx = cmd.index("--")
        assert "test_calculator.py" in cmd[sep_idx:], (
            f"Test specifier must appear after '--': {cmd}"
        )
        # No pytest options may appear after '--'
        post_sep = cmd[sep_idx + 1:]
        for arg in post_sep:
            assert not arg.startswith("-"), (
                f"No option flags should appear after '--': {arg!r} in {cmd}"
            )


def test_d9_double_dash_prevents_option_from_executing(test_repo):
    """
    Verify that a '-x' specifier (which bypassed the -prefix check, e.g. via
    a hypothetical future code path) does NOT cause pytest to exit-first —
    instead it fails collection, not option-parsing.

    This tests the '--' separator as a second independent layer of defence.
    We directly invoke the subprocess command structure that the verifier would use.
    """
    # Build the command as the verifier would, inserting '-x' after '--'
    # This simulates a bypass of the validation layer.
    cmd = ["python", "-m", "pytest", "-v", "--tb=short", "--", "-x"]

    result = subprocess.run(
        cmd,
        cwd=str(test_repo),
        capture_output=True,
        text=True,
        timeout=30,
    )

    # pytest should exit with code 4 (no tests collected / file not found),
    # NOT exit with code 0 (tests passed) or run with the -x behavior.
    # The important thing is that "1 passed" or exit 0 does NOT happen.
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        "'-x' after '--' should cause a collection error, not pass as an option"
    )
    # pytest reports it as a file-not-found, not option help or option behavior
    assert "-x" in combined or "not found" in combined.lower() or "no tests" in combined.lower()


# ── Control characters ────────────────────────────────────────────────────────

def test_control_chars_rejected():
    """Null bytes and newlines in specifiers are rejected."""
    for bad in ["test.py\x00", "test.py\n", "test.py\r"]:
        with pytest.raises(InvalidTestSpecifierError, match="metachar"):
            validate_test_specifier(bad)


def test_empty_specifier_rejected():
    """An empty required_tests entry is rejected."""
    with pytest.raises(InvalidTestSpecifierError, match="empty"):
        validate_test_specifier("")


def test_validate_required_tests_all_valid():
    """validate_required_tests passes cleanly when all entries are valid."""
    result = validate_required_tests(
        ["test_a.py", "test_b.py::test_function", "tests/test_c.py::Class::method"]
    )
    assert result == ["test_a.py", "test_b.py::test_function", "tests/test_c.py::Class::method"]


def test_validate_required_tests_empty_list():
    """An empty required_tests list is valid (no tests to run)."""
    result = validate_required_tests([])
    assert result == []
