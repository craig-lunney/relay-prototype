from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    ALLOWED_TRANSITIONS,
    Approval,
    AuditEvent,
    AuditEventType,
    Execution,
    ExecutionLease,
    ExecutionStatus,
    ImplementationReport,
    Task,
    TaskStatus,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


class InvalidTransitionError(Exception):
    pass


class StateManager:
    """
    SQLite-backed state for Relay. Events are append-only.
    Thread-safe: each thread gets its own connection via threading.local.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id                TEXT PRIMARY KEY,
                project_id             TEXT NOT NULL,
                repository             TEXT NOT NULL,
                base_ref               TEXT NOT NULL,
                working_ref            TEXT NOT NULL,
                objective              TEXT NOT NULL,
                requirements           TEXT NOT NULL,
                acceptance_criteria    TEXT NOT NULL,
                requested_actions      TEXT NOT NULL,
                forbidden_actions      TEXT NOT NULL,
                required_tests         TEXT NOT NULL,
                expected_deleted_tests TEXT NOT NULL DEFAULT '[]',
                approval_policy        TEXT NOT NULL,
                status                 TEXT NOT NULL,
                protocol_version       TEXT NOT NULL DEFAULT '0.1',
                max_execution_attempts INTEGER NOT NULL DEFAULT 3,
                attempt_count          INTEGER NOT NULL DEFAULT 0,
                error                  TEXT,
                created_at             TEXT NOT NULL,
                updated_at             TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS executions (
                execution_id              TEXT PRIMARY KEY,
                task_id                   TEXT NOT NULL REFERENCES tasks(task_id),
                attempt_number            INTEGER NOT NULL,
                status                    TEXT NOT NULL,
                workspace                 TEXT NOT NULL,
                worker_identity           TEXT NOT NULL,
                policy_version            TEXT NOT NULL,
                capability_snapshot       TEXT NOT NULL,
                started_at                TEXT NOT NULL,
                ended_at                  TEXT,
                exit_code                 INTEGER,
                stdout                    TEXT NOT NULL DEFAULT '',
                stderr                    TEXT NOT NULL DEFAULT '',
                error                     TEXT,
                report                    TEXT,
                baseline_tests_collected  TEXT NOT NULL DEFAULT '[]',
                baseline_test_exit        INTEGER,
                baseline_test_output      TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS execution_leases (
                execution_id   TEXT PRIMARY KEY REFERENCES executions(execution_id),
                task_id        TEXT NOT NULL,
                workspace      TEXT NOT NULL,
                pid            INTEGER,
                started_at     TEXT NOT NULL,
                expires_at     TEXT NOT NULL,
                worker_identity TEXT NOT NULL,
                released       INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                task_id     TEXT NOT NULL REFERENCES tasks(task_id),
                approver    TEXT NOT NULL,
                decision    TEXT NOT NULL,
                reason      TEXT,
                timestamp   TEXT NOT NULL,
                is_human    INTEGER NOT NULL DEFAULT 0
            );

            -- Events are append-only. Workers cannot modify them.
            CREATE TABLE IF NOT EXISTS events (
                event_id     TEXT PRIMARY KEY,
                sequence     INTEGER NOT NULL UNIQUE,
                event_type   TEXT NOT NULL,
                task_id      TEXT NOT NULL,
                execution_id TEXT,
                timestamp    TEXT NOT NULL,
                actor        TEXT NOT NULL,
                details      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_task      ON events(task_id);
            CREATE INDEX IF NOT EXISTS idx_events_sequence  ON events(sequence);
            CREATE INDEX IF NOT EXISTS idx_executions_task  ON executions(task_id);
            CREATE INDEX IF NOT EXISTS idx_leases_workspace ON execution_leases(workspace, released);

            -- P1-B: enforce at-most-one active lease per workspace at the database level.
            -- A partial UNIQUE index covers only released=0 rows, so historical
            -- released=1 rows for the same workspace accumulate without constraint.
            -- sqlite3.IntegrityError on INSERT is the authoritative failure signal.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_workspace_active
                ON execution_leases(workspace) WHERE released=0;
        """)
        # P1-J: migrate existing databases that pre-date these columns.
        for stmt in (
            "ALTER TABLE tasks ADD COLUMN expected_deleted_tests TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE executions ADD COLUMN baseline_tests_collected TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE executions ADD COLUMN baseline_test_exit INTEGER",
            "ALTER TABLE executions ADD COLUMN baseline_test_output TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()

    # ── Tasks ─────────────────────────────────────────────────────────────────

    def create_task(self, task: Task) -> None:
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, project_id, repository, base_ref, working_ref,
                objective, requirements, acceptance_criteria,
                requested_actions, forbidden_actions, required_tests,
                expected_deleted_tests, approval_policy, status, protocol_version,
                max_execution_attempts, attempt_count, error,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task.task_id, task.project_id, task.repository,
                task.base_ref, task.working_ref, task.objective,
                json.dumps(task.requirements),
                json.dumps(task.acceptance_criteria),
                json.dumps(task.requested_actions),
                json.dumps(task.forbidden_actions),
                json.dumps(task.required_tests),
                json.dumps(task.expected_deleted_tests),
                task.approval_policy, task.status.value,
                task.protocol_version, task.max_execution_attempts,
                task.attempt_count, task.error,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
            ),
        )
        conn.commit()

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn().execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def update_task_status(
        self,
        task_id: str,
        new_status: TaskStatus,
        error: str | None = None,
        validate_transition: bool = True,
    ) -> None:
        conn = self._conn()
        if not validate_transition:
            conn.execute(
                "UPDATE tasks SET status=?, error=?, updated_at=? WHERE task_id=?",
                (new_status.value, error, _now_iso(), task_id),
            )
            conn.commit()
            return

        # P1-B: atomic validate-then-update via CAS (compare-and-swap).
        # Read the current status first, validate the transition, then UPDATE
        # using WHERE ... AND status=<current> so that a concurrent writer
        # changing the status between our read and our UPDATE will cause
        # rowcount=0, which we detect as a concurrent-modification error.
        # This prevents two concurrent writers from both validating against
        # stale state and producing an impossible transition.
        row = conn.execute(
            "SELECT status FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()

        if row is not None:
            current = TaskStatus(row["status"])
            allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
            if new_status not in allowed:
                raise InvalidTransitionError(
                    f"Transition {current.value} → {new_status.value} is not allowed."
                )
            cursor = conn.execute(
                "UPDATE tasks SET status=?, error=?, updated_at=? "
                "WHERE task_id=? AND status=?",
                (new_status.value, error, _now_iso(), task_id, current.value),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise InvalidTransitionError(
                    f"Concurrent modification: task {task_id} status changed "
                    f"during transition {current.value} → {new_status.value}. "
                    "Another writer committed first."
                )
        else:
            conn.execute(
                "UPDATE tasks SET status=?, error=?, updated_at=? WHERE task_id=?",
                (new_status.value, error, _now_iso(), task_id),
            )
            conn.commit()

    def increment_attempt_count(self, task_id: str) -> None:
        self._conn().execute(
            "UPDATE tasks SET attempt_count=attempt_count+1, updated_at=? WHERE task_id=?",
            (_now_iso(), task_id),
        )
        self._conn().commit()

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            project_id=row["project_id"],
            repository=row["repository"],
            base_ref=row["base_ref"],
            working_ref=row["working_ref"],
            objective=row["objective"],
            requirements=json.loads(row["requirements"]),
            acceptance_criteria=json.loads(row["acceptance_criteria"]),
            requested_actions=json.loads(row["requested_actions"]),
            forbidden_actions=json.loads(row["forbidden_actions"]),
            required_tests=json.loads(row["required_tests"]),
            expected_deleted_tests=json.loads(row["expected_deleted_tests"] or "[]"),
            approval_policy=row["approval_policy"],
            status=TaskStatus(row["status"]),
            protocol_version=row["protocol_version"],
            max_execution_attempts=row["max_execution_attempts"],
            attempt_count=row["attempt_count"],
            error=row["error"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    # ── Executions ────────────────────────────────────────────────────────────

    def create_execution(self, execution: Execution) -> None:
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO executions (
                execution_id, task_id, attempt_number, status, workspace,
                worker_identity, policy_version, capability_snapshot,
                started_at, ended_at, exit_code, stdout, stderr, error, report,
                baseline_tests_collected, baseline_test_exit, baseline_test_output
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                execution.execution_id, execution.task_id, execution.attempt_number,
                execution.status.value, execution.workspace, execution.worker_identity,
                execution.policy_version, json.dumps(execution.capability_snapshot),
                execution.started_at.isoformat(),
                execution.ended_at.isoformat() if execution.ended_at else None,
                execution.exit_code, execution.stdout, execution.stderr,
                execution.error,
                json.dumps(execution.report.to_dict()) if execution.report else None,
                json.dumps(execution.baseline_tests_collected),
                execution.baseline_test_exit,
                execution.baseline_test_output,
            ),
        )
        conn.commit()

    def update_execution(self, execution: Execution) -> None:
        self._conn().execute(
            """
            UPDATE executions SET
                status=?, ended_at=?, exit_code=?, stdout=?, stderr=?, error=?, report=?,
                baseline_tests_collected=?, baseline_test_exit=?, baseline_test_output=?
            WHERE execution_id=?
            """,
            (
                execution.status.value,
                execution.ended_at.isoformat() if execution.ended_at else None,
                execution.exit_code, execution.stdout, execution.stderr,
                execution.error,
                json.dumps(execution.report.to_dict()) if execution.report else None,
                json.dumps(execution.baseline_tests_collected),
                execution.baseline_test_exit,
                execution.baseline_test_output,
                execution.execution_id,
            ),
        )
        self._conn().commit()

    def get_execution(self, execution_id: str) -> Execution | None:
        row = self._conn().execute(
            "SELECT * FROM executions WHERE execution_id=?", (execution_id,)
        ).fetchone()
        return self._row_to_execution(row) if row else None

    def get_executions_for_task(self, task_id: str) -> list[Execution]:
        rows = self._conn().execute(
            "SELECT * FROM executions WHERE task_id=? ORDER BY attempt_number",
            (task_id,),
        ).fetchall()
        return [self._row_to_execution(r) for r in rows]

    def _row_to_execution(self, row: sqlite3.Row) -> Execution:
        report = None
        if row["report"]:
            try:
                report = ImplementationReport.from_dict(json.loads(row["report"]))
            except (json.JSONDecodeError, KeyError):
                pass
        return Execution(
            execution_id=row["execution_id"],
            task_id=row["task_id"],
            attempt_number=row["attempt_number"],
            status=ExecutionStatus(row["status"]),
            workspace=row["workspace"],
            worker_identity=row["worker_identity"],
            policy_version=row["policy_version"],
            capability_snapshot=json.loads(row["capability_snapshot"]),
            started_at=_parse_dt(row["started_at"]),
            ended_at=_parse_dt(row["ended_at"]),
            exit_code=row["exit_code"],
            stdout=row["stdout"] or "",
            stderr=row["stderr"] or "",
            error=row["error"],
            report=report,
            baseline_tests_collected=json.loads(row["baseline_tests_collected"] or "[]"),
            baseline_test_exit=row["baseline_test_exit"],
            baseline_test_output=row["baseline_test_output"] or "",
        )

    # ── Leases ────────────────────────────────────────────────────────────────

    def acquire_lease(self, lease: ExecutionLease) -> bool:
        """Acquire a workspace lease. Returns False if the workspace is already leased.

        P1-B: atomicity is enforced by the UNIQUE partial index
        idx_leases_workspace_active ON execution_leases(workspace) WHERE released=0.
        The INSERT either succeeds (we own the lease) or raises IntegrityError
        (another active lease exists for this workspace). There is no SELECT
        preliminary check — the database constraint is the sole authority.
        Two concurrent callers cannot both receive True for the same workspace.
        """
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO execution_leases
                    (execution_id, task_id, workspace, pid, started_at, expires_at,
                     worker_identity, released)
                VALUES (?,?,?,?,?,?,?,0)
                """,
                (
                    lease.execution_id, lease.task_id, lease.workspace, lease.pid,
                    lease.started_at.isoformat(), lease.expires_at.isoformat(),
                    lease.worker_identity,
                ),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False

    def release_lease(self, execution_id: str) -> None:
        self._conn().execute(
            "UPDATE execution_leases SET released=1 WHERE execution_id=?",
            (execution_id,),
        )
        self._conn().commit()

    def get_active_lease(self, workspace: str) -> ExecutionLease | None:
        row = self._conn().execute(
            "SELECT * FROM execution_leases WHERE workspace=? AND released=0",
            (workspace,),
        ).fetchone()
        return self._row_to_lease(row) if row else None

    def get_all_active_leases(self) -> list[ExecutionLease]:
        rows = self._conn().execute(
            "SELECT * FROM execution_leases WHERE released=0"
        ).fetchall()
        return [self._row_to_lease(r) for r in rows]

    def get_active_leases_for_task(self, task_id: str) -> list[ExecutionLease]:
        """Return all active (unreleased) leases associated with a task."""
        rows = self._conn().execute(
            "SELECT * FROM execution_leases WHERE task_id=? AND released=0",
            (task_id,),
        ).fetchall()
        return [self._row_to_lease(r) for r in rows]

    def _row_to_lease(self, row: sqlite3.Row) -> ExecutionLease:
        return ExecutionLease(
            execution_id=row["execution_id"],
            task_id=row["task_id"],
            workspace=row["workspace"],
            pid=row["pid"],
            started_at=_parse_dt(row["started_at"]),
            expires_at=_parse_dt(row["expires_at"]),
            worker_identity=row["worker_identity"],
        )

    # ── Approvals ─────────────────────────────────────────────────────────────

    def record_approval(self, approval: Approval) -> None:
        """
        Persist a Relay operator approval or rejection decision.

        is_human=True is set by the caller — it must only be True when invoked
        from the Relay operator CLI (approve-task / reject-task). This method
        does not authenticate the caller; the security boundary is the explicit
        operator code path, not a flag value.

        Agent output, task JSON, and repository content MUST NOT call this method.
        """
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO approvals
                (approval_id, task_id, approver, decision, reason, timestamp, is_human)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                approval.approval_id, approval.task_id, approval.approver,
                approval.decision, approval.reason,
                approval.timestamp.isoformat(),
                1 if approval.is_human else 0,
            ),
        )
        conn.commit()

    def get_valid_approval_for_task(self, task_id: str) -> Approval | None:
        """
        Return the most recent APPROVED human approval for this task, or None.

        An approval is valid if and only if:
          - task_id matches
          - decision == 'APPROVED'
          - is_human == 1 (recorded via the Relay operator path)
        A rejected decision or a non-human approval does not satisfy this check.
        """
        row = self._conn().execute(
            """
            SELECT * FROM approvals
            WHERE task_id=? AND decision='APPROVED' AND is_human=1
            ORDER BY timestamp DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return self._row_to_approval(row) if row else None

    def get_approval_for_task(self, task_id: str) -> Approval | None:
        """Return the most recent approval record (any decision) for this task."""
        row = self._conn().execute(
            "SELECT * FROM approvals WHERE task_id=? ORDER BY timestamp DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return self._row_to_approval(row) if row else None

    def _row_to_approval(self, row: sqlite3.Row) -> Approval:
        return Approval(
            approval_id=row["approval_id"],
            task_id=row["task_id"],
            approver=row["approver"],
            decision=row["decision"],
            reason=row["reason"],
            timestamp=_parse_dt(row["timestamp"]),
            is_human=bool(row["is_human"]),
        )

    # ── Events (append-only) ──────────────────────────────────────────────────

    def append_event(self, event: AuditEvent) -> None:
        """Append an audit event. Workers must never call this directly.

        H-4: sequence assignment is made collision-safe via retry.  Two
        concurrent appenders may read the same MAX(sequence) and race to
        INSERT; the UNIQUE constraint on 'sequence' ensures only one wins.
        The loser catches IntegrityError, rolls back, re-reads MAX, and
        retries.  No valid audit event is silently discarded — the loop
        raises RuntimeError only after exhausting all retries, which in
        normal operation (sequential audit writes from a single process) will
        never happen.
        """
        conn = self._conn()
        _MAX_RETRIES = 10
        for attempt in range(_MAX_RETRIES):
            try:
                seq = (
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence),0) FROM events"
                    ).fetchone()[0]
                ) + 1
                conn.execute(
                    """
                    INSERT INTO events
                        (event_id, sequence, event_type, task_id, execution_id,
                         timestamp, actor, details)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        event.event_id, seq, event.event_type.value,
                        event.task_id, event.execution_id,
                        event.timestamp.isoformat(), event.actor,
                        json.dumps(event.details),
                    ),
                )
                conn.commit()
                return
            except sqlite3.IntegrityError:
                # Sequence collision: another writer committed first.
                # Roll back and retry with a fresh MAX read.
                try:
                    conn.rollback()
                except Exception:
                    pass
        raise RuntimeError(
            f"append_event: failed to assign a unique sequence for event "
            f"{event.event_id!r} after {_MAX_RETRIES} attempts. "
            "Concurrent sequence collisions not resolved."
        )

    def get_events_for_task(self, task_id: str) -> list[AuditEvent]:
        rows = self._conn().execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY sequence", (task_id,)
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def get_all_events(self) -> list[AuditEvent]:
        rows = self._conn().execute(
            "SELECT * FROM events ORDER BY sequence"
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def event_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def _row_to_event(self, row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            event_id=row["event_id"],
            event_type=AuditEventType(row["event_type"]),
            task_id=row["task_id"],
            execution_id=row["execution_id"],
            timestamp=_parse_dt(row["timestamp"]),
            actor=row["actor"],
            details=json.loads(row["details"]),
            sequence=row["sequence"],
        )
