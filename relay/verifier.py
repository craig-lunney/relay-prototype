from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .models import Execution, Task
from .paths import InvalidTestSpecifierError, WorkspaceBoundaryError, validate_required_tests


@dataclass
class VerificationResult:
    """
    Independent verification result produced by Relay, not by Claude.
    Claude's ImplementationReport is compared against this, never the reverse.
    """
    passed: bool
    branch: str
    git_status_lines: list[str]
    files_changed: list[str]
    files_added: list[str]
    files_deleted: list[str]
    unexpected_files: list[str]
    test_exit_code: int | None
    test_output: str
    acceptance_results: dict[str, bool]
    report_agreement: dict[str, bool]
    errors: list[str]

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "branch": self.branch,
            "files_changed": self.files_changed,
            "files_added": self.files_added,
            "files_deleted": self.files_deleted,
            "unexpected_files": self.unexpected_files,
            "test_exit_code": self.test_exit_code,
            "acceptance_results": self.acceptance_results,
            "report_agreement": self.report_agreement,
            "errors": self.errors,
        }


class Verifier:
    """
    Independent verification layer.

    Relay's verifier does NOT trust Claude's ImplementationReport.
    It inspects the workspace directly: git state, diff, test results.
    Where deterministic checks are possible, it runs them.
    """

    def verify(
        self,
        task: Task,
        execution: Execution,
        expected_new_files: list[str] | None = None,
    ) -> VerificationResult:
        # P1-C: canonicalize the workspace path from the execution record before any
        # operations. Fail closed if the path is not a real directory.
        raw_workspace = execution.workspace
        try:
            canonical_ws = Path(raw_workspace).resolve()
            if not canonical_ws.is_dir():
                raise WorkspaceBoundaryError(
                    f"Workspace {raw_workspace!r} is not an existing directory."
                )
        except WorkspaceBoundaryError as exc:
            return VerificationResult(
                passed=False,
                branch="UNKNOWN",
                git_status_lines=[],
                files_changed=[],
                files_added=[],
                files_deleted=[],
                unexpected_files=[],
                test_exit_code=None,
                test_output="",
                acceptance_results={},
                report_agreement={},
                errors=[f"Workspace boundary error: {exc}"],
            )
        workspace = str(canonical_ws)
        errors: list[str] = []

        branch = self._get_branch(workspace, errors)
        status_lines = self._get_status_lines(workspace, errors)
        changed, added, deleted = self._get_changed_files(workspace, errors)
        unexpected = self._find_unexpected(added, expected_new_files or [])

        test_exit, test_output = self._run_tests(workspace, task.required_tests, errors)
        acceptance = self._check_acceptance(task, test_output, test_exit)
        report_agreement = self._compare_report(execution, changed, added, deleted, test_exit)

        passed = (
            test_exit == 0
            and not errors
            and not unexpected
            and all(v for v in acceptance.values() if v is not None)
        )

        return VerificationResult(
            passed=passed,
            branch=branch,
            git_status_lines=status_lines,
            files_changed=changed,
            files_added=added,
            files_deleted=deleted,
            unexpected_files=unexpected,
            test_exit_code=test_exit,
            test_output=test_output,
            acceptance_results=acceptance,
            report_agreement=report_agreement,
            errors=errors,
        )

    # ── Git inspection ────────────────────────────────────────────────────────

    def _get_branch(self, workspace: str, errors: list[str]) -> str:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace, capture_output=True, text=True,
        )
        if result.returncode != 0:
            errors.append(f"git branch failed: {result.stderr.strip()}")
            return "UNKNOWN"
        return result.stdout.strip() or "UNKNOWN"

    def _get_status_lines(self, workspace: str, errors: list[str]) -> list[str]:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace, capture_output=True, text=True,
        )
        if result.returncode != 0:
            errors.append(f"git status failed: {result.stderr.strip()}")
            return []
        return [l for l in result.stdout.splitlines() if l.strip()]

    def _get_changed_files(
        self, workspace: str, errors: list[str]
    ) -> tuple[list[str], list[str], list[str]]:
        """Return (changed, added, deleted) file lists from git status --porcelain."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace, capture_output=True, text=True,
        )
        if result.returncode != 0:
            errors.append(f"git status --porcelain failed: {result.stderr.strip()}")
            return [], [], []

        changed, added, deleted = [], [], []
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            xy = line[:2]
            fname = line[3:].strip().split(" -> ")[-1]  # handle renames
            if xy in (" M", "M ", "MM"):
                changed.append(fname)
            elif xy in ("A ", " A", "??"):
                added.append(fname)
            elif xy in ("D ", " D", "AD"):
                deleted.append(fname)
            elif xy[0] == "R":
                changed.append(fname)
        return changed, added, deleted

    def _find_unexpected(
        self, added: list[str], expected: list[str]
    ) -> list[str]:
        if not expected:
            return []
        return [f for f in added if f not in expected]

    # ── Test execution ────────────────────────────────────────────────────────

    def _run_tests(
        self, workspace: str, required_tests: list[str], errors: list[str]
    ) -> tuple[int | None, str]:
        if not required_tests:
            return None, ""

        # P1-D: validate test specifiers against workspace boundary before constructing
        # the subprocess command. Rejects option injection, control characters, and
        # path traversal. This is defence-in-depth — the primary barrier is shell=False
        # and the '--' separator below, but we fail closed on malformed specifiers.
        try:
            validate_required_tests(required_tests, workspace)
        except InvalidTestSpecifierError as exc:
            errors.append(f"Invalid test specifier rejected: {exc}")
            return None, ""

        # Options before '--', test specifiers after '--'.
        # The '--' separator is the authoritative option/argument boundary:
        # any value after it is treated as a positional argument (file/node path),
        # never as a pytest option — even if it starts with '-'.
        # shell=False (the default) ensures no shell interpretation occurs.
        test_args = ["-v", "--tb=short", "--"] + required_tests

        for cmd_prefix in (
            ["python", "-m", "pytest"],
            ["pytest"],
        ):
            cmd = cmd_prefix + test_args
            try:
                result = subprocess.run(
                    cmd,
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                return result.returncode, result.stdout + result.stderr
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                errors.append("Test run timed out after 120s.")
                return -1, "TIMEOUT"

        errors.append("Neither 'python -m pytest' nor 'pytest' found.")
        return None, ""

    # ── Acceptance criteria ───────────────────────────────────────────────────

    def _check_acceptance(
        self, task: Task, test_output: str, test_exit: int | None
    ) -> dict[str, bool | None]:
        results: dict[str, bool | None] = {}
        lower_output = test_output.lower()

        for criterion in task.acceptance_criteria:
            lc = criterion.lower()
            if any(kw in lc for kw in ("all tests pass", "no failing test", "tests pass")):
                results[criterion] = (test_exit == 0 and bool(test_output))
            elif "test" in lc and "pass" in lc:
                results[criterion] = test_exit == 0
            elif "test" in lc and ("fail" in lc or "error" in lc):
                results[criterion] = test_exit != 0
            else:
                # Cannot deterministically verify — leave as None (unverified, not failed)
                results[criterion] = None
        return results

    # ── Report comparison ─────────────────────────────────────────────────────

    def _compare_report(
        self,
        execution: Execution,
        actual_changed: list[str],
        actual_added: list[str],
        actual_deleted: list[str],
        actual_test_exit: int | None,
    ) -> dict[str, bool]:
        if execution.report is None:
            return {"report_present": False}

        report = execution.report
        agreement: dict[str, bool] = {"report_present": True}

        # If Claude claimed COMPLETED but tests failed, that's a false assertion.
        if report.status == "COMPLETED" and actual_test_exit not in (0, None):
            agreement["completed_claim_verified"] = False
        else:
            agreement["completed_claim_verified"] = True

        # Loose file-set comparison — git output format may differ from Claude's report.
        reported_set = set(report.files_changed + report.files_added)
        actual_set = set(actual_changed + actual_added)
        agreement["files_roughly_match"] = (
            len(reported_set.symmetric_difference(actual_set)) <= 3
        )

        return agreement
