"""Tests for Relay data models."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from relay.models import (
    ALLOWED_TRANSITIONS,
    AuditEvent,
    AuditEventType,
    Execution,
    ExecutionStatus,
    ImplementationReport,
    Task,
    TaskStatus,
)


def test_task_new_id_format():
    tid = Task.new_id()
    assert tid.startswith("TASK-")
    assert len(tid) == 13  # TASK- + 8 hex chars


def test_execution_new_id_format():
    eid = Execution.new_id()
    assert eid.startswith("EXEC-")
    assert len(eid) == 13


def test_audit_event_new_id_format():
    evid = AuditEvent.new_id()
    assert evid.startswith("EVT-")


def test_task_default_status():
    task = Task(
        task_id="TASK-TEST0001",
        project_id="p",
        repository="/tmp/repo",
        base_ref="main",
        working_ref="main",
        objective="do something",
        requirements=[],
        acceptance_criteria=[],
        requested_actions=[],
        forbidden_actions=[],
        required_tests=[],
        approval_policy="auto",
    )
    assert task.status == TaskStatus.CREATED
    assert task.attempt_count == 0
    assert task.protocol_version == "0.1"


def test_task_status_enum_values():
    statuses = [
        "CREATED", "VALIDATING", "READY", "RUNNING", "VERIFYING",
        "COMPLETED", "FAILED", "BLOCKED", "TIMEOUT", "CANCELLED",
        "REJECTED", "INPUT_REQUIRED", "RECOVERY_REQUIRED",
    ]
    for s in statuses:
        ts = TaskStatus(s)
        assert ts.value == s


def test_allowed_transitions_terminal_states():
    terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.REJECTED}
    for state in terminal:
        assert ALLOWED_TRANSITIONS[state] == frozenset(), (
            f"{state} should be terminal (no outgoing transitions)"
        )


def test_allowed_transitions_running():
    allowed = ALLOWED_TRANSITIONS[TaskStatus.RUNNING]
    assert TaskStatus.VERIFYING in allowed
    assert TaskStatus.TIMEOUT in allowed
    assert TaskStatus.RECOVERY_REQUIRED in allowed
    assert TaskStatus.COMPLETED not in allowed  # Relay never jumps RUNNING → COMPLETED directly


def test_implementation_report_from_dict():
    d = {
        "protocol_version": "0.1",
        "message_type": "IMPLEMENTATION_REPORT",
        "task_id": "TASK-ABCD1234",
        "execution_id": "EXEC-EFGH5678",
        "status": "COMPLETED",
        "summary": "Fixed the bug",
        "files_changed": ["calculator.py"],
        "files_added": [],
        "files_deleted": [],
        "tests_run": ["test_calculator.py"],
        "risks": [],
        "uncertainties": [],
        "blockers": [],
        "recommended_next_action": "none",
    }
    report = ImplementationReport.from_dict(d)
    assert report.status == "COMPLETED"
    assert report.files_changed == ["calculator.py"]
    assert report.message_type == "IMPLEMENTATION_REPORT"


def test_implementation_report_to_dict_roundtrip():
    d = {
        "protocol_version": "0.1",
        "message_type": "IMPLEMENTATION_REPORT",
        "task_id": "TASK-ABCD1234",
        "execution_id": "EXEC-EFGH5678",
        "status": "PARTIAL",
        "summary": "Partially done",
        "files_changed": [],
        "files_added": ["new_file.py"],
        "files_deleted": [],
        "tests_run": [],
        "risks": ["something might break"],
        "uncertainties": [],
        "blockers": ["unclear requirement"],
        "recommended_next_action": "review",
    }
    report = ImplementationReport.from_dict(d)
    result = report.to_dict()
    assert result == d


def test_implementation_report_from_dict_missing_fields():
    # Must not raise even with minimal dict
    report = ImplementationReport.from_dict({"message_type": "IMPLEMENTATION_REPORT"})
    assert report.task_id == ""
    assert report.files_changed == []
    assert report.status == "UNKNOWN"


def test_audit_event_type_enum_completeness():
    required = [
        "TASK_CREATED", "TASK_VALIDATED", "TASK_REJECTED",
        "POLICY_CHECKED", "EXECUTION_STARTED", "EXECUTION_COMPLETED",
        "EXECUTION_FAILED", "EXECUTION_TIMEOUT", "EXECUTION_BLOCKED",
        "VERIFICATION_STARTED", "VERIFICATION_COMPLETED", "VERIFICATION_FAILED",
        "TASK_COMPLETED", "TASK_FAILED", "TASK_CANCELLED", "RECOVERY_REQUIRED",
    ]
    for name in required:
        assert AuditEventType(name).value == name


# ── P1-K: VERIFYING → RECOVERY_REQUIRED transition ───────────────────────────

def test_verifying_to_recovery_required_is_allowed():
    allowed = ALLOWED_TRANSITIONS[TaskStatus.VERIFYING]
    assert TaskStatus.RECOVERY_REQUIRED in allowed, (
        "VERIFYING must allow transition to RECOVERY_REQUIRED"
    )


def test_verifying_still_allows_completed_and_failed():
    allowed = ALLOWED_TRANSITIONS[TaskStatus.VERIFYING]
    assert TaskStatus.COMPLETED in allowed
    assert TaskStatus.FAILED in allowed


# ── P1-K: new audit event types ──────────────────────────────────────────────

def test_p1k_audit_event_types_exist():
    new_types = [
        "RECOVERY_ACCEPTED",
        "TASK_RETRIED",
        "RECOVERY_SCAN_STALE_LEASE",
    ]
    for name in new_types:
        evt = AuditEventType(name)
        assert evt.value == name, f"AuditEventType.{name} has wrong value: {evt.value!r}"
