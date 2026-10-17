# Operator Guide

This guide is for the person running Relay and recording protocol steps in the A2A ledger. Every command here was run against the real CLIs.

The scenarios end to end are in `demo/acceptance_demo.py`, and its transcript is `docs/acceptance/acceptance-transcript.md`.

Both CLIs:
- print errors to stderr;
- exit with code `1` on failure;
- say explicitly when no state was changed.

`--db` selects the database and must come before the command (`relay --db my.db status ...`). The defaults are `relay.db` and `a2a.db` in the current directory.

---

## 0. One-time setup

```bash
pip install -e ".[dev]"            # or use: python -m relay / python -m a2a
```

**For real worker runs:**
- The Claude Code CLI (`claude`) must be on PATH.
- `RELAY_API_KEY_FILE` must point to a file that contains an Anthropic API key. Relay passes the key to the worker through an `apiKeyHelper`. The key never enters the worker's environment.

```powershell
$env:RELAY_API_KEY_FILE = "C:\secure\anthropic-key.txt"     # PowerShell
```

---

## 1. Prepare a task

A task is a JSON file. The schema is `schemas/task.schema.json`, and it matches the runtime validator exactly.

`sample_task.json` is a working example. Set `repository` to the absolute path of the target repository first. It must be the root of its own git repository. Relay copies it and never modifies it. Paths matching production patterns (currently any path containing `lunneys-social`, `loshop`, `production` or `prod`) are rejected by policy.

| Field | Notes |
|---|---|
| `project_id`, `repository`, `objective` | Required and non-empty. |
| `base_ref` | The branch or tag the worker starts from (default `main`). Plain names only. |
| `requested_actions` | Must be within policy: `read_repository`, `modify_working_tree`, `run_tests`. Anything else, including `push`, `deploy` or `merge`, is rejected. These are checked and given to the worker as context; the worker's actual tools are fixed by Relay. |
| `required_tests` | The tests Relay runs itself, for example `test_calculator.py` or `tests/`. Optional in the schema, but **in practice required**: a task without them cannot pass verification. They must exist at the baseline. |
| `acceptance_criteria` | Only phrases such as "All tests pass" are checked automatically. Everything else becomes `UNVERIFIED` and needs your acknowledgement (§5). |
| `expected_new_files` | Workspace-relative paths the worker may create. Any other new file fails verification. |
| `expected_deleted_tests` | Baseline test IDs the task may delete. |
| `approval_policy` | `auto` (READY immediately) or `manual` (BLOCKED until you approve it). |

Unknown fields are rejected. For example, a task file containing `"approved": true` cannot pre-approve itself.

## 2. Validate it

```bash
relay create-task my_task.json
```

| Result | Meaning |
|---|---|
| `Status: READY` | Ready to run. |
| `Status: BLOCKED` | `approval_policy` is `manual`. Approve or reject it: `relay approve-task TASK-XXXXXXXX --actor <you> [--reason ...]` or `relay reject-task TASK-XXXXXXXX --actor <you> --reason ...`. |
| `Task REJECTED: ...` (exit 1) | The policy refused it. The reason names the rule. |
| `Invalid task definition` / `not valid JSON` / `not UTF-8` (exit 1) | Nothing was created. |

## 3. Run it

```bash
relay run-task TASK-XXXXXXXX --workspace ./workspaces --timeout 300 [--keep-workspace]
```

What happens, in order:
1. Relay takes an exclusive lease on the workspace.
2. It copies the repository and checks out `base_ref`.
3. It collects the baseline tests and records an integrity hash of every file.
4. It runs the worker.
5. It verifies independently.

The outcome is printed on the last line:

| Outcome | Meaning | What to do |
|---|---|---|
| `COMPLETED (independently verified)` | Every check passed and there are no UNVERIFIED criteria. | Review the diff if you like (use `--keep-workspace` to keep the copy). |
| `ACCEPTANCE_REQUIRED` | Automated verification passed, but some criteria could not be checked. The workspace is kept. | §5. |
| `FAILED - Verification failed. Errors: [...]` | The work was rejected. `Errors` names protected test or config changes and unparseable output. Other reasons can leave `Errors: []`: look in the verification record (§4) at `no_op`, `unexpected_files`, `deleted_tests`, `deactivated_tests`, `test_exit_code` and `acceptance_results`. | Read `relay status` and `relay audit`. Do not retry blindly. |
| `FAILED - <reason>` with no verification | The worker never ran: authentication, `claude` not found, checkout failure. | Fix the cause and create a new task. |
| `TIMEOUT` | The worker exceeded `--timeout` (capped at 300 s); its process tree was ended. | `relay retry-task TASK-XXXXXXXX --actor <you>`. |
| `Workspace lease is held by another execution. Claude was not launched ... No state was changed.` (or `Task is no longer READY ...`) | Another run owns this task. The task's status is unchanged; the refused attempt is still recorded as a failed execution and audit event. | Wait for it, or see §6. |
| `RECOVERY_REQUIRED - Verification aborted ...` | Verification crashed or was interrupted. | §6. |

## 4. Inspect the result and the evidence

```bash
relay status TASK-XXXXXXXX        # status, attempts, every execution, criteria awaiting you
relay audit TASK-XXXXXXXX         # the full append-only event trail
relay audit                       # all events
relay audit --verify              # check the audit log's hash chain; prints the head hash
```

`relay audit --verify` exits 1 and lists the problems if any event was edited, deleted or re-ordered, or inserted mid-log. Note the head hash it prints, for example at each A2A gate: a log that was later rebuilt or cut short would no longer match it.

The full verification record is the `VERIFICATION_COMPLETED` event. It includes:

| Field | Contents |
|---|---|
| `files_changed` / `files_added` / `files_deleted` | What the worker changed, according to Relay's own hash manifest rather than git. |
| `protected_violations` | Altered tests or test configuration. Any entry fails the task. |
| `unexpected_files` | New files that were not in `expected_new_files`. |
| `ignored_artifacts` | Bytecode caches. These are reported, and they are never read during verification. |
| `test_exit_code`, `deleted_tests`, `deactivated_tests` | The test results. |
| `acceptance_results` | `PASS` / `FAIL` / `UNVERIFIED` per criterion. |

The worker's own report is recorded as evidence. It never decides the outcome.

## 5. Acknowledge criteria Relay could not check

For a task in `ACCEPTANCE_REQUIRED`:
1. `relay status` lists the criteria that are waiting on you.
2. Check them yourself in the kept workspace (`./workspaces/TASK-XXXXXXXX`).
3. Record your decision:

```bash
relay acknowledge-criteria TASK-XXXXXXXX --actor <you> --reason "checked 84.99 by hand"
relay acknowledge-criteria TASK-XXXXXXXX --actor <you> --reject --reason "wrong rounding"
```

Accepting makes the task `COMPLETED`, and rejecting makes it `FAILED`. Your decision is recorded as its own audit event. The automated verdict stays `UNVERIFIED`, so the record always shows what software checked and what a human checked.

## 6. Recover from an interrupted or failed execution

```bash
relay recover
relay accept-recovery TASK-XXXXXXXX --actor <you> --workspace ./workspaces
```

`relay recover` finds:
- executions whose lease expired or whose Relay process died;
- tasks left RUNNING or VERIFYING with no lease, for example after a crash during verification.

It marks them `RECOVERY_REQUIRED`. It never marks them COMPLETED.

Before accepting:
- Make sure no `claude` process for the task is still running. On Windows, `accept-recovery` cannot move a workspace that a running process is using and will refuse; on other platforms it cannot tell.
- Pass the same `--workspace` base that `run-task` used. With a different base, nothing is moved aside and the next run replaces the old workspace.
- `accept-recovery` moves the old workspace aside to `TASK-XXXXXXXX.interrupted.<id>` so you can inspect it. It refuses to proceed (and changes nothing) if the workspace cannot be moved.
- Accepting makes the task READY for another attempt. Each task allows at most 3 attempts.

---

## 7. Record protocol steps in the A2A ledger

The ledger follows `docs/a2a-protocol-v0.2.md`. Each step names who is acting and in which role:
- `HUMAN` is you.
- `CHATGPT` is used when you record ChatGPT's spec or review.
- `CLAUDE` is used for implementation steps.

Name the person or agent honestly. The ledger refuses a step the protocol assigns to a different role. It also refuses an actor who has already acted on the task in another role.

Actor names must be plain: letters and digits joined by single `.`, `_`, `@`, `-` or space, such as `craig`, `Craig Paterson` or `chatgpt`. Names are compared case-insensitively, so `Claude` and `claude` are the same actor.

```bash
a2a create-task --title "Fix discount rounding" --tier T2 \
    --issue-url https://github.com/<owner>/<repo>/issues/<n> --actor craig

a2a advance A2A-XXXXXXXX --to SPECCING        --actor craig   --role HUMAN
a2a advance A2A-XXXXXXXX --to AWAITING_GATE_1 --actor chatgpt --role CHATGPT --evidence <spec comment URL>
a2a advance A2A-XXXXXXXX --to IN_IMPLEMENTATION --actor craig --role HUMAN          # Gate 1
#   ... run the work through Relay until the Relay task is COMPLETED ...
a2a advance A2A-XXXXXXXX --to UNDER_REVIEW    --actor claude  --role CLAUDE \
    --evidence <PR URL> --relay-task TASK-XXXXXXXX
a2a advance A2A-XXXXXXXX --to AWAITING_GATE_2 --actor chatgpt --role CHATGPT --evidence <review comment URL>
a2a advance A2A-XXXXXXXX --to SHIPPED         --actor craig   --role HUMAN   --evidence <post-deploy evidence URL>
a2a advance A2A-XXXXXXXX --to CLOSED          --actor craig   --role HUMAN
```

- **T1 tasks** skip specification and review: DRAFT → IN_IMPLEMENTATION → AWAITING_GATE_2 (cite the Relay task here) → SHIPPED → CLOSED.
- **T3 tasks:** before SHIPPED, re-run one prescribed verification command yourself and record it:

  ```bash
  a2a record-human-verification A2A-XXXXXXXX --actor craig \
      --command "python -m pytest -q" --output-file rerun-output.txt
  ```

- **Escalating a T2 task to T3:** if an amendment adds an EXCLUSIVE resource, do it at Gate 1. Escalation is one-way:

  ```bash
  a2a escalate-tier A2A-XXXXXXXX --actor craig --reason "now touches crontab" --evidence <amendment URL>
  ```

- **Decisions that need `--reason`:** rejecting at Gate 1 (→ DRAFT), resolving or abandoning a discovery, returning to Gate 1 (resource conflict, protocol §7.2, or significant amendment, protocol §10.3), and closing as INCOMPLETE (protocol §12.4).
- **Steps that need `--evidence`:** any step triggered by a posted artefact, such as a spec, PR, review or discovery report.

```bash
a2a get-task A2A-XXXXXXXX     # state plus the full attested history and any re-runs
a2a list-tasks
a2a check                     # exit 1 if any task's state disagrees with its recorded history
```

The relay database is read **read-only** (`--relay-db`, default `relay.db`). Only a `COMPLETED` Relay task counts as evidence, and each Relay task can be cited by one A2A task only.

---

## 8. Housekeeping

- Workspaces of `COMPLETED`, `FAILED` and `TIMEOUT` tasks are deleted unless you passed `--keep-workspace`. Workspaces for `ACCEPTANCE_REQUIRED` tasks are kept, and stay after you acknowledge; recovered workspaces are moved aside. Delete them when you're done.
- Back up `relay.db` and `a2a.db` like any other record. They are plain SQLite files.
- Read `docs/limitations.md` for what these controls do and do not protect against.

---

## 9. The office: dashboard and in-tray

```bash
python -m office --relay-db relay.db --a2a-db a2a.db --out office.html [--intray DIR]
python -m office.jobs [--intray DIR] list
python -m office.jobs [--intray DIR] run <job> [--live] [job options]
```

The in-tray folder defaults to `~/.agent-office/intray`; set `"intray": "<folder>"` in `~/.agent-office/config.json` to keep the reports somewhere else (for example a OneDrive folder). Only the reports move: the config file stays in `~/.agent-office`. Each run writes
`<job>.json` there (status, summary, findings, next step); the dashboard shows
the latest result of every job. The exit code is 1 when a job's status is
`ALERT` or `ERROR`, so a scheduler can notice.

| Job | Level | What it does | Live access |
|---|---|---|---|
| `backup-check` | Report | Over SSH, lists the newest two files matching the configured pattern (`stat`, read-only) and alerts if the newest is older than `max_age_days` (7), under `min_mb` (20) or under 80% of the previous one. On loshop the copies are the manual `db_backup_*.sql` dumps taken before site changes; Krystal's own daily backups are not visible to it. | Read-only SSH, only with `--live`. |
| `share-preview` | Report | For one `https://loshop.co.uk/` link (`--url`; other sites are refused): reads the page's og/twitter tags and fetches the share image; flags a missing title, description or image, an image that does not load, is under 600 px wide or is not about 1.91:1, and notes when the page uses the general Lunneys share image. The office shows a Facebook-style card. Not part of the morning briefing. | HTTPS GET of that page and its image, only with `--live`. |
| `ai-readiness` | Report | Samples `--sample` (40) enabled, visible products via the REST API (token from the `magento_api` config, never printed; `/rest/all/V1` only) plus a quarter as many Euronics agency items, which are reported separately and not scored. Per product: the product-copy checks; brand, GTIN (check digit), MPN against the SKU, price, stock, energy rating and dimensions where relevant, and Google Shopping exclusion; the page's Product JSON-LD against the catalogue (name, brand, GTIN, price, availability, delivery, rating); the og:image as share-preview checks it. Site: robots.txt must let the AI and search crawlers fetch product pages (ALERT if not) and `/llms.txt` must exist. Writes `ai-readiness.md` (area scores, top 10 fixes) and `ai-readiness.csv`. Fetches are `--delay` (2 s) apart with the bot-check back-off; the sample is seeded by the date (`--seed`). | Read-only REST GETs and HTTPS GETs of the sampled pages, their images, robots.txt and llms.txt, only with `--live`. |
| `crontab-guard` | Report | `crontab -l` on your server; alerts if it has fewer lines than the baseline or if a required entry is missing or commented out. Extra lines are reported, not alerted. Server details come from the local config file below. | Read-only SSH, only with `--live` (key-based; `BatchMode`, so it never prompts). |
| `morning-briefing` | Report | One page (`morning-briefing-YYYY-MM-DD.md`): what's waiting on you, every other job's latest result, and with `--live` the robots.txt guard (Googlebot rules, AI-crawler block, apex sitemap line, no query-string Disallow) and the sitemap. | HTTPS GET of two public URLs, only with `--live`. |
| `social-batch` | Draft | Picks approved knowledge items (`LUNNEYS_APPROVED` + `APPROVED`) that have no approved visual yet, one per category first, and prints the `knowledge_run.py` commands. You approve at Gate 1 and Gate 2 as usual. `--social-repo`, `--count`. | None; reads the Lunneys Social folder. |
| `category-images` | Change (plan only) | CCP 3. With `--live`, Phase 1: reads `sitemap.xml`, takes the category pages (priority 0.5), fetches each one (2 s apart, `--delay`) and records whether its `og:image` is the store-logo fallback. Writes `ccp3-audit.md` and `ccp3-audit.csv` (rows written as they arrive). Stops at `--max-minutes` (50). If the host's "One moment, please" bot check answers, it waits 20 s and retries once, then reports `bot check`. Phase 2 (artwork, live changes) stays a plan for your approval. | HTTPS GET of `sitemap.xml` and every category page, only with `--live`; only `https://loshop.co.uk/` URLs are fetched. |
| `reviews-replies` | Draft | With `--input reviews.json` (a list of `{name, rating, text, product}`), writes `reviews-reply-brief.md`: reply rules, reviews to draft, and anything rated 3 or below for you first. Posts nothing. | None. |

The crontab guard reads its server details from `~/.agent-office/config.json`
(`--config` to use another file), which stays on your machine and out of the
repository:

```json
{
  "crontab_guard": {
    "ssh_target": "user@your-host.example",
    "ssh_port": 22,
    "ssh_options": ["MACs=hmac-sha2-256"],
    "baseline_lines": 25,
    "required_entries": {"Magento cron": "bin/magento cron:run"}
  },
  "backup_check": {
    "glob": "/home/user/backups/db_backup_*.sql",
    "max_age_days": 7,
    "min_mb": 20
  }
}
```

To retire a job you no longer want, add `"retired_jobs": ["category-images"]` (job
names from `python -m office.jobs list`) to the same file: it disappears from the
office buttons, the in-tray and the morning briefing, and can still be run by name.
Remove the name to bring it back.

`ssh_options` may only set algorithm, key and known-hosts options; anything that
could run a command (such as `ProxyCommand`) is refused. Without a config file the
job reports "Not configured" and does nothing.

### Alerts

`python -m office.notify` shows a Windows notification when any job that is not
retired last reported `ALERT` or `ERROR`; otherwise it does nothing.
`--test` shows a test notification. The morning routine runs it after the jobs.

### The office app

```bash
python -m office.server            # http://127.0.0.1:8765/ opens in your browser
```

The dashboard with a **Hand out a job** panel: one button per job. **Run** does
the job's offline part; the orange **Run live** buttons read the live server or
website. One job runs at a time and the page refreshes itself until it is done;
the last job's output is shown on the page. Close the app's window to stop it.

It is for this computer only: it listens on 127.0.0.1, refuses requests for any
other host name, needs a code created at start-up on every button press (so
another website cannot press them), cannot be framed and loads nothing from
outside. A second copy cannot start on the same port. Times on every office
page are UK time (GMT/BST); stored records stay in UTC.

Run the live checks yourself the first time and read the output before you
schedule them. Nothing in the office writes to the server, the website,
Buffer, Reviews.io or Base.

