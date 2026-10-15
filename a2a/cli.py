from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .models import A2AProtocolState, A2ATask, A2ATaskTier
from .protocol import A2ARole
from .service import A2AService, A2AInvalidTaskError, A2ATaskNotFoundError, A2ATransitionError
from .state import A2AAttestation, A2AStateManager, relay_status_from_db


def _make_service(args) -> A2AService:
    relay_db = getattr(args, "relay_db", None)
    lookup = relay_status_from_db(relay_db) if relay_db else None
    return A2AService(A2AStateManager(args.db, relay_status_lookup=lookup))


def _task_lines(task: A2ATask) -> str:
    return (
        f"{task.task_id}  [{task.state.value}]  {task.tier.value}  {task.title}\n"
        f"  Issue:   {task.github_issue_url}\n"
        f"  Created: {task.created_at.isoformat()}"
    )


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_create_task(args) -> None:
    svc = _make_service(args)
    tier = A2ATaskTier(args.tier)   # safe: argparse choices already validated
    try:
        task = svc.create_task(
            title=args.title,
            tier=tier,
            github_issue_url=args.issue_url,
            created_by=getattr(args, "actor", None),
        )
    except A2AInvalidTaskError as exc:
        print(f"Error: {exc.reason}", file=sys.stderr)
        sys.exit(1)
    print(f"Created: {task.task_id}")
    print(_task_lines(task))


def cmd_get_task(args) -> None:
    svc = _make_service(args)
    try:
        task = svc.get_task(args.task_id)
        history = svc.history(args.task_id)
        reruns = svc.human_verifications(args.task_id)
        tier_changes = svc.tier_changes(args.task_id)
    except A2ATaskNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(_task_lines(task))
    if history:
        print("  History:")
        for rec in history:
            src = rec.from_state.value if rec.from_state else "(created)"
            line = (f"    #{rec.seq} {rec.timestamp.isoformat()} {src} -> "
                    f"{rec.to_state.value} by {rec.actor} [{rec.role.value}]")
            print(line)
            for label, value in (("reason", rec.reason), ("evidence", rec.evidence_ref),
                                 ("relay task", rec.relay_task_id)):
                if value:
                    print(f"        {label}: {value}")
    for c in tier_changes:
        print(f"  Tier {c.from_tier.value} -> {c.to_tier.value} by {c.actor} at "
              f"{c.timestamp.isoformat()} (after step #{c.after_seq}): {c.reason}")
    for v in reruns:
        print(f"  Human re-run by {v.actor} at {v.recorded_at.isoformat()} "
              f"(Gate 2 window from step #{v.gate2_entry_seq}): {v.command}")
        lines = v.output.splitlines()
        for line in lines[:20]:
            print(f"      | {line}")
        if len(lines) > 20:
            print(f"      | ... ({len(lines) - 20} more lines; full output is in the ledger)")


def cmd_list_tasks(args) -> None:
    svc = _make_service(args)
    tasks = svc.list_tasks()
    if not tasks:
        print("No tasks.")
        return
    for i, task in enumerate(tasks):
        if i:
            print()
        print(_task_lines(task))


def cmd_advance(args) -> None:
    svc = _make_service(args)
    new_state = A2AProtocolState(args.to_state)   # safe: argparse choices validated
    attestation = A2AAttestation(
        actor=args.actor,
        role=A2ARole(args.role),                  # safe: argparse choices validated
        reason=getattr(args, "reason", None),
        evidence_ref=getattr(args, "evidence", None),
        relay_task_id=getattr(args, "relay_task", None),
    )
    try:
        task = svc.advance(args.task_id, new_state, attestation)
    except A2ATaskNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except A2ATransitionError as exc:
        print(f"Error: {exc} No state was changed.", file=sys.stderr)
        sys.exit(1)
    print(f"Advanced: {task.task_id}")
    print(_task_lines(task))


def cmd_record_human_verification(args) -> None:
    svc = _make_service(args)
    if args.output_file:
        try:
            # Bytes decoded as-is: line endings are preserved verbatim.
            output = Path(args.output_file).read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"Error: cannot read {args.output_file!r}: {exc}. Nothing recorded.",
                  file=sys.stderr)
            sys.exit(1)
    else:
        output = args.output or ""
    try:
        record = svc.record_human_verification(args.task_id, args.actor, args.command, output)
    except A2ATaskNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except A2ATransitionError as exc:
        print(f"Error: {exc.reason} Nothing recorded.", file=sys.stderr)
        sys.exit(1)
    print(f"Recorded human re-run for {args.task_id} by {record.actor}.")


def cmd_escalate_tier(args) -> None:
    svc = _make_service(args)
    try:
        task = svc.escalate_tier(args.task_id, args.actor, args.reason, args.evidence)
    except A2ATaskNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except A2ATransitionError as exc:
        print(f"Error: {exc.reason} No state was changed.", file=sys.stderr)
        sys.exit(1)
    print(f"Escalated: {task.task_id} is now {task.tier.value}")
    print(_task_lines(task))


def cmd_check(args) -> None:
    problems = _make_service(args).check_consistency()
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        sys.exit(1)
    print("Ledger consistent: every task's state matches its recorded history.")


# ── Argument parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a2a",
        description="A2A Engineering Collaboration Protocol - attested task ledger",
    )
    parser.add_argument(
        "--db",
        default="a2a.db",
        metavar="PATH",
        help="SQLite database file (default: a2a.db)",
    )
    parser.add_argument(
        "--relay-db",
        default="relay.db",
        metavar="PATH",
        help="Relay database consulted read-only for --relay-task evidence (default: relay.db)",
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
    p_create.add_argument("--actor", required=True, help="Human creating the task")
    p_create.set_defaults(func=cmd_create_task)

    # get-task
    p_get = sub.add_parser("get-task", help="Show a task with its full transition history")
    p_get.add_argument("task_id", metavar="TASK_ID")
    p_get.set_defaults(func=cmd_get_task)

    # list-tasks
    p_list = sub.add_parser("list-tasks", help="List all tasks ordered by creation time")
    p_list.set_defaults(func=cmd_list_tasks)

    # advance
    p_adv = sub.add_parser(
        "advance", help="Advance a task to a new protocol state (recorded in the ledger)"
    )
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
    p_adv.add_argument("--actor", required=True, help="Who is taking this step")
    p_adv.add_argument(
        "--role", required=True, choices=[r.value for r in A2ARole],
        help="Protocol role the actor is acting in",
    )
    p_adv.add_argument("--reason", default=None, help="Reason (required for some decisions)")
    p_adv.add_argument(
        "--evidence", default=None, metavar="REF",
        help="Trigger evidence, e.g. the GitHub comment or PR URL (required for some steps)",
    )
    p_adv.add_argument(
        "--relay-task", default=None, metavar="TASK_ID",
        help="COMPLETED Relay task cited as implementation evidence",
    )
    p_adv.set_defaults(func=cmd_advance)

    # record-human-verification
    p_hv = sub.add_parser(
        "record-human-verification",
        help="Record a T3 human re-run of a prescribed verification command (Gate 2)",
    )
    p_hv.add_argument("task_id", metavar="TASK_ID")
    p_hv.add_argument("--actor", required=True, help="Human who ran the command")
    p_hv.add_argument("--command", required=True, help="Exact command that was run")
    out = p_hv.add_mutually_exclusive_group(required=True)
    out.add_argument("--output", help="Verbatim output")
    out.add_argument("--output-file", help="File containing the verbatim output")
    p_hv.set_defaults(func=cmd_record_human_verification)

    # escalate-tier
    p_tier = sub.add_parser(
        "escalate-tier",
        help="Escalate a T2 task to T3 at Gate 1 (Human decision, recorded in the ledger)",
    )
    p_tier.add_argument("task_id", metavar="TASK_ID")
    p_tier.add_argument("--actor", required=True, help="Human making the decision")
    p_tier.add_argument("--reason", required=True, help="Why (e.g. new EXCLUSIVE resource)")
    p_tier.add_argument("--evidence", default=None, metavar="REF", help="Amendment comment URL")
    p_tier.set_defaults(func=cmd_escalate_tier)

    # check
    p_check = sub.add_parser(
        "check", help="Replay every task's history and report out-of-ledger state changes"
    )
    p_check.set_defaults(func=cmd_check)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except sqlite3.Error as exc:
        print(f"Error: database problem with {args.db!r}: {exc}. No state was changed.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
