# Project History

How Agent to Agent / Relay reached v1. Current behaviour is documented in the
README and docs; this file is the record of how it got there.

## Prototype 0 (2026-09-06)

`aa50dd8`: policy engine, SQLite state and audit log, executor that ran the
Claude Code CLI against a copy of `test-repository/`, verifier comparing git
state and test results, recovery engine for interrupted executions.

The first acceptance run found **CF-01**: the worker loaded the operator's
global `~/.claude/CLAUDE.md` (which started an unrelated Magento workflow).
Fixed in P1-A by launching the worker with `--bare` and a Relay-owned
`relay/worker_config/CLAUDE.md`.

## Prototype 1 hardening (2026-09-06 → 2026-09-08)

| Milestone | Delivered |
|---|---|
| P1-A–P1-D (`57d234b`) | Worker configuration isolation; atomic CAS state transitions; workspace path boundaries; test-command injection protection |
| P1-E / P1-F (`8c85caa`) | Runtime schema validation of task input; manual approval policy (BLOCKED → approve-task) |
| P1-G (`995ab31`) | Allowlisted worker environment (no credentials inherited) |
| P1-H (`855393d`) | Verifier and workspace lifecycle hardening |
| P1-J (`542be2f`) | Verification authority and task-outcome binding: baseline test IDs, deleted/deactivated-test detection, base_ref checkout |
| P1-K (`4b7f82d`) | Pre-launch failure hardening, atomic recovery/retry, adversarial review fixes |

## A2A protocol (2026-09-09 → 2026-09-12)

The collaboration protocol v0.2 was frozen (`4e34de2`) and the `a2a/` package
built in milestones A2A-14 (model) → A2A-15 (SQLite) → A2A-16 (service) →
A2A-17 (CLI) → A2A-18 (tier paths) → A2A-19 (issue URL) → A2A-20 (title), plus
a T2/T3 review-gate bypass fix (`062aa2e`) and H-3 test-accountability node-ID
hardening (`74cc7c6`, 703 tests).

## v1 completion (2026-10-15/16)

A read-only completion audit established the baseline (703 tests passing) and
found one critical defect — a worker could leave a bug unfixed, redefine every
test as `pass` with the same names, and pass independent verification — plus a
broken install path and an A2A package that enforced state shape but not
authority or evidence. Decisions D1–D5 were then taken by the project owner and
the work was done in batches, each independently reviewed before commit:

| Batch | Delivered |
|---|---|
| 1 — Verification integrity (`7e1c4ea`) | Relay-owned workspace hash manifest; protected tests and test configuration (D3); bytecode-cache poisoning closed; no-op and new-file detection through the real CLI; PASS/FAIL/UNVERIFIED acceptance criteria with human acknowledgement (D5) |
| 2 — Packaging and schema (`eb50ee1`) | Working editable install; `python -m relay/a2a`; one task-schema contract; friendly task-file errors |
| 3 — Execution robustness (`1dacee6`) | Orphaned-task recovery; lease before destructive workspace prep; ownership-safe pre-launch failure handling; lease renewal through verification; base_ref and required_tests validation |
| 4 — A2A attested ledger (`2a591b8`) | Actor/role/evidence on every step; append-only history; Relay evidence link (D1/D2); T3 human re-run (D4); tier escalation; `a2a check` |
| 5 — Documentation and acceptance (`8c36ed1`) | README, operator guide, limitations, development guide; scripted end-to-end acceptance demonstration |
| 6 — Audit hash chain (`ed70f18`) | Tamper-evident Relay audit log; `relay audit --verify`; one-command real run (`demo/real_run.py`) |
| 7 — Agent Office | Read-only dashboard (`python -m office`); five morning in-tray jobs (`python -m office.jobs`) |
| 8 — Office app and CCP 3 Phase 1 | `python -m office.server` (a button per job, this computer only); public-page CCP 3 category-image audit; UK time on every office page |
