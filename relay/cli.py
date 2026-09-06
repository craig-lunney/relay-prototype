from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import AuditLog
from .executor import ExecutionEngine
from .models import Task, TaskStatus
from .policy import PolicyEngine
from .recovery import RecoveryEngine
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
        approval_policy=task_def.get("approval_policy", "auto"),
        status=TaskStatus.CREATED,
    )

    state.create_task(task)
    audit.task_created(task.task_id, task.project_id, task.repository)

    state.update_task_status(task.task_id, TaskStatus.VALIDATING)

    result = policy.validate_task(task)
    audit.policy_checked(task.task_id, result.allowed, result.reason)

    if result.allowed:
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

    print(f"Starting execution for {task.task_id}...")
    execution = engine.run(task, workspace_base, timeout_seconds=args.timeout)
    print(f"Execution {execution.execution_id}: {execution.status.value}")

    if execution.status.value in ("COMPLETED", "FAILED"):
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
        else:
            reason = f"Verification failed. Errors: {result.errors}"
            state.update_task_status(
                task.task_id, TaskStatus.FAILED, error=reason, validate_transition=True
            )
            audit.task_failed(task.task_id, reason)
            print(f"Task {task.task_id}: FAILED — {reason}", file=sys.stderr)
            sys.exit(1)

    elif execution.status.value == "TIMEOUT":
        state.update_task_status(
            task.task_id, TaskStatus.TIMEOUT, validate_transition=True
        )
        audit.execution_timeout(
            task.task_id, execution.execution_id, args.timeout
        )
        print(f"Task {task.task_id}: TIMEOUT", file=sys.stderr)
        sys.exit(1)


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
    p.set_defaults(func=cmd_run_task)

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
