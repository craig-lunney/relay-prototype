from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .audit import AuditLog
from .executor import ExecutionEngine
from .models import Approval, Task, TaskStatus
from .policy import PolicyEngine
from .recovery import RecoveryEngine
from .schemas import ValidationError, validate_task_input
from .state import StateManager
from .verifier import Verifier


def _make_components(db_path: str):
    state = StateManager(db_path)
    policy = PolicyEngine()
    audit = AuditLog(state)
    return state, policy, audit


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_create_task(args) -> None:
    state, policy, audit = _make_components(args.db)

    task_def = json.loads(Path(args.task_file).read_text(encoding="utf-8"))

    # P1-E: validate task definition structure before any state is created.
    # Rejects missing fields, wrong types, and unexpected fields (e.g. "approved":true).
    try:
        validate_task_input(task_def)
    except ValidationError as exc:
        print(f"Invalid task definition: {exc}", file=sys.stderr)
        sys.exit(1)

    task = Task(
        task_id=Task.new_id(),
        project_id=task_def["project_id"],
        repository=task_def["repository"],
        base_ref=task_def.get("base_ref", "main"),
        working_ref=task_def.get("working_ref", "main"),
        objective=task_def["objective"],
        requirements=task_def.get("requirements", []),
        acceptance_criteria=task_def.get("acceptance_criteria", []),
        requested_actions=task_def.get("requested_actions", []),
        forbidden_actions=task_def.get("forbidden_actions", []),
        required_tests=task_def.get("required_tests", []),
        expected_deleted_tests=task_def.get("expected_deleted_tests", []),
        approval_policy=task_def.get("approval_policy", "auto"),
        status=TaskStatus.CREATED,
    )

    state.create_task(task)
    audit.task_created_with_hash(task)

    state.update_task_status(task.task_id, TaskStatus.VALIDATING)

    result = policy.validate_task(task)
    audit.policy_checked(task.task_id, result.allowed, result.reason)

    if result.allowed:
        if result.requires_approval:
            # P1-F: manual approval required — task enters BLOCKED state.
            # Execution is forbidden until an operator runs 'relay approve-task'.
            state.update_task_status(task.task_id, TaskStatus.BLOCKED)
            audit.task_approval_required(task.task_id)
            print(f"Task created and validated: {task.task_id}")
            print(f"Status: BLOCKED (awaiting manual human approval)")
            print(f"Run: relay approve-task {task.task_id} --actor <name>")
        else:
            state.update_task_status(task.task_id, TaskStatus.READY)
            audit.task_validated(task.task_id)
            print(f"Task created and validated: {task.task_id}")
            print(f"Status: READY")
    else:
        state.update_task_status(
            task.task_id, TaskStatus.REJECTED, error=result.reason
        )
        audit.task_rejected(task.task_id, result.reason)
        print(f"Task REJECTED: {result.reason}", file=sys.stderr)
        sys.exit(1)


def cmd_run_task(args) -> None:
    state, policy, audit = _make_components(args.db)
    task = state.get_task(args.task_id)

    if task is None:
        print(f"Task {args.task_id} not found.", file=sys.stderr)
        sys.exit(1)
    if task.status != TaskStatus.READY:
        print(
            f"Task {args.task_id} is {task.status.value}. Expected READY.",
            file=sys.stderr,
        )
        sys.exit(1)

    engine = ExecutionEngine(state, audit, policy)
    verifier = Verifier()
    workspace_base = Path(args.workspace)
    keep_workspace = getattr(args, "keep_workspace", False)

    print(f"Starting execution for {task.task_id}...")
    execution = engine.run(task, workspace_base, timeout_seconds=args.timeout)
    print(f"Execution {execution.execution_id}: {execution.status.value}")

    # The discriminator for whether the worker process was actually launched.
    # exit_code is only set by proc.returncode or -1 on timeout; it stays None
    # for every pre-launch failure (approval denied, lease held, auth error, CLI
    # not found). This is the sole gate determining whether verification is valid.
    worker_ran = execution.exit_code is not None

    # P1-J: outer finally releases the workspace lease regardless of outcome
    # (verification success, verification failure, timeout, pre-launch failure).
    # The lease is held by engine.run() through the end of execution and released
    # here — never inside the individual branches.
    try:
        if execution.status.value == "COMPLETED" or (execution.status.value == "FAILED" and worker_ran):
            # P1-J: lease is held through verification to prevent a concurrent
            # execution from acquiring the workspace between worker exit and
            # verification completion.
            state.update_task_status(
                task.task_id, TaskStatus.VERIFYING, validate_transition=True
            )
            audit.verification_started(task.task_id, execution.execution_id)

            result = verifier.verify(task, execution)
            audit.verification_completed(
                task.task_id, execution.execution_id, result.passed, result.as_dict()
            )

            if result.passed:
                state.update_task_status(
                    task.task_id, TaskStatus.COMPLETED, validate_transition=True
                )
                audit.task_completed(task.task_id)
                print(f"Task {task.task_id}: COMPLETED (independently verified)")
                # H-2: clean up workspace after successful verification, unless
                # operator requested it be kept for inspection.
                if not keep_workspace and execution.workspace:
                    engine.cleanup_workspace(execution.workspace, workspace_base)
            else:
                reason = f"Verification failed. Errors: {result.errors}"
                state.update_task_status(
                    task.task_id, TaskStatus.FAILED, error=reason, validate_transition=True
                )
                audit.task_failed(task.task_id, reason)
                # H-2: clean up workspace even on verification failure; the failed
                # state is recorded in the DB and audit log.
                if not keep_workspace and execution.workspace:
                    engine.cleanup_workspace(execution.workspace, workspace_base)
                print(f"Task {task.task_id}: FAILED — {reason}", file=sys.stderr)
                sys.exit(1)

        elif execution.status.value == "FAILED":
            # Pre-launch failure: the worker was never launched (exit_code is None).
            # Verifying the unchanged baseline workspace could produce a false
            # TASK_COMPLETED if the existing tests already pass. Fail unconditionally.
            # P1-J: READY→FAILED is now in ALLOWED_TRANSITIONS, so validate_transition=True
            # is correct for both READY and RUNNING pre-launch failure paths.
            reason = execution.error or "Worker was not launched."
            state.update_task_status(
                task.task_id, TaskStatus.FAILED, error=reason, validate_transition=True
            )
            audit.task_failed(task.task_id, reason)
            if not keep_workspace and execution.workspace:
                engine.cleanup_workspace(execution.workspace, workspace_base)
            print(f"Task {task.task_id}: FAILED — {reason}", file=sys.stderr)
            sys.exit(1)

        elif execution.status.value == "TIMEOUT":
            state.update_task_status(
                task.task_id, TaskStatus.TIMEOUT, validate_transition=True
            )
            audit.execution_timeout(
                task.task_id, execution.execution_id, args.timeout
            )
            # H-2: clean up workspace after timeout; the lease is released by
            # the outer finally block.
            if not keep_workspace and execution.workspace:
                engine.cleanup_workspace(execution.workspace, workspace_base)
            print(f"Task {task.task_id}: TIMEOUT", file=sys.stderr)
            sys.exit(1)

    finally:
        if execution.workspace:
            engine.release_workspace_lease(execution.execution_id)


def cmd_approve_task(args) -> None:
    """Record a Relay operator approval, unblocking a manual-policy task."""
    state, _, audit = _make_components(args.db)
    task = state.get_task(args.task_id)

    if task is None:
        print(f"Task {args.task_id} not found.", file=sys.stderr)
        sys.exit(1)
    if task.status != TaskStatus.BLOCKED:
        print(
            f"Task {args.task_id} is {task.status.value}. "
            "Only BLOCKED tasks can be approved.",
            file=sys.stderr,
        )
        sys.exit(1)
    if task.approval_policy != "manual":
        print(
            f"Task {args.task_id} has approval_policy={task.approval_policy!r}. "
            "Only manual-policy tasks use the approval gate.",
            file=sys.stderr,
        )
        sys.exit(1)

    # P1-F: is_human=True is set HERE and only here — in the Relay operator CLI path.
    # This flag is never set from task JSON, repository content, or agent output.
    # The security boundary is this code path, not the actor string (the prototype
    # has no operator authentication mechanism).
    approval = Approval(
        approval_id=Approval.new_id(),
        task_id=task.task_id,
        approver=args.actor,
        decision="APPROVED",
        reason=getattr(args, "reason", None),
        timestamp=datetime.now(timezone.utc),
        is_human=True,
    )
    state.record_approval(approval)
    state.update_task_status(task.task_id, TaskStatus.READY)
    audit.task_approved(task.task_id, approval.approval_id, args.actor,
                        getattr(args, "reason", None))

    print(f"Task {task.task_id} approved.")
    print(f"Approval ID: {approval.approval_id}")
    print(f"Approver:    {args.actor}")
    print(f"Status:      READY")


def cmd_reject_task(args) -> None:
    """Record a Relay operator rejection, terminating a manual-policy task."""
    state, _, audit = _make_components(args.db)
    task = state.get_task(args.task_id)

    if task is None:
        print(f"Task {args.task_id} not found.", file=sys.stderr)
        sys.exit(1)
    if task.status != TaskStatus.BLOCKED:
        print(
            f"Task {args.task_id} is {task.status.value}. "
            "Only BLOCKED tasks can be rejected via this command.",
            file=sys.stderr,
        )
        sys.exit(1)

    reason = getattr(args, "reason", None)
    approval = Approval(
        approval_id=Approval.new_id(),
        task_id=task.task_id,
        approver=args.actor,
        decision="REJECTED",
        reason=reason,
        timestamp=datetime.now(timezone.utc),
        is_human=True,
    )
    state.record_approval(approval)
    state.update_task_status(
        task.task_id,
        TaskStatus.REJECTED,
        error=f"Rejected by {args.actor!r}: {reason or 'no reason given'}",
    )
    audit.task_approval_rejected(task.task_id, approval.approval_id, args.actor, reason)

    print(f"Task {task.task_id} rejected.")
    print(f"Approval ID: {approval.approval_id}")
    print(f"Approver:    {args.actor}")
    print(f"Status:      REJECTED")


def cmd_status(args) -> None:
    state, _, _ = _make_components(args.db)
    task = state.get_task(args.task_id)
    if task is None:
        print(f"Task {args.task_id} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Task:       {task.task_id}")
    print(f"Status:     {task.status.value}")
    print(f"Attempts:   {task.attempt_count}/{task.max_execution_attempts}")
    print(f"Repository: {task.repository}")
    print(f"Objective:  {task.objective}")

    executions = state.get_executions_for_task(args.task_id)
    if executions:
        print("\nExecutions:")
        for ex in executions:
            ended = ex.ended_at.isoformat() if ex.ended_at else "running"
            print(f"  [{ex.attempt_number}] {ex.execution_id}: {ex.status.value}"
                  f" (exit={ex.exit_code}, ended={ended})")
            if ex.error:
                print(f"       error: {ex.error}")


def cmd_audit(args) -> None:
    state, _, _ = _make_components(args.db)
    events = (
        state.get_events_for_task(args.task_id)
        if args.task_id
        else state.get_all_events()
    )
    if not events:
        print("No events found.")
        return
    for ev in events:
        print(
            f"[{ev.sequence:04d}] {ev.timestamp.isoformat()} "
            f"{ev.event_type.value:<30} "
            f"task={ev.task_id} exec={ev.execution_id or '-'} actor={ev.actor}"
        )


def cmd_recover(args) -> None:
    state, _, audit = _make_components(args.db)
    engine = RecoveryEngine(state, audit)
    tasks = engine.scan()
    if tasks:
        print(f"Recovery required for: {', '.join(tasks)}")
    else:
        print("No recovery required.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="Project Relay — safe AI-to-AI engineering collaboration prototype",
    )
    parser.add_argument("--db", default="relay.db", help="SQLite database path")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("create-task", help="Create and validate a task from a JSON file")
    p.add_argument("task_file", help="Path to task definition JSON")
    p.set_defaults(func=cmd_create_task)

    p = sub.add_parser("run-task", help="Run the next execution attempt for a READY task")
    p.add_argument("task_id")
    p.add_argument("--workspace", default="./workspaces", help="Workspace base directory")
    p.add_argument("--timeout", type=int, default=120, help="Execution timeout (seconds)")
    p.add_argument(
        "--keep-workspace", action="store_true", default=False,
        help="Preserve the task workspace directory after execution (default: delete after completion)",
    )
    p.set_defaults(func=cmd_run_task)

    p = sub.add_parser("approve-task", help="Record a human approval for a BLOCKED manual task")
    p.add_argument("task_id")
    p.add_argument("--actor", required=True, help="Identity of the approving operator")
    p.add_argument("--reason", default=None, help="Optional reason for approval")
    p.set_defaults(func=cmd_approve_task)

    p = sub.add_parser("reject-task", help="Record a human rejection for a BLOCKED manual task")
    p.add_argument("task_id")
    p.add_argument("--actor", required=True, help="Identity of the rejecting operator")
    p.add_argument("--reason", default=None, help="Optional reason for rejection")
    p.set_defaults(func=cmd_reject_task)

    p = sub.add_parser("status", help="Show task status and execution history")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("audit", help="Show audit log")
    p.add_argument("task_id", nargs="?", help="Filter by task_id (omit for all events)")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("recover", help="Scan for interrupted executions")
    p.set_defaults(func=cmd_recover)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
