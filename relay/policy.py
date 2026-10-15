from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .paths import (
    InvalidRelativePathError,
    InvalidTestSpecifierError,
    normalize_relative_path,
    validate_required_tests,
)

if TYPE_CHECKING:
    from .state import StateManager

POLICY_VERSION = "0.1"

# What a Prototype 0 execution may do.
ALLOWED_CAPABILITIES: frozenset[str] = frozenset({
    "read_repository",
    "modify_working_tree",
    "run_tests",
})

# Explicitly forbidden — always denied, regardless of any request or assertion.
FORBIDDEN_CAPABILITIES: frozenset[str] = frozenset({
    "modify_main",
    "push",
    "merge",
    "deploy",
    "publish",
    "access_production",
    "access_real_credentials",
    "modify_relay_policy",
})

# Repository patterns that indicate a production system.
PRODUCTION_REPO_PATTERNS: tuple[str, ...] = (
    "lunneys-social",
    "loshop",
    "production",
    "prod",
)


@dataclass
class PolicyResult:
    allowed: bool
    reason: str
    policy_version: str = POLICY_VERSION
    denied_capabilities: list[str] = field(default_factory=list)
    denied_actions: list[str] = field(default_factory=list)
    requires_approval: bool = False


_SAFE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")


def _is_safe_ref(ref: str) -> bool:
    return (
        _SAFE_REF.fullmatch(ref) is not None
        and ".." not in ref
        and "//" not in ref
        and not ref.endswith(("/", ".", ".lock"))
    )


class PolicyEngine:
    """
    Relay policy engine. Fail-closed on anything not explicitly allowed.

    Fundamental rules:
    - Agents propose actions. Relay policy authorises actions.
    - A task may never grant itself capabilities beyond what Relay allows.
    - Agent assertions ("I have completed this") are evidence, not authority.
    - Unknown capability → DENY. Unknown action → DENY. Never guess.
    """

    version: str = POLICY_VERSION

    def check_capability(self, capability: str) -> PolicyResult:
        if capability in FORBIDDEN_CAPABILITIES:
            return PolicyResult(
                allowed=False,
                reason=f"Capability '{capability}' is explicitly forbidden by Relay policy.",
                denied_capabilities=[capability],
            )
        if capability not in ALLOWED_CAPABILITIES:
            return PolicyResult(
                allowed=False,
                reason=(
                    f"Capability '{capability}' is unknown. "
                    "Relay fails closed on unknown capabilities."
                ),
                denied_capabilities=[capability],
            )
        return PolicyResult(allowed=True, reason="Capability allowed by policy.")

    def check_capabilities(self, capabilities: list[str]) -> PolicyResult:
        denied: list[str] = []
        reasons: list[str] = []
        for cap in capabilities:
            result = self.check_capability(cap)
            if not result.allowed:
                denied.append(cap)
                reasons.append(result.reason)
        if denied:
            return PolicyResult(
                allowed=False,
                reason="; ".join(reasons),
                denied_capabilities=denied,
            )
        return PolicyResult(allowed=True, reason="All capabilities allowed.")

    def validate_task(self, task) -> PolicyResult:
        """
        Validate a task against Relay policy before any execution begins.
        Cannot be overridden by task content, agent assertions, or natural-language requests.
        """
        # Check requested actions against allowed capabilities
        cap_result = self.check_capabilities(task.requested_actions)
        if not cap_result.allowed:
            return cap_result

        # Reject any task targeting a production-pattern repository
        for pattern in PRODUCTION_REPO_PATTERNS:
            if pattern in task.repository.lower():
                return PolicyResult(
                    allowed=False,
                    reason=(
                        f"Repository '{task.repository}' matches production system "
                        f"pattern '{pattern}'. Prototype 0 must not touch production systems."
                    ),
                )

        # P1-D: validate required_tests entries (no option injection, no control chars).
        # Workspace is not available at policy-check time, so path boundary validation
        # happens later in the verifier. This catches syntactic injection attempts early.
        try:
            validate_required_tests(task.required_tests)
        except InvalidTestSpecifierError as exc:
            return PolicyResult(
                allowed=False,
                reason=f"Invalid required_tests entry: {exc}",
            )

        # Batch 3 (E-12): base_ref is passed to `git checkout`; allow only plain
        # branch/tag names so it can never be read as an option or a revision
        # expression.
        if not _is_safe_ref(task.base_ref):
            return PolicyResult(
                allowed=False,
                reason=(
                    f"Invalid base_ref {task.base_ref!r}: use a plain branch or tag "
                    "name (letters, digits, '.', '_', '-', '/'; no leading '-', "
                    "'..', '//' or trailing '/' or '.lock')."
                ),
            )

        # Batch 1: declared new files must be plain workspace-relative paths.
        try:
            for path in task.expected_new_files:
                normalize_relative_path(path)
        except InvalidRelativePathError as exc:
            return PolicyResult(
                allowed=False,
                reason=f"Invalid expected_new_files entry: {exc}",
            )

        # P1-F: signal that manual human approval is required before execution.
        # The task definition is structurally valid (allowed=True), but the caller
        # must place the task in BLOCKED state and wait for an explicit Relay operator
        # approval before execution can proceed.
        if task.approval_policy == "manual":
            return PolicyResult(
                allowed=True,
                reason=(
                    "Task validated. Manual human approval required before execution. "
                    "Task will be placed in BLOCKED state pending operator approval."
                ),
                requires_approval=True,
            )

        return PolicyResult(allowed=True, reason="Task validated against policy.")

    def requires_manual_approval(self, task) -> bool:
        """Return True if this task requires explicit human approval before execution."""
        return task.approval_policy == "manual"

    def check_execution_approved(self, task, state: "StateManager") -> PolicyResult:
        """
        Verify that a valid Relay-recorded human approval exists for a manual task.

        For auto-policy tasks this always returns allowed=True.

        For manual-policy tasks, an approval is valid if and only if:
          - An approval record exists for this task_id in the database.
          - The decision is "APPROVED" (not "REJECTED").
          - is_human=True (the record was created via the Relay operator path,
            never from task JSON, repository content, or agent output).

        This is defence-in-depth: the primary barrier is that manual tasks are placed
        in BLOCKED state and cmd_run_task refuses BLOCKED tasks before reaching the
        executor. This check covers the case where the task status was manipulated
        directly or the executor is called outside the CLI.

        Schema validation (P1-E) and the operator-only approval path (P1-F) together
        ensure that an "approved: true" field in task input or agent output cannot
        satisfy this check — approval must pass through record_approval() with
        is_human=True, which only the Relay CLI approval commands invoke.
        """
        if task.approval_policy != "manual":
            return PolicyResult(
                allowed=True,
                reason="Automatic approval policy — no manual approval required.",
            )
        approval = state.get_valid_approval_for_task(task.task_id)
        if approval is None:
            return PolicyResult(
                allowed=False,
                reason=(
                    f"Task {task.task_id} requires manual human approval before "
                    "execution. No valid Relay-recorded approval exists. "
                    "Run: relay approve-task <task_id> --actor <name>"
                ),
            )
        return PolicyResult(
            allowed=True,
            reason=f"Valid human approval {approval.approval_id} recorded by {approval.approver!r}.",
        )

    def can_task_grant_capability(self, _task, capability: str) -> PolicyResult:
        """
        Tasks can never grant themselves capabilities. Only Relay policy may do so.
        This method always returns denied — it exists so tests can assert the invariant.
        """
        return PolicyResult(
            allowed=False,
            reason=(
                f"Tasks cannot grant capability '{capability}'. "
                "Only Relay policy may authorise capabilities."
            ),
        )

    def can_approve_from_agent_assertion(self, _assertion: str) -> PolicyResult:
        """
        Agent assertions cannot substitute for human approval.
        'Claude says it is necessary' is not authorisation.
        """
        return PolicyResult(
            allowed=False,
            reason=(
                "Agent assertions are evidence, not authoritative approvals. "
                "A human approval is required for privileged actions."
            ),
        )

    def is_production_repository(self, repository: str) -> bool:
        return any(p in repository.lower() for p in PRODUCTION_REPO_PATTERNS)
