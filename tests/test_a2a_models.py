"""Unit tests for A2A Protocol v0.2 state model."""
from __future__ import annotations

import pytest

from a2a.models import (
    ALLOWED_A2A_TRANSITIONS,
    A2AProtocolState,
    A2ATask,
    A2ATaskTier,
)


# ---------------------------------------------------------------------------
# Enum completeness
# ---------------------------------------------------------------------------

def test_all_ten_states_defined():
    expected = {
        "DRAFT", "SPECCING", "AWAITING_GATE_1", "IN_IMPLEMENTATION",
        "BLOCKED_BY_DISCOVERY", "UNDER_REVIEW", "REVISION_NEEDED",
        "AWAITING_GATE_2", "SHIPPED", "CLOSED",
    }
    assert {s.value for s in A2AProtocolState} == expected


def test_all_tiers_defined():
    assert {t.value for t in A2ATaskTier} == {"T1", "T2", "T3"}


def test_transitions_map_covers_all_states():
    for state in A2AProtocolState:
        assert state in ALLOWED_A2A_TRANSITIONS, f"{state} missing from ALLOWED_A2A_TRANSITIONS"


def test_transitions_values_are_frozensets():
    for state, targets in ALLOWED_A2A_TRANSITIONS.items():
        assert isinstance(targets, frozenset), f"{state} targets must be a frozenset"


# ---------------------------------------------------------------------------
# A2ATask construction and identity
# ---------------------------------------------------------------------------

def _task(tier: A2ATaskTier = A2ATaskTier.T2) -> A2ATask:
    return A2ATask(
        task_id=A2ATask.new_id(),
        title="Test task",
        tier=tier,
        github_issue_url="https://github.com/org/repo/issues/1",
    )


def test_task_new_id_format():
    tid = A2ATask.new_id()
    assert tid.startswith("A2A-")
    assert len(tid) == 12  # "A2A-" (4) + 8 hex chars


def test_task_default_state_is_draft():
    assert _task().state == A2AProtocolState.DRAFT


def test_task_default_protocol_version():
    assert _task().protocol_version == "0.2"


def test_task_new_ids_are_unique():
    ids = {A2ATask.new_id() for _ in range(100)}
    assert len(ids) == 100


# ---------------------------------------------------------------------------
# T1 happy path transitions
# ---------------------------------------------------------------------------

def test_t1_draft_to_in_implementation():
    assert A2AProtocolState.IN_IMPLEMENTATION in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.DRAFT]


def test_t1_in_implementation_to_awaiting_gate_2():
    assert A2AProtocolState.AWAITING_GATE_2 in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.IN_IMPLEMENTATION]


def test_awaiting_gate_2_to_shipped():
    assert A2AProtocolState.SHIPPED in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.AWAITING_GATE_2]


def test_shipped_to_closed():
    assert A2AProtocolState.CLOSED in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.SHIPPED]


# ---------------------------------------------------------------------------
# T2/T3 happy path transitions
# ---------------------------------------------------------------------------

def test_t2_draft_to_speccing():
    assert A2AProtocolState.SPECCING in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.DRAFT]


def test_speccing_to_awaiting_gate_1():
    assert A2AProtocolState.AWAITING_GATE_1 in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.SPECCING]


def test_gate_1_approval_to_in_implementation():
    assert A2AProtocolState.IN_IMPLEMENTATION in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.AWAITING_GATE_1]


def test_gate_1_rejection_returns_to_draft():
    assert A2AProtocolState.DRAFT in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.AWAITING_GATE_1]


def test_in_implementation_to_under_review():
    assert A2AProtocolState.UNDER_REVIEW in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.IN_IMPLEMENTATION]


def test_under_review_blocked_to_revision_needed():
    assert A2AProtocolState.REVISION_NEEDED in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.UNDER_REVIEW]


def test_under_review_unblocked_to_awaiting_gate_2():
    assert A2AProtocolState.AWAITING_GATE_2 in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.UNDER_REVIEW]


def test_revision_needed_to_in_implementation():
    assert A2AProtocolState.IN_IMPLEMENTATION in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.REVISION_NEEDED]


def test_awaiting_gate_2_post_deploy_failure_to_revision_needed():
    assert A2AProtocolState.REVISION_NEEDED in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.AWAITING_GATE_2]


# ---------------------------------------------------------------------------
# BLOCKED_BY_DISCOVERY — applies to all tiers
# ---------------------------------------------------------------------------

def test_in_implementation_to_blocked_by_discovery():
    assert A2AProtocolState.BLOCKED_BY_DISCOVERY in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.IN_IMPLEMENTATION]


def test_blocked_by_discovery_resolved_to_in_implementation():
    assert A2AProtocolState.IN_IMPLEMENTATION in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.BLOCKED_BY_DISCOVERY]


def test_blocked_by_discovery_can_close():
    assert A2AProtocolState.CLOSED in ALLOWED_A2A_TRANSITIONS[A2AProtocolState.BLOCKED_BY_DISCOVERY]


# ---------------------------------------------------------------------------
# Terminal state
# ---------------------------------------------------------------------------

def test_closed_has_no_outgoing_transitions():
    assert ALLOWED_A2A_TRANSITIONS[A2AProtocolState.CLOSED] == frozenset()


# ---------------------------------------------------------------------------
# Invalid transitions (fail-closed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src, dst", [
    (A2AProtocolState.DRAFT, A2AProtocolState.CLOSED),
    (A2AProtocolState.DRAFT, A2AProtocolState.SHIPPED),
    (A2AProtocolState.DRAFT, A2AProtocolState.AWAITING_GATE_2),
    (A2AProtocolState.SPECCING, A2AProtocolState.IN_IMPLEMENTATION),
    (A2AProtocolState.SPECCING, A2AProtocolState.SHIPPED),
    (A2AProtocolState.AWAITING_GATE_1, A2AProtocolState.SHIPPED),
    (A2AProtocolState.UNDER_REVIEW, A2AProtocolState.SHIPPED),     # must pass through AWAITING_GATE_2
    (A2AProtocolState.UNDER_REVIEW, A2AProtocolState.IN_IMPLEMENTATION),
    (A2AProtocolState.SHIPPED, A2AProtocolState.DRAFT),
    (A2AProtocolState.SHIPPED, A2AProtocolState.IN_IMPLEMENTATION),
    (A2AProtocolState.CLOSED, A2AProtocolState.DRAFT),
    (A2AProtocolState.CLOSED, A2AProtocolState.SHIPPED),
])
def test_invalid_transition_not_in_allowed(src, dst):
    assert dst not in ALLOWED_A2A_TRANSITIONS[src], (
        f"Expected {src} → {dst} to be INVALID but it is allowed"
    )


@pytest.mark.parametrize("state", list(A2AProtocolState))
def test_self_transitions_are_invalid(state):
    assert state not in ALLOWED_A2A_TRANSITIONS[state], (
        f"Self-transition {state} → {state} must not be allowed"
    )


# ---------------------------------------------------------------------------
# Reachability — every state reachable from DRAFT
# ---------------------------------------------------------------------------

def test_all_states_reachable_from_draft():
    visited: set[A2AProtocolState] = set()
    queue = [A2AProtocolState.DRAFT]
    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        queue.extend(ALLOWED_A2A_TRANSITIONS[current])
    unreachable = set(A2AProtocolState) - visited
    assert not unreachable, f"Unreachable states: {unreachable}"
