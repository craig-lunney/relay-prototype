from __future__ import annotations

from .models import A2AProtocolState, A2ATask, A2ATaskTier
from .state import A2AInvalidTransitionError, A2AStateManager

# States that exist only in the T2/T3 protocol path.
# T1 tasks (DRAFT → IN_IMPLEMENTATION → AWAITING_GATE_2 → SHIPPED → CLOSED)
# must never enter these states regardless of what the abstract graph permits.
_T1_FORBIDDEN_STATES: frozenset[A2AProtocolState] = frozenset({
    A2AProtocolState.SPECCING,
    A2AProtocolState.AWAITING_GATE_1,
    A2AProtocolState.UNDER_REVIEW,
})


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

    All protocol state-transition rules are enforced by the underlying
    A2AStateManager; this service adds domain-level errors, auto-generated
    task IDs, and a clean API surface for the CLI and future callers.

    A2AService never accesses SQLite directly.
    """

    def __init__(self, state_manager: A2AStateManager) -> None:
        self._sm = state_manager

    def create_task(
        self,
        title: str,
        tier: A2ATaskTier,
        github_issue_url: str,
    ) -> A2ATask:
        """Create a new A2A task in DRAFT state and persist it.

        A unique task_id is generated automatically. The task is returned
        in the state it was stored, which is always DRAFT.
        """
        task = A2ATask(
            task_id=A2ATask.new_id(),
            title=title,
            tier=tier,
            github_issue_url=github_issue_url,
        )
        self._sm.create_task(task)
        return task

    def advance(
        self,
        task_id: str,
        new_state: A2AProtocolState,
    ) -> A2ATask:
        """Advance task to new_state, enforcing the A2A protocol transition rules.

        Raises A2ATaskNotFoundError if the task does not exist.
        Raises A2ATransitionError if the transition is not allowed from the
        current state, if a tier-specific path constraint is violated, or if a
        concurrent writer committed a change first.
        """
        task = self._sm.get_task(task_id)
        if task is None:
            raise A2ATaskNotFoundError(task_id)

        # Tier-specific path enforcement.  The abstract state graph is intentionally
        # tier-agnostic (it is the union of all valid paths); the tier constraints
        # are enforced here in the service layer before delegating to the state layer.
        if task.tier == A2ATaskTier.T1 and new_state in _T1_FORBIDDEN_STATES:
            raise A2ATransitionError(
                task_id=task_id,
                from_state=task.state,
                to_state=new_state,
                reason=(
                    f"{new_state.value} is not part of the T1 protocol path. "
                    "T1 tasks follow: DRAFT → IN_IMPLEMENTATION → AWAITING_GATE_2 "
                    "→ SHIPPED → CLOSED."
                ),
            )
        if (
            task.tier in (A2ATaskTier.T2, A2ATaskTier.T3)
            and task.state == A2AProtocolState.DRAFT
            and new_state == A2AProtocolState.IN_IMPLEMENTATION
        ):
            raise A2ATransitionError(
                task_id=task_id,
                from_state=task.state,
                to_state=new_state,
                reason=(
                    "T2/T3 tasks must enter IN_IMPLEMENTATION via AWAITING_GATE_1, "
                    "not directly from DRAFT. "
                    "Required path: DRAFT → SPECCING → AWAITING_GATE_1 → IN_IMPLEMENTATION."
                ),
            )

        try:
            return self._sm.transition_state(task_id, new_state)
        except A2AInvalidTransitionError as exc:
            # Re-read state: a concurrent modification may have changed it
            # between our pre-check and the CAS UPDATE inside transition_state.
            refreshed = self._sm.get_task(task_id)
            raise A2ATransitionError(
                task_id=task_id,
                from_state=refreshed.state if refreshed is not None else task.state,
                to_state=new_state,
                reason=str(exc),
            ) from exc

    def get_task(self, task_id: str) -> A2ATask:
        """Return the task for task_id.

        Raises A2ATaskNotFoundError if not found.
        """
        task = self._sm.get_task(task_id)
        if task is None:
            raise A2ATaskNotFoundError(task_id)
        return task

    def list_tasks(self) -> list[A2ATask]:
        """Return all tasks ordered by created_at ascending."""
        return self._sm.list_tasks()
