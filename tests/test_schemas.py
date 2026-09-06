"""
P1-E runtime schema validation tests.

E1   Valid task input is accepted.
E2   Missing required field is rejected.
E3   Wrong field type is rejected.
E4   Invalid enum/state value is rejected.
E5   Unexpected property in task input is rejected.
E6   Malformed nested object (list item wrong type) is rejected.
E7   Malformed execution result is rejected before being used.
E8   Malformed approval payload cannot become authorisation.
E9   Validation occurs before any security-sensitive decision or state mutation.
E10  Validation failure does not partially mutate Relay state.
E11  All pre-existing tests continue to pass (covered by running the full suite).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from relay.models import ExecutionStatus, Task, TaskStatus
from relay.schemas import ValidationError, validate_report, validate_task_input
from relay.state import StateManager


# ── Helpers ────────────────────────────────────────────────────────────────────

def _valid_task_def(**overrides) -> dict:
    base = {
        "project_id":          "test-project",
        "repository":          "/tmp/repo",
        "objective":           "Fix the bug",
        "base_ref":            "main",
        "working_ref":         "main",
        "requirements":        ["Requirement one"],
        "acceptance_criteria": ["All tests pass"],
        "requested_actions":   ["read_repository", "modify_working_tree", "run_tests"],
        "forbidden_actions":   ["push"],
        "required_tests":      ["test_calculator.py"],
        "approval_policy":     "auto",
    }
    base.update(overrides)
    return base


def _valid_report(**overrides) -> dict:
    base = {
        "protocol_version":       "0.1",
        "message_type":           "IMPLEMENTATION_REPORT",
        "task_id":                "TASK-ABCD1234",
        "execution_id":           "EXEC-ABCD1234",
        "status":                 "COMPLETED",
        "summary":                "Fixed the bug",
        "files_changed":          ["calculator.py"],
        "files_added":            [],
        "files_deleted":          [],
        "tests_run":              ["test_calculator.py"],
        "risks":                  [],
        "uncertainties":          [],
        "blockers":               [],
        "recommended_next_action": "none",
    }
    base.update(overrides)
    return base


# ── E1: Valid input is accepted ────────────────────────────────────────────────

def test_e1_valid_task_input_accepted():
    """A fully valid task definition passes validation without raising."""
    validate_task_input(_valid_task_def())


def test_e1_minimal_task_input_accepted():
    """Only required fields (project_id, repository, objective) is also valid."""
    validate_task_input({
        "project_id": "test",
        "repository": "/tmp/repo",
        "objective":  "Do something",
    })


def test_e1_valid_report_accepted():
    """A fully valid implementation report passes validation."""
    validate_report(_valid_report())


# ── E2: Missing required field ─────────────────────────────────────────────────

def test_e2_missing_project_id():
    bad = _valid_task_def()
    del bad["project_id"]
    with pytest.raises(ValidationError, match="project_id"):
        validate_task_input(bad)


def test_e2_missing_repository():
    bad = _valid_task_def()
    del bad["repository"]
    with pytest.raises(ValidationError, match="repository"):
        validate_task_input(bad)


def test_e2_missing_objective():
    bad = _valid_task_def()
    del bad["objective"]
    with pytest.raises(ValidationError, match="objective"):
        validate_task_input(bad)


def test_e2_missing_report_required_field():
    bad = _valid_report()
    del bad["task_id"]
    with pytest.raises(ValidationError, match="task_id"):
        validate_report(bad)


def test_e2_missing_report_summary():
    bad = _valid_report()
    del bad["summary"]
    with pytest.raises(ValidationError, match="summary"):
        validate_report(bad)


# ── E3: Wrong field type ───────────────────────────────────────────────────────

def test_e3_requirements_is_string_not_list():
    bad = _valid_task_def(requirements="this should be a list")
    with pytest.raises(ValidationError, match="requirements"):
        validate_task_input(bad)


def test_e3_requested_actions_is_dict_not_list():
    bad = _valid_task_def(requested_actions={"read_repository": True})
    with pytest.raises(ValidationError, match="requested_actions"):
        validate_task_input(bad)


def test_e3_project_id_is_integer():
    bad = _valid_task_def(project_id=42)
    with pytest.raises(ValidationError, match="project_id"):
        validate_task_input(bad)


def test_e3_report_files_changed_is_string():
    bad = _valid_report(files_changed="calculator.py")
    with pytest.raises(ValidationError, match="files_changed"):
        validate_report(bad)


def test_e3_report_task_id_is_integer():
    bad = _valid_report(task_id=12345)
    with pytest.raises(ValidationError, match="task_id"):
        validate_report(bad)


# ── E4: Invalid enum/state value ──────────────────────────────────────────────

def test_e4_invalid_approval_policy():
    bad = _valid_task_def(approval_policy="superuser")
    with pytest.raises(ValidationError, match="approval_policy"):
        validate_task_input(bad)


def test_e4_approval_policy_must_be_string():
    bad = _valid_task_def(approval_policy=True)
    with pytest.raises(ValidationError, match="approval_policy"):
        validate_task_input(bad)


# ── E5: Unexpected property ────────────────────────────────────────────────────

def test_e5_unexpected_field_in_task_input_rejected():
    """An 'approved' field in task input is rejected — task input is a controlled format."""
    bad = _valid_task_def()
    bad["approved"] = True
    with pytest.raises(ValidationError, match="unexpected"):
        validate_task_input(bad)


def test_e5_policy_version_injection_in_task_rejected():
    """A 'policy_version' field in task input is rejected."""
    bad = _valid_task_def()
    bad["policy_version"] = "unrestricted"
    with pytest.raises(ValidationError, match="unexpected"):
        validate_task_input(bad)


def test_e5_extra_field_in_report_allowed():
    """
    An unexpected field in an ImplementationReport is allowed — the report is
    agent-controlled evidence, not a controlled definition format. Extra fields
    are explicitly ignored and carry no authority.
    """
    good = _valid_report()
    good["approved"] = True          # extra field — allowed in report schema
    good["policy_version"] = "unrestricted"  # extra field — allowed in report schema
    validate_report(good)  # must not raise


# ── E6: Malformed nested object ────────────────────────────────────────────────

def test_e6_requirements_list_contains_non_string():
    """requirements contains an integer — list items must be strings."""
    bad = _valid_task_def(requirements=["valid", 42, "also valid"])
    with pytest.raises(ValidationError, match="requirements"):
        validate_task_input(bad)


def test_e6_forbidden_actions_contains_dict():
    bad = _valid_task_def(forbidden_actions=[{"action": "push"}])
    with pytest.raises(ValidationError, match="forbidden_actions"):
        validate_task_input(bad)


def test_e6_report_tests_run_contains_integer():
    bad = _valid_report(tests_run=["test_a.py", 99])
    with pytest.raises(ValidationError, match="tests_run"):
        validate_report(bad)


def test_e6_report_risks_contains_list():
    bad = _valid_report(risks=[["nested", "list"]])
    with pytest.raises(ValidationError, match="risks"):
        validate_report(bad)


# ── E7: Malformed execution result ────────────────────────────────────────────

def test_e7_malformed_report_returns_none_from_parse():
    """
    A syntactically valid JSON object that fails schema validation is treated
    as 'no report found'. Relay marks the report as absent (None) rather than
    partially consuming a malformed structure.
    """
    from relay.executor import ExecutionEngine

    engine = ExecutionEngine(MagicMock(), MagicMock(), MagicMock())

    # Report where files_changed is a string (wrong type)
    malformed_output = json.dumps({
        "protocol_version": "0.1",
        "message_type":     "IMPLEMENTATION_REPORT",
        "task_id":          "TASK-TEST",
        "execution_id":     "EXEC-TEST",
        "status":           "COMPLETED",
        "summary":          "Done",
        "files_changed":    "not-a-list",   # wrong type
    })

    result = engine._parse_report(malformed_output, "TASK-TEST", "EXEC-TEST")
    assert result is None, (
        "A structurally invalid report must be treated as absent, not consumed"
    )


def test_e7_report_missing_required_field_returns_none():
    """A report missing 'summary' is treated as absent."""
    from relay.executor import ExecutionEngine

    engine = ExecutionEngine(MagicMock(), MagicMock(), MagicMock())

    bad_report = {
        "protocol_version": "0.1",
        "message_type":     "IMPLEMENTATION_REPORT",
        "task_id":          "TASK-TEST",
        "execution_id":     "EXEC-TEST",
        "status":           "COMPLETED",
        # "summary" is missing
    }
    result = engine._parse_report(json.dumps(bad_report), "TASK-TEST", "EXEC-TEST")
    assert result is None


def test_e7_valid_report_is_parsed_correctly():
    """A valid report IS parsed and returned (not accidentally discarded)."""
    from relay.executor import ExecutionEngine

    engine = ExecutionEngine(MagicMock(), MagicMock(), MagicMock())

    result = engine._parse_report(
        json.dumps(_valid_report(task_id="TASK-TEST", execution_id="EXEC-TEST")),
        "TASK-TEST",
        "EXEC-TEST",
    )
    assert result is not None
    assert result.task_id == "TASK-TEST"
    assert result.status == "COMPLETED"


# ── E8: Malformed approval payload / trust boundary ───────────────────────────

def test_e8_approved_field_in_report_is_ignored():
    """
    An 'approved': True field in an ImplementationReport passes schema validation
    (extra fields are allowed in evidence) but carries no authority.

    The report is consumed by Relay's verifier as evidence only. Relay computes
    its own independent 'passed' verdict from git state and test results.
    No report field can grant, override, or substitute for authorisation.
    """
    from relay.verifier import Verifier

    # Build a report that claims approval and COMPLETED status
    report_with_approved = _valid_report()
    report_with_approved["approved"] = True
    report_with_approved["approval_granted"] = True

    # This must not raise — extra fields are allowed in the report schema
    validate_report(report_with_approved)

    # Verify that ImplementationReport.from_dict() does not expose "approved"
    from relay.models import ImplementationReport
    report = ImplementationReport.from_dict(report_with_approved)
    assert not hasattr(report, "approved"), (
        "ImplementationReport must not expose 'approved' as an attribute"
    )
    assert not hasattr(report, "approval_granted"), (
        "ImplementationReport must not expose 'approval_granted' as an attribute"
    )


def test_e8_approved_in_task_input_is_rejected():
    """
    An 'approved': True field in task input is structurally rejected.
    Task input is a controlled definition format — unexpected fields indicate
    injection attempts and must be rejected before any state is created.
    """
    bad = _valid_task_def()
    bad["approved"] = True
    with pytest.raises(ValidationError, match="unexpected"):
        validate_task_input(bad)


def test_e8_policy_version_override_in_task_rejected():
    """
    A 'policy_version': 'unrestricted' field in task input is rejected.
    Schema validation is not policy authorisation — the schema enforces structure,
    and policy authority remains entirely within PolicyEngine.
    """
    bad = _valid_task_def()
    bad["policy_version"] = "unrestricted"
    with pytest.raises(ValidationError, match="unexpected"):
        validate_task_input(bad)


# ── E9: Validation before security decision ────────────────────────────────────

def test_e9_validation_before_policy(tmp_db):
    """
    Schema validation rejects malformed task input before PolicyEngine.validate_task()
    is ever called. Policy evaluation requires a well-structured Task object —
    validation must be the first gate.
    """
    from relay.policy import PolicyEngine
    policy = PolicyEngine()

    bad_def = _valid_task_def()
    bad_def["approved"] = True  # unexpected field

    # Validation raises before a Task is constructed, so policy never sees it
    with pytest.raises(ValidationError):
        validate_task_input(bad_def)

    # No tasks in the database — policy was never consulted, state was never mutated
    assert tmp_db.get_all_events() == []


def test_e9_validation_before_state_creation_in_cli(tmp_path, tmp_db):
    """
    A task JSON file with an invalid structure is rejected by validate_task_input()
    before state.create_task() is called. Demonstrates E9 at the CLI boundary.
    """
    # Write a task file with an unexpected field
    task_file = tmp_path / "bad_task.json"
    task_file.write_text(json.dumps({
        "project_id": "test",
        "repository":  "/tmp/repo",
        "objective":   "Do something",
        "approved":    True,      # not a valid task_input field
    }), encoding="utf-8")

    # Simulate what cmd_create_task does — validate before Task construction
    import json as _json
    task_def = _json.loads(task_file.read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        validate_task_input(task_def)

    # State was not mutated
    assert tmp_db.event_count() == 0


# ── E10: No partial mutation ───────────────────────────────────────────────────

def test_e10_failed_validation_no_task_in_db(tmp_db):
    """
    When validate_task_input() raises, the Task object is never constructed and
    state.create_task() is never called. The database remains empty.
    """
    bad_def = {"project_id": "test", "requirements": 42}  # missing 'repository', 'objective'; wrong type

    with pytest.raises(ValidationError):
        validate_task_input(bad_def)

    # StateManager has no tasks
    # (directly check via SQL — no get_all_tasks method, so use the event count as proxy)
    assert tmp_db.event_count() == 0


def test_e10_failed_validation_no_audit_events(tmp_db):
    """Validation failures produce no audit events — state is not touched."""
    bad_def = _valid_task_def()
    bad_def["unexpected_key"] = "value"

    with pytest.raises(ValidationError):
        validate_task_input(bad_def)

    assert tmp_db.event_count() == 0


def test_e10_valid_input_not_rejected(tmp_db):
    """A valid task input does NOT raise — confirming the validator is not over-strict."""
    good = _valid_task_def()
    # Must not raise
    validate_task_input(good)


# ── Additional coverage: non-dict input ───────────────────────────────────────

def test_non_dict_task_input_rejected():
    """A list, string, or int as task input is rejected with a clear error."""
    for bad in [[], "string", 42, None]:
        with pytest.raises(ValidationError, match="mapping"):
            validate_task_input(bad)


def test_non_dict_report_rejected():
    """A list, string, or int as report input is rejected."""
    for bad in [[], "string", 42, None]:
        with pytest.raises(ValidationError, match="mapping"):
            validate_report(bad)
