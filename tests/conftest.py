"""Shared fixtures for Relay test suite."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from relay.audit import AuditLog
from relay.executor import ExecutionEngine
from relay.models import Task, TaskStatus
from relay.policy import PolicyEngine
from relay.state import StateManager

# Absolute path to the disposable test repository.
TEST_REPO_SRC = Path(__file__).parent.parent / "test-repository"

# Fake key used across all tests — never a real credential.
_FAKE_API_KEY = "sk-ant-test-fake-key-do-not-use-in-production"


@pytest.fixture(autouse=True)
def _no_op_checkout_and_baseline(monkeypatch):
    """Suppress P1-J best-effort pre-flight operations in all unit tests.

    _checkout_base_ref and _capture_baseline both call subprocess.Popen.
    Tests that mock subprocess.Popen to inspect Claude's invocation command
    would otherwise see git/pytest calls at call_args_list[0] and [1],
    shifting the index of the Claude call.  These operations are best-effort
    and are covered by dedicated P1-J tests; suppress them globally so all
    other tests see the Claude Popen call at index 0 as before P1-J.
    """
    monkeypatch.setattr(
        ExecutionEngine, "_checkout_base_ref",
        lambda self, base_ref, workspace: None,
    )
    monkeypatch.setattr(
        ExecutionEngine, "_capture_baseline",
        lambda self, task, execution, workspace: None,
    )


@pytest.fixture(autouse=True)
def _relay_api_key_file(tmp_path, monkeypatch):
    """Provide RELAY_API_KEY_FILE for every test that exercises executor code.

    Tests that specifically test the missing-env-var failure path should call
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False) after this fixture
    runs (fixtures run before the test body, so delenv in the test body removes
    what this fixture set).
    """
    key_file = tmp_path / "fake_relay_api_key.txt"
    key_file.write_text(_FAKE_API_KEY, encoding="ascii")
    monkeypatch.setenv("RELAY_API_KEY_FILE", str(key_file))
    return key_file


def _git(*args, cwd, **kwargs):
    return subprocess.run(
        ["git", "-c", "user.email=relay@test.local", "-c", "user.name=RelayTest",
         *args],
        cwd=cwd, check=True, capture_output=True, text=True, **kwargs,
    )


@pytest.fixture
def tmp_db(tmp_path) -> StateManager:
    return StateManager(tmp_path / "relay_test.db")


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine()


@pytest.fixture
def audit(tmp_db) -> AuditLog:
    return AuditLog(tmp_db)


@pytest.fixture
def test_repo(tmp_path) -> Path:
    """
    A git-initialised copy of test-repository in a temp directory.
    Safe to modify — a fresh copy is created per test.
    """
    repo = tmp_path / "test-repo"
    shutil.copytree(str(TEST_REPO_SRC), str(repo))
    _git("init", cwd=str(repo))
    _git("add", ".", cwd=str(repo))
    _git("commit", "-m", "initial commit", cwd=str(repo))
    return repo


@pytest.fixture
def sample_task(tmp_db, test_repo) -> Task:
    """
    A READY task pointing at the test-repository copy.
    Pre-inserted into tmp_db.
    """
    task = Task(
        task_id=Task.new_id(),
        project_id="relay-prototype-test",
        repository=str(test_repo),
        base_ref="main",
        working_ref="main",
        objective=(
            "Fix the floor-division bug in calculate_discount_price. "
            "Ensure all tests pass."
        ),
        requirements=[
            "Change // to / in the discount_amount line of calculate_discount_price",
            "Ensure calculate_discount_price(99.99, 15.0) returns 84.99",
            "Ensure calculate_discount_price(33.33, 10.0) returns 30.00",
            "Run the full test suite and confirm all tests pass",
        ],
        acceptance_criteria=[
            "All tests pass",
            "calculate_discount_price(99.99, 15.0) == 84.99",
        ],
        requested_actions=["read_repository", "modify_working_tree", "run_tests"],
        forbidden_actions=["push", "deploy", "merge", "modify_main", "modify_relay_policy"],
        required_tests=["test_calculator.py"],
        approval_policy="auto",
        status=TaskStatus.READY,
    )
    tmp_db.create_task(task)
    return task


def hold_worker_lease(db_path, execution) -> None:
    """Make a faked ExecutionEngine.run honour the real contract: run() returns
    with the execution recorded and its workspace lease still held (cmd_run_task
    renews that lease before verifying and releases it afterwards)."""
    from datetime import datetime, timedelta, timezone

    from relay.models import ExecutionLease

    state = StateManager(str(db_path))
    if state.get_execution(execution.execution_id) is None:
        state.create_execution(execution)
    now = datetime.now(timezone.utc)
    assert state.acquire_lease(ExecutionLease(
        execution_id=execution.execution_id,
        task_id=execution.task_id,
        workspace=execution.workspace or f"lease-for-{execution.execution_id}",
        pid=os.getpid(),
        started_at=now,
        expires_at=now + timedelta(seconds=600),
        worker_identity="test-fake-run",
    ))
