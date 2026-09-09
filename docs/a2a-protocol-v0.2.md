# A2A Engineering Collaboration Protocol — v0.2

**Status:** Frozen.
**Frozen:** 2026-09-09
**Replaces:** v0.1 (2026-09-09)
**Scope:** Human-initiated engineering tasks delegated across Human → ChatGPT → Claude Code → GitHub.

---

## 1. Overview

Three principles govern every task:

1. **No agent self-certifies.** Every claim of success requires verifiable evidence anchored in GitHub.
2. **WHAT and HOW are separate.** The Human defines what success looks like. ChatGPT defines how to achieve it. Claude builds it. ChatGPT reviews against the Human's criteria — not its own specification.
3. **GitHub is the permanent record.** Code state, specifications, evidence, decisions, and approvals all live in GitHub. Anything not in GitHub does not exist in the protocol.

---

## 2. Roles and Authority Boundaries

| Actor | Authority | Cannot Do |
|---|---|---|
| **Human** | Write/change AC, assign tiers, approve gates, override protocol steps, resolve all conflicts, mark SHIPPED | Bypass safety invariants (§11) |
| **ChatGPT** | Write CCP Spec and Verification Commands, draft AC proposals for Human confirmation, review against Human's AC | Edit the AC section directly, resolve conflicts without Human decision, approve its own spec as a review |
| **Claude** | Implement, run prescribed verification commands, post evidence, report discoveries, refuse safety invariant bypasses | Choose its own verification methods, self-approve, proceed without prescribed commands, bypass safety invariants |
| **GitHub** | Permanent record of code, specs, evidence, and decisions | Nothing — passive record |

**ChatGPT → GitHub:** ChatGPT produces all review comments, spec texts, amendment notes, and retrospective reviews as formatted text in the conversation. The Human copies these verbatim into the relevant GitHub Issue or PR comment. The pasted comment must begin with the appropriate header (`CHATGPT REVIEW —`, `SPEC AMENDMENT`, `RETROSPECTIVE REVIEW`, etc.). The Human may not edit the content before pasting. If the Human disagrees with part of a review, they post a separate Human comment immediately below it — they do not alter ChatGPT's text.

---

## 3. Risk Tiers

Every Task Brief must state the tier. If omitted, ChatGPT assigns one when writing the spec and the Human confirms at Gate 1.

### T1 — Low Risk

**Qualifies when:** Change touches only CSS, templates (`*.phtml`), or `pub/static-files`. No DB changes, no crontab, no PHP logic, no deploy sequence beyond `cache:flush`.

**Protocol:** Gate 2 only. No Gate 1. No ChatGPT CCP Spec. No ChatGPT review.
- Human writes AC directly in the Issue
- Human instructs Claude to proceed
- Claude implements and posts evidence comment on the PR
- Human reviews diff and evidence directly at Gate 2
- Safety invariants still apply

**Minimum evidence:** Commit hash + origin-direct `curl` output confirming **all** AC items + post-deploy evidence comment on Issue.

### T2 — Standard

**Qualifies when:** Any change not meeting T1 or T3 criteria.

**Protocol:** Full v0.2 protocol — both gates, ChatGPT spec, ChatGPT review.

### T3 — High Risk

**Qualifies when:** Task declares any EXCLUSIVE resource (`crontab`, `config.php`, `db:schema`, `deploy:sequence`).

**Protocol:** Full v0.2 protocol plus: before Gate 2 approval, the Human independently re-runs at least one prescribed verification command from the CCP Spec and posts their own verbatim output as a separate PR comment. Gate 2 may not proceed without this Human-posted output.

---

## 4. GitHub Issue Structure

Every task lives in one GitHub Issue. Mandatory structure:

```
## Acceptance Criteria          ← Human-authored. Immutable after Gate 1.
AC-1: [verifiable outcome]
AC-2: [verifiable outcome]

## CCP Spec                     ← ChatGPT-authored (T2/T3 only).
### Implementation Steps
### Resources
### Verification Commands
### Rollback Plan
### Deploy Sequence
```

For T1 tasks, the Issue contains only the `## Acceptance Criteria` section. No CCP Spec section is added.

---

## 5. Acceptance Criteria — WHAT

**Author:** Human only. ChatGPT never edits the AC section of the Issue body.

**Draft-and-confirm flow** (for informal requests): ChatGPT proposes numbered AC items as an Issue *comment*. Human reviews, edits if needed, and pastes the final version into the Issue body `## Acceptance Criteria` section. The Human's act of writing it into the Issue body is the authorship signal.

**Immutability:** Frozen after Gate 1 approval (`approved-for-implementation` label). To change AC: Human removes the label, edits the AC section, re-approves. Gate 1 re-approval is required whenever the AC changes.

**ChatGPT's review** checks the implementation against the AC items by number — not against CCP Spec steps.

---

## 6. CCP Spec — HOW (T2/T3 only)

ChatGPT authors all subsections after the AC is confirmed. All five subsections are mandatory — a spec missing any one is rejected at Gate 1.

**`### Implementation Steps`** — numbered, atomic, reversible-first order.

**`### Resources`** — shared resource declarations (§7). Missing = spec rejected.

**`### Verification Commands`** — one prescribed command block per AC item (§9). Missing = spec rejected.

**`### Rollback Plan`** — how to undo each implementation step.

**`### Deploy Sequence`** — verbatim command chain.

---

## 7. Concurrent Resource Control

### 7.1 Resource Declaration

The `### Resources` section uses fixed vocabulary, two tiers:

```
EXCLUSIVE: crontab, deploy:sequence
STANDARD:  db:rows, theme/module-files
```

| Resource | Tier | Notes |
|---|---|---|
| `crontab` | EXCLUSIVE | One task at a time, no exceptions |
| `config.php` | EXCLUSIVE | Module enable/disable; CLI `module:disable` prohibited — use direct PHP config edit |
| `db:schema` | EXCLUSIVE | New attributes, new tables, EAV structural changes |
| `deploy:sequence` | EXCLUSIVE | Any task requiring `setup:di:compile` and/or `setup:static-content:deploy`. Tasks requiring only `cache:flush` do not declare this resource. |
| `pub/static-files` | STANDARD | `robots.txt`, `.htaccess`, `pub/*.html` — note for awareness |
| `db:rows` | STANDARD | Product/category/CMS row updates — safe to parallelise across different entities |
| `theme/module-files` | STANDARD | Filesystem conflicts surfaced by git |

### 7.2 Conflict Resolution

**At Gate 1:** Human scans all open `IN_IMPLEMENTATION` Issues for overlapping EXCLUSIVE resources before approving. Conflict found: Human posts `RESOURCE HOLD — waiting for Issue #N to reach SHIPPED` on the waiting task. Gate 1 deferred until the blocking task reaches SHIPPED.

**Post-Gate-1 conflict discovered:** Human posts `RESOURCE CONFLICT — HOLD` on the later task's PR. Claude stops immediately. Earlier task proceeds to SHIPPED first; later task returns to `AWAITING_GATE_1`. Both Issues record the incident.

### 7.3 Crontab Hard Rules

Three non-negotiable rules for any task declaring `EXCLUSIVE: crontab`:

1. Only one such task may be `IN_IMPLEMENTATION` at any time.
2. Baseline is captured **atomically at edit time** — not at task start. Baseline captured at task start is invalid. Mandatory pattern:

```bash
CTFILE=/tmp/ct_$(date +%Y%m%d_%H%M%S)
crontab -l > "$CTFILE"
echo "BASELINE: $(wc -l < "$CTFILE") lines"
echo "NEW_ENTRY" >> "$CTFILE"
crontab "$CTFILE"
echo "POST-EDIT: $(crontab -l | wc -l) lines"
```

3. Post-edit count ≠ baseline + lines added → Claude immediately restores from `$CTFILE`, verifies count matches baseline, and transitions to `BLOCKED-BY-DISCOVERY`. No PR is opened until resolved.

---

## 8. State Machine

### T2 / T3 (Full Protocol)

```
DRAFT ──(Human submits Brief)──► SPECCING
                                     │
                    (ChatGPT posts complete CCP Spec in Issue)
                                     │
                                     ▼
                              AWAITING_GATE_1
                                     │
                     (Human: approved-for-implementation label)
                                     │
                                     ▼
                         ┌── IN_IMPLEMENTATION ◄──────────────┐
                         │           │                         │
              (discovery)│           │                (resolved)│
                         ▼           │                         │
               BLOCKED-BY-DISCOVERY──┘                         │
                                     │                         │
                    (Claude: PR + pre-merge evidence comment)   │
                                     │                         │
                                     ▼                         │
                              UNDER_REVIEW                      │
                                     │                         │
              ┌──────────────────────┴──────────────────────┐  │
              │ ChatGPT: BLOCKED                             │  │
              ▼                                              │  │
       REVISION_NEEDED ─────────────────────────────────────┘  │
                        (Claude revises + re-posts evidence)    │
                                                                │
                          ChatGPT: UNBLOCKED-PENDING-HUMAN-MERGE│
                                        │                       │
                                        ▼                       │
                                 AWAITING_GATE_2                │
                                        │                       │
                              (Human merges PR)                 │
                                        │                       │
                     (Claude: deploy + post-deploy evidence      │
                      comment on Issue)                         │
                                        │                       │
                         ┌──────────────┴──────────────┐       │
                         │ post-deploy AC fails          │ pass  │
                         ▼                              ▼       │
                  REVISION_NEEDED ──────────────────► SHIPPED   │
                         │                      (Human closes   │
                         └──────────────────────── Issue)       │
                                                                │
                                                              CLOSED
```

### T1 (Simplified)

```
DRAFT ──(Human writes AC + instructs Claude)──► IN_IMPLEMENTATION
                                                        │
                                        (Claude: PR + evidence comment)
                                                        │
                                                 AWAITING_GATE_2
                                                        │
                                     (Human reviews diff + evidence directly)
                                                        │
                                              (Human merges PR)
                                                        │
                                   (Claude: post-deploy evidence on Issue)
                                                        │
                              ┌─────────────────────────┴───────────────┐
                              │ post-deploy AC fails                      │ pass
                              ▼                                          ▼
                       REVISION_NEEDED                                SHIPPED
                                                               (Human closes Issue)
                                                                      │
                                                                    CLOSED
```

`BLOCKED-BY-DISCOVERY` applies to all tiers and may interrupt `IN_IMPLEMENTATION` on either path.

**Valid transitions only.** No agent may advance state without the defined trigger.

---

## 9. Evidence Requirements

### 9.1 Verification Commands (ChatGPT — T2/T3 only)

The `### Verification Commands` section prescribes one command block per AC item:

```
AC-1: og:description must contain prose, not CSS
  CMD: curl -s --resolve loshop.co.uk:443:77.72.2.78 \
       https://loshop.co.uk/home-appliances.html \
       | grep -o 'og:description" content="[^"]*"'
  EXPECT: value begins with a letter, not '.' or '{'

AC-2: No store_id=1 orphan after REST write
  CMD: mysql -u USER -p'PASS' -N -B -e \
       "SELECT store_id, value FROM mgkm_catalog_category_entity_varchar
        WHERE attribute_id=51 AND entity_id=44 ORDER BY store_id;"
  EXPECT: exactly one row, store_id=0

REGRESSION (T2/T3 — prescribed, not Claude's choice):
  URL-1: https://loshop.co.uk/
  URL-2: https://loshop.co.uk/home-appliances/laundry/washing-machines.html
  URL-3: https://loshop.co.uk/brands/dyson.html
```

Claude runs these commands verbatim. Substitution of an equivalent command is not permitted.

### 9.2 Mandatory Evidence Rules (All Tiers)

| Rule | Requirement |
|---|---|
| **Origin-direct curl** | All live-site checks use `--resolve loshop.co.uk:443:77.72.2.78`. Non-`--resolve` output is inadmissible for live-site AC items. |
| **JSON-LD checks** | Must use the Python parser pattern. `grep '"@type":` against loshop pages is prohibited — documented to return false negatives regardless of page state. |
| **Verbatim output** | Raw terminal output only. No paraphrase, no summary. |
| **Commit-anchored** | `git rev-parse HEAD` captured immediately before running verifications, included in evidence header, must match PR head commit. |
| **After push** | Verification commands run after `git push`. Pre-push output is not evidence. |

### 9.3 Pre-task State Capture (All Tiers)

Before implementation begins, Claude captures the current state of any resource the spec (T2/T3) or task (T1) makes explicit assumptions about (`SELECT` before an `UPDATE`, `crontab -l` before a crontab edit, relevant config snippet before a config change). Posted as the opening comment on the PR. Detects divergence between assumed and actual state before any change is made.

### 9.4 Pre-merge Evidence Bundle (PR Comment — All Tiers)

Posted as a **PR comment** (not the PR description — comments show edit history; descriptions do not).

**T2/T3 format:**
```
PRE-MERGE EVIDENCE — CCP-07
Commit: a3f9d2c  (git rev-parse HEAD)
Run at: 2026-09-09T14:32Z

AC-1: [exact CMD as prescribed in Verification Commands]
OUTPUT: [verbatim]
RESULT: PASS

AC-2: [exact CMD as prescribed]
OUTPUT: [verbatim]
RESULT: PASS

REGRESSION (as prescribed in CCP Spec):
  [URL-1]: [verbatim]
  [URL-2]: [verbatim]
  [URL-3]: [verbatim]
```

**T1 format:**
```
PRE-MERGE EVIDENCE — T1-TASK-03
Commit: a3f9d2c  (git rev-parse HEAD)
Run at: 2026-09-09T14:32Z

AC-1: [CMD used, with --resolve]
OUTPUT: [verbatim]
RESULT: PASS

AC-2: [CMD used, with --resolve]
OUTPUT: [verbatim]
RESULT: PASS
```

T1 tasks have no prescribed regression URLs. Human determines at Gate 2 whether any regression check is needed.

### 9.5 Post-deploy Evidence (Issue Comment — All Tiers)

After the deploy sequence completes, Claude posts a **separate comment on the GitHub Issue**:

```
POST-DEPLOY EVIDENCE — CCP-07
Deployed at: 2026-09-09T15:10Z
Commit: a3f9d2c

AC-1: [CMD with --resolve] → OUTPUT: [verbatim] → PASS
AC-2: [CMD] → OUTPUT: [verbatim] → PASS
```

The Human does not close the Issue until this comment exists and all AC items show PASS.

### 9.6 ChatGPT Review (T2/T3 only — Autonomous Scope)

ChatGPT cannot SSH or curl. Honest scope:

**ChatGPT verifies autonomously:**
- Commands used = commands prescribed in spec (command fidelity — checked first, blocking if failed)
- Required flags present (`--resolve`, Python parser, `-N -B` for mysql batch)
- Output format consistent with what the commands actually produce
- Commit hash present and matches PR head
- No red flags in output (error codes, `0 rows`, empty `content=""`, tracebacks)
- Each AC item satisfied based on the output

**ChatGPT review format** (Human pastes verbatim into PR comment):

```
CHATGPT REVIEW — CCP-07
────────────────────────
Command fidelity:      PASS / FAIL [list any substitutions]
Required flags:        PASS / FAIL
Output plausibility:   PASS / FAIL [flag red flags]
AC-1:                  PASS / FAIL
AC-2:                  PASS / FAIL
Scope adherence:       PASS / FAIL [diff vs spec — flag extra files/rows]
Security check:        PASS / FAIL
Overall verdict:       UNBLOCKED-PENDING-HUMAN-MERGE / BLOCKED [reason + line refs]
```

### 9.7 T3 Human Re-run (T3 only)

Before Gate 2 approval, the Human independently runs at least one prescribed verification command from the CCP Spec and posts their own verbatim output as a separate PR comment:

```
HUMAN VERIFICATION — CCP-07
CMD: [exact command run]
OUTPUT: [verbatim]
```

Gate 2 may not proceed without this comment.

---

## 10. Unexpected Discoveries

### 10.1 Stop or Continue Decision

**Stop and post a discovery report (→ BLOCKED-BY-DISCOVERY) when proceeding would require:**

- Touching a resource outside the task's authorised scope:
  - T2/T3: a resource not declared in `### Resources`
  - T1: anything outside CSS, templates, and `pub/static-files`
- An irreversible action not authorised by the agreed scope:
  - T2/T3: not in the CCP Spec
  - T1: not in the AC or tier constraints
- An outcome different from the AC (any tier)
- Skipping a safety invariant (§11) (any tier)
- Significantly more rows/files affected than the agreed scope described

**Note in evidence bundle and continue when:**
- HOW deviates but AC outcome is still reachable via the same authorised resources
- A known site pattern applies directly (e.g. `store_id=1` orphan after REST write — expected; DELETE of orphan is within `db:rows` scope if already declared)
- A pre-existing problem exists in adjacent code/data — document it, do not touch it
- The deviation is defensive and reversible (e.g. an additional backup)

**Rule of thumb:** if a senior developer would fix it without asking, Claude notes it and continues. If they would raise it first, Claude stops.

### 10.2 Discovery Report Format

Posted as a comment on the **GitHub Issue** (not the PR):

```
DISCOVERY — CCP-07
──────────────────
Sources in conflict: [if applicable — e.g. CCP Spec vs Repository]
What I found:        [specific finding — verbatim value, file path, or SQL output]
Why this matters:    [impact on AC, resources, or authorised scope]
Options:
  A) [action + risk]
  B) [action + risk]
Claude recommends:   A — [one sentence]
Status: BLOCKED-BY-DISCOVERY
Claude has not proceeded beyond this point.
```

Claude does not push code before the discovery is resolved.

### 10.3 Scope Amendment Tiers

| Type | Trigger | Process |
|---|---|---|
| **Minor** | HOW changes; AC unchanged; no new EXCLUSIVE resource | Human approval comment on Issue + ChatGPT posts `SPEC AMENDMENT [date]: [what and why]` comment. No Gate 1 re-run. |
| **Significant** | AC changes, or new EXCLUSIVE resource added | Human edits AC section. ChatGPT rewrites affected spec section. Gate 1 re-approval required. |

Claude's evidence bundle must reference any discovery comments by GitHub comment number.

---

## 11. Human Overrides

### 11.1 Override Format

The Human may bypass any protocol step by posting on the GitHub Issue:

```
OVERRIDE: [step being skipped] — [reason]
```

- Posted by the **Human only** — not Claude, not ChatGPT
- Must name the step and give a reason — blank reasons are not valid
- Claude does not proceed without this comment existing in the Issue
- Verbal instructions are not valid overrides

Claude acknowledges in its first PR comment: `NOTE: Operating under OVERRIDE — [step] skipped per Issue comment #[N]`

### 11.2 ChatGPT Review Under Override

**T2/T3 overrides:** ChatGPT review is retrospective and non-blocking. Posted after the PR is open (or after merge if Gate 2 was overridden), labelled `RETROSPECTIVE REVIEW`. Flags risks for Human awareness — cannot block anything. The overridden Issue may not be closed as SHIPPED until ChatGPT has posted the `RETROSPECTIVE REVIEW` comment. If urgent closure is needed before the review is complete, the Human posts `RETROSPECTIVE-WAIVED: [reason]` on the Issue — treated as a second override, permanently logged, and must state why the review was not obtained.

**T1 overrides:** No retrospective review required. ChatGPT has no role in T1 tasks. The Human notes the overridden step in the Issue closure comment when marking the task SHIPPED.

### 11.3 Safety Invariants — Never Bypassable

Claude refuses to skip these and states why. This is Claude's only authority to decline a Human instruction.

| Invariant | Why Non-Bypassable |
|---|---|
| DB backup before any DB change | Recovery without it is impossible. A dump takes 30 seconds. The faster the emergency, the more it is needed. |
| `.bak` before editing any non-git-tracked file (`app/etc/config.php`, `app/etc/env.php`, `pub/robots.txt`, `.htaccess`, server-side configs) | No version history exists for these files; `.bak` is the only recovery path. For git-tracked source files, git history is the backup — no `.bak` required. |
| Crontab atomic pattern (§7.3) | Incident history: wipe caused 45-minute outage. Splitting the capture across calls is the known failure mode. |
| Minimal GitHub record | Commit hash + one-sentence description of what changed. Without this the source-of-truth guarantee collapses entirely. |

### 11.4 Minimum Evidence Floor Under Override

Even under full override, Claude must post: commit hash, one-sentence description of what changed, reference to the OVERRIDE comment by number. Zero-record deploys are not permitted.

---

## 12. Task Closure and SHIPPED

### 12.1 SHIPPED Conditions

All three must be true simultaneously:

1. **Code in git** — PR merged to main branch
2. **Change is live** — deploy sequence complete appropriate to the change type
3. **AC satisfied on live production site** — post-deploy evidence comment exists on the Issue with all AC items showing PASS, verified against origin (not CF cache)

Merge alone is not SHIPPED.

### 12.2 How SHIPPED Is Marked

The **Human** closes the Issue and adds the `shipped` label. Deliberate act. No automation.

Trigger: Claude's post-deploy evidence comment (§9.5) exists on the Issue with all AC items PASS, and (for T3) the Human re-run comment exists on the PR.

### 12.3 Merge Does Not Auto-Close

PR merge is a code event. Issue closure is a production state. They are not coupled. Deploy on loshop is manual (SSH); merge does not deploy.

### 12.4 Merged But Not Complete

If post-deploy verification fails after merge: Issue stays open, task returns to `REVISION_NEEDED`. Claude opens a follow-up PR on the same Issue. An Issue may have multiple PRs. SHIPPED requires AC satisfied on live site — a merged PR that does not satisfy the AC does not close the Issue.

If the remaining gap is substantial and unrelated to the original spec, the Human may close the Issue as `INCOMPLETE` and open a new DRAFT task.

### 12.5 Permanent Record

| Location | Required Content |
|---|---|
| **Issue** | AC section · CCP Spec (T2/T3) · Discovery/Override/Amendment comments · Post-deploy evidence comment · Closure comment · `shipped` label |
| **PR** | Diff · Pre-task state capture comment · Pre-merge evidence bundle comment · ChatGPT review comment (T2/T3) · T3 Human re-run comment (T3) |

**Minimum for any closed Issue:** AC items, commit hash, post-deploy evidence comment, closure comment. Issues closed without these four are protocol violations and must be reopened and annotated.

---

## 13. Source-of-Truth Hierarchy

### 13.1 The Rule

**AC wins for WHAT. Repo reflects current state. When they conflict, the Human decides.**

| Source | Authoritative For |
|---|---|
| Human's AC | What success looks like — always wins |
| CCP Spec | Intended HOW (T2/T3) |
| Repository / code | Actual current state |
| PR | Proposed change — bridge between intended and actual |

### 13.2 Conflict Resolution

| Situation | Resolution |
|---|---|
| Repo satisfies AC, deviates from spec | Repo wins. ChatGPT posts `SPEC AMENDMENT` comment. No Gate 1 re-run. |
| Repo does not satisfy AC | Repo must be fixed. Spec guides the correction. Task stays open. |
| Spec assumptions about current repo state are wrong | BLOCKED-BY-DISCOVERY. Human decides approach. |
| PR diff touches files/rows not in spec | ChatGPT flags in review (scope adherence check). Human decides if acceptable. |
| AC is ambiguous or incorrect | Human edits AC section. Gate 1 re-approval required. |
| Repo already satisfies some or all AC items | Claude reports before implementing. Does not re-implement what is already correct. |

### 13.3 Stale Spec Amendment

When resolved in favour of the repo (spec was stale, AC still satisfied): ChatGPT posts `SPEC AMENDMENT [date]: [what changed and why]` as a comment below the CCP Spec section. The Issue body is not edited. No Gate 1 re-run. The amendment comment is the permanent record.

---

## 14. Human Approval Gates

### Gate 1 (T2/T3 only)

**Trigger:** ChatGPT has posted a complete CCP Spec in the Issue (all five subsections present).

**Human checks before approving:**
- AC section is non-empty and correctly represents the task intent
- Tier is correctly assigned
- `### Resources` section declares all affected resources with correct tiers
- `### Verification Commands` section has one command block per AC item with `--resolve` on all live-site checks
- No open EXCLUSIVE resource conflict with current `IN_IMPLEMENTATION` Issues
- Rollback plan exists

**Signal:** `approved-for-implementation` label on Issue.

### Gate 2 (All Tiers)

**Trigger (T2/T3):** ChatGPT has posted `UNBLOCKED-PENDING-HUMAN-MERGE` on the PR.
**Trigger (T1):** PR is open with pre-merge evidence comment; Human reviews directly.

**Human checks before merging:**
- Pre-merge evidence bundle comment exists on PR with commit hash
- T2/T3: ChatGPT verdict is `UNBLOCKED-PENDING-HUMAN-MERGE`
- T2/T3: Evidence references prescribed commands (command fidelity confirmed by ChatGPT)
- T3: Human re-run comment exists on PR with verbatim output
- No open `BLOCKED` or `HOLD` comments unresolved

**Signal:** PR merge.

---

## 15. Failure Scenarios

| Scenario | Detection | Recovery |
|---|---|---|
| Missing evidence | ChatGPT completeness check fails | REVISION_NEEDED. ChatGPT lists missing items. Claude posts new evidence comment (does not edit original). |
| Command substitution | ChatGPT fidelity check fails | BLOCKED. Claude re-runs using prescribed commands verbatim. |
| Crontab count mismatch | Post-edit count ≠ expected | Claude restores from `$CTFILE` immediately. Verifies. BLOCKED-BY-DISCOVERY. New task opened to re-attempt. |
| Post-deploy verification fails | Post-deploy evidence shows FAIL | Issue stays open. REVISION_NEEDED. Follow-up PR on same Issue. |
| EXCLUSIVE resource conflict found late | Human or Claude detects overlap | Human posts HOLD on later task's PR. Claude stops. Earlier task proceeds first. Incident noted in both Issues. |
| Discovery mid-implementation | Spec assumption wrong or undeclared resource needed | BLOCKED-BY-DISCOVERY on Issue. Claude does not push. Waits for Human resolution. |
| Merged but AC not satisfied | Post-deploy evidence fails | Issue stays open. REVISION_NEEDED. Follow-up PR. |
| Override without T2/T3 retrospective review | Human needs urgent closure | `RETROSPECTIVE-WAIVED: [reason]` on Issue. Permanently logged. |

---

## 16. Design History

This protocol was developed through the following sessions:

| Session | Topic |
|---|---|
| A2A-01 | Initial protocol design (v0.1) |
| A2A-02 | Adversarial review — identified 9 structural weaknesses |
| A2A-03 | Independent Acceptance Criteria — separated WHAT from HOW |
| A2A-04 | Evidence integrity — prescribed commands, origin-direct curl, Python JSON-LD parser |
| A2A-05 | Concurrent resource control — EXCLUSIVE/STANDARD vocabulary, crontab hard rules |
| A2A-06 | Unexpected discoveries — BLOCKED-BY-DISCOVERY state, stop/continue decision rule |
| A2A-07 | Human overrides — OVERRIDE format, safety invariants, retrospective review |
| A2A-08 | Task closure — two-stage evidence model, SHIPPED conditions, post-deploy evidence |
| A2A-09 | Source-of-truth hierarchy — AC > CCP Spec for WHAT; repo for current state |
| A2A-10 | v0.2 consolidation |
| A2A-11 | Resolved open items H-1 through H-4 (tiering, re-run, retrospective, GitHub access) |
| A2A-12 | Final v0.2 draft |
| A2A-12.5 | Consistency check — identified 6 internal contradictions |
| A2A-12.6 | Applied 6 consistency fixes; READY TO FREEZE verdict |
| A2A-13 | Frozen |
