from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .executor import _worker_environment
from .integrity import IntegrityReport, WorkspaceSnapshot, compare_snapshots, snapshot_workspace
from .models import Execution, Task
from .paths import (
    InvalidRelativePathError,
    InvalidTestSpecifierError,
    WorkspaceBoundaryError,
    normalize_relative_path,
    validate_required_tests,
)
from .testing import collect_tests, parse_test_results, run_test_suite

# ── Acceptance-criterion verdicts (Batch 1, D5) ───────────────────────────────
# PASS        checked by Relay and satisfied.
# FAIL        checked by Relay and not satisfied (or the check could not run
#             because verification had already failed).
# UNVERIFIED  not established by automated verification.  Needs an explicit
#             human acknowledgement before the task can be COMPLETED; the
#             acknowledgement is recorded separately and never changes this
#             verdict.
AC_PASS = "PASS"
AC_FAIL = "FAIL"
AC_UNVERIFIED = "UNVERIFIED"

# The only criteria Relay checks automatically: statements that the required
# test suite passes, matched as whole (normalised) strings.  Matching is
# deliberately exact rather than by keyword: a criterion such as "add a
# regression test for X; all tests pass" asserts more than the suite result, so
# it must be UNVERIFIED rather than marked PASS on the strength of the suite.
# Classification depends only on the operator-authored task text, never on
# worker output, so a worker cannot move a criterion from FAIL to UNVERIFIED.
_TEST_SUITE_CRITERIA = frozenset({
    "all tests pass",
    "all tests must pass",
    "all tests passing",
    "all required tests pass",
    "all required tests must pass",
    "tests pass",
    "the test suite passes",
    "test suite passes",
    "the full test suite passes",
    "full test suite passes",
    "no failing tests",
    "no test failures",
    "tests must not fail",
})


def is_automatically_verifiable(criterion: str) -> bool:
    """True if Relay checks *criterion* itself (a required-test-suite criterion)."""
    normalised = re.sub(r"\s+", " ", criterion.strip().lower()).rstrip(".")
    return normalised in _TEST_SUITE_CRITERIA


def unverified_criteria(task: Task) -> list[str]:
    """Criteria of *task* that automated verification never establishes."""
    return [c for c in dict.fromkeys(task.acceptance_criteria)
            if not is_automatically_verifiable(c)]


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
    acceptance_results: dict[str, str]
    report_agreement: dict[str, bool]
    errors: list[str]
    # P1-J: test accountability fields
    no_op: bool = False
    baseline_tests_collected: list[str] = field(default_factory=list)
    deleted_tests: list[str] = field(default_factory=list)
    deactivated_tests: list[str] = field(default_factory=list)
    node_ids_all_passed: bool = True
    test_results: dict[str, str] = field(default_factory=dict)
    # Batch 1: workspace integrity (Relay manifest comparison).
    integrity_checked: bool = False
    protected_violations: list[str] = field(default_factory=list)
    ignored_artifacts: list[str] = field(default_factory=list)
    excluded_from_test_run: list[str] = field(default_factory=list)

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
            "integrity_checked": self.integrity_checked,
            "protected_violations": self.protected_violations,
            "ignored_artifacts": self.ignored_artifacts,
            "excluded_from_test_run": self.excluded_from_test_run,
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

        # Batch 1: when Relay captured a baseline for this execution (always, on
        # the production path), change detection comes from the Relay-owned
        # integrity manifest, not from git.  The legacy git-status path remains
        # only for executions that never had a baseline captured.
        integrity_required = (
            execution.baseline_manifest is not None
            or execution.baseline_manifest_error is not None
            or execution.baseline_test_exit is not None
        )
        integrity: IntegrityReport | None = None
        if integrity_required:
            integrity = self._check_integrity(execution, workspace, errors)
            if integrity is None:
                changed, added, deleted = [], [], []
                status_lines: list[str] = []
            else:
                changed, added, deleted = integrity.changed, integrity.added, integrity.deleted
                status_lines = (
                    [f"M {f}" for f in changed]
                    + [f"A {f}" for f in added]
                    + [f"D {f}" for f in deleted]
                )
            expected = self._expected_new_files(task, expected_new_files, errors)
            unexpected = [f for f in added if f not in expected]
        else:
            status_lines = self._get_status_lines(workspace, errors)
            changed, added, deleted = self._get_changed_files(workspace, errors)
            unexpected = self._find_unexpected(added, expected_new_files or [])

        protected = list(integrity.protected_violations) if integrity else []
        if protected:
            errors.append(
                "Protected tests or test configuration were altered by the worker: "
                + "; ".join(protected)
            )

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
                    acceptance_results=self._check_acceptance(task, "", None),
                    report_agreement={},
                    errors=errors,
                    integrity_checked=integrity is not None,
                    protected_violations=protected,
                )

        # Batch 1: if integrity could not be established, or protected files were
        # altered, the outcome is already FAILED — do not execute worker-influenced
        # test code at all.  Otherwise run the suite with worker-added modules
        # excluded from collection.
        excluded = list(integrity.new_python_files) if integrity else []
        if integrity_required and (integrity is None or protected):
            test_exit, test_output = None, ""
        else:
            # P1-J: run tests via shared utility so output is consistently captured.
            test_exit, test_output, test_errors = run_test_suite(
                workspace, task.required_tests, _worker_environment(),
                ignore_paths=excluded,
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

        # H-1..H-10: every condition must hold for verification to pass.
        # H-2 is conditional: only enforced when baseline collection was attempted.
        # UNVERIFIED criteria do not fail verification; the CLI holds the task in
        # ACCEPTANCE_REQUIRED until a human acknowledges them.
        passed = (
            test_exit == 0                                                          # H-1
            and (not baseline_was_attempted or bool(baseline))                      # H-2
            and not deleted_tests                                                    # H-3
            and not deactivated_tests                                               # H-4
            and node_ids_all_passed                                                 # H-5
            and not no_op                                                           # H-6
            and not errors                                                          # H-7
            and AC_FAIL not in acceptance.values()                                  # H-8
            and not unexpected                                                      # H-9
            and not protected                                                       # H-10
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
            integrity_checked=integrity is not None,
            protected_violations=protected,
            ignored_artifacts=list(integrity.ignored_artifacts) if integrity else [],
            excluded_from_test_run=excluded,
        )

    # ── Workspace integrity (Batch 1) ─────────────────────────────────────────

    def _check_integrity(
        self, execution: Execution, workspace: str, errors: list[str]
    ) -> IntegrityReport | None:
        """Compare the workspace with the Relay-captured baseline manifest.

        Returns None (with an error appended) when no trustworthy baseline is
        available; the caller treats that as a verification failure.
        """
        if execution.baseline_manifest_error:
            errors.append(execution.baseline_manifest_error)
            return None
        if execution.baseline_manifest is None:
            errors.append(
                "No workspace integrity manifest was captured for this execution. "
                "Changes cannot be verified. Fail closed."
            )
            return None
        try:
            baseline = WorkspaceSnapshot.from_json(execution.baseline_manifest)
            current = snapshot_workspace(workspace)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"Workspace integrity check failed: {exc}. Fail closed.")
            return None
        return compare_snapshots(baseline, current, execution.baseline_tests_collected)

    def _expected_new_files(
        self, task: Task, override: list[str] | None, errors: list[str]
    ) -> set[str]:
        declared = override if override is not None else task.expected_new_files
        expected: set[str] = set()
        for path in declared:
            try:
                expected.add(normalize_relative_path(path))
            except InvalidRelativePathError as exc:
                errors.append(f"Invalid expected_new_files entry: {exc}")
        return expected

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
    ) -> dict[str, str]:
        """Return a PASS / FAIL / UNVERIFIED verdict for every acceptance criterion.

        Every criterion appears in the result; none is ever omitted.  A
        test-suite criterion is PASS only if the required tests actually ran and
        exited 0 with output; if they did not run (or failed), it is FAIL.
        """
        results: dict[str, str] = {}
        suite_passed = test_exit == 0 and bool(test_output)
        for criterion in task.acceptance_criteria:
            if is_automatically_verifiable(criterion):
                results[criterion] = AC_PASS if suite_passed else AC_FAIL
            else:
                results[criterion] = AC_UNVERIFIED
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
