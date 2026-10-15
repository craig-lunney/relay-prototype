"""
Batch 1 — Verification integrity, exercised end to end through the real CLI.

Every test in the E2E section drives ``relay create-task`` and ``relay run-task``
through ``relay.cli.main`` exactly as an operator would.  The only substitution
is the worker: the real ``claude`` CLI is replaced by a shim that runs
``tests/fake_worker.py`` with a scripted behaviour.  Workspace preparation,
baseline capture, the executor, the verifier and outcome binding are all the
production code paths (the suite-wide stubs of ``_capture_baseline`` and
``_checkout_base_ref`` from conftest.py are undone for these tests).

Each test builds its own git repository from ``test-repository/`` inside
pytest's tmp_path, so the tests are isolated, deterministic and repeatable.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import relay.executor as executor_mod
from relay.executor import ExecutionEngine
from relay.models import AuditEventType, TaskStatus
from relay.state import StateManager

TEST_REPO_SRC = Path(__file__).parent.parent / "test-repository"
FAKE_WORKER = Path(__file__).parent / "fake_worker.py"

# Captured at import time, before conftest's autouse fixtures replace them.
_REAL_CAPTURE_BASELINE = ExecutionEngine._capture_baseline
_REAL_CHECKOUT_BASE_REF = ExecutionEngine._checkout_base_ref

ALL_TESTS_PASS = "All tests pass"
NUMERIC_CRITERION = "calculate_discount_price(99.99, 15.0) == 84.99"


@dataclass
class Outcome:
    task_id: str
    status: TaskStatus
    verification: dict
    workspace: Path
    event_types: list[str] = field(default_factory=list)
    db: Path | None = None


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=relay@test.local", "-c", "user.name=RelayTest", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


def _make_source_repo(root: Path) -> Path:
    repo = root / "source-repo"
    shutil.copytree(
        TEST_REPO_SRC, repo, ignore=shutil.ignore_patterns(".git", "__pycache__")
    )
    _git("init", "-b", "main", cwd=repo)
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


def _make_shim(root: Path, mode: str) -> Path:
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    if sys.platform == "win32":
        shim = bindir / f"claude-{mode}.cmd"
        shim.write_text(
            f'@"{sys.executable}" "{FAKE_WORKER}" {mode} %*\r\n', encoding="utf-8"
        )
    else:
        shim = bindir / f"claude-{mode}"
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_WORKER}" {mode} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _relay(monkeypatch, *argv: str) -> int:
    """Invoke relay.cli.main with argv; return the exit code (0 on normal return)."""
    from relay.cli import main

    monkeypatch.setattr(sys, "argv", ["relay", *argv])
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.fixture
def relay_e2e(tmp_path, monkeypatch):
    """Return run(mode, ...) -> Outcome, driving the real CLI with a fake worker."""
    monkeypatch.setattr(ExecutionEngine, "_capture_baseline", _REAL_CAPTURE_BASELINE)
    monkeypatch.setattr(ExecutionEngine, "_checkout_base_ref", _REAL_CHECKOUT_BASE_REF)
    db = tmp_path / "relay.db"
    ws_base = tmp_path / "workspaces"

    def run(
        mode: str,
        *,
        acceptance: tuple[str, ...] = (ALL_TESTS_PASS,),
        expected_new_files: list[str] | None = None,
        required_tests: tuple[str, ...] = ("test_calculator.py",),
    ) -> Outcome:
        repo = _make_source_repo(tmp_path)
        shim = _make_shim(tmp_path, mode)
        monkeypatch.setattr(executor_mod, "_find_claude", lambda: str(shim))

        task_def = {
            "project_id": "batch1-integrity",
            "repository": str(repo),
            "base_ref": "main",
            "objective": "Fix the floor-division bug in calculate_discount_price.",
            "requirements": ["Change // to / in calculate_discount_price"],
            "acceptance_criteria": list(acceptance),
            "requested_actions": ["read_repository", "modify_working_tree", "run_tests"],
            "forbidden_actions": ["push", "deploy", "merge", "modify_main", "modify_relay_policy"],
            "required_tests": list(required_tests),
            "approval_policy": "auto",
        }
        if expected_new_files is not None:
            task_def["expected_new_files"] = expected_new_files
        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps(task_def), encoding="utf-8")

        assert _relay(monkeypatch, "--db", str(db), "create-task", str(task_file)) == 0
        with sqlite3.connect(str(db)) as conn:
            task_id = conn.execute(
                "SELECT task_id FROM tasks ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]

        _relay(
            monkeypatch, "--db", str(db), "run-task", task_id,
            "--workspace", str(ws_base), "--timeout", "60", "--keep-workspace",
        )

        state = StateManager(db)
        events = state.get_events_for_task(task_id)
        verification = next(
            (e.details for e in reversed(events)
             if e.event_type == AuditEventType.VERIFICATION_COMPLETED),
            {},
        )
        return Outcome(
            task_id=task_id,
            status=state.get_task(task_id).status,
            verification=verification,
            workspace=ws_base / task_id,
            event_types=[e.event_type.value for e in events],
            db=db,
        )

    return run


def _violations(outcome: Outcome) -> str:
    return " | ".join(outcome.verification.get("protected_violations", []))


# ── E2E: the original critical attack ─────────────────────────────────────────

class TestNeuteredTestsAttack:
    def test_redefined_pass_tests_with_bug_unfixed_is_rejected(self, relay_e2e):
        """The audit's E-1 attack: bug left unfixed, every test redefined as ``pass``
        with the same name.  Before Batch 1 this ended COMPLETED."""
        out = relay_e2e("neuter_redefine")
        assert out.status == TaskStatus.FAILED
        assert out.verification["passed"] is False
        assert "test_calculator.py" in _violations(out)
        # The bug really is unfixed in the workspace.
        assert "discount_percent // 100" in (out.workspace / "calculator.py").read_text(
            encoding="utf-8"
        )

    def test_in_place_pass_bodies_are_rejected(self, relay_e2e):
        out = relay_e2e("neuter_inplace")
        assert out.status == TaskStatus.FAILED
        assert "test_calculator.py" in _violations(out)

    def test_worker_code_is_not_executed_when_integrity_fails(self, relay_e2e):
        """Once protected files are known to be altered, the verifier must not run
        the worker-influenced test suite at all."""
        out = relay_e2e("neuter_redefine")
        assert out.verification["test_exit_code"] is None
        assert out.verification["acceptance_results"][ALL_TESTS_PASS] == "FAIL"

    def test_poisoned_bytecode_cache_is_not_loaded(self, relay_e2e):
        """A pytest-format pyc for the *unchanged* test file, carrying neutered
        code and a forged header, must not be loaded by the verifier."""
        out = relay_e2e("poison_pyc")
        assert out.status == TaskStatus.FAILED
        # Tests ran against the real source and detected the unfixed bug.
        assert out.verification["test_exit_code"] == 1
        assert out.verification["acceptance_results"][ALL_TESTS_PASS] == "FAIL"


# ── E2E: protected tests and test configuration ──────────────────────────────

class TestProtectedFiles:
    def test_editing_existing_test_rejected_even_with_correct_fix(self, relay_e2e):
        out = relay_e2e("fix_edit_test")
        assert out.status == TaskStatus.FAILED
        assert "modified: test_calculator.py" in _violations(out)

    def test_deleting_protected_test_file_rejected(self, relay_e2e):
        out = relay_e2e("delete_test_file")
        assert out.status == TaskStatus.FAILED
        assert "deleted: test_calculator.py" in _violations(out)

    def test_renaming_protected_test_file_rejected(self, relay_e2e):
        out = relay_e2e("rename_test_file")
        assert out.status == TaskStatus.FAILED
        assert "deleted: test_calculator.py" in _violations(out)

    def test_injected_conftest_rejected(self, relay_e2e):
        out = relay_e2e("conftest_inject")
        assert out.status == TaskStatus.FAILED
        assert "added: conftest.py" in _violations(out)

    def test_added_pytest_ini_rejected(self, relay_e2e):
        out = relay_e2e("pytest_ini")
        assert out.status == TaskStatus.FAILED
        assert "pytest.ini" in _violations(out)

    def test_pytest_config_rejected_even_if_declared_as_expected_file(self, relay_e2e):
        """expected_new_files cannot authorise test-runner configuration."""
        out = relay_e2e("pyproject_pytest", expected_new_files=["pyproject.toml"])
        assert out.status == TaskStatus.FAILED
        assert "pyproject.toml" in _violations(out)

    def test_pyproject_without_pytest_section_allowed_when_declared(self, relay_e2e):
        out = relay_e2e("pyproject_plain", expected_new_files=["pyproject.toml"])
        assert out.status == TaskStatus.COMPLETED, out.verification
        assert out.verification["protected_violations"] == []

    def test_added_pth_startup_hook_rejected(self, relay_e2e):
        out = relay_e2e("pth_inject", expected_new_files=["relay_evil.pth"])
        assert out.status == TaskStatus.FAILED
        assert "relay_evil.pth" in _violations(out)


# ── E2E: unchanged work and unexpected files ─────────────────────────────────

class TestChangeDetection:
    def test_no_op_worker_is_detected(self, relay_e2e):
        out = relay_e2e("noop")
        assert out.status == TaskStatus.FAILED
        assert out.verification["no_op"] is True
        assert out.verification["files_added"] == []

    def test_legitimate_fix_completes(self, relay_e2e):
        out = relay_e2e("fix")
        assert out.status == TaskStatus.COMPLETED, out.verification
        assert out.verification["passed"] is True
        assert out.verification["files_changed"] == ["calculator.py"]
        assert out.verification["no_op"] is False
        assert out.verification["acceptance_results"] == {ALL_TESTS_PASS: "PASS"}

    def test_unexpected_new_file_rejected_through_cli(self, relay_e2e):
        out = relay_e2e("fix_new_file")
        assert out.status == TaskStatus.FAILED
        assert out.verification["unexpected_files"] == ["NOTES.md"]

    def test_declared_new_file_allowed_through_cli(self, relay_e2e):
        out = relay_e2e("fix_new_file", expected_new_files=["NOTES.md"])
        assert out.status == TaskStatus.COMPLETED, out.verification
        assert out.verification["unexpected_files"] == []
        assert out.verification["files_added"] == ["NOTES.md"]


# ── E2E: acceptance criteria and human acknowledgement ───────────────────────

class TestAcceptanceCriteria:
    def test_unverified_criterion_blocks_completion_until_acknowledged(
        self, relay_e2e, monkeypatch
    ):
        out = relay_e2e("fix", acceptance=(ALL_TESTS_PASS, NUMERIC_CRITERION))
        assert out.status == TaskStatus.ACCEPTANCE_REQUIRED
        assert out.verification["passed"] is True
        assert out.verification["acceptance_results"] == {
            ALL_TESTS_PASS: "PASS",
            NUMERIC_CRITERION: "UNVERIFIED",
        }
        assert AuditEventType.TASK_COMPLETED.value not in out.event_types

        rc = _relay(
            monkeypatch, "--db", str(out.db), "acknowledge-criteria", out.task_id,
            "--actor", "craig", "--reason", "checked by hand",
        )
        assert rc == 0
        state = StateManager(out.db)
        assert state.get_task(out.task_id).status == TaskStatus.COMPLETED

        events = state.get_events_for_task(out.task_id)
        ack = [e for e in events if e.event_type == AuditEventType.ACCEPTANCE_ACKNOWLEDGED]
        assert len(ack) == 1
        assert ack[0].actor == "craig"
        assert ack[0].details["criteria"] == [NUMERIC_CRITERION]
        # The automated verdict is never rewritten by the acknowledgement.
        verification = [
            e for e in events if e.event_type == AuditEventType.VERIFICATION_COMPLETED
        ][-1]
        assert verification.details["acceptance_results"][NUMERIC_CRITERION] == "UNVERIFIED"

    def test_acknowledgement_can_reject(self, relay_e2e, monkeypatch):
        out = relay_e2e("fix", acceptance=(ALL_TESTS_PASS, NUMERIC_CRITERION))
        rc = _relay(
            monkeypatch, "--db", str(out.db), "acknowledge-criteria", out.task_id,
            "--actor", "craig", "--reject", "--reason", "value is wrong",
        )
        assert rc == 0
        state = StateManager(out.db)
        assert state.get_task(out.task_id).status == TaskStatus.FAILED
        types = [e.event_type for e in state.get_events_for_task(out.task_id)]
        assert AuditEventType.ACCEPTANCE_REJECTED in types
        assert AuditEventType.TASK_COMPLETED not in types

    def test_failed_criterion_is_not_downgraded_and_cannot_be_acknowledged(
        self, relay_e2e, monkeypatch
    ):
        out = relay_e2e("noop", acceptance=(ALL_TESTS_PASS, "The README is updated"))
        assert out.status == TaskStatus.FAILED
        assert out.verification["acceptance_results"] == {
            ALL_TESTS_PASS: "FAIL",
            "The README is updated": "UNVERIFIED",
        }
        rc = _relay(
            monkeypatch, "--db", str(out.db), "acknowledge-criteria", out.task_id,
            "--actor", "craig",
        )
        assert rc == 1
        assert StateManager(out.db).get_task(out.task_id).status == TaskStatus.FAILED


class TestNewTestFilesExcluded:
    def test_new_test_module_cannot_patch_application_at_import(self, relay_e2e):
        """A declared new test module that monkeypatches the application at import
        time is excluded from the verification run, so the unfixed bug is caught."""
        out = relay_e2e(
            "monkeypatch_via_new_test",
            required_tests=(".",),
            expected_new_files=["test_aaa_patch.py"],
        )
        assert out.status == TaskStatus.FAILED
        assert out.verification["excluded_from_test_run"] == ["test_aaa_patch.py"]
        assert out.verification["protected_violations"] == []
        assert out.verification["test_exit_code"] == 1

    def test_control_without_exclusion_the_attack_would_succeed(
        self, relay_e2e, monkeypatch
    ):
        """Control: with the --ignore exclusion disabled, the same worker passes.
        Proves the exclusion (not some other check) is what stops the attack."""
        import relay.verifier as verifier_mod

        real = verifier_mod.run_test_suite

        def without_exclusion(workspace, required_tests, env, timeout=120, ignore_paths=None):
            return real(workspace, required_tests, env, timeout=timeout, ignore_paths=[])

        monkeypatch.setattr(verifier_mod, "run_test_suite", without_exclusion)
        out = relay_e2e(
            "monkeypatch_via_new_test",
            required_tests=(".",),
            expected_new_files=["test_aaa_patch.py"],
        )
        assert out.status == TaskStatus.COMPLETED


class TestBaselinePersistence:
    def test_manifest_capture_failure_is_recorded_not_raised(
        self, tmp_db, sample_task, monkeypatch
    ):
        import relay.integrity as integrity_mod
        from relay.audit import AuditLog
        from relay.policy import PolicyEngine

        def boom(_workspace):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(integrity_mod, "snapshot_workspace", boom)
        engine = ExecutionEngine(tmp_db, AuditLog(tmp_db), PolicyEngine())
        execution = _execution(Path(sample_task.repository), task_id=sample_task.task_id,
                               baseline_test_exit=None, baseline_tests_collected=[])
        tmp_db.create_execution(execution)
        _REAL_CAPTURE_BASELINE(engine, sample_task, execution, sample_task.repository)

        stored = tmp_db.get_execution(execution.execution_id)
        assert stored.baseline_manifest is None
        assert "RuntimeError: disk on fire" in stored.baseline_manifest_error
        result = Verifier().verify(sample_task, stored)
        assert result.passed is False
        assert any("disk on fire" in e for e in result.errors)

    def test_manifest_persisted_with_execution(self, relay_e2e):
        out = relay_e2e("fix")
        executions = StateManager(out.db).get_executions_for_task(out.task_id)
        assert executions[-1].baseline_manifest is not None
        assert executions[-1].baseline_manifest_error is None
        assert "test_calculator.py" in json.loads(executions[-1].baseline_manifest)["files"]


# ── Unit: integrity manifest ──────────────────────────────────────────────────

from relay.integrity import (  # noqa: E402
    WorkspaceSnapshot,
    compare_snapshots,
    is_bytecode_artifact,
    snapshot_workspace,
)


def _snap(files: dict[str, str], config: dict[str, str] | None = None) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(files=dict(files), pytest_config=dict(config or {}))


class TestIntegrityUnit:
    def test_bytecode_artifact_classification_is_narrow(self):
        assert is_bytecode_artifact("__pycache__/calculator.cpython-314.pyc")
        assert is_bytecode_artifact("pkg/__pycache__/x.cpython-314-pytest-9.1.1.pyc")
        assert is_bytecode_artifact(".pytest_cache/v/cache/nodeids")
        # Non-bytecode content under __pycache__ is a substantive change.
        assert not is_bytecode_artifact("__pycache__/payload.py")
        assert not is_bytecode_artifact("__pycache__.pyc")
        assert not is_bytecode_artifact("calculator.py")

    def test_artifacts_are_reported_not_counted_as_changes(self):
        base = _snap({"a.py": "1"})
        cur = _snap({"a.py": "1", "__pycache__/a.cpython-314.pyc": "x"})
        r = compare_snapshots(base, cur)
        assert (r.changed, r.added, r.deleted) == ([], [], [])
        assert r.ignored_artifacts == ["__pycache__/a.cpython-314.pyc"]

    def test_helper_module_in_tests_dir_is_protected(self):
        base = _snap({"tests/helpers.py": "1", "app.py": "1"})
        cur = _snap({"tests/helpers.py": "2", "app.py": "1"})
        assert compare_snapshots(base, cur).protected_violations == [
            "modified: tests/helpers.py (test file)"
        ]

    def test_test_file_named_only_by_baseline_node_id_is_protected(self):
        base = _snap({"checks/spec_calc.py": "1"})
        cur = _snap({"checks/spec_calc.py": "2"})
        r = compare_snapshots(base, cur, ["checks/spec_calc.py::test_x"])
        assert r.protected_violations == ["modified: checks/spec_calc.py (test file)"]

    def test_new_test_file_allowed_but_excluded(self):
        base = _snap({"test_a.py": "1"})
        cur = _snap({"test_a.py": "1", "test_b.py": "1"})
        r = compare_snapshots(base, cur)
        assert r.protected_violations == []
        assert r.new_python_files == ["test_b.py"]

    def test_init_added_above_test_files_is_protected(self):
        base = _snap({"test_a.py": "1", "src/app.py": "1"})
        cur = _snap({"test_a.py": "1", "src/app.py": "1",
                     "__init__.py": "x", "src/__init__.py": "x"})
        assert compare_snapshots(base, cur).protected_violations == [
            "added: __init__.py (test package __init__)"
        ]

    def test_pyproject_non_pytest_edit_is_not_a_violation(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "a"\n[tool.pytest.ini_options]\naddopts = "-q"\n'
        )
        base = snapshot_workspace(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "b"\n[tool.pytest.ini_options]\naddopts = "-q"\n'
        )
        r = compare_snapshots(base, snapshot_workspace(tmp_path))
        assert r.changed == ["pyproject.toml"]
        assert r.protected_violations == []

    @pytest.mark.parametrize("name,before,after", [
        ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-q"\n',
         '[tool.pytest.ini_options]\naddopts = "-k add"\n'),
        ("setup.cfg", "[tool:pytest]\naddopts = -q\n", "[tool:pytest]\naddopts = -k add\n"),
        ("tox.ini", "[tox]\n", "[tox]\n[pytest]\naddopts = -k add\n"),
        ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-q"\n', ""),
    ])
    def test_pytest_section_changes_are_violations(self, tmp_path, name, before, after):
        (tmp_path / name).write_text(before)
        base = snapshot_workspace(tmp_path)
        (tmp_path / name).write_text(after)
        assert compare_snapshots(base, snapshot_workspace(tmp_path)).protected_violations == [
            f"pytest configuration changed: {name}"
        ]

    def test_unparseable_config_change_is_a_violation(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[metadata\nname = x\n")
        base = snapshot_workspace(tmp_path)
        (tmp_path / "setup.cfg").write_text("[metadata\nname = y\n")
        assert compare_snapshots(base, snapshot_workspace(tmp_path)).protected_violations == [
            "pytest configuration changed: setup.cfg"
        ]

    def test_git_directory_is_not_part_of_the_manifest(self, tmp_path):
        ws = tmp_path / "ws"
        (ws / ".git").mkdir(parents=True)
        (ws / ".git" / "config").write_text("x")
        (ws / "a.py").write_text("x")
        assert set(snapshot_workspace(ws).files) == {"a.py"}

    def test_snapshot_round_trips_through_json(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\nx = 1\n')
        snap = snapshot_workspace(tmp_path)
        assert WorkspaceSnapshot.from_json(snap.to_json()) == snap


# ── Unit: verifier fail-closed paths ──────────────────────────────────────────

from datetime import datetime, timezone  # noqa: E402

from relay.models import Execution, ExecutionStatus, Task  # noqa: E402
from relay.verifier import (  # noqa: E402
    AC_FAIL,
    AC_PASS,
    AC_UNVERIFIED,
    Verifier,
    is_automatically_verifiable,
    unverified_criteria,
)


def _task(**overrides) -> Task:
    fields = dict(
        task_id="TASK-UNIT0001", project_id="p", repository=".", base_ref="main",
        working_ref="main", objective="o", requirements=[],
        acceptance_criteria=[ALL_TESTS_PASS], requested_actions=["run_tests"],
        forbidden_actions=[], required_tests=["test_calculator.py"],
        approval_policy="auto",
    )
    fields.update(overrides)
    return Task(**fields)


def _execution(workspace: Path, **overrides) -> Execution:
    fields = dict(
        execution_id="EXEC-UNIT0001", task_id="TASK-UNIT0001", attempt_number=1,
        status=ExecutionStatus.COMPLETED, workspace=str(workspace),
        worker_identity="w", policy_version="0.1", capability_snapshot=[],
        started_at=datetime.now(timezone.utc), exit_code=0,
        baseline_tests_collected=["test_calculator.py::test_add"], baseline_test_exit=0,
    )
    fields.update(overrides)
    return Execution(**fields)


@pytest.fixture
def unit_workspace(tmp_path) -> Path:
    ws = tmp_path / "ws"
    shutil.copytree(TEST_REPO_SRC, ws, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    _git("init", "-b", "main", cwd=ws)
    _git("add", ".", cwd=ws)
    _git("commit", "-m", "initial", cwd=ws)
    return ws


class TestVerifierFailClosed:
    @pytest.mark.parametrize("overrides,message", [
        ({"baseline_manifest": None}, "No workspace integrity manifest"),
        ({"baseline_manifest_error": "Could not capture workspace integrity manifest: x"},
         "Could not capture"),
        ({"baseline_manifest": "{not json"}, "Workspace integrity check failed"),
    ])
    def test_missing_or_bad_manifest_fails_without_running_tests(
        self, unit_workspace, monkeypatch, overrides, message
    ):
        import relay.verifier as verifier_mod

        calls = []
        monkeypatch.setattr(verifier_mod, "run_test_suite",
                            lambda *a, **k: calls.append(a) or (0, "", []))
        result = Verifier().verify(_task(), _execution(unit_workspace, **overrides))
        assert result.passed is False
        assert result.integrity_checked is False
        assert any(message in e for e in result.errors)
        assert calls == [], "worker-influenced tests must not run without a trusted baseline"
        assert result.acceptance_results == {ALL_TESTS_PASS: AC_FAIL}

    def test_manifest_path_does_not_run_git_status(self, unit_workspace, monkeypatch):
        """git status can execute repository-configured programs (fsmonitor,
        clean filters) from the worker-writable .git/config; the manifest path
        must not invoke it."""
        manifest = snapshot_workspace(unit_workspace).to_json()
        app = unit_workspace / "calculator.py"
        app.write_text(
            app.read_text(encoding="utf-8").replace(
                "discount_percent // 100", "discount_percent / 100"),
            encoding="utf-8",
        )
        real_run = subprocess.run
        git_commands = []

        def spy(cmd, *a, **k):
            if cmd and cmd[0] == "git":
                git_commands.append(cmd[1:])
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(subprocess, "run", spy)
        result = Verifier().verify(
            _task(), _execution(unit_workspace, baseline_manifest=manifest)
        )
        assert result.integrity_checked is True
        assert result.files_changed == ["calculator.py"]
        assert not any(c and c[0] == "status" for c in git_commands), git_commands


# ── Unit: hardened pytest invocation ──────────────────────────────────────────

class TestPytestInvocation:
    def test_run_uses_relay_owned_pycache_and_ignores(self, tmp_path, monkeypatch):
        from relay import testing

        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["env"] = kwargs["env"]
            prefix_arg = next(a for a in cmd if a.startswith("pycache_prefix="))
            seen["prefix"] = Path(prefix_arg.split("=", 1)[1])
            seen["prefix_existed"] = seen["prefix"].is_dir()
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        testing.run_test_suite(tmp_path, ["test_x.py"], {"PATH": "p"},
                               ignore_paths=["test_new.py"])
        cmd = seen["cmd"]
        ignore_arg = f"--ignore={tmp_path / 'test_new.py'}"
        assert cmd[:3] == ["python", "-B", "-X"]
        assert cmd[cmd.index("-p") + 1] == "no:cacheprovider"
        assert ignore_arg in cmd and cmd.index(ignore_arg) < cmd.index("--")
        assert seen["prefix_existed"]
        assert tmp_path not in seen["prefix"].parents
        assert not seen["prefix"].exists(), "Relay pycache dir must be removed after the run"
        assert seen["env"] == {"PATH": "p"}, "env must stay exactly the allowlisted mapping"

    def test_collection_uses_the_same_hardening(self, tmp_path, monkeypatch):
        from relay import testing

        seen = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                seen["cmd"] = cmd
                self.returncode = 0

            def communicate(self, timeout=None):
                return "", ""

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        testing.collect_tests(tmp_path, ["test_x.py"], {})
        assert seen["cmd"][:3] == ["python", "-B", "-X"]
        assert "no:cacheprovider" in seen["cmd"]


# ── Unit: acceptance-criterion verdicts ───────────────────────────────────────

class TestAcceptanceVerdicts:
    @pytest.mark.parametrize("criterion", [
        "All tests pass", "all tests pass.", "  The test suite passes ",
        "Tests must not fail", "No failing tests",
    ])
    def test_recognised_suite_criteria_are_verifiable(self, criterion):
        assert is_automatically_verifiable(criterion)

    @pytest.mark.parametrize("criterion", [
        "Add a regression test for negative prices; all tests pass",
        "calculate_discount_price(99.99, 15.0) == 84.99",
        "Tests for the new endpoint pass",
        "The README is updated",
    ])
    def test_other_criteria_are_unverified_not_guessed(self, criterion):
        assert not is_automatically_verifiable(criterion)

    def test_every_criterion_gets_a_verdict(self):
        task = _task(acceptance_criteria=[ALL_TESTS_PASS, "Docs updated", "tests must not fail"])
        v = Verifier()
        assert v._check_acceptance(task, "x PASSED", 0) == {
            ALL_TESTS_PASS: AC_PASS, "Docs updated": AC_UNVERIFIED,
            "tests must not fail": AC_PASS,
        }
        # Previously "tests must not fail" was inverted (PASS only when tests failed).
        assert v._check_acceptance(task, "x FAILED", 1)["tests must not fail"] == AC_FAIL
        # Tests that never ran cannot satisfy a test criterion.
        assert v._check_acceptance(task, "", None)[ALL_TESTS_PASS] == AC_FAIL

    def test_unverified_criteria_derived_from_task_text(self):
        task = _task(acceptance_criteria=[ALL_TESTS_PASS, "Docs updated", "Docs updated"])
        assert unverified_criteria(task) == ["Docs updated"]


# ── Unit: task input, policy, state ───────────────────────────────────────────

from relay.models import ALLOWED_TRANSITIONS  # noqa: E402
from relay.paths import InvalidRelativePathError, normalize_relative_path  # noqa: E402
from relay.policy import PolicyEngine  # noqa: E402
from relay.state import InvalidTransitionError  # noqa: E402


class TestExpectedNewFilesInput:
    @pytest.mark.parametrize("raw,normalised", [
        ("NOTES.md", "NOTES.md"), ("./docs/a.md", "docs/a.md"), ("docs\\a.md", "docs/a.md"),
    ])
    def test_normalisation(self, raw, normalised):
        assert normalize_relative_path(raw) == normalised

    @pytest.mark.parametrize("bad", [
        "", "/etc/passwd", "C:/x.txt", "../outside.txt", "a/../../b", "*.md", "a;b",
    ])
    def test_invalid_paths_rejected(self, bad):
        with pytest.raises(InvalidRelativePathError):
            normalize_relative_path(bad)

    def test_policy_rejects_invalid_expected_new_files(self):
        result = PolicyEngine().validate_task(
            _task(expected_new_files=["../escape.py"], requested_actions=["read_repository"])
        )
        assert result.allowed is False
        assert "expected_new_files" in result.reason


class TestAcceptanceState:
    def test_acceptance_required_transitions(self):
        assert TaskStatus.ACCEPTANCE_REQUIRED in ALLOWED_TRANSITIONS[TaskStatus.VERIFYING]
        assert ALLOWED_TRANSITIONS[TaskStatus.ACCEPTANCE_REQUIRED] == frozenset(
            {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        )
        # Only VERIFYING leads into ACCEPTANCE_REQUIRED.
        sources = [s for s, targets in ALLOWED_TRANSITIONS.items()
                   if TaskStatus.ACCEPTANCE_REQUIRED in targets]
        assert sources == [TaskStatus.VERIFYING]

    def test_acknowledge_outside_acceptance_required_changes_nothing(self, tmp_path):
        state = StateManager(tmp_path / "r.db")
        task = _task(status=TaskStatus.VERIFYING)
        state.create_task(task)
        with pytest.raises(InvalidTransitionError):
            state.acknowledge_acceptance_atomic(task.task_id, "craig", ["x"], accept=True)
        assert state.get_task(task.task_id).status == TaskStatus.VERIFYING
        assert state.get_events_for_task(task.task_id) == []

    def test_old_database_is_migrated(self, tmp_path):
        db = tmp_path / "old.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,"
                " repository TEXT NOT NULL, base_ref TEXT NOT NULL,"
                " working_ref TEXT NOT NULL, objective TEXT NOT NULL,"
                " requirements TEXT NOT NULL, acceptance_criteria TEXT NOT NULL,"
                " requested_actions TEXT NOT NULL, forbidden_actions TEXT NOT NULL,"
                " required_tests TEXT NOT NULL, approval_policy TEXT NOT NULL,"
                " status TEXT NOT NULL, protocol_version TEXT NOT NULL DEFAULT '0.1',"
                " max_execution_attempts INTEGER NOT NULL DEFAULT 3,"
                " attempt_count INTEGER NOT NULL DEFAULT 0, error TEXT,"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
        state = StateManager(db)
        task = _task(expected_new_files=["NOTES.md"])
        state.create_task(task)
        assert state.get_task(task.task_id).expected_new_files == ["NOTES.md"]
        with sqlite3.connect(str(db)) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(executions)")}
        assert {"baseline_manifest", "baseline_manifest_error"} <= cols
