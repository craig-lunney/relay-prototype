# Agent to Agent — Relay and the A2A protocol ledger

A research prototype for **controlled AI-to-AI software engineering**: a human
defines the work, ChatGPT specifies and reviews it, Claude Code implements it,
and software — not the agents' own reports — decides what actually happened.

```
Craig (human)  ──defines task, approves gates, decides──┐
ChatGPT        ──specifies, reviews                      │   A2A ledger (a2a/)
Claude Code    ──implements                              │   who did each step, in what role,
                                                         │   on what evidence
                 Relay (relay/) ◄────────────────────────┘
                 policy → isolated workspace → Claude worker
                 → independent verification → audit trail
```

**The principle:** an agent can *report* that work is done; that report is not
proof. Relay verifies independently, and the ledger records every protocol step
with a named actor and role.

This is a **research prototype**, not a production system. Read
[docs/limitations.md](docs/limitations.md) before relying on it.

---

## What is in the box

| Component | What it does | Authoritative for |
|---|---|---|
| **Relay** (`relay/`) | Validates a task against policy, copies the target repository into a disposable workspace under an exclusive lease, runs the Claude Code worker there with a restricted tool allowlist and curated environment, then **independently verifies** the result: workspace integrity (hash manifest), protected tests and test configuration, required tests, acceptance criteria. Records everything in an append-only audit log. | Execution state of a Relay task (`relay.db`) |
| **A2A ledger** (`a2a/`) | Tracks a task through the frozen [A2A protocol v0.2](docs/a2a-protocol-v0.2.md): enforces the state graph, tier paths, which role may take each step, required evidence, the Relay evidence link, and the T3 human re-run — and records each step in an append-only history. | Protocol state of an A2A task (`a2a.db`) |
| **Agent Office** (`office/`) | A read-only dashboard over both databases (`python -m office`), a local app with a button per job (`python -m office.server`), and the morning in-tray jobs (`python -m office.jobs`): crontab guard, morning briefing, weekly social batch, category images (CCP 3), Reviews.io reply pack. Jobs report or prepare drafts; none writes to a live system. | Nothing — it only reads |

The two are linked **by reference**: an A2A task can only enter review (T2/T3)
or Gate 2 (T1) by citing a Relay task that is `COMPLETED`. A2A reads the Relay
database read-only; Relay knows nothing about A2A.

## What is deliberately not implemented

No pushing, merging, pull requests, deployment or publishing. No autonomous
approval. No GitHub API integration (the ledger stores GitHub URLs as evidence
references; it does not read GitHub). No OS-level sandbox for the worker. No
operator authentication. Full list and reasoning: [docs/limitations.md](docs/limitations.md).

## Safety boundaries

- Relay never pushes, merges, creates PRs or deploys. It copies the target
  repository into a disposable workspace and runs the worker there. The target
  must be the root of its own git repository, and Relay's own git commands run
  only in a copy it has confirmed is a git root.
- A task cannot grant itself capabilities: requests outside policy (e.g.
  `push`) are rejected and unknown capabilities are denied. The worker's tool
  allowlist is fixed by Relay, not by the task.
- The worker's report is evidence, never authority. Verification fails if the
  worker alters existing tests, test configuration or Python start-up hooks,
  makes no change, creates undeclared files, or if required tests fail.
- Acceptance criteria Relay cannot check are `UNVERIFIED` and need an explicit
  human acknowledgement before a task is `COMPLETED`.
- In the ledger, an actor may hold only one role per task (no self-review or
  self-approval), Gate 1, Gate 2 and SHIPPED are Human steps, and T3 tasks need a
  recorded human re-run before SHIPPED.

These are **process controls on a single machine**, not a security boundary
against a hostile local user or process — see the limitations document.

---

## Install

Requires Python 3.11+ and git. Claude Code CLI (`claude`) is needed only for
real worker runs.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"
relay --help
a2a --help
```

`python -m relay` and `python -m a2a` work without installing.

## Run the tests

```bash
python -m pytest -q
```

The suite never launches the real Claude CLI: end-to-end tests replace it with
a scripted fake worker (`tests/fake_worker.py`). See
[docs/development.md](docs/development.md).

## Run the acceptance demonstration (no API credit used)

```bash
python demo/acceptance_demo.py
```

Drives the real `relay` and `a2a` CLIs end to end against a throwaway copy of
`test-repository/`, with the fake worker, covering the success path and the
failure paths (test weakening, forbidden capability, unapproved task, timeout
and retry, crash recovery, T2 and T3 protocol walk-throughs). It writes a
transcript; the committed one is in
[docs/acceptance/acceptance-transcript.md](docs/acceptance/acceptance-transcript.md).

## One real Claude run

```bash
python demo/save_api_key.py             # or double-click "Save API key.bat": paste, save, check
python demo/real_run.py                 # uses ~/.relay_api_key.txt; small API cost
```

`save_api_key.py` takes the key from the clipboard or a hidden prompt, saves it
to `~/.relay_api_key.txt`, and checks it with Anthropic's free models endpoint.
It never prints the key.

Runs `sample_task.json` with the real Claude Code CLI against a throwaway copy
of `test-repository/` and writes a Markdown report (status, Relay's
verification, the worker's diff, the commands and their output). A successful
run ends in `ACCEPTANCE_REQUIRED`: the sample's numeric criteria are yours to
check and acknowledge.

The v1 real run (2026-10-16) is recorded in
[docs/acceptance/real-run-2026-10-16.md](docs/acceptance/real-run-2026-10-16.md).

## The office: dashboard and in-tray

```bash
python -m office.jobs list                       # the five in-tray jobs
python -m office.jobs run social-batch           # reads ~/lunneys-social, read-only
python -m office.jobs run crontab-guard --live   # read-only SSH: crontab -l
python -m office.jobs run morning-briefing       # add --live for robots.txt + sitemap
python -m office.jobs run category-images --live # CCP 3 audit of public category pages
python -m office                                 # writes office.html
python -m office.server                          # the office as an app: a button per job
python -m office.jobs run backup-check --live    # age and size of the newest database copy
python -m office.jobs run share-preview --live --url https://loshop.co.uk/   # link preview
python -m office.notify                          # Windows notification if a job needs a look
```

`office.html` shows what is waiting on you (Relay decisions, A2A gates, the T3
re-run), the in-tray results, audit-chain and ledger health, and recent activity
by desk. It opens both databases read-only (their contents are never changed) and executes nothing; every action is
a command for you to run. Jobs that read the live server or website refuse to do
so without `--live`. Details: [operator guide §9](docs/operator-guide.md#9-the-office-dashboard-and-in-tray).

## Operating it

Step-by-step instructions for a real run — preparing a task, approval,
execution, reviewing verification, acknowledging criteria, recovery, and
recording each protocol step in the ledger — are in the
**[operator guide](docs/operator-guide.md)**.

## Documentation map

| Document | For |
|---|---|
| [docs/operator-guide.md](docs/operator-guide.md) | Running tasks, reading results, recovering, using the ledger |
| [docs/limitations.md](docs/limitations.md) | What is not enforced or not implemented, and why |
| [docs/verification-integrity.md](docs/verification-integrity.md) | How Relay decides what the worker changed and what it may not change |
| [docs/development.md](docs/development.md) | Code layout, test strategy, conventions |
| [docs/a2a-dev.md](docs/a2a-dev.md) | A2A package reference |
| [docs/a2a-protocol-v0.2.md](docs/a2a-protocol-v0.2.md) | The frozen collaboration protocol (authoritative, unchanged) |
| [docs/history.md](docs/history.md) | How the prototype got here (P0 → P1 → v1 batches) |
