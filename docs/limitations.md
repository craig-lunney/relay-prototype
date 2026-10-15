# Known Limitations and Deferred Capabilities (v1)

This prototype enforces a defined process on a single operator's machine. It
records and checks; it does not defend against a hostile user or process with
the same account. Everything below is a deliberate v1 boundary, recorded so
nobody has to infer it.

## 1. No OS-level sandbox (decided: document for v1)

The Claude worker is constrained by Claude Code's own controls — `--bare`
(no operator configuration), an explicit tool allowlist
(`Read Edit Bash(python -m pytest *) Bash(pytest *)`), a curated environment
without credentials, and the workspace as its working directory. These are
**advisory controls enforced by the Claude Code CLI**, not by the operating
system.

Consequences:

- The worker can run arbitrary Python as the operator's account by writing it
  into a file and running `pytest`.
- Relay's verifier runs the worker-modified application code (that is what
  testing it means), also as the operator's account.
- Code running as that account can read or write anything the operator can,
  including `relay.db`, `a2a.db`, the Relay source and the API key file.

What Relay does about it (Batch 1–3): protected tests and configuration are
hash-checked before anything runs; no test runs at all when integrity is
unknown or violated; workspace bytecode and pytest caches are never loaded;
worker-added test modules are not collected; `git status` is not run in the
worker-writable repository (`git branch --show-current` still is, and `.git/`
itself is not part of the integrity manifest); the verdict is computed from a
manifest held in memory since before the worker started.

`requested_actions` and `forbidden_actions` are checked against policy and
included in the worker's prompt, but they do not change what the worker can
do: its tool allowlist is fixed by Relay for every task.

Closing the gap needs the worker and verifier to run under a separate
low-privilege identity or container with only the workspace mounted. That is a
post-v1 design task.

## 2. No operator authentication

`--actor` (Relay approvals and acknowledgements, every ledger step) is a name
typed by whoever runs the CLI. The ledger and audit log record *claims*. The
protections that do hold are structural: a role can only take the steps the
protocol assigns it, one actor cannot hold two roles on the same task, and every
claim is recorded permanently with its evidence.

## 3. Local databases are tamper-evident only in part

- The ledger tables (`a2a_transitions`, `a2a_human_verifications`,
  `a2a_tier_changes`) have UPDATE/DELETE-blocking triggers, a task's tier can
  only move T2 → T3 (and only through `a2a escalate-tier`), and `a2a check`
  replays every task's history against the protocol rules and reports state
  changed outside the ledger, inserted rows that break a rule, and tasks with
  no history. A forged row that obeys every rule cannot be told apart from a
  real one.
- Relay's audit events are hash-chained (Batch 6): each event stores the
  previous event's hash and its own content hash. `relay audit --verify`
  detects edited, deleted or re-ordered events, events inserted mid-log, and
  unhashed rows. It cannot detect a chain rebuilt from scratch, a correctly
  hashed event appended at the end, or events cut from the end, unless you
  recorded the head hash it prints somewhere independent (for example as
  evidence in the A2A ledger). Events written before the upgrade are chained
  once, on the first open after upgrading (do this with no relay process
  running); their earlier history cannot be vouched for, and downgrading to a
  pre-chain version of Relay is not supported. Someone with file access could
  reset the database's schema version and wipe every hash to force a re-chain;
  the version gate only raises the bar. The externally recorded head hash is
  the real control.
- Anyone with file access can drop triggers or rewrite either SQLite file.
  These measures catch accidents and casual edits, not a determined local
  attacker.

## 4. The protocol's GitHub side is not automated

The frozen protocol makes GitHub the permanent record (Issues, PR comments,
labels, merges). The ledger stores GitHub URLs as evidence references but never
calls GitHub: it does not check that a comment exists, read labels, or detect
merges. Keeping GitHub and the ledger in step is the operator's job.
Full GitHub API integration is deferred.

## 4a. The Relay evidence link is a status check, not a binding

A2A requires a cited Relay task to be `COMPLETED` and never cited before. It
does not check that the Relay task worked on the same repository, PR or commit
as the A2A task, and `--relay-db` (which database was consulted) is chosen per
command and not recorded in the ledger. Evidence references (`--evidence`) are
free text; the ledger does not check that they are URLs or that they exist.

## 4b. Tier choice is on trust

The ledger enforces each tier's path, but not the protocol's §3 rules for
choosing a tier (e.g. "declares an EXCLUSIVE resource → T3"). A T1 task cannot
be escalated, and a T2 task only at Gate 1. Pre-merge evidence (protocol §9.2,
§9.4: commit hash matching the PR head) and the safety invariants (§11.3) are
not modelled.

## 5. T3 human re-run: recorded, not provable (decision D4)

A T3 task cannot reach SHIPPED until a human re-run (command, verbatim output,
actor) has been recorded in the current Gate 2 window, and the recording actor
must not have acted on the task as ChatGPT or Claude. Software cannot verify
that the human really ran the command or that the output is genuine; the record
is the human's attestation.

## 6. Protocol rules not enforced by the ledger

| Protocol rule | Status |
|---|---|
| Acceptance criteria immutability after Gate 1 (§5) | Not modelled — AC live in the GitHub Issue |
| Gate 1 content checks: 5 spec sections, one verification command per AC (§6, §14) | Not checked — the spec lives on GitHub; the ledger requires an evidence reference to the posted spec |
| EXCLUSIVE resource declaration and conflicts, crontab rules (§7) | Not modelled (loshop-specific); the §7.2 return-to-Gate-1 transition exists |
| Human overrides with retrospective review (§11) | Not modelled |
| Post-deploy evidence content (§9.5) | Evidence reference required for SHIPPED; content not checked |
| Closure record (§12.5) | Abandonment (BLOCKED → CLOSED) requires a reason; the full record lives on GitHub |

## 7. Verification scope

- Only a closed set of "the test suite passes" phrasings are checked
  automatically; any other acceptance criterion is `UNVERIFIED` and needs a
  human acknowledgement (all-or-nothing per task).
- Test-support code outside `tests/`/`test/` directories (e.g. a pytest plugin
  named in configuration) is not protected.
- Application code can special-case test inputs; verification is only as strong
  as the protected tests.
- There is no mechanism for tasks whose purpose is to change existing tests
  (decision D3): such tasks cannot pass verification.
- Every workspace file is hashed twice per execution; very large workspaces are
  slow.

## 8. Operational

- **Timeouts:** `--timeout` is capped at 300 seconds. On a timeout Relay ends
  the worker's whole process tree (on Windows via `taskkill /T`, which matters
  when the Claude CLI is installed as a `.cmd` shim, as npm does); on other
  platforms it kills the worker process it started.
- **accept-recovery** refuses to proceed if the interrupted workspace cannot be
  moved aside. On Windows that also protects against a still-running worker;
  on other platforms a running process does not block the move, so check that
  no worker is still running first. Pass the same `--workspace` base that
  `run-task` used: with a different base nothing is moved aside and the next
  run replaces the interrupted workspace.

- Opening a database creates or upgrades its schema inside a write
  transaction, so even read-only commands (`status`, `get-task`) briefly take
  the write lock; simultaneous commands wait (SQLite busy timeout, 5 s).
- A database created before the A2A ledger is migrated once, on first open:
  each existing task receives a `legacy-import` creation row in its state at
  that moment. Its earlier history is not reconstructed.

- `relay recover` must be run by the operator; nothing runs it on a schedule.
- Workspaces of tasks left in `ACCEPTANCE_REQUIRED` are kept until the operator
  removes them.
- The lease PID is Relay's own; a crashed Relay with a still-running Claude child
  is treated as stale (see accept-recovery above).

## Deferred beyond v1

GitHub API integration · OS-level sandboxing · operator authentication ·
test-maintenance task type · deployment,
publishing, merging (out of scope by design) · autonomous approval (prohibited).
