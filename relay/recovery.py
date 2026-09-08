from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from .audit import AuditLog
from .models import ExecutionStatus, TaskStatus
from .state import StateManager

# TaskStatus values that represent "work is actively in progress" and therefore
# require operator-reviewed recovery when a stale lease is detected.
_RECOVERABLE_STATUSES = frozenset({TaskStatus.RUNNING, TaskStatus.VERIFYING})


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
        """Scan all active leases for stale executions (expired lease or dead worker PID).

        Returns task_ids that transitioned to RECOVERY_REQUIRED during this scan.

        For each stale lease, delegates to StateManager.mark_stale_lease_atomic(),
        which performs all database changes in a single transaction:
          - Marks the execution INTERRUPTED (if still RUNNING).
          - Releases the workspace lease.
          - Transitions RUNNING|VERIFYING tasks to RECOVERY_REQUIRED.
          - Emits a RECOVERY_SCAN_STALE_LEASE audit event.

        READY tasks with stale leases have their leases released but their status
        is preserved; they are not included in the returned list.

        The scan is idempotent: once a lease is released, get_all_active_leases()
        will not return it again, so repeated scans do not produce duplicate
        state transitions or duplicate authority events.
        """
        now = datetime.now(timezone.utc)
        requiring_recovery: list[str] = []

        for lease in self._state.get_all_active_leases():
            task = self._state.get_task(lease.task_id)
            if task is None:
                continue

            lease_expired = now > lease.expires_at
            worker_gone = not self._worker_is_alive(lease)

            if not (lease_expired or worker_gone):
                continue  # Live worker with a valid lease — not stale.

            # Record whether this task requires recovery *before* the atomic
            # operation, so the return value reflects the pre-scan state.
            # mark_stale_lease_atomic() uses CAS guards and is idempotent on
            # tasks that are not RUNNING|VERIFYING.
            was_recoverable = task.status in _RECOVERABLE_STATUSES

            self._state.mark_stale_lease_atomic(lease.execution_id)

            if was_recoverable:
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

        # P1-C: check for active leases on the task's actual workspace path(s).
        # Pre-fix bug: this called get_active_lease(task.repository), which is the
        # SOURCE repository path — never a lease workspace (leases are issued on
        # workspace copies, not the source repo). That check always returned None,
        # making the guard ineffective.
        leases = self._state.get_active_leases_for_task(task_id)
        if leases:
            return False, (
                f"Active lease {leases[0].execution_id} still exists on workspace "
                f"{leases[0].workspace!r}. Cannot restart safely."
            )

        return True, "Safe to reset to READY."

    # ── Internal helpers ──────────────────────────────────────────────────────

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
