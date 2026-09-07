from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .executor import _worker_environment
from .models import Execution, Task
from .paths import InvalidTestSpecifierError, WorkspaceBoundaryError, validate_required_tests
from .testing import collect_tests, parse_test_results, run_test_suite


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
    # P1-J: test accountability fields
    no_op: bool = False
    baseline_tests_collected: list[str] = field(default_factory=list)
    deleted_tests: list[str] = field(default_factory=list)
    deactivated_tests: list[str] = field(default_factory=list)
    node_ids_all_passed: bool = True
    test_results: dict[str, str] = field(default_factory=dict)

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
            "no_op": self.no_op,
            "baseline_tests_collected": self.baseline_tests_collected,
            "deleted_tests": self.deleted_tests,
            "deactivated_tests": self.deactivated_tests,
            "node_ids_all_passed": self.node_ids_all_passed,
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

        # P1-D: validate test specifiers before running (defence-in-depth).
        if task.required_tests:
            try:
                validate_required_tests(task.required_tests, workspace)
            except InvalidTestSpecifierError as exc:
                errors.append(f"Invalid test specifier rejected: {exc}")
                return VerificationResult(
                    passed=False,
                    branch=branch,
                    git_status_lines=status_lines,
                    files_changed=changed,
                    files_added=added,
                    files_deleted=deleted,
                    unexpected_files=unexpected,
                    test_exit_code=None,
                    test_output="",
                    acceptance_results={},
                    report_agreement={},
                    errors=errors,
                )

        # P1-J: run tests via shared utility so output is consistently captured.
        test_exit, test_output, test_errors = run_test_suite(
            workspace, task.required_tests, _worker_environment()
        )
        errors.extend(test_errors)

        acceptance = self._check_acceptance(task, test_output, test_exit)
        report_agreement = self._compare_report(execution, changed, added, deleted, test_exit)

        # P1-J: test accountability checks.
        no_op = not (changed or added or deleted)
        baseline = list(execution.baseline_tests_collected)
        # H-2 is only enforced when the executor actually attempted baseline
        # collection (baseline_test_exit is not None). Pre-P1-J executions
        # have baseline_test_exit=None and are not penalised for missing data.
        baseline_was_attempted = execution.baseline_test_exit is not None
        (
            deleted_tests,
            deactivated_tests,
            node_ids_all_passed,
            test_results,
            accountability_errors,
        ) = self._check_test_accountability(
            task, baseline, baseline_was_attempted, test_output, test_exit
        )
        errors.extend(accountability_errors)

        # H-1..H-9: every condition must hold for TASK_COMPLETED.
        # H-2 is conditional: only enforced when baseline collection was attempted.
        passed = (
            test_exit == 0                                                          # H-1
            and (not baseline_was_attempted or bool(baseline))                      # H-2
            and not deleted_tests                                                    # H-3
            and not deactivated_tests                                               # H-4
            and node_ids_all_passed                                                 # H-5
            and not no_op                                                           # H-6
            and not errors                                                          # H-7
            and all(v for v in acceptance.values() if v is not None)               # H-8
            and not unexpected                                                      # H-9
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
            no_op=no_op,
            baseline_tests_collected=baseline,
            deleted_tests=deleted_tests,
            deactivated_tests=deactivated_tests,
            node_ids_all_passed=node_ids_all_passed,
            test_results=test_results,
        )

    # ── Git inspection ────────────────────────────────────────────────────────

    def _get_branch(self, workspace: str, errors: list[str]) -> str:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace, capture_output=True, text=True,
            env=_worker_environment(),  # H-1: explicit curated env; never inherit parent
        )
        if result.returncode != 0:
            errors.append(f"git branch failed: {result.stderr.strip()}")
            return "UNKNOWN"
        return result.stdout.strip() or "UNKNOWN"

    def _get_status_lines(self, workspace: str, errors: list[str]) -> list[str]:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace, capture_output=True, text=True,
            env=_worker_environment(),  # H-1: explicit curated env; never inherit parent
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
            env=_worker_environment(),  # H-1: explicit curated env; never inherit parent
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

    # ── Test accountability (P1-J) ────────────────────────────────────────────

    def _check_test_accountability(
        self,
        task: Task,
        baseline: list[str],
        baseline_was_attempted: bool,
        test_output: str,
        test_exit: int | None,
    ) -> tuple[list[str], list[str], bool, dict[str, str], list[str]]:
        """
        Verify test integrity against the baseline captured before Claude ran.

        Returns:
            (deleted_tests, deactivated_tests, node_ids_all_passed,
             test_results, new_errors)

        Fail-closed: if output cannot be parsed (parse_test_results returns
        None), node_ids_all_passed is False and the error is appended.

        Scope: only tests within the required_tests specifier scope are tracked.
        Tests outside this scope may appear or disappear without consequence here.
        """
        new_errors: list[str] = []

        if not baseline_was_attempted or not baseline or not task.required_tests:
            # No baseline to compare against — cannot detect deletions.
            # node_ids_all_passed is True here because there are no required
            # node-ID specifiers to check (file-level only, or no tests at all).
            return [], [], True, {}, new_errors

        # P1-D: validate specifiers before using them.
        try:
            validate_required_tests(task.required_tests)
        except InvalidTestSpecifierError as exc:
            new_errors.append(f"Invalid test specifier in required_tests: {exc}")
            return [], [], False, {}, new_errors

        test_results = parse_test_results(test_output)
        if test_results is None:
            new_errors.append(
                "Test output could not be parsed (conflicting outcomes or "
                "unparseable format). Fail closed."
            )
            return [], [], False, {}, new_errors

        # Separate required_tests into file-level specifiers and node-ID specifiers.
        node_id_specifiers: list[str] = [s for s in task.required_tests if "::" in s]

        # Detect deletions: baseline tests that are no longer in the test results.
        # A declaration in expected_deleted_tests is only authoritative if the test
        # was actually collected in the Relay-controlled baseline — it cannot expand
        # the protected scope or authorize deletion of tests that never existed.
        authorised_deletions = set(task.expected_deleted_tests) & set(baseline)
        deleted_tests: list[str] = [
            t for t in baseline
            if t not in test_results and t not in authorised_deletions
        ]

        # Detect de-activations: baseline tests that turned SKIPPED or XFAILED.
        deactivated_tests: list[str] = [
            t for t in baseline
            if test_results.get(t) in ("SKIPPED", "XFAILED")
        ]

        # Check that every required node-ID specifier appears as PASSED.
        # For parameterised tests, match on the base node ID as a prefix.
        node_ids_all_passed = True
        for spec in node_id_specifiers:
            if not self._node_id_passed(spec, test_results):
                node_ids_all_passed = False
                break

        # Zero parsed results with exit_code=0 is suspicious — fail closed.
        if test_exit == 0 and not test_results and test_output.strip():
            new_errors.append(
                "Test exit code was 0 but no outcome lines were parsed. "
                "Fail closed."
            )
            node_ids_all_passed = False

        return deleted_tests, deactivated_tests, node_ids_all_passed, test_results, new_errors

    def _node_id_passed(self, spec: str, test_results: dict[str, str]) -> bool:
        """
        Return True if *spec* passed.

        Exact match is tried first. For parameterised tests, ALL variants
        (e.g. spec="test_foo::test_bar" matching "test_foo::test_bar[0]",
        "test_foo::test_bar[1]") must be PASSED. A single failing variant
        causes False.
        """
        if spec in test_results:
            return test_results[spec] == "PASSED"
        prefix = spec + "["
        variants = [(k, v) for k, v in test_results.items() if k.startswith(prefix)]
        if variants:
            return all(v == "PASSED" for _, v in variants)
        return False

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
