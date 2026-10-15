from __future__ import annotations

import argparse
import sys

from .models import A2AProtocolState, A2ATask, A2ATaskTier
from .service import A2AService, A2AInvalidTaskError, A2ATaskNotFoundError, A2ATransitionError
from .state import A2AStateManager


def _make_service(db_path: str) -> A2AService:
    return A2AService(A2AStateManager(db_path))


def _task_lines(task: A2ATask) -> str:
    return (
        f"{task.task_id}  [{task.state.value}]  {task.tier.value}  {task.title}\n"
        f"  Issue:   {task.github_issue_url}\n"
        f"  Created: {task.created_at.isoformat()}"
    )


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_create_task(args) -> None:
    svc = _make_service(args.db)
    tier = A2ATaskTier(args.tier)   # safe: argparse choices already validated
    try:
        task = svc.create_task(
            title=args.title,
            tier=tier,
            github_issue_url=args.issue_url,
        )
    except A2AInvalidTaskError as exc:
        print(f"Error: {exc.reason}", file=sys.stderr)
        sys.exit(1)
    print(f"Created: {task.task_id}")
    print(_task_lines(task))


def cmd_get_task(args) -> None:
    svc = _make_service(args.db)
    try:
        task = svc.get_task(args.task_id)
    except A2ATaskNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(_task_lines(task))


def cmd_list_tasks(args) -> None:
    svc = _make_service(args.db)
    tasks = svc.list_tasks()
    if not tasks:
        print("No tasks.")
        return
    for i, task in enumerate(tasks):
        if i:
            print()
        print(_task_lines(task))


def cmd_advance(args) -> None:
    svc = _make_service(args.db)
    new_state = A2AProtocolState(args.to_state)   # safe: argparse choices validated
    try:
        task = svc.advance(args.task_id, new_state)
    except A2ATaskNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except A2ATransitionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Advanced: {task.task_id}")
    print(_task_lines(task))


# ── Argument parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a2a",
        description="A2A Engineering Collaboration Protocol - task CLI",
    )
    parser.add_argument(
        "--db",
        default="a2a.db",
        metavar="PATH",
        help="SQLite database file (default: a2a.db)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # create-task
    p_create = sub.add_parser("create-task", help="Create a new task in DRAFT state")
    p_create.add_argument("--title", required=True, help="Task title")
    p_create.add_argument(
        "--tier",
        required=True,
        choices=[t.value for t in A2ATaskTier],
        help="Risk tier: T1 (low), T2 (standard), T3 (high)",
    )
    p_create.add_argument(
        "--issue-url",
        required=True,
        metavar="URL",
        help="GitHub Issue URL",
    )
    p_create.set_defaults(func=cmd_create_task)

    # get-task
    p_get = sub.add_parser("get-task", help="Retrieve a task by ID")
    p_get.add_argument("task_id", metavar="TASK_ID")
    p_get.set_defaults(func=cmd_get_task)

    # list-tasks
    p_list = sub.add_parser("list-tasks", help="List all tasks ordered by creation time")
    p_list.set_defaults(func=cmd_list_tasks)

    # advance
    p_adv = sub.add_parser("advance", help="Advance a task to a new protocol state")
    p_adv.add_argument("task_id", metavar="TASK_ID")
    p_adv.add_argument(
        "--to",
        required=True,
        dest="to_state",
        choices=[s.value for s in A2AProtocolState],
        metavar="STATE",
        help=(
            "Target protocol state. "
            f"One of: {', '.join(s.value for s in A2AProtocolState)}"
        ),
    )
    p_adv.set_defaults(func=cmd_advance)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
