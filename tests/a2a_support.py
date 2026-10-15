"""
Test helpers for the A2A attested ledger (Batch 4).

Most A2A tests are about the state graph and tier paths, not about who may act.
These helpers let them walk the protocol *as the protocol prescribes*: for each
step they attest the role the protocol assigns to that step, cite evidence, cite
a Relay task where one is required, and record the T3 human re-run before T3
Gate 2.  Tests that exercise role authority, evidence or the Relay link build
their own A2AAttestation explicitly instead.
"""
from __future__ import annotations

import itertools
from pathlib import Path

from a2a.models import A2AProtocolState, A2ATaskTier
from a2a.protocol import RULES, A2ARole
from a2a.state import A2AAttestation, A2AStateManager

ACTORS = {A2ARole.HUMAN: "craig", A2ARole.CHATGPT: "chatgpt", A2ARole.CLAUDE: "claude"}
EVIDENCE = "https://github.com/org/repo/issues/1#issuecomment-1"
RELAY_OK_PREFIX = "TASK-RELAYOK"
_relay_ids = itertools.count(1)


def fake_relay_lookup(relay_task_id: str) -> str | None:
    """Every Relay task whose ID starts with TASK-RELAYOK is COMPLETED."""
    return "COMPLETED" if relay_task_id.startswith(RELAY_OK_PREFIX) else None


def protocol_attestation(
    sm: A2AStateManager, task_id: str, to_state: A2AProtocolState
) -> A2AAttestation:
    """The attestation the protocol expects for task_id's step to to_state.

    For a step that is not in the protocol at all, a HUMAN attestation is
    returned so the graph/tier check is what rejects it.
    """
    task = sm.get_task(task_id)
    rule = RULES.get((task.state, to_state)) if task else None
    role = sorted(rule.roles, key=lambda r: r.value)[0] if rule else A2ARole.HUMAN
    return A2AAttestation(
        actor=ACTORS[role],
        role=role,
        reason="protocol step",
        evidence_ref=EVIDENCE,
        # Each implementation step needs its own verified Relay run.
        relay_task_id=(f"{RELAY_OK_PREFIX}-{task_id}-{next(_relay_ids)}"
                       if rule and rule.relay_required else None),
    )


def _ensure_t3_rerun(sm: A2AStateManager, task_id: str, to_state: A2AProtocolState) -> None:
    task = sm.get_task(task_id)
    if (task is not None and task.tier == A2ATaskTier.T3
            and task.state == A2AProtocolState.AWAITING_GATE_2
            and to_state == A2AProtocolState.SHIPPED):
        sm.record_human_verification(task_id, ACTORS[A2ARole.HUMAN],
                                     "pytest -q", "5 passed")


def transition(sm: A2AStateManager, task_id: str, to_state: A2AProtocolState):
    """A2AStateManager.transition_state with the protocol's own attestation."""
    _ensure_t3_rerun(sm, task_id, to_state)
    return sm.transition_state(task_id, to_state, protocol_attestation(sm, task_id, to_state))


def advance(svc, task_id: str, to_state: A2AProtocolState):
    """A2AService.advance with the protocol's own attestation."""
    sm = svc._sm
    _ensure_t3_rerun(sm, task_id, to_state)
    return svc.advance(task_id, to_state, protocol_attestation(sm, task_id, to_state))


def make_relay_db(path: str | Path, completed: list[str] = (), other: dict | None = None):
    """Create a real Relay database containing tasks in the given statuses."""
    from relay.models import Task, TaskStatus
    from relay.state import StateManager

    state = StateManager(path)
    statuses = {tid: TaskStatus.COMPLETED for tid in completed}
    statuses.update({tid: TaskStatus(s) for tid, s in (other or {}).items()})
    for tid, status in statuses.items():
        state.create_task(Task(
            task_id=tid, project_id="a2a-link", repository=".", base_ref="main",
            working_ref="main", objective="evidence", requirements=[],
            acceptance_criteria=[], requested_actions=[], forbidden_actions=[],
            required_tests=[], approval_policy="auto", status=status,
        ))
    return state
