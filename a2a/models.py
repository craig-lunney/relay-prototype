from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class A2ATaskTier(str, Enum):
    T1 = "T1"  # Low risk — Gate 2 only, no ChatGPT spec/review
    T2 = "T2"  # Standard — full protocol, both gates
    T3 = "T3"  # High risk — full protocol + Human re-run at Gate 2


class A2AProtocolState(str, Enum):
    DRAFT = "DRAFT"
    SPECCING = "SPECCING"
    AWAITING_GATE_1 = "AWAITING_GATE_1"
    IN_IMPLEMENTATION = "IN_IMPLEMENTATION"
    BLOCKED_BY_DISCOVERY = "BLOCKED_BY_DISCOVERY"
    UNDER_REVIEW = "UNDER_REVIEW"
    REVISION_NEEDED = "REVISION_NEEDED"
    AWAITING_GATE_2 = "AWAITING_GATE_2"
    SHIPPED = "SHIPPED"
    CLOSED = "CLOSED"


# No agent may advance state without a defined trigger.
# T1 path:    DRAFT → IN_IMPLEMENTATION → AWAITING_GATE_2 → SHIPPED → CLOSED
# T2/T3 path: DRAFT → SPECCING → AWAITING_GATE_1 → IN_IMPLEMENTATION
#             → UNDER_REVIEW → AWAITING_GATE_2 → SHIPPED → CLOSED
# BLOCKED_BY_DISCOVERY interrupts IN_IMPLEMENTATION on any tier.
ALLOWED_A2A_TRANSITIONS: dict[A2AProtocolState, frozenset[A2AProtocolState]] = {
    A2AProtocolState.DRAFT: frozenset({
        A2AProtocolState.SPECCING,           # T2/T3: Human submits Brief to ChatGPT
        A2AProtocolState.IN_IMPLEMENTATION,  # T1: Human writes AC + instructs Claude
    }),
    A2AProtocolState.SPECCING: frozenset({
        A2AProtocolState.AWAITING_GATE_1,    # ChatGPT posts complete CCP Spec
    }),
    A2AProtocolState.AWAITING_GATE_1: frozenset({
        A2AProtocolState.IN_IMPLEMENTATION,  # Human approves (Gate 1)
        A2AProtocolState.DRAFT,              # Human rejects — AC must change before re-spec
    }),
    A2AProtocolState.IN_IMPLEMENTATION: frozenset({
        A2AProtocolState.BLOCKED_BY_DISCOVERY,  # Discovery found during implementation
        A2AProtocolState.UNDER_REVIEW,           # T2/T3: Claude opens PR + posts evidence
        A2AProtocolState.AWAITING_GATE_2,        # T1: Claude opens PR + posts evidence
    }),
    A2AProtocolState.BLOCKED_BY_DISCOVERY: frozenset({
        A2AProtocolState.IN_IMPLEMENTATION,  # Human resolves discovery on Issue
        A2AProtocolState.CLOSED,             # Task abandoned after discovery
    }),
    A2AProtocolState.UNDER_REVIEW: frozenset({
        A2AProtocolState.REVISION_NEEDED,    # ChatGPT review: BLOCKED
        A2AProtocolState.AWAITING_GATE_2,    # ChatGPT review: UNBLOCKED-PENDING-HUMAN-MERGE
    }),
    A2AProtocolState.REVISION_NEEDED: frozenset({
        A2AProtocolState.IN_IMPLEMENTATION,  # Claude revises + re-posts evidence
    }),
    A2AProtocolState.AWAITING_GATE_2: frozenset({
        A2AProtocolState.SHIPPED,            # Human merges + post-deploy AC passes
        A2AProtocolState.REVISION_NEEDED,    # Post-deploy AC fails
    }),
    A2AProtocolState.SHIPPED: frozenset({
        A2AProtocolState.CLOSED,             # Human closes Issue
    }),
    A2AProtocolState.CLOSED: frozenset(),    # Terminal — no outgoing transitions
}


@dataclass
class A2ATask:
    """An engineering task governed by the A2A Engineering Collaboration Protocol v0.2."""
    task_id: str
    title: str
    tier: A2ATaskTier
    github_issue_url: str
    state: A2AProtocolState = A2AProtocolState.DRAFT
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    protocol_version: str = "0.2"

    @classmethod
    def new_id(cls) -> str:
        return f"A2A-{uuid.uuid4().hex[:8].upper()}"
