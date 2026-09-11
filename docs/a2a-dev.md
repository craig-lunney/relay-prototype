# A2A Implementation — Developer Reference

This document covers the `a2a/` Python package. For the governance protocol that this code
implements, see `docs/a2a-protocol-v0.2.md` (frozen — do not modify).

---

## Package structure

```
a2a/
├── __init__.py       — package marker
├── models.py         — A2ATaskTier, A2AProtocolState, ALLOWED_A2A_TRANSITIONS, A2ATask
├── state.py          — A2AStateManager (SQLite persistence), A2AInvalidTransitionError
├── service.py        — A2AService (lifecycle entry point), A2ATaskNotFoundError, A2ATransitionError
└── cli.py            — argparse CLI (create-task, get-task, list-tasks, advance); entry point: `a2a`
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

## `A2AStateManager`

SQLite-backed persistence for `A2ATask`. Located in `a2a/state.py`.

```python
from a2a.state import A2AStateManager, A2AInvalidTransitionError
from a2a.models import A2ATask, A2ATaskTier, A2AProtocolState

sm = A2AStateManager("path/to/a2a.db")

# Create
task = A2ATask(task_id=A2ATask.new_id(), title="...", tier=A2ATaskTier.T2,
               github_issue_url="https://github.com/org/repo/issues/1")
sm.create_task(task)

# Read
task = sm.get_task(task.task_id)   # returns A2ATask | None
tasks = sm.list_tasks()            # returns list[A2ATask] ordered by created_at

# State transition — enforces ALLOWED_A2A_TRANSITIONS, raises on violation
try:
    updated = sm.transition_state(task.task_id, A2AProtocolState.SPECCING)
except A2AInvalidTransitionError as e:
    ...  # invalid transition, or task not found, or concurrent modification
```

**Schema:** one table, `a2a_tasks`, with columns matching `A2ATask` fields. WAL mode
enabled. `task_id` is the PRIMARY KEY.

**CAS guard:** `transition_state` uses `UPDATE ... WHERE task_id=? AND state=<current>`.
If `rowcount == 0` after a valid-looking transition, a concurrent writer changed the state
first — this is raised as `A2AInvalidTransitionError` rather than silently overwriting.

**Thread safety:** each thread gets its own `sqlite3.Connection` via `threading.local`.

---

## `A2AService`

The controlled entry point for task lifecycle operations. Located in `a2a/service.py`.
Never accesses SQLite directly — delegates all persistence to `A2AStateManager`.

```python
from a2a.state import A2AStateManager
from a2a.service import A2AService, A2ATaskNotFoundError, A2ATransitionError
from a2a.models import A2ATaskTier, A2AProtocolState

sm  = A2AStateManager("path/to/a2a.db")
svc = A2AService(sm)

# Create — task_id generated automatically, initial state is DRAFT
task = svc.create_task(
    title="Fix OG description on brand pages",
    tier=A2ATaskTier.T2,
    github_issue_url="https://github.com/org/repo/issues/7",
)

# Advance — enforces ALLOWED_A2A_TRANSITIONS via A2AStateManager
try:
    updated = svc.advance(task.task_id, A2AProtocolState.SPECCING)
except A2ATaskNotFoundError as e:
    print(e.task_id)        # task_id attribute
except A2ATransitionError as e:
    print(e.from_state)     # A2AProtocolState
    print(e.to_state)       # A2AProtocolState
    print(e.reason)         # str — from the state layer

# Retrieve
task = svc.get_task(task.task_id)   # raises A2ATaskNotFoundError if missing

# List
tasks = svc.list_tasks()            # list[A2ATask] ordered by created_at
```

**Error types:**

| Exception | When raised | Key attributes |
|---|---|---|
| `A2ATaskNotFoundError` | `get_task` or `advance` on unknown `task_id` | `.task_id` |
| `A2ATransitionError` | `advance` with forbidden target state | `.task_id`, `.from_state`, `.to_state`, `.reason` |

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

`tests/test_a2a_state.py` covers:
- Creation and full field round-trip
- Retrieval (`get_task`, `list_tasks`, ordering)
- Duplicate create raises `sqlite3.IntegrityError`
- T1 and T2/T3 happy-path transition sequences
- BLOCKED_BY_DISCOVERY interruption on both tiers
- Invalid transitions: validation failure and no state corruption
- Terminal CLOSED rejects all targets
- Non-existent task raises `A2AInvalidTransitionError`
- Return value, `updated_at` advancement, immutable field preservation
- Persistence across separate `A2AStateManager` instances (new DB connections)
- Concurrent transition: threading test confirms exactly one winner

Run with: `pytest tests/test_a2a_state.py -v`

`tests/test_a2a_service.py` covers:
- Creation: fields, tier acceptance, unique ID generation, DRAFT initial state
- Retrieval: correct task returned, `A2ATaskNotFoundError` with `task_id` attribute
- Listing: empty, multiple tasks, creation-order ordering
- Valid transitions: T1/T2 happy paths, BLOCKED_BY_DISCOVERY roundtrip, Gate 1 rejection
- Invalid transitions: `A2ATransitionError` with `from_state`/`to_state`/`reason`, CLOSED terminal
- Missing task in `advance`: `A2ATaskNotFoundError`
- State corruption: state unchanged after invalid transition attempt
- Persistence via state manager: data created/advanced by service is visible directly
  via `A2AStateManager`, and across separate service instances

Run with: `pytest tests/test_a2a_service.py -v`

`tests/test_a2a_cli.py` covers:
- `create-task`: prints task_id/DRAFT/title/tier/issue_url, persists to DB, all three tiers, DB configurable
- `get-task`: prints task_id/state/issue_url; missing → exit 1 + stderr message containing task_id + no stdout
- `list-tasks`: "No tasks." on empty, single/multiple task display, shows task_ids, exit 0 on empty
- `advance`: prints new state + task_id, updates DB, T1 path, full T2 path; invalid transition → exit 1 + stderr + no stdout + state unchanged; missing task → exit 1 + stderr
- Parser structure: subcommand required, `--title`/`--tier` required on create-task, `--to` required on advance, `--db` defaults to `"a2a.db"`, `--db` override works

Run with: `pytest tests/test_a2a_cli.py -v`

---

## CLI

Entry point registered in `pyproject.toml` as `a2a = "a2a.cli:main"`. After `pip install -e .`:

```bash
a2a --help
a2a create-task --title "Fix OG tags" --tier T2 --issue-url https://github.com/org/repo/issues/7
a2a list-tasks
a2a get-task A2A-3F9D2C1A
a2a advance A2A-3F9D2C1A --to SPECCING
```

All commands accept `--db PATH` to specify a non-default SQLite file. Errors print to stderr and exit with code 1; success prints to stdout and exits 0.

**Commands:**

| Command | Required args | Action |
|---|---|---|
| `create-task` | `--title`, `--tier` (T1/T2/T3), `--issue-url` | Creates task in DRAFT; prints task_id + detail |
| `get-task` | `TASK_ID` (positional) | Prints task detail; exit 1 if not found |
| `list-tasks` | — | Lists all tasks ordered by creation time; "No tasks." if empty |
| `advance` | `TASK_ID` (positional), `--to STATE` | Advances state; exit 1 on invalid transition or missing task |

---

## Relationship to `relay/`

The `a2a/` package is independent of `relay/`. They share the same repository but have
separate concerns:

- `relay/` — Relay prototype: safe AI-to-AI subprocess execution (P0/P1 series)
- `a2a/` — A2A protocol: governance layer for Human→ChatGPT→Claude→GitHub collaboration

The state enums do not overlap. `relay/models.py:TaskStatus` governs Relay's internal
execution lifecycle; `a2a/models.py:A2AProtocolState` governs the A2A collaboration
protocol. Neither imports from the other.
