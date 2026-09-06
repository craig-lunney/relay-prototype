from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from .audit import AuditLog
from .models import ExecutionStatus, TaskStatus
from .state import StateManager


class RecoveryEngine:
    """
    Detects and classifies interrupted or ambiguous executions.

    Relay never blindly restarts after an ambiguous state.
    If it cannot safely determine what happened, it sets RECOVERY_REQUIRED
    and waits for operator review.
    """

    def __init__(self, state: StateManager, audit: AuditLog) -> None:
        self._state = state
        self._audit = audit

    def scan(self) -> list[str]:
        """
        Scan all active leases for expired or dead workers.
        Returns task_ids that now require recovery.
        """
        now = datetime.now(timezone.utc)
        requiring_recovery: list[str] = []

        for lease in self._state.get_all_active_leases():
            task = self._state.get_task(lease.task_id)
            if task is None:
                continue

            lease_expired = now > lease.expires_at
            worker_gone = not self._worker_is_alive(lease)

            if lease_expired or worker_gone:
                reason = (
                    f"Lease {lease.execution_id} expired at {lease.expires_at.isoformat()}."
                    if lease_expired
                    else f"Worker PID {lease.pid} is no longer alive."
                )
                self._transition_to_recovery(
                    lease.task_id, lease.execution_id, reason
                )
                requiring_recovery.append(lease.task_id)

        return requiring_recovery

    def classify_execution(self, execution_id: str) -> str:
        """
        Classify what happened to an execution.
        Returns one of: COMPLETED | FAILED | TIMEOUT | CANCELLED | INTERRUPTED | UNKNOWN
        """
        execution = self._state.get_execution(execution_id)
        if execution is None:
            return "UNKNOWN"

        return {
            ExecutionStatus.COMPLETED:    "COMPLETED",
            ExecutionStatus.FAILED:       "FAILED",
            ExecutionStatus.TIMEOUT:      "TIMEOUT",
            ExecutionStatus.CANCELLED:    "CANCELLED",
            ExecutionStatus.INTERRUPTED:  "INTERRUPTED",
            ExecutionStatus.PENDING:      "UNKNOWN",
            ExecutionStatus.RUNNING:      "INTERRUPTED",  # still RUNNING when we classify = interrupted
        }.get(execution.status, "UNKNOWN")

    def can_safely_restart(self, task_id: str) -> tuple[bool, str]:
        """
        Determine whether it is safe to reset a RECOVERY_REQUIRED task to READY.
        Returns (can_restart, reason).
        """
        task = self._state.get_task(task_id)
        if task is None:
            return False, "Task not found."

        if task.status != TaskStatus.RECOVERY_REQUIRED:
            return False, (
                f"Task is {task.status.value}, not RECOVERY_REQUIRED. "
                "Only RECOVERY_REQUIRED tasks can be restarted via this path."
            )

        if task.attempt_count >= task.max_execution_attempts:
            return False, (
                f"Task has used all {task.max_execution_attempts} allowed attempts. "
                "Manual intervention required."
            )

        # Ensure no active lease is still outstanding
        lease = self._state.get_active_lease(task.repository)
        if lease:
            return False, (
                f"Active lease {lease.execution_id} still exists on the workspace. "
                "Cannot restart safely."
            )

        return True, "Safe to reset to READY."

    # ── Internal ──────────────────────────────────────────────────────────────

    def _worker_is_alive(self, lease) -> bool:
        if lease.pid is None:
            return False
        if sys.platform == "win32":
            return self._pid_exists_windows(lease.pid)
        try:
            os.kill(lease.pid, 0)
            return True
        except (OSError, ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def _pid_exists_windows(pid: int) -> bool:
        import subprocess as sp
        try:
            result = sp.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return f'"{pid}"' in result.stdout
        except Exception:
            return False

    def _transition_to_recovery(
        self, task_id: str, execution_id: str, reason: str
    ) -> None:
        execution = self._state.get_execution(execution_id)
        if execution and execution.status == ExecutionStatus.RUNNING:
            execution.status = ExecutionStatus.INTERRUPTED
            execution.error = reason
            execution.ended_at = datetime.now(timezone.utc)
            self._state.update_execution(execution)

        self._state.release_lease(execution_id)

        task = self._state.get_task(task_id)
        if task and task.status == TaskStatus.RUNNING:
            self._state.update_task_status(
                task_id,
                TaskStatus.RECOVERY_REQUIRED,
                error=reason,
                validate_transition=True,
            )
            self._audit.recovery_required(task_id, execution_id, reason)
