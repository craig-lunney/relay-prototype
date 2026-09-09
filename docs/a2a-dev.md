# A2A Implementation — Developer Reference

This document covers the `a2a/` Python package. For the governance protocol that this code
implements, see `docs/a2a-protocol-v0.2.md` (frozen — do not modify).

---

## Package structure

```
a2a/
├── __init__.py       — package marker
└── models.py         — A2ATaskTier, A2AProtocolState, ALLOWED_A2A_TRANSITIONS, A2ATask
```

---

## State model

`A2AProtocolState` enumerates the ten states from §8 of the protocol:

| State | Description |
|---|---|
| `DRAFT` | Task created; AC not yet confirmed |
| `SPECCING` | T2/T3 — ChatGPT authoring CCP Spec |
| `AWAITING_GATE_1` | T2/T3 — Human reviewing spec before approving implementation |
| `IN_IMPLEMENTATION` | Claude implementing |
| `BLOCKED_BY_DISCOVERY` | Discovery found; Human must resolve before implementation resumes |
| `UNDER_REVIEW` | T2/T3 — PR open; ChatGPT reviewing against AC |
| `REVISION_NEEDED` | Review blocked; Claude must revise and re-post evidence |
| `AWAITING_GATE_2` | PR ready; Human merge decision pending |
| `SHIPPED` | Merged + deployed + post-deploy AC passed |
| `CLOSED` | Terminal — Issue closed by Human |

`ALLOWED_A2A_TRANSITIONS` is a `dict[A2AProtocolState, frozenset[A2AProtocolState]]`
following the same pattern as `relay/models.py:ALLOWED_TRANSITIONS`. The dict captures
the **union** of T1 and T2/T3 transitions — tier-specific enforcement is a higher-level
concern layered on top.

### T1 path (Gate 2 only)

```
DRAFT → IN_IMPLEMENTATION → AWAITING_GATE_2 → SHIPPED → CLOSED
```

T1 tasks skip SPECCING, AWAITING_GATE_1, and UNDER_REVIEW entirely.

### T2/T3 path (full protocol)

```
DRAFT → SPECCING → AWAITING_GATE_1 → IN_IMPLEMENTATION
      → UNDER_REVIEW → AWAITING_GATE_2 → SHIPPED → CLOSED
```

### BLOCKED_BY_DISCOVERY (all tiers)

Any task in `IN_IMPLEMENTATION` may transition to `BLOCKED_BY_DISCOVERY` when Claude
finds something not covered by the agreed AC or CCP Spec. Resolution: Human posts
decision on the Issue; task returns to `IN_IMPLEMENTATION`. If abandoned: `CLOSED`.

---

## Risk tiers

`A2ATaskTier` encodes the three risk tiers from §3 of the protocol:

| Tier | Protocol |
|---|---|
| `T1` | Gate 2 only; no ChatGPT spec or review; CSS/template/static changes only |
| `T2` | Full protocol — both gates, ChatGPT spec, ChatGPT review |
| `T3` | Full protocol + Human independently re-runs ≥1 verification command at Gate 2 |

---

## `A2ATask`

Minimal dataclass representing one protocol task. Fields:

| Field | Type | Default |
|---|---|---|
| `task_id` | `str` | — required |
| `title` | `str` | — required |
| `tier` | `A2ATaskTier` | — required |
| `github_issue_url` | `str` | — required |
| `state` | `A2AProtocolState` | `DRAFT` |
| `created_at` | `datetime` | UTC now |
| `updated_at` | `datetime` | UTC now |
| `protocol_version` | `str` | `"0.2"` |

Generate a unique task ID:

```python
from a2a.models import A2ATask
task_id = A2ATask.new_id()  # "A2A-3F9D2C1A" (prefix + 8 uppercase hex chars)
```

---

## Tests

`tests/test_a2a_models.py` covers:
- Enum completeness (all 10 states, all 3 tiers)
- Transition map completeness (every state has an entry)
- T1 and T2/T3 happy-path transitions
- BLOCKED_BY_DISCOVERY interruption on any tier
- CLOSED as terminal (no outgoing transitions)
- Invalid transitions (fail-closed parametrised table)
- Self-transitions invalid for every state
- Full reachability from DRAFT

Run with: `pytest tests/test_a2a_models.py -v`

---

## Relationship to `relay/`

The `a2a/` package is independent of `relay/`. They share the same repository but have
separate concerns:

- `relay/` — Relay prototype: safe AI-to-AI subprocess execution (P0/P1 series)
- `a2a/` — A2A protocol: governance layer for Human→ChatGPT→Claude→GitHub collaboration

The state enums do not overlap. `relay/models.py:TaskStatus` governs Relay's internal
execution lifecycle; `a2a/models.py:A2AProtocolState` governs the A2A collaboration
protocol. Neither imports from the other.
