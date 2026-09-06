"""Shared fixtures for Relay test suite."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from relay.audit import AuditLog
from relay.models import Task, TaskStatus
from relay.policy import PolicyEngine
from relay.state import StateManager

# Absolute path to the disposable test repository.
TEST_REPO_SRC = Path(__file__).parent.parent / "test-repository"


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
