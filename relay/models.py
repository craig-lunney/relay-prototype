from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    # Batch 1 (D5): automated verification passed, but one or more acceptance
    # criteria are UNVERIFIED and await explicit human acknowledgement.
    ACCEPTANCE_REQUIRED = "ACCEPTANCE_REQUIRED"


class ExecutionStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class AuditEventType(str, Enum):
    TASK_CREATED = "TASK_CREATED"
    TASK_VALIDATED = "TASK_VALIDATED"
    TASK_REJECTED = "TASK_REJECTED"
    POLICY_CHECKED = "POLICY_CHECKED"
    TASK_APPROVAL_REQUIRED = "TASK_APPROVAL_REQUIRED"
    TASK_APPROVED = "TASK_APPROVED"
    TASK_APPROVAL_REJECTED = "TASK_APPROVAL_REJECTED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    EXECUTION_BLOCKED = "EXECUTION_BLOCKED"
    VERIFICATION_STARTED = "VERIFICATION_STARTED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERY_ACCEPTED = "RECOVERY_ACCEPTED"
    TASK_RETRIED = "TASK_RETRIED"
    RECOVERY_SCAN_STALE_LEASE = "RECOVERY_SCAN_STALE_LEASE"
    ACCEPTANCE_REQUIRED = "ACCEPTANCE_REQUIRED"
    ACCEPTANCE_ACKNOWLEDGED = "ACCEPTANCE_ACKNOWLEDGED"
    ACCEPTANCE_REJECTED = "ACCEPTANCE_REJECTED"


# Relay owns all state transitions. Agents cannot declare their own status.
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED:           frozenset({TaskStatus.VALIDATING, TaskStatus.CANCELLED}),
    TaskStatus.VALIDATING:        frozenset({TaskStatus.READY, TaskStatus.REJECTED, TaskStatus.BLOCKED}),
    TaskStatus.READY:             frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED,
                                             TaskStatus.FAILED}),
    TaskStatus.RUNNING:           frozenset({TaskStatus.VERIFYING, TaskStatus.FAILED,
                                             TaskStatus.TIMEOUT, TaskStatus.RECOVERY_REQUIRED}),
    TaskStatus.VERIFYING:         frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED,
                                             TaskStatus.RECOVERY_REQUIRED,
                                             TaskStatus.ACCEPTANCE_REQUIRED}),
    TaskStatus.COMPLETED:         frozenset(),
    TaskStatus.FAILED:            frozenset(),
    TaskStatus.BLOCKED:           frozenset({TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.REJECTED}),
    TaskStatus.TIMEOUT:           frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.CANCELLED:         frozenset(),
    TaskStatus.REJECTED:          frozenset(),
    TaskStatus.INPUT_REQUIRED:    frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.RECOVERY_REQUIRED: frozenset({TaskStatus.READY, TaskStatus.CANCELLED,
                                             TaskStatus.FAILED}),
    # Left only by an explicit human decision (relay acknowledge-criteria).
    TaskStatus.ACCEPTANCE_REQUIRED: frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED,
                                               TaskStatus.CANCELLED}),
}


@dataclass
class Task:
    task_id: str
    project_id: str
    repository: str
    base_ref: str
    working_ref: str
    objective: str
    requirements: list[str]
    acceptance_criteria: list[str]
    requested_actions: list[str]
    forbidden_actions: list[str]
    required_tests: list[str]
    approval_policy: str
    # P1-J: authorised deletions within required_tests scope (operator-declared).
    # Prevents false-failure when a task legitimately removes a test.
    expected_deleted_tests: list[str] = field(default_factory=list)
    # Batch 1: workspace-relative paths the worker is expected to create.
    # Any other new (non-cache) file fails verification.  This cannot authorise
    # protected additions such as conftest.py or pytest configuration.
    expected_new_files: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.CREATED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    protocol_version: str = "0.1"
    max_execution_attempts: int = 3
    attempt_count: int = 0
    error: str | None = None

    @classmethod
    def new_id(cls) -> str:
        return f"TASK-{uuid.uuid4().hex[:8].upper()}"


@dataclass
class ExecutionLease:
    execution_id: str
    task_id: str
    workspace: str
    pid: int | None
    started_at: datetime
    expires_at: datetime
    worker_identity: str


@dataclass
class Execution:
    execution_id: str
    task_id: str
    attempt_number: int
    status: ExecutionStatus
    workspace: str
    worker_identity: str
    policy_version: str
    capability_snapshot: list[str]
    started_at: datetime
    ended_at: datetime | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    report: ImplementationReport | None = None
    # P1-J: baseline test IDs collected before Claude ran (--collect-only -q).
    baseline_tests_collected: list[str] = field(default_factory=list)
    baseline_test_exit: int | None = None
    baseline_test_output: str = ""
    # Batch 1: Relay-captured workspace integrity manifest (WorkspaceSnapshot
    # JSON), taken after baseline collection and before the worker launches.
    # baseline_manifest_error records a capture failure so verification can
    # fail closed rather than silently skip the integrity check.
    baseline_manifest: str | None = None
    baseline_manifest_error: str | None = None
    # Batch 3: runtime-only flags (not persisted), consumed by cmd_run_task.
    # ownership_denied: this attempt never owned the task — the workspace lease
    #   was held by another execution, or the task stopped being READY before
    #   this attempt could claim it.  The CLI must change nothing.
    # task_claimed: this attempt moved the task READY → RUNNING, so a later
    #   pre-launch failure transitions it from RUNNING (otherwise from READY).
    ownership_denied: bool = False
    task_claimed: bool = False

    @classmethod
    def new_id(cls) -> str:
        return f"EXEC-{uuid.uuid4().hex[:8].upper()}"


@dataclass
class ImplementationReport:
    """
    Claude's structured output. Treated as evidence, not authoritative truth.
    Relay independently verifies all claims before accepting any result.
    """
    protocol_version: str
    message_type: str
    task_id: str
    execution_id: str
    status: str
    summary: str
    files_changed: list[str]
    files_added: list[str]
    files_deleted: list[str]
    tests_run: list[str]
    risks: list[str]
    uncertainties: list[str]
    blockers: list[str]
    recommended_next_action: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ImplementationReport:
        return cls(
            protocol_version=str(d.get("protocol_version", "0.1")),
            message_type=str(d.get("message_type", "IMPLEMENTATION_REPORT")),
            task_id=str(d.get("task_id", "")),
            execution_id=str(d.get("execution_id", "")),
            status=str(d.get("status", "UNKNOWN")),
            summary=str(d.get("summary", "")),
            files_changed=list(d.get("files_changed", [])),
            files_added=list(d.get("files_added", [])),
            files_deleted=list(d.get("files_deleted", [])),
            tests_run=list(d.get("tests_run", [])),
            risks=list(d.get("risks", [])),
            uncertainties=list(d.get("uncertainties", [])),
            blockers=list(d.get("blockers", [])),
            recommended_next_action=str(d.get("recommended_next_action", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "message_type": self.message_type,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "status": self.status,
            "summary": self.summary,
            "files_changed": self.files_changed,
            "files_added": self.files_added,
            "files_deleted": self.files_deleted,
            "tests_run": self.tests_run,
            "risks": self.risks,
            "uncertainties": self.uncertainties,
            "blockers": self.blockers,
            "recommended_next_action": self.recommended_next_action,
        }


@dataclass
class AuditEvent:
    event_id: str
    event_type: AuditEventType
    task_id: str
    execution_id: str | None
    timestamp: datetime
    actor: str
    details: dict[str, Any]
    sequence: int = 0

    @classmethod
    def new_id(cls) -> str:
        return f"EVT-{uuid.uuid4().hex[:8].upper()}"


@dataclass
class Approval:
    """
    A Relay-recorded human approval or rejection for a manual-approval task.

    is_human=True is set ONLY by the Relay operator CLI (approve-task / reject-task
    commands). It is never set from task JSON, repository content, or agent output.
    The security boundary is the code path that calls record_approval() with is_human=True,
    not the string value of the approver field (which is not authenticated in this prototype).
    """
    approval_id: str
    task_id: str
    approver: str
    decision: str  # "APPROVED" or "REJECTED"
    reason: str | None
    timestamp: datetime
    is_human: bool

    @classmethod
    def new_id(cls) -> str:
        return f"APR-{uuid.uuid4().hex[:8].upper()}"
