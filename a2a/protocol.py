"""
A2A protocol rules as data (Batch 4 — attested ledger).

Encodes, from the frozen protocol v0.2, *who* may trigger each transition (§2,
§8, §14), which tiers may take it (§3), and what must be attested with it
(§8 "no agent may advance state without the defined trigger", §9, §12).

Enforcement lives in A2AStateManager.transition_state, inside the same
database transaction as the state change.  These are attestations recorded by
whoever operates the CLI, not authenticated identities: the ledger makes the
claimed actor, role and evidence for every step explicit and permanent, and
refuses steps that the protocol assigns to a different role.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import A2AProtocolState as S
from .models import A2ATaskTier as T


class A2ARole(str, Enum):
    HUMAN = "HUMAN"
    CHATGPT = "CHATGPT"
    CLAUDE = "CLAUDE"


H, G, C = A2ARole.HUMAN, A2ARole.CHATGPT, A2ARole.CLAUDE

# Note on CHATGPT steps: per §2 the Human pastes ChatGPT's text into GitHub, but
# the ledger records the *author* of a step.  Record ChatGPT's spec and review
# steps with a ChatGPT actor name (e.g. "chatgpt"), not the operator's own name:
# one actor may hold only one role per task.
ALL_TIERS = frozenset(T)
FULL_PROTOCOL = frozenset({T.T2, T.T3})


@dataclass(frozen=True)
class TransitionRule:
    roles: frozenset[A2ARole]
    tiers: frozenset[T] = ALL_TIERS
    evidence_required: bool = False   # a trigger artefact (comment/PR/report) must be cited
    reason_required: bool = False     # a decision that must be explained
    relay_required: bool = False      # a COMPLETED Relay task must be cited (D1)
    trigger: str = ""


RULES: dict[tuple[S, S], TransitionRule] = {
    (S.DRAFT, S.SPECCING): TransitionRule(
        frozenset({H}), FULL_PROTOCOL, trigger="Human submits Brief"),
    (S.DRAFT, S.IN_IMPLEMENTATION): TransitionRule(
        frozenset({H}), frozenset({T.T1}), trigger="Human writes AC and instructs Claude"),
    (S.SPECCING, S.AWAITING_GATE_1): TransitionRule(
        frozenset({G}), FULL_PROTOCOL, evidence_required=True,
        trigger="ChatGPT posts complete CCP Spec"),
    (S.AWAITING_GATE_1, S.IN_IMPLEMENTATION): TransitionRule(
        frozenset({H}), FULL_PROTOCOL, trigger="Gate 1: Human approves for implementation"),
    (S.AWAITING_GATE_1, S.DRAFT): TransitionRule(
        frozenset({H}), FULL_PROTOCOL, reason_required=True,
        trigger="Gate 1: Human rejects; AC must change"),
    (S.IN_IMPLEMENTATION, S.BLOCKED_BY_DISCOVERY): TransitionRule(
        frozenset({C, H}), evidence_required=True, trigger="Discovery report posted"),
    (S.IN_IMPLEMENTATION, S.UNDER_REVIEW): TransitionRule(
        frozenset({C}), FULL_PROTOCOL, evidence_required=True, relay_required=True,
        trigger="Claude opens PR and posts pre-merge evidence"),
    (S.IN_IMPLEMENTATION, S.AWAITING_GATE_2): TransitionRule(
        frozenset({C}), frozenset({T.T1}), evidence_required=True, relay_required=True,
        trigger="Claude opens PR and posts evidence"),
    (S.IN_IMPLEMENTATION, S.AWAITING_GATE_1): TransitionRule(
        frozenset({H}), FULL_PROTOCOL, reason_required=True,
        trigger="§7.2 post-Gate-1 resource conflict: task returns to Gate 1"),
    (S.BLOCKED_BY_DISCOVERY, S.IN_IMPLEMENTATION): TransitionRule(
        frozenset({H}), evidence_required=True, reason_required=True,
        trigger="Human resolves discovery (decision comment on the Issue)"),
    (S.BLOCKED_BY_DISCOVERY, S.AWAITING_GATE_1): TransitionRule(
        frozenset({H}), FULL_PROTOCOL, reason_required=True,
        trigger="§10.3 significant amendment: Gate 1 re-approval"),
    (S.BLOCKED_BY_DISCOVERY, S.CLOSED): TransitionRule(
        frozenset({H}), reason_required=True, trigger="Human abandons task after discovery"),
    (S.UNDER_REVIEW, S.REVISION_NEEDED): TransitionRule(
        frozenset({G}), FULL_PROTOCOL, evidence_required=True,
        trigger="ChatGPT review: BLOCKED"),
    (S.UNDER_REVIEW, S.AWAITING_GATE_2): TransitionRule(
        frozenset({G}), FULL_PROTOCOL, evidence_required=True,
        trigger="ChatGPT review: UNBLOCKED-PENDING-HUMAN-MERGE"),
    (S.REVISION_NEEDED, S.IN_IMPLEMENTATION): TransitionRule(
        frozenset({C}), trigger="Claude revises"),
    (S.REVISION_NEEDED, S.CLOSED): TransitionRule(
        frozenset({H}), reason_required=True,
        trigger="§12.4 Human closes the Issue as INCOMPLETE"),
    (S.AWAITING_GATE_2, S.SHIPPED): TransitionRule(
        frozenset({H}), evidence_required=True,
        trigger="Gate 2: Human merges; post-deploy AC pass"),
    (S.AWAITING_GATE_2, S.REVISION_NEEDED): TransitionRule(
        frozenset({H, C}), evidence_required=True, trigger="Post-deploy AC fails"),
    (S.SHIPPED, S.CLOSED): TransitionRule(
        frozenset({H}), trigger="Human closes Issue"),
}
