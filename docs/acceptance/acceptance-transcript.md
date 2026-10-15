# Agent to Agent v1 — Acceptance Transcript

Generated 2026-10-15 21:33 UTC by `demo/acceptance_demo.py` on Python 3.14.3 (win32).

Real `relay` and `a2a` CLIs; the Claude worker is the scripted fake worker (`tests/fake_worker.py`), so no API credit is used. `<demo>` is a fresh temp dir.

**Result: 39/39 expectations met.**

| Expectation | Result |
|---|---|
| task is validated and READY | PASS |
| run-task exits 0 and the task is COMPLETED | PASS |
| verifier saw exactly calculator.py change; integrity checked; no protected violations | PASS |
| acceptance criterion "All tests pass" is PASS | PASS |
| audit trail records creation, policy, execution, verification and completion | PASS |
| task is FAILED | PASS |
| verifier reports the protected test file as modified and did not run it | PASS |
| a worker that changes nothing fails with no_op | PASS |
| a correct fix plus an undeclared new file fails, and only because of that file | PASS |
| create-task exits 1 and the task is recorded as REJECTED | PASS |
| a task file that tries to pre-approve itself is refused; no task is created | PASS |
| task is BLOCKED | PASS |
| run-task refuses a BLOCKED task and changes nothing | PASS |
| after approval it runs and is COMPLETED | PASS |
| a BLOCKED task can be rejected by a human (REJECTED, terminal) | PASS |
| task is TIMEOUT | PASS |
| the 3 s timeout is enforced: the worker tree is killed well before its 12 s sleep ends | PASS |
| retry-task makes it READY | PASS |
| the retry completes | PASS |
| the worker had really started when Relay was killed | PASS |
| recover reports the task and it is RECOVERY_REQUIRED | PASS |
| accept-recovery preserves the interrupted workspace and makes the task READY | PASS |
| the re-run completes | PASS |
| verification passes but the task is ACCEPTANCE_REQUIRED | PASS |
| the numeric criterion is UNVERIFIED (not silently passed) | PASS |
| relay status lists the criterion awaiting human acknowledgement | PASS |
| Claude cannot approve Gate 1 (Human-only) | PASS |
| a Relay task still awaiting acceptance is not accepted as evidence | PASS |
| citing the COMPLETED Relay task from scenario 1 moves it to UNDER_REVIEW | PASS |
| the implementer cannot also act as reviewer | PASS |
| the T2 task reaches CLOSED | PASS |
| get-task shows the attested history with the cited Relay task | PASS |
| after human acknowledgement the scenario 8 task is COMPLETED | PASS |
| SHIPPED is refused without a recorded human re-run | PASS |
| after the re-run is recorded, SHIPPED succeeds | PASS |
| a T2 task is escalated to T3 at Gate 1 and the change is recorded | PASS |
| list-tasks shows every ledger task | PASS |
| the ledger is consistent | PASS |
| `a2a check` detects the out-of-ledger state change | PASS |

## 1. Successful task

A correct fix is independently verified and COMPLETED.

```
$ relay create-task <demo>\task-1.json
Task created and validated: TASK-67D1BB1F
Status: READY
[exit 0]
```
- PASS: task is validated and READY
```
$ relay run-task TASK-67D1BB1F --workspace <demo>\workspaces --timeout 60 --keep-workspace
Starting execution for TASK-67D1BB1F...
Execution EXEC-21744C54: COMPLETED
Task TASK-67D1BB1F: COMPLETED (independently verified)
[exit 0]
```
- PASS: run-task exits 0 and the task is COMPLETED
- PASS: verifier saw exactly calculator.py change; integrity checked; no protected violations
- PASS: acceptance criterion "All tests pass" is PASS
```
$ relay audit TASK-67D1BB1F
[0001] 2026-10-15T21:33:36.603752+00:00 TASK_CREATED                   task=TASK-67D1BB1F exec=- actor=relay
[0002] 2026-10-15T21:33:36.605270+00:00 POLICY_CHECKED                 task=TASK-67D1BB1F exec=- actor=relay
[0003] 2026-10-15T21:33:36.606414+00:00 TASK_VALIDATED                 task=TASK-67D1BB1F exec=- actor=relay
[0004] 2026-10-15T21:33:39.359415+00:00 EXECUTION_STARTED              task=TASK-67D1BB1F exec=EXEC-21744C54 actor=relay
[0005] 2026-10-15T21:33:39.459028+00:00 EXECUTION_COMPLETED            task=TASK-67D1BB1F exec=EXEC-21744C54 actor=relay
[0006] 2026-10-15T21:33:39.464001+00:00 VERIFICATION_STARTED           task=TASK-67D1BB1F exec=EXEC-21744C54 actor=relay
[0007] 2026-10-15T21:33:42.773596+00:00 VERIFICATION_COMPLETED         task=TASK-67D1BB1F exec=EXEC-21744C54 actor=relay
[0008] 2026-10-15T21:33:42.776359+00:00 TASK_COMPLETED                 task=TASK-67D1BB1F exec=- actor=relay
[exit 0]
```
- PASS: audit trail records creation, policy, execution, verification and completion

## 2. Test weakening is rejected

The worker leaves the bug unfixed and redefines every test as `pass` with the same names (the audit's critical attack).

```
$ relay create-task <demo>\task-2.json
Task created and validated: TASK-8654BD32
Status: READY
[exit 0]
```
```
$ relay run-task TASK-8654BD32 --workspace <demo>\workspaces --timeout 60 --keep-workspace
Starting execution for TASK-8654BD32...
Execution EXEC-7950D78B: COMPLETED
Task TASK-8654BD32: FAILED - Verification failed. Errors: ['Protected tests or test configuration were altered by the worker: modified: test_calculator.py (test file)']
[exit 1]
```
- PASS: task is FAILED
- PASS: verifier reports the protected test file as modified and did not run it

## 3. Unchanged work and undeclared files are rejected



```
$ relay create-task <demo>\task-3.json
Task created and validated: TASK-41E4F516
Status: READY
[exit 0]
```
```
$ relay run-task TASK-41E4F516 --workspace <demo>\workspaces --timeout 60 --keep-workspace
Starting execution for TASK-41E4F516...
Execution EXEC-496C6072: COMPLETED
Task TASK-41E4F516: FAILED - Verification failed. Errors: []
[exit 1]
```
- PASS: a worker that changes nothing fails with no_op
```
$ relay create-task <demo>\task-4.json
Task created and validated: TASK-85AAA51E
Status: READY
[exit 0]
```
```
$ relay run-task TASK-85AAA51E --workspace <demo>\workspaces --timeout 60 --keep-workspace
Starting execution for TASK-85AAA51E...
Execution EXEC-41A2CAC9: COMPLETED
Task TASK-85AAA51E: FAILED - Verification failed. Errors: []
[exit 1]
```
- PASS: a correct fix plus an undeclared new file fails, and only because of that file

## 4. Forbidden capability

A task that requests `push` is rejected by policy.

```
$ relay create-task <demo>\task-5.json
Task REJECTED: Capability 'push' is explicitly forbidden by Relay policy.
[exit 1]
```
- PASS: create-task exits 1 and the task is recorded as REJECTED
```
$ relay create-task <demo>\task-6.json
Invalid task definition: task_input: unexpected field(s) ['approved'] - task input must not contain unrecognised fields
[exit 1]
```
- PASS: a task file that tries to pre-approve itself is refused; no task is created

## 5. Manual approval gate

A manual-policy task cannot run until a human approves it.

```
$ relay create-task <demo>\task-7.json
Task created and validated: TASK-53B19604
Status: BLOCKED (awaiting manual human approval)
Run: relay approve-task TASK-53B19604 --actor <name>
[exit 0]
```
- PASS: task is BLOCKED
```
$ relay run-task TASK-53B19604 --workspace <demo>\workspaces --timeout 60 --keep-workspace
Task TASK-53B19604 is BLOCKED. Expected READY.
[exit 1]
```
- PASS: run-task refuses a BLOCKED task and changes nothing
```
$ relay approve-task TASK-53B19604 --actor craig --reason demo approval
Task TASK-53B19604 approved.
Approval ID: APR-1D90B9F3
Approver:    craig
Status:      READY
[exit 0]
```
```
$ relay run-task TASK-53B19604 --workspace <demo>\workspaces --timeout 60 --keep-workspace
Starting execution for TASK-53B19604...
Execution EXEC-44FE470C: COMPLETED
Task TASK-53B19604: COMPLETED (independently verified)
[exit 0]
```
- PASS: after approval it runs and is COMPLETED
```
$ relay create-task <demo>\task-8.json
Task created and validated: TASK-18115DF2
Status: BLOCKED (awaiting manual human approval)
Run: relay approve-task TASK-18115DF2 --actor <name>
[exit 0]
```
```
$ relay reject-task TASK-18115DF2 --actor craig --reason not now
Task TASK-18115DF2 rejected.
Approval ID: APR-87CE4270
Approver:    craig
Status:      REJECTED
[exit 0]
```
- PASS: a BLOCKED task can be rejected by a human (REJECTED, terminal)

## 6. Timeout and retry

A hung worker times out; the operator retries.

```
$ relay create-task <demo>\task-9.json
Task created and validated: TASK-F7707E48
Status: READY
[exit 0]
```
```
$ relay run-task TASK-F7707E48 --workspace <demo>\workspaces --timeout 3 --keep-workspace
Starting execution for TASK-F7707E48...
Execution EXEC-E4B694D5: TIMEOUT
Task TASK-F7707E48: TIMEOUT
[exit 1]
```
- PASS: task is TIMEOUT
- PASS: the 3 s timeout is enforced: the worker tree is killed well before its 12 s sleep ends
```
$ relay retry-task TASK-F7707E48 --actor craig
Task TASK-F7707E48: queued for retry.
Status: READY
[exit 0]
```
- PASS: retry-task makes it READY
```
$ relay run-task TASK-F7707E48 --workspace <demo>\workspaces --timeout 60 --keep-workspace
Starting execution for TASK-F7707E48...
Execution EXEC-E91EE6A1: COMPLETED
Task TASK-F7707E48: COMPLETED (independently verified)
[exit 0]
```
- PASS: the retry completes

## 7. Crash recovery

Relay itself is killed mid-execution. `relay recover` detects it; the operator accepts recovery; the interrupted workspace is preserved; the task re-runs.

```
$ relay create-task <demo>\task-10.json
Task created and validated: TASK-65C07F6E
Status: READY
[exit 0]
```
```
$ relay run-task TASK-65C07F6E --workspace <demo>\workspaces --timeout 60 --keep-workspace   (started in background)
```
- PASS: the worker had really started when Relay was killed
```
(relay run-task process killed while the worker was running)
```
```
$ relay recover
Recovery required for: TASK-65C07F6E
[exit 0]
```
- PASS: recover reports the task and it is RECOVERY_REQUIRED
```
$ relay accept-recovery TASK-65C07F6E --actor craig --workspace <demo>\workspaces
Workspace preserved: <demo>\workspaces\TASK-65C07F6E.interrupted.EXEC-007
Task TASK-65C07F6E: recovery accepted.
Status: READY
[exit 0]
```
- PASS: accept-recovery preserves the interrupted workspace and makes the task READY
```
$ relay run-task TASK-65C07F6E --workspace <demo>\workspaces --timeout 60 --keep-workspace
Starting execution for TASK-65C07F6E...
Execution EXEC-AD83AC91: COMPLETED
Task TASK-65C07F6E: COMPLETED (independently verified)
[exit 0]
```
- PASS: the re-run completes

## 8. Criteria Relay cannot check need a human

A criterion beyond 'the tests pass' is UNVERIFIED; the task waits in ACCEPTANCE_REQUIRED until acknowledged.

```
$ relay create-task <demo>\task-11.json
Task created and validated: TASK-F65DE73F
Status: READY
[exit 0]
```
```
$ relay run-task TASK-F65DE73F --workspace <demo>\workspaces --timeout 60 --keep-workspace
Starting execution for TASK-F65DE73F...
Execution EXEC-09FC7506: COMPLETED
Task TASK-F65DE73F: ACCEPTANCE_REQUIRED (automated verification passed; human acknowledgement needed)
Criteria not verified automatically:
  - calculate_discount_price(99.99, 15.0) == 84.99
Workspace kept for inspection: <demo>\workspaces\TASK-F65DE73F
Run: relay acknowledge-criteria TASK-F65DE73F --actor <name> [--reject] [--reason <text>]
[exit 0]
```
- PASS: verification passes but the task is ACCEPTANCE_REQUIRED
- PASS: the numeric criterion is UNVERIFIED (not silently passed)
```
$ relay status TASK-F65DE73F
Task:       TASK-F65DE73F
Status:     ACCEPTANCE_REQUIRED
Attempts:   1/3
Repository: <demo>\target-repo
Objective:  Fix the floor-division bug in calculate_discount_price.
Awaiting human acknowledgement of criteria not verified automatically:
  - calculate_discount_price(99.99, 15.0) == 84.99

Executions:
  [1] EXEC-09FC7506: COMPLETED (exit=0, ended=2026-10-15T21:34:37.059434+00:00)
[exit 0]
```
- PASS: relay status lists the criterion awaiting human acknowledgement

## 9. A2A T2 walk-through (attested ledger)

Every protocol step is attested by a role; review requires a COMPLETED Relay task.

```
$ a2a create-task --title Fix discount rounding --tier T2 --issue-url https://github.com/craig-lunney/relay-prototype/issues/12 --actor craig
Created: A2A-6CA89C70
A2A-6CA89C70  [DRAFT]  T2  Fix discount rounding
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/12
  Created: 2026-10-15T21:34:38.851578+00:00
[exit 0]
```
```
$ a2a advance A2A-6CA89C70 --to SPECCING --actor craig --role HUMAN
Advanced: A2A-6CA89C70
A2A-6CA89C70  [SPECCING]  T2  Fix discount rounding
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/12
  Created: 2026-10-15T21:34:38.851578+00:00
[exit 0]
```
```
$ a2a advance A2A-6CA89C70 --to AWAITING_GATE_1 --actor chatgpt --role CHATGPT --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
Advanced: A2A-6CA89C70
A2A-6CA89C70  [AWAITING_GATE_1]  T2  Fix discount rounding
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/12
  Created: 2026-10-15T21:34:38.851578+00:00
[exit 0]
```
```
$ a2a advance A2A-6CA89C70 --to IN_IMPLEMENTATION --actor claude --role CLAUDE
Error: Cannot advance task 'A2A-6CA89C70' from AWAITING_GATE_1 to IN_IMPLEMENTATION: AWAITING_GATE_1 \u2192 IN_IMPLEMENTATION (Gate 1: Human approves for implementation) may only be performed by HUMAN, not CLAUDE. No state was changed.
[exit 1]
```
- PASS: Claude cannot approve Gate 1 (Human-only)
```
$ a2a advance A2A-6CA89C70 --to IN_IMPLEMENTATION --actor craig --role HUMAN
Advanced: A2A-6CA89C70
A2A-6CA89C70  [IN_IMPLEMENTATION]  T2  Fix discount rounding
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/12
  Created: 2026-10-15T21:34:38.851578+00:00
[exit 0]
```
```
$ a2a advance A2A-6CA89C70 --to UNDER_REVIEW --actor claude --role CLAUDE --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo --relay-task TASK-F65DE73F
Error: Cannot advance task 'A2A-6CA89C70' from IN_IMPLEMENTATION to UNDER_REVIEW: Relay task 'TASK-F65DE73F' is ACCEPTANCE_REQUIRED; only a COMPLETED Relay task (independently verified, with any UNVERIFIED criteria acknowledged) counts as implementation evidence. No state was changed.
[exit 1]
```
- PASS: a Relay task still awaiting acceptance is not accepted as evidence
```
$ a2a advance A2A-6CA89C70 --to UNDER_REVIEW --actor claude --role CLAUDE --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo --relay-task TASK-67D1BB1F
Advanced: A2A-6CA89C70
A2A-6CA89C70  [UNDER_REVIEW]  T2  Fix discount rounding
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/12
  Created: 2026-10-15T21:34:38.851578+00:00
[exit 0]
```
- PASS: citing the COMPLETED Relay task from scenario 1 moves it to UNDER_REVIEW
```
$ a2a advance A2A-6CA89C70 --to AWAITING_GATE_2 --actor claude --role CHATGPT --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
Error: Cannot advance task 'A2A-6CA89C70' from UNDER_REVIEW to AWAITING_GATE_2: Actor 'claude' already acted on this task as CLAUDE; one actor may not take two protocol roles on the same task (no self-review or self-approval). No state was changed.
[exit 1]
```
- PASS: the implementer cannot also act as reviewer
```
$ a2a advance A2A-6CA89C70 --to AWAITING_GATE_2 --actor chatgpt --role CHATGPT --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
Advanced: A2A-6CA89C70
A2A-6CA89C70  [AWAITING_GATE_2]  T2  Fix discount rounding
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/12
  Created: 2026-10-15T21:34:38.851578+00:00
[exit 0]
```
```
$ a2a advance A2A-6CA89C70 --to SHIPPED --actor craig --role HUMAN --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
Advanced: A2A-6CA89C70
A2A-6CA89C70  [SHIPPED]  T2  Fix discount rounding
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/12
  Created: 2026-10-15T21:34:38.851578+00:00
[exit 0]
```
```
$ a2a advance A2A-6CA89C70 --to CLOSED --actor craig --role HUMAN
Advanced: A2A-6CA89C70
A2A-6CA89C70  [CLOSED]  T2  Fix discount rounding
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/12
  Created: 2026-10-15T21:34:38.851578+00:00
[exit 0]
```
- PASS: the T2 task reaches CLOSED
```
$ a2a get-task A2A-6CA89C70
A2A-6CA89C70  [CLOSED]  T2  Fix discount rounding
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/12
  Created: 2026-10-15T21:34:38.851578+00:00
  History:
    #1 2026-10-15T21:34:38.851578+00:00 (created) -> DRAFT by craig [HUMAN]
        reason: task created
    #2 2026-10-15T21:34:38.953530+00:00 DRAFT -> SPECCING by craig [HUMAN]
    #3 2026-10-15T21:34:39.060330+00:00 SPECCING -> AWAITING_GATE_1 by chatgpt [CHATGPT]
        evidence: https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
    #4 2026-10-15T21:34:39.256312+00:00 AWAITING_GATE_1 -> IN_IMPLEMENTATION by craig [HUMAN]
    #5 2026-10-15T21:34:39.517580+00:00 IN_IMPLEMENTATION -> UNDER_REVIEW by claude [CLAUDE]
        evidence: https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
        relay task: TASK-67D1BB1F
    #6 2026-10-15T21:34:39.723561+00:00 UNDER_REVIEW -> AWAITING_GATE_2 by chatgpt [CHATGPT]
        evidence: https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
    #7 2026-10-15T21:34:39.825108+00:00 AWAITING_GATE_2 -> SHIPPED by craig [HUMAN]
        evidence: https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
    #8 2026-10-15T21:34:39.926946+00:00 SHIPPED -> CLOSED by craig [HUMAN]
[exit 0]
```
- PASS: get-task shows the attested history with the cited Relay task

## 10. A2A T3 walk-through: human re-run

A T3 task cannot ship until a human re-run is recorded (protocol §9.7).

```
$ relay acknowledge-criteria TASK-F65DE73F --actor craig --reason checked 84.99 by hand
Task TASK-F65DE73F: criteria accepted by craig.
  - calculate_discount_price(99.99, 15.0) == 84.99 (UNVERIFIED by Relay; human-accepted)
Status: COMPLETED
[exit 0]
```
- PASS: after human acknowledgement the scenario 8 task is COMPLETED
```
$ a2a create-task --title Rotate crontab entry --tier T3 --issue-url https://github.com/craig-lunney/relay-prototype/issues/13 --actor craig
Created: A2A-3227AF33
A2A-3227AF33  [DRAFT]  T3  Rotate crontab entry
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/13
  Created: 2026-10-15T21:34:40.272146+00:00
[exit 0]
```
```
$ a2a advance A2A-3227AF33 --to SPECCING --actor craig --role HUMAN
Advanced: A2A-3227AF33
A2A-3227AF33  [SPECCING]  T3  Rotate crontab entry
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/13
  Created: 2026-10-15T21:34:40.272146+00:00
[exit 0]
```
```
$ a2a advance A2A-3227AF33 --to AWAITING_GATE_1 --actor chatgpt --role CHATGPT --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
Advanced: A2A-3227AF33
A2A-3227AF33  [AWAITING_GATE_1]  T3  Rotate crontab entry
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/13
  Created: 2026-10-15T21:34:40.272146+00:00
[exit 0]
```
```
$ a2a advance A2A-3227AF33 --to IN_IMPLEMENTATION --actor craig --role HUMAN
Advanced: A2A-3227AF33
A2A-3227AF33  [IN_IMPLEMENTATION]  T3  Rotate crontab entry
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/13
  Created: 2026-10-15T21:34:40.272146+00:00
[exit 0]
```
```
$ a2a advance A2A-3227AF33 --to UNDER_REVIEW --actor claude --role CLAUDE --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo --relay-task TASK-F65DE73F
Advanced: A2A-3227AF33
A2A-3227AF33  [UNDER_REVIEW]  T3  Rotate crontab entry
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/13
  Created: 2026-10-15T21:34:40.272146+00:00
[exit 0]
```
```
$ a2a advance A2A-3227AF33 --to AWAITING_GATE_2 --actor chatgpt --role CHATGPT --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
Advanced: A2A-3227AF33
A2A-3227AF33  [AWAITING_GATE_2]  T3  Rotate crontab entry
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/13
  Created: 2026-10-15T21:34:40.272146+00:00
[exit 0]
```
```
$ a2a advance A2A-3227AF33 --to SHIPPED --actor craig --role HUMAN --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
Error: Cannot advance task 'A2A-3227AF33' from AWAITING_GATE_2 to SHIPPED: T3 Gate 2 may not proceed without a recorded human re-run of a prescribed verification command (protocol §9.7). Run 'a2a record-human-verification A2A-3227AF33 ...' first. No state was changed.
[exit 1]
```
- PASS: SHIPPED is refused without a recorded human re-run
```
$ a2a record-human-verification A2A-3227AF33 --actor craig --command python -m pytest -q test_calculator.py --output 17 passed
Recorded human re-run for A2A-3227AF33 by craig.
[exit 0]
```
```
$ a2a advance A2A-3227AF33 --to SHIPPED --actor craig --role HUMAN --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
Advanced: A2A-3227AF33
A2A-3227AF33  [SHIPPED]  T3  Rotate crontab entry
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/13
  Created: 2026-10-15T21:34:40.272146+00:00
[exit 0]
```
- PASS: after the re-run is recorded, SHIPPED succeeds
```
$ a2a create-task --title Adjust cron cadence --tier T2 --issue-url https://github.com/craig-lunney/relay-prototype/issues/14 --actor craig
Created: A2A-7184728C
A2A-7184728C  [DRAFT]  T2  Adjust cron cadence
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/14
  Created: 2026-10-15T21:34:41.449792+00:00
[exit 0]
```
```
$ a2a advance A2A-7184728C --to SPECCING --actor craig --role HUMAN
Advanced: A2A-7184728C
A2A-7184728C  [SPECCING]  T2  Adjust cron cadence
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/14
  Created: 2026-10-15T21:34:41.449792+00:00
[exit 0]
```
```
$ a2a advance A2A-7184728C --to AWAITING_GATE_1 --actor chatgpt --role CHATGPT --evidence https://github.com/craig-lunney/relay-prototype/issues/1#issuecomment-demo
Advanced: A2A-7184728C
A2A-7184728C  [AWAITING_GATE_1]  T2  Adjust cron cadence
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/14
  Created: 2026-10-15T21:34:41.449792+00:00
[exit 0]
```
```
$ a2a escalate-tier A2A-7184728C --actor craig --reason amendment adds EXCLUSIVE resource: crontab
Escalated: A2A-7184728C is now T3
A2A-7184728C  [AWAITING_GATE_1]  T3  Adjust cron cadence
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/14
  Created: 2026-10-15T21:34:41.449792+00:00
[exit 0]
```
- PASS: a T2 task is escalated to T3 at Gate 1 and the change is recorded
```
$ a2a list-tasks
A2A-6CA89C70  [CLOSED]  T2  Fix discount rounding
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/12
  Created: 2026-10-15T21:34:38.851578+00:00

A2A-3227AF33  [SHIPPED]  T3  Rotate crontab entry
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/13
  Created: 2026-10-15T21:34:40.272146+00:00

A2A-7184728C  [AWAITING_GATE_1]  T3  Adjust cron cadence
  Issue:   https://github.com/craig-lunney/relay-prototype/issues/14
  Created: 2026-10-15T21:34:41.449792+00:00
[exit 0]
```
- PASS: list-tasks shows every ledger task
```
$ a2a check
Ledger consistent: every task's state matches its recorded history.
[exit 0]
```
- PASS: the ledger is consistent
```
(raw SQL: UPDATE a2a_tasks SET state='CLOSED' ... bypassing the ledger)
```
```
$ a2a check
A2A-3227AF33: stored state CLOSED does not match the last recorded transition (SHIPPED); the state was changed outside the ledger.
[exit 1]
```
- PASS: `a2a check` detects the out-of-ledger state change
