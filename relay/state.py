from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    ALLOWED_TRANSITIONS,
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
                task_id            TEXT PRIMARY KEY,
                project_id         TEXT NOT NULL,
                repository         TEXT NOT NULL,
                base_ref           TEXT NOT NULL,
                working_ref        TEXT NOT NULL,
                objective          TEXT NOT NULL,
                requirements       TEXT NOT NULL,
                acceptance_criteria TEXT NOT NULL,
                requested_actions  TEXT NOT NULL,
                forbidden_actions  TEXT NOT NULL,
                required_tests     TEXT NOT NULL,
                approval_policy    TEXT NOT NULL,
                status             TEXT NOT NULL,
                protocol_version   TEXT NOT NULL DEFAULT '0.1',
                max_execution_attempts INTEGER NOT NULL DEFAULT 3,
                attempt_count      INTEGER NOT NULL DEFAULT 0,
                error              TEXT,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS executions (
                execution_id       TEXT PRIMARY KEY,
                task_id            TEXT NOT NULL REFERENCES tasks(task_id),
                attempt_number     INTEGER NOT NULL,
                status             TEXT NOT NULL,
                workspace          TEXT NOT NULL,
                worker_identity    TEXT NOT NULL,
                policy_version     TEXT NOT NULL,
                capability_snapshot TEXT NOT NULL,
                started_at         TEXT NOT NULL,
                ended_at           TEXT,
                exit_code          INTEGER,
                stdout             TEXT NOT NULL DEFAULT '',
                stderr             TEXT NOT NULL DEFAULT '',
                error              TEXT,
                report             TEXT
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
        """)
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
                approval_policy, status, protocol_version,
                max_execution_attempts, attempt_count, error,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task.task_id, task.project_id, task.repository,
                task.base_ref, task.working_ref, task.objective,
                json.dumps(task.requirements),
                json.dumps(task.acceptance_criteria),
                json.dumps(task.requested_actions),
                json.dumps(task.forbidden_actions),
                json.dumps(task.required_tests),
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
        if validate_transition:
            task = self.get_task(task_id)
            if task is not None:
                allowed = ALLOWED_TRANSITIONS.get(task.status, frozenset())
                if new_status not in allowed:
                    raise InvalidTransitionError(
                        f"Transition {task.status.value} → {new_status.value} is not allowed."
                    )
        self._conn().execute(
            "UPDATE tasks SET status=?, error=?, updated_at=? WHERE task_id=?",
            (new_status.value, error, _now_iso(), task_id),
        )
        self._conn().commit()

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
                started_at, ended_at, exit_code, stdout, stderr, error, report
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            ),
        )
        conn.commit()

    def update_execution(self, execution: Execution) -> None:
        self._conn().execute(
            """
            UPDATE executions SET
                status=?, ended_at=?, exit_code=?, stdout=?, stderr=?, error=?, report=?
            WHERE execution_id=?
            """,
            (
                execution.status.value,
                execution.ended_at.isoformat() if execution.ended_at else None,
                execution.exit_code, execution.stdout, execution.stderr,
                execution.error,
                json.dumps(execution.report.to_dict()) if execution.report else None,
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
        )

    # ── Leases ────────────────────────────────────────────────────────────────

    def acquire_lease(self, lease: ExecutionLease) -> bool:
        """Atomically acquire a workspace lease. Returns False if already held."""
        conn = self._conn()
        existing = conn.execute(
            "SELECT execution_id FROM execution_leases WHERE workspace=? AND released=0",
            (lease.workspace,),
        ).fetchone()
        if existing:
            return False
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

    # ── Events (append-only) ──────────────────────────────────────────────────

    def append_event(self, event: AuditEvent) -> None:
        """Append an audit event. Workers must never call this directly."""
        conn = self._conn()
        seq = (conn.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0]) + 1
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
