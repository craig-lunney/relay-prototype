# A2A Implementation: Developer Reference

This document covers the `a2a/` Python package, the **attested ledger** for the A2A collaboration protocol.

The protocol itself is defined in `docs/a2a-protocol-v0.2.md`. That document is frozen: do not modify it.

What the ledger does **not** enforce is listed in `docs/limitations.md`.

---

## Package structure

```
a2a/
├── models.py    A2ATaskTier, A2AProtocolState, ALLOWED_A2A_TRANSITIONS (graph), A2ATask
├── protocol.py  A2ARole and RULES: who may take each step, on which tiers, with what evidence
├── state.py     A2AStateManager (SQLite ledger; enforces everything), A2AAttestation,
│                history/verification records, relay_status_from_db
├── service.py   A2AService (domain errors, generated IDs), A2A*Error
└── cli.py       `a2a` CLI: create-task, get-task, list-tasks, advance,
                 record-human-verification, check
```

---

## Model

### States

`A2AProtocolState` holds the ten states from protocol §8:

| State | Meaning |
|---|---|
| `DRAFT` | Task created |
| `SPECCING` | ChatGPT is writing the CCP spec (T2/T3) |
| `AWAITING_GATE_1` | The Human is reviewing the spec (T2/T3) |
| `IN_IMPLEMENTATION` | Claude is implementing |
| `BLOCKED_BY_DISCOVERY` | A discovery is awaiting a Human decision |
| `UNDER_REVIEW` | ChatGPT is reviewing the PR against the acceptance criteria (T2/T3) |
| `REVISION_NEEDED` | The review blocked, or post-deploy acceptance criteria failed |
| `AWAITING_GATE_2` | The Human's merge decision is pending |
| `SHIPPED` | Merged, deployed, and post-deploy acceptance criteria passed |
| `CLOSED` | Terminal |

### Transitions and tiers

`ALLOWED_A2A_TRANSITIONS` is the union graph for all tiers.

`protocol.RULES` has exactly one entry per graph edge; a test enforces this. Each `TransitionRule` names:

- the **roles** that may trigger the step (`HUMAN`, `CHATGPT`, `CLAUDE`; §2, §8, §14);
- the **tiers** the step belongs to (§3);
- whether a **reason** or **evidence reference** must be attested;
- whether a **COMPLETED Relay task** must be cited.

| Path | Steps (triggering role) |
|---|---|
| T1 | DRAFT → IN_IMPLEMENTATION (H) → AWAITING_GATE_2 (C, evidence + Relay) → SHIPPED (H, evidence) → CLOSED (H) |
| T2/T3 | DRAFT → SPECCING (H) → AWAITING_GATE_1 (G, evidence) → IN_IMPLEMENTATION (H, Gate 1) → UNDER_REVIEW (C, evidence + Relay) → AWAITING_GATE_2 (G, evidence) → SHIPPED (H, evidence; **T3: recorded human re-run**) → CLOSED (H) |
| Loops and exits | AWAITING_GATE_1 → DRAFT (H, reason) · IN_IMPLEMENTATION → BLOCKED_BY_DISCOVERY (C/H, evidence) · BLOCKED_BY_DISCOVERY → IN_IMPLEMENTATION (H, reason + evidence) · BLOCKED_BY_DISCOVERY → CLOSED (H, reason) · UNDER_REVIEW → REVISION_NEEDED (G, evidence) · REVISION_NEEDED → IN_IMPLEMENTATION (C) · REVISION_NEEDED → CLOSED (§12.4 INCOMPLETE; H, reason) · AWAITING_GATE_2 → REVISION_NEEDED (H/C, evidence) |
| Return to Gate 1 | IN_IMPLEMENTATION → AWAITING_GATE_1 (§7.2 resource conflict; H, reason) · BLOCKED_BY_DISCOVERY → AWAITING_GATE_1 (§10.3 significant amendment; H, reason). T2/T3 only |

Role key: H = Human, G = ChatGPT, C = Claude.

The ledger records the **author** of each step. ChatGPT's spec and review steps should be recorded with a ChatGPT actor name, such as `chatgpt`. Do not use the operator's own name: under §2 the Human pastes ChatGPT's text, but the step is still ChatGPT's.

**Tier escalation:** `escalate_tier` moves a T2 task to T3 (§3, §10.3, §14).
- It is a Human decision and needs a reason.
- It is allowed only while the task is in `AWAITING_GATE_1`.
- It is one-way: T3 controls can never be shed.

---

## `A2AStateManager`: the ledger

```python
from a2a.models import A2ATask, A2ATaskTier, A2AProtocolState as S
from a2a.protocol import A2ARole
from a2a.state import A2AStateManager, A2AAttestation, relay_status_from_db

sm = A2AStateManager("a2a.db", relay_status_lookup=relay_status_from_db("relay.db"))

task = A2ATask(task_id=A2ATask.new_id(), title="Fix OG tags", tier=A2ATaskTier.T2,
               github_issue_url="https://github.com/org/repo/issues/7")
sm.create_task(task, created_by="craig")          # validated here: DRAFT only, title, URL

sm.transition_state(task.task_id, S.SPECCING, A2AAttestation(actor="craig", role=A2ARole.HUMAN))
sm.transition_state(task.task_id, S.AWAITING_GATE_1, A2AAttestation(
    actor="chatgpt", role=A2ARole.CHATGPT,
    evidence_ref="https://github.com/org/repo/issues/7#issuecomment-1"))

sm.get_history(task.task_id)           # list[A2ATransitionRecord], creation first
sm.record_human_verification(tid, actor, command, output)   # T3, at Gate 2
sm.get_human_verifications(tid)
sm.check_consistency()                 # list[str] of problems; [] when consistent
```

### What `transition_state` checks

All checks run inside **one `BEGIN IMMEDIATE` transaction** with the write, so no caller can skip a check and no concurrent writer can slip in between. This closes the audit's D3 (the state layer bypassed tier rules) and D4 (the tier check ran outside the CAS). The checks run in this order:

1. The task exists.
2. The step is in the graph.
3. The tier may take it.
4. The attested **role** may trigger it.
5. The **actor** has not acted in a different role on this task (no self-review or self-approval). Actor names are compared in canonical form (NFKC, invisible characters removed, whitespace collapsed, case-folded), so `Claude` and ` claude ` are the same actor.
   - After normalisation a name must be **plain**: ASCII letters and digits, joined by single `.`, `_`, `@`, `-` or space, at most 64 characters.
   - Control characters, combining marks, fillers and look-alike letters from other scripts are refused rather than silently normalised.
   - The binding covers creation, transitions, human re-runs and tier changes.
   - The placeholders `unattributed` and `legacy-import` are reserved and never bind a role.
6. A required **reason** or **evidence reference** is present.
7. A cited **Relay task** is found and is `COMPLETED`. It must never have been cited before, by any task, including this one. A revision therefore needs its own verified Relay run. A Relay task cited on a step that does not take one is refused.
8. For a **T3** task entering SHIPPED, a human re-run was recorded in the current Gate 2 window.

Any failure raises `A2AInvalidTransitionError`, and nothing is written. On success the state is updated with a CAS guard and the step is appended to `a2a_transitions`.

### Relay evidence (D1)

`relay_status_from_db(path)` opens the Relay database in SQLite **read-only URI mode** for each lookup, and Relay data is never changed. SQLite may still create its usual `-wal` and `-shm` side files beside a WAL-mode database.

A missing, unreadable or locked Relay database is reported as such (`A2ARelayUnavailableError`), never as "task not found". The lookup runs **before** the A2A write transaction, so a slow Relay database never holds the A2A write lock.

If no lookup is configured, steps that require Relay evidence fail closed.

Only `COMPLETED` counts as evidence. `ACCEPTANCE_REQUIRED` does not: its UNVERIFIED acceptance criteria have not yet been acknowledged by a human.

### T3 human re-run (D4, protocol §9.7)

`record_human_verification` is allowed only for T3 tasks in `AWAITING_GATE_2`, and only by an actor who has not acted on the task as ChatGPT or Claude.

Each record is bound to the task's latest entry into Gate 2. If the task goes back through REVISION_NEEDED, a fresh re-run is required before it can ship.

The record is the human's attestation. Software cannot prove the command was actually run.

### Schema

| Table | Purpose |
|---|---|
| `a2a_tasks` | Current state. A trigger allows only a T2 → T3 tier change. |
| `a2a_transitions` | Append-only history: `seq`, `from_state` (NULL for creation), `to_state`, `actor`, `role`, `reason`, `evidence_ref`, `relay_task_id`, `timestamp`. Triggers reject UPDATE and DELETE. |
| `a2a_human_verifications` | T3 re-runs: `actor`, `command`, `output` (verbatim, line endings preserved), `gate2_entry_seq`, `recorded_at`. Append-only. |
| `a2a_tier_changes` | Tier escalations: `from_tier`, `to_tier`, `actor`, `reason`, `evidence_ref`, `after_seq`. Append-only. |

The creation row records the task's tier, and `check` replays the tier from it.

**One-time migration:** the database's `PRAGMA user_version` records the schema version.
- When a database created before the ledger is first opened, each existing task gets one `legacy-import` creation row with its state and tier at that moment.
- This happens **once**, and only when the ledger table did not exist before.
- After that, any task with no history can only have been inserted outside the ledger, and `check` reports it.

`check_consistency()` replays every task's history against `RULES` and reports:
- history steps that do not chain;
- steps outside the graph or off the task's tier path at that point;
- a step taken by a role the rule does not allow;
- a missing reason, evidence reference or Relay citation;
- an actor holding two roles;
- a T3 task SHIPPED without a re-run in its Gate 2 window;
- a Relay task cited twice;
- a stored state or tier that disagrees with the history;
- a task with no history;
- rows that cannot be parsed.

This catches raw-SQL edits and rows forged by insertion.

---

## `A2AService`

`A2AService` wraps the state manager with domain errors and generated IDs. It never accesses SQLite directly.

| Method | Raises |
|---|---|
| `create_task(title, tier, github_issue_url, created_by=None)` | `A2AInvalidTaskError(.reason)` |
| `advance(task_id, new_state, attestation)` | `A2ATaskNotFoundError(.task_id)`; `A2ATransitionError(.task_id, .from_state, .to_state, .reason)` |
| `record_human_verification(task_id, actor, command, output)` | `A2ATaskNotFoundError`, `A2ATransitionError` |
| `escalate_tier(task_id, actor, reason, evidence_ref=None)` | `A2ATaskNotFoundError`, `A2ATransitionError` |
| `get_task`, `list_tasks`, `history`, `human_verifications`, `check_consistency` | `A2ATaskNotFoundError` where relevant |

---

## CLI

```bash
a2a --db a2a.db --relay-db relay.db <command> ...

a2a create-task --title "Fix OG tags" --tier T2 \
    --issue-url https://github.com/org/repo/issues/7 --actor craig
a2a advance A2A-XXXXXXXX --to SPECCING --actor craig --role HUMAN
a2a advance A2A-XXXXXXXX --to AWAITING_GATE_1 --actor chatgpt --role CHATGPT --evidence <url>
a2a advance A2A-XXXXXXXX --to UNDER_REVIEW --actor claude --role CLAUDE \
    --evidence <pr-url> --relay-task TASK-XXXXXXXX
a2a record-human-verification A2A-XXXXXXXX --actor craig --command "pytest -q" --output-file out.txt
a2a escalate-tier A2A-XXXXXXXX --actor craig --reason "new EXCLUSIVE resource: crontab"
a2a get-task A2A-XXXXXXXX        # includes the full attested history
a2a list-tasks
a2a check                        # exit 1 if any task's state disagrees with its history
```

Errors, including database problems, are printed to stderr without a traceback and exit with code 1. The message says that no state was changed. `--relay-db` defaults to `relay.db`.

---

## Relationship to `relay/`

- **Relay** owns execution state (`relay.db`).
- **A2A** owns protocol state (`a2a.db`).

They are linked by reference only. An implementation step in A2A cites a Relay task ID, and A2A reads that task's status read-only. Relay does not import A2A. Neither package's state enum depends on the other.

---

## Tests

| File | Covers |
|---|---|
| `tests/test_a2a_models.py` | Enums, graph completeness and reachability, valid and invalid edges |
| `tests/test_a2a_state.py` | Persistence, CAS, graph and tier enforcement at the state layer |
| `tests/test_a2a_service.py` | Service API, domain errors, tier paths |
| `tests/test_a2a_cli.py` | CLI commands and exit codes |
| `tests/test_a2a_ledger.py` | Batch 4 and its review findings: role authority, canonical actor binding, evidence and reasons, append-only tables, tier escalation, legacy import, `check` replay (including forged rows), the Relay evidence link against a real read-only Relay DB (missing/unreadable/re-cited), T3 re-run windows, and the CLI end to end |

Most of the older tests walk the protocol through `tests/a2a_support.py`. For each step, it attests the role the protocol prescribes, cites evidence, cites a Relay task where one is required, and records the T3 re-run. Tests that exercise authority or evidence build their own `A2AAttestation` instead.
