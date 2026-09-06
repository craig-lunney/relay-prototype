# Project Relay — Prototype 0

Research prototype for safe AI-to-AI engineering collaboration.

```
Human-defined task
  → Relay policy validation
  → Claude Code execution (constrained subprocess)
  → isolated test repository
  → independent verification
  → structured implementation result
  → durable audit trail
```

This is a **research prototype**, not a production system.

---

## Safety boundaries

This prototype will **never**:
- Modify the Lunneys Social repository or any production system
- Push, merge, or create pull requests
- Access production credentials
- Use `--dangerously-skip-permissions`
- Allow a task to grant itself capabilities beyond Relay policy
- Accept Claude's "COMPLETED" assertion without independent verification

The only repository Claude modifies is the disposable `test-repository/` copy
inside this project.

---

## Architecture

```
ChatGPT (planning)
    ↓ task definition JSON
Relay CLI (policy engine + orchestrator)
    ↓ constrained subprocess
Claude Code (implementation worker)
    ↓ git diff + pytest
Relay Verifier (independent verification)
    ↓ 
SQLite audit log (append-only, worker-inaccessible)
```

**Fundamental rules:**
1. Agents propose actions. Relay policy authorises actions.
2. Agent assertions are evidence, not authoritative state.

---

## Structure

```
relay-prototype/
├── relay/
│   ├── models.py      — Task, Execution, ImplementationReport, AuditEvent, enums
│   ├── policy.py      — PolicyEngine (fail-closed, Prototype 0 capabilities)
│   ├── state.py       — SQLite state manager (append-only events)
│   ├── audit.py       — AuditLog (Relay-only write path)
│   ├── executor.py    — ExecutionEngine (Claude subprocess + lease)
│   ├── verifier.py    — Verifier (independent git + test inspection)
│   ├── recovery.py    — RecoveryEngine (interrupted execution detection)
│   └── cli.py         — relay CLI (create-task / run-task / status / audit / recover)
├── schemas/
│   ├── task.schema.json    — machine-readable task definition schema
│   └── report.schema.json  — machine-readable implementation report schema
├── tests/             — 95 tests covering all modules + 15 security tests
├── test-repository/   — disposable target: calculator.py with deliberate bug
└── sample_task.json   — starter task definition
```

---

## Prototype 0 capabilities

| Capability | Allowed |
|------------|---------|
| `read_repository` | ✓ |
| `modify_working_tree` | ✓ |
| `run_tests` | ✓ |
| `push` | ✗ |
| `merge` | ✗ |
| `deploy` | ✗ |
| `modify_main` | ✗ |
| `access_production` | ✗ |
| `modify_relay_policy` | ✗ |

Unknown capabilities → **DENY** (fail-closed, no guessing).

---

## State model

```
CREATED → VALIDATING → READY → RUNNING → VERIFYING → COMPLETED
                     ↘ REJECTED        ↘ FAILED
                                       ↘ TIMEOUT → READY (retry)
                                       ↘ RECOVERY_REQUIRED → READY (operator)
```

Relay owns all transitions. Claude cannot declare a task COMPLETED.

---

## Quick start

```bash
cd relay-prototype
pip install -e ".[dev]"

# Initialize the test-repository git repo
cd test-repository && git init && git add . && git commit -m "initial" && cd ..

# Edit sample_task.json — set "repository" to the absolute path of test-repository/

# Create a task
relay --db relay.db create-task sample_task.json

# Run it (requires `claude` on PATH)
relay --db relay.db run-task TASK-XXXXXXXX --workspace ./workspaces

# Inspect the audit trail
relay --db relay.db audit

# Check for interrupted executions
relay --db relay.db recover
```

---

## Running tests

```bash
pytest tests/ -v
```

95 tests, including 15 security tests mapped to the spec requirements.
The test-repository does **not** need to be git-initialised for the tests —
`conftest.py` does this per-test in a temp directory.

---

## Security test map

| # | Test | What it proves |
|---|------|---------------|
| 1 | `test_sec1` | Valid capability allowed |
| 2 | `test_sec2` | Unknown capability denied (fail-closed) |
| 3 | `test_sec3` | All forbidden capabilities denied |
| 4 | `test_sec4` | Task cannot grant itself capabilities |
| 5 | `test_sec5` | Agent assertion cannot substitute human approval |
| 6 | `test_sec6` | `modify_main` denied |
| 7 | `test_sec7` | `push` denied |
| 8 | `test_sec8` | `deploy` / `publish` denied |
| 9 | `test_sec9` | Timeout recorded with correct status + audit event |
| 10 | `test_sec10` | Failed execution preserves stdout/stderr/timestamps for recovery |
| 11 | `test_sec11` | Malicious repo comment cannot alter Relay policy |
| 12 | `test_sec12` | Claude's "COMPLETED" assertion rejected without verification |
| 13 | `test_sec13` | Second execution blocked when workspace lease is active |
| 14 | `test_sec14` | Stale execution → RECOVERY_REQUIRED, not silent COMPLETED |
| 15 | `test_sec15` | Audit records have no worker-accessible delete/update path |
