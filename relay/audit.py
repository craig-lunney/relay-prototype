from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .models import AuditEvent, AuditEventType, Task
from .state import StateManager

# Only Relay writes authoritative audit events. Workers have no path to this module.
_RELAY_ACTOR = "relay"


class AuditLog:
    """
    Append-only audit log. All events are written by Relay, never by the worker.
    Workers cannot call this directly — they return output that Relay then audits.
    """

    def __init__(self, state: StateManager) -> None:
        self._state = state

    def _emit(
        self,
        event_type: AuditEventType,
        task_id: str,
        details: dict[str, Any],
        execution_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=AuditEvent.new_id(),
            event_type=event_type,
            task_id=task_id,
            execution_id=execution_id,
            timestamp=datetime.now(timezone.utc),
            actor=_RELAY_ACTOR,
            details=details,
        )
        self._state.append_event(event)
        return event

    def task_created(self, task_id: str, project_id: str, repository: str) -> AuditEvent:
        return self._emit(AuditEventType.TASK_CREATED, task_id, {
            "project_id": project_id,
            "repository": repository,
        })

    def task_created_with_hash(self, task: Task) -> AuditEvent:
        """P1-J: emit TASK_CREATED with a content_hash binding task content to its ID.

        content_hash is SHA-256 of the JSON-serialised content fields in
        canonical (sorted-key) form. It binds task identity to content at
        creation time for audit trail integrity. It is not a security control —
        the DB is the authoritative source; the hash is diagnostic evidence.
        """
        content = {
            "objective": task.objective,
            "requirements": task.requirements,
            "acceptance_criteria": task.acceptance_criteria,
            "requested_actions": task.requested_actions,
            "forbidden_actions": task.forbidden_actions,
            "required_tests": task.required_tests,
            "expected_deleted_tests": task.expected_deleted_tests,
            "base_ref": task.base_ref,
        }
        blob = json.dumps(content, sort_keys=True, ensure_ascii=True)
        content_hash = hashlib.sha256(blob.encode()).hexdigest()
        return self._emit(AuditEventType.TASK_CREATED, task.task_id, {
            "project_id": task.project_id,
            "repository": task.repository,
            "content_hash": content_hash,
        })

    def task_validated(self, task_id: str) -> AuditEvent:
        return self._emit(AuditEventType.TASK_VALIDATED, task_id, {})

    def task_rejected(self, task_id: str, reason: str) -> AuditEvent:
        return self._emit(AuditEventType.TASK_REJECTED, task_id, {"reason": reason})

    def policy_checked(
        self,
        task_id: str,
        allowed: bool,
        reason: str,
        execution_id: str | None = None,
    ) -> AuditEvent:
        return self._emit(AuditEventType.POLICY_CHECKED, task_id, {
            "allowed": allowed,
            "reason": reason,
        }, execution_id=execution_id)

    def task_approval_required(self, task_id: str) -> AuditEvent:
        return self._emit(AuditEventType.TASK_APPROVAL_REQUIRED, task_id, {})

    def task_approved(
        self,
        task_id: str,
        approval_id: str,
        approver: str,
        reason: str | None = None,
    ) -> AuditEvent:
        return self._emit(AuditEventType.TASK_APPROVED, task_id, {
            "approval_id": approval_id,
            "approver": approver,
            "reason": reason,
        })

    def task_approval_rejected(
        self,
        task_id: str,
        approval_id: str,
        approver: str,
        reason: str | None = None,
    ) -> AuditEvent:
        return self._emit(AuditEventType.TASK_APPROVAL_REJECTED, task_id, {
            "approval_id": approval_id,
            "approver": approver,
            "reason": reason,
        })

    def execution_started(
        self, task_id: str, execution_id: str, workspace: str, attempt: int
    ) -> AuditEvent:
        return self._emit(AuditEventType.EXECUTION_STARTED, task_id, {
            "workspace": workspace,
            "attempt": attempt,
        }, execution_id=execution_id)

    def execution_completed(
        self, task_id: str, execution_id: str, exit_code: int
    ) -> AuditEvent:
        return self._emit(AuditEventType.EXECUTION_COMPLETED, task_id, {
            "exit_code": exit_code,
        }, execution_id=execution_id)

    def execution_failed(
        self, task_id: str, execution_id: str, error: str
    ) -> AuditEvent:
        return self._emit(AuditEventType.EXECUTION_FAILED, task_id, {
            "error": error,
        }, execution_id=execution_id)

    def execution_timeout(
        self, task_id: str, execution_id: str, timeout_seconds: int
    ) -> AuditEvent:
        return self._emit(AuditEventType.EXECUTION_TIMEOUT, task_id, {
            "timeout_seconds": timeout_seconds,
        }, execution_id=execution_id)

    def execution_blocked(
        self, task_id: str, execution_id: str, reason: str
    ) -> AuditEvent:
        return self._emit(AuditEventType.EXECUTION_BLOCKED, task_id, {
            "reason": reason,
        }, execution_id=execution_id)

    def verification_started(self, task_id: str, execution_id: str) -> AuditEvent:
        return self._emit(
            AuditEventType.VERIFICATION_STARTED, task_id, {}, execution_id=execution_id
        )

    def verification_completed(
        self, task_id: str, execution_id: str, passed: bool, details: dict[str, Any]
    ) -> AuditEvent:
        return self._emit(AuditEventType.VERIFICATION_COMPLETED, task_id, {
            "passed": passed,
            **details,
        }, execution_id=execution_id)

    def verification_failed(
        self, task_id: str, execution_id: str, reason: str
    ) -> AuditEvent:
        return self._emit(AuditEventType.VERIFICATION_FAILED, task_id, {
            "reason": reason,
        }, execution_id=execution_id)

    def task_completed(self, task_id: str) -> AuditEvent:
        return self._emit(AuditEventType.TASK_COMPLETED, task_id, {})

    def acceptance_required(self, task_id: str, criteria: list[str]) -> AuditEvent:
        """Batch 1 (D5): verification passed; UNVERIFIED criteria await a human."""
        return self._emit(AuditEventType.ACCEPTANCE_REQUIRED, task_id, {
            "unverified_criteria": criteria,
        })

    def task_failed(self, task_id: str, reason: str) -> AuditEvent:
        return self._emit(AuditEventType.TASK_FAILED, task_id, {"reason": reason})

    def task_cancelled(self, task_id: str, reason: str) -> AuditEvent:
        return self._emit(AuditEventType.TASK_CANCELLED, task_id, {"reason": reason})

    def recovery_required(
        self, task_id: str, execution_id: str, reason: str
    ) -> AuditEvent:
        return self._emit(AuditEventType.RECOVERY_REQUIRED, task_id, {
            "reason": reason,
        }, execution_id=execution_id)
