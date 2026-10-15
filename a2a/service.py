from __future__ import annotations

from .models import A2AProtocolState, A2ATask, A2ATaskTier
from .state import (
    A2AAttestation,
    A2AHumanVerification,
    A2AInvalidTaskDataError,
    A2AInvalidTransitionError,
    A2AStateManager,
    A2ATierChange,
    A2ATransitionRecord,
)


class A2AInvalidTaskError(Exception):
    """Raised when task creation input fails validation.

    Attributes:
        reason: Human-readable explanation of the validation failure.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class A2ATaskNotFoundError(Exception):
    """Raised when a task_id does not exist in the state store."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"Task {task_id!r} not found.")


class A2ATransitionError(Exception):
    """Raised when a state transition is invalid or cannot be applied.

    Attributes:
        task_id:    The task that was being advanced.
        from_state: The state the task was in when the error occurred.
        to_state:   The state that was requested.
        reason:     Human-readable explanation from the state layer.
    """

    def __init__(
        self,
        task_id: str,
        from_state: A2AProtocolState,
        to_state: A2AProtocolState,
        reason: str,
    ) -> None:
        self.task_id = task_id
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason
        super().__init__(
            f"Cannot advance task {task_id!r} from {from_state.value} "
            f"to {to_state.value}: {reason}"
        )


class A2AService:
    """Controlled entry point for A2A task lifecycle operations.

    Batch 4: every protocol rule — graph, tier path, role authority, required
    evidence, Relay evidence and the T3 human re-run — is enforced by
    A2AStateManager inside the transaction that records the change, so this
    service cannot be bypassed by calling the state layer directly.  The
    service adds domain-level errors, generated IDs and a clean API.

    A2AService never accesses SQLite directly.
    """

    def __init__(self, state_manager: A2AStateManager) -> None:
        self._sm = state_manager

    def create_task(
        self,
        title: str,
        tier: A2ATaskTier,
        github_issue_url: str,
        created_by: str | None = None,
    ) -> A2ATask:
        """Create a new A2A task in DRAFT state and persist it.

        Raises A2AInvalidTaskError if the title is empty or github_issue_url is
        not a GitHub Issue URL (https://github.com/<owner>/<repo>/issues/<N>).
        """
        task = A2ATask(
            task_id=A2ATask.new_id(),
            title=title,
            tier=tier,
            github_issue_url=github_issue_url,
        )
        try:
            self._sm.create_task(task, created_by=created_by)
        except A2AInvalidTaskDataError as exc:
            raise A2AInvalidTaskError(str(exc)) from exc
        return task

    def get_task(self, task_id: str) -> A2ATask:
        """Return the task for task_id.

        Raises A2ATaskNotFoundError if no task with that ID exists.
        """
        task = self._sm.get_task(task_id)
        if task is None:
            raise A2ATaskNotFoundError(task_id)
        return task

    def list_tasks(self) -> list[A2ATask]:
        """Return all tasks ordered by creation time."""
        return self._sm.list_tasks()

    def history(self, task_id: str) -> list[A2ATransitionRecord]:
        self.get_task(task_id)
        return self._sm.get_history(task_id)

    def human_verifications(self, task_id: str) -> list[A2AHumanVerification]:
        self.get_task(task_id)
        return self._sm.get_human_verifications(task_id)

    def advance(
        self,
        task_id: str,
        new_state: A2AProtocolState,
        attestation: A2AAttestation,
    ) -> A2ATask:
        """Advance task to new_state under the protocol, recording the attestation.

        Raises A2ATaskNotFoundError if the task does not exist.
        Raises A2ATransitionError if any protocol rule rejects the step.
        """
        task = self.get_task(task_id)
        try:
            return self._sm.transition_state(task_id, new_state, attestation)
        except A2AInvalidTransitionError as exc:
            current = self._sm.get_task(task_id)
            raise A2ATransitionError(
                task_id=task_id,
                from_state=current.state if current else task.state,
                to_state=new_state,
                reason=str(exc),
            ) from exc

    def record_human_verification(
        self, task_id: str, actor: str, command: str, output: str
    ) -> A2AHumanVerification:
        """Record a T3 human re-run (protocol §9.7).

        Raises A2ATaskNotFoundError / A2ATransitionError.
        """
        task = self.get_task(task_id)
        try:
            return self._sm.record_human_verification(task_id, actor, command, output)
        except A2AInvalidTransitionError as exc:
            raise A2ATransitionError(
                task_id=task_id, from_state=task.state, to_state=task.state,
                reason=str(exc),
            ) from exc

    def tier_changes(self, task_id: str) -> list[A2ATierChange]:
        self.get_task(task_id)
        return self._sm.get_tier_changes(task_id)

    def escalate_tier(
        self, task_id: str, actor: str, reason: str, evidence_ref: str | None = None
    ) -> A2ATask:
        """Escalate a T2 task to T3 at Gate 1 (Human).  Raises A2ATransitionError."""
        task = self.get_task(task_id)
        try:
            return self._sm.escalate_tier(task_id, actor, reason, evidence_ref)
        except A2AInvalidTransitionError as exc:
            raise A2ATransitionError(
                task_id=task_id, from_state=task.state, to_state=task.state,
                reason=str(exc),
            ) from exc

    def check_consistency(self) -> list[str]:
        return self._sm.check_consistency()
