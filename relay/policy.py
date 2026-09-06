from __future__ import annotations

from dataclasses import dataclass, field

from .paths import InvalidTestSpecifierError, validate_required_tests

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

        return PolicyResult(allowed=True, reason="Task validated against policy.")

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
