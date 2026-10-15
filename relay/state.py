from __future__ import annotations

import hashlib
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



# ── Audit hash chain (Batch 6) ────────────────────────────────────────────────
# Every event stores the hash of the previous event and its own hash over its
# full content, so editing, deleting or re-ordering any committed event is
# detectable by StateManager.verify_audit_chain().  This is tamper EVIDENCE,
# not prevention: someone with write access to the file can rebuild the whole
# chain.  Record the head hash (`relay audit --verify`) somewhere independent,
# e.g. as evidence in the A2A ledger, to anchor it.
GENESIS_HASH = "0" * 64


def event_hash(prev_hash: str, sequence: int, event_id: str, event_type: str,
               task_id: str, execution_id: str | None, timestamp: str,
               actor: str, details: str) -> str:
    payload = json.dumps(
        [prev_hash, sequence, event_id, event_type, task_id, execution_id,
         timestamp, actor, details],
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _insert_chained_event(conn: sqlite3.Connection, event_id: str, event_type: str,
                          task_id: str, execution_id: str | None, timestamp: str,
                          actor: str, details: str) -> int:
    """Insert one audit event linked to the current chain head.

    Must run inside the caller's transaction (it does not commit).  Sequence
    and previous hash are read from the same head row, so a concurrent writer
    that commits first makes this INSERT fail the UNIQUE(sequence) check
    instead of forking the chain.
    """
    head = conn.execute(
        "SELECT sequence, hash FROM events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    seq = (head[0] if head else 0) + 1
    prev = (head[1] if head and head[1] else GENESIS_HASH)
    digest = event_hash(prev, seq, event_id, event_type, task_id, execution_id,
                        timestamp, actor, details)
    conn.execute(
        """
        INSERT INTO events
            (event_id, sequence, event_type, task_id, execution_id,
             timestamp, actor, details, prev_hash, hash)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (event_id, seq, event_type, task_id, execution_id, timestamp, actor,
         details, prev, digest),
    )
    return seq


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
                expected_new_files     TEXT NOT NULL DEFAULT '[]',
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
                baseline_test_output      TEXT NOT NULL DEFAULT '',
                baseline_manifest         TEXT,
                baseline_manifest_error   TEXT
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
            # Batch 1: verification integrity.
            "ALTER TABLE tasks ADD COLUMN expected_new_files TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE executions ADD COLUMN baseline_manifest TEXT",
            "ALTER TABLE executions ADD COLUMN baseline_manifest_error TEXT",
            # Batch 6: audit hash chain.
            "ALTER TABLE events ADD COLUMN prev_hash TEXT",
            "ALTER TABLE events ADD COLUMN hash TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
        self._chain_legacy_events(conn)

    # ── Tasks ─────────────────────────────────────────────────────────────────

    def create_task(self, task: Task) -> None:
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, project_id, repository, base_ref, working_ref,
                objective, requirements, acceptance_criteria,
                requested_actions, forbidden_actions, required_tests,
                expected_deleted_tests, expected_new_files, approval_policy, status,
                protocol_version, max_execution_attempts, attempt_count, error,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                json.dumps(task.expected_new_files),
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

    def compare_and_set_status(
        self,
        task_id: str,
        expected: TaskStatus,
        new_status: TaskStatus,
        error: str | None = None,
    ) -> bool:
        """Move task_id from *expected* to *new_status*, or change nothing.

        Batch 3: unlike update_task_status (which validates against whatever the
        current status is), this only succeeds if the task is still exactly in
        the status the caller owns.  Returns False — with no write — otherwise.
        """
        if new_status not in ALLOWED_TRANSITIONS.get(expected, frozenset()):
            raise InvalidTransitionError(
                f"Transition {expected.value} → {new_status.value} is not allowed."
            )
        cursor = self._conn().execute(
            "UPDATE tasks SET status=?, error=?, updated_at=? WHERE task_id=? AND status=?",
            (new_status.value, error, _now_iso(), task_id, expected.value),
        )
        self._conn().commit()
        return cursor.rowcount == 1

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
            expected_new_files=json.loads(row["expected_new_files"] or "[]"),
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
                baseline_tests_collected, baseline_test_exit, baseline_test_output,
                baseline_manifest, baseline_manifest_error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                execution.baseline_manifest,
                execution.baseline_manifest_error,
            ),
        )
        conn.commit()

    def update_execution(self, execution: Execution) -> None:
        self._conn().execute(
            """
            UPDATE executions SET
                status=?, ended_at=?, exit_code=?, stdout=?, stderr=?, error=?, report=?,
                baseline_tests_collected=?, baseline_test_exit=?, baseline_test_output=?,
                baseline_manifest=?, baseline_manifest_error=?
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
                execution.baseline_manifest,
                execution.baseline_manifest_error,
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
            "SELECT * FROM executions WHERE task_id=? "
            "ORDER BY attempt_number, started_at, rowid",
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
            baseline_manifest=row["baseline_manifest"],
            baseline_manifest_error=row["baseline_manifest_error"],
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

    def renew_lease(self, execution_id: str, expires_at: datetime) -> bool:
        """Extend an active lease (Batch 3, E-10).  Returns False if the lease is
        no longer active — e.g. `relay recover` reclaimed it — in which case the
        caller no longer owns the workspace and must not continue."""
        cursor = self._conn().execute(
            "UPDATE execution_leases SET expires_at=? "
            "WHERE execution_id=? AND released=0",
            (expires_at.isoformat(), execution_id),
        )
        self._conn().commit()
        return cursor.rowcount == 1

    def get_tasks_by_status(self, statuses: list[TaskStatus]) -> list[Task]:
        placeholders = ",".join("?" for _ in statuses)
        rows = self._conn().execute(
            f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY created_at",
            [s.value for s in statuses],
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

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

        MAINTENANCE NOTE: This method commits independently via its own retry
        loop and MUST NOT be called inside a ``with conn:`` block or after any
        uncommitted DML on the same connection. Doing so would split work
        across transactions, breaking atomicity. For operations that must
        commit a state transition and an audit event together (all-or-nothing),
        use the dedicated ``*_atomic()`` methods, which inline both the
        sequence allocation and the event INSERT inside the same transaction.

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
                _insert_chained_event(
                    conn, event.event_id, event.event_type.value,
                    event.task_id, event.execution_id,
                    event.timestamp.isoformat(), event.actor,
                    json.dumps(event.details),
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

    # PRAGMA user_version for the Relay database.  0: before the audit hash
    # chain; 1: chained (Batch 6).
    _AUDIT_CHAIN_VERSION = 1

    def _chain_legacy_events(self, conn: sqlite3.Connection) -> None:
        """ONE-TIME migration: hash events written before the chain existed.

        Gated by PRAGMA user_version, so it runs at most once per database: on
        the first open after upgrading (or creating) it.  Afterwards unhashed
        rows are only ever REPORTED by verify_audit_chain, never repaired, so
        wiping hashes and reopening cannot launder a tampered log (unless the
        user_version is reset too; only an externally recorded head hash
        detects that).  The common
        path (already migrated) takes no write lock and scans nothing, so
        read-only commands keep working while another process writes.

        Upgrade with no relay process running: an old-version writer appending
        unhashed rows after the migration would show up as a broken chain.
        """
        if conn.execute("PRAGMA user_version").fetchone()[0] >= self._AUDIT_CHAIN_VERSION:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] < self._AUDIT_CHAIN_VERSION:
                legacy = 0
                prev = GENESIS_HASH
                rows = conn.execute("SELECT * FROM events ORDER BY sequence")
                pending = []
                for r in rows:
                    if r["hash"]:
                        break
                    pending.append(r)
                for r in pending:
                    digest = event_hash(prev, r["sequence"], r["event_id"], r["event_type"],
                                        r["task_id"], r["execution_id"], r["timestamp"],
                                        r["actor"], r["details"])
                    conn.execute(
                        "UPDATE events SET prev_hash=?, hash=? WHERE sequence=?",
                        (prev, digest, r["sequence"]),
                    )
                    prev = digest
                    legacy += 1
                if legacy:
                    _insert_chained_event(
                        conn, AuditEvent.new_id(), AuditEventType.AUDIT_CHAIN_STARTED.value,
                        "-", None, _now_iso(), "relay",
                        json.dumps({"legacy_events_chained": legacy}),
                    )
                conn.execute(f"PRAGMA user_version = {self._AUDIT_CHAIN_VERSION}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def verify_audit_chain(self) -> tuple[list[str], int, str]:
        """Re-compute the audit hash chain.

        Returns (problems, event_count, head_hash).  Detects edited, deleted or
        re-ordered events, events inserted mid-log, unhashed rows, sequence
        gaps, and a misplaced or repeated AUDIT_CHAIN_STARTED marker.  Cannot
        detect a chain rebuilt from scratch, a correctly hashed event appended
        at the end, or events cut from the end, unless the head hash was
        recorded elsewhere beforehand.
        """
        problems: list[str] = []
        prev = GENESIS_HASH
        expected_seq = 1
        count = 0
        head = GENESIS_HASH
        markers = 0
        for r in self._conn().execute("SELECT * FROM events ORDER BY sequence"):
            count += 1
            seq = r["sequence"]
            if seq != expected_seq:
                problems.append(f"sequence gap: expected #{expected_seq}, found #{seq}")
                expected_seq = seq
            if not r["hash"]:
                problems.append(f"#{seq}: event has no hash (written outside Relay?)")
            else:
                if r["prev_hash"] != prev:
                    problems.append(f"#{seq}: link to the previous event is broken")
                recomputed = event_hash(r["prev_hash"] or "", seq, r["event_id"],
                                        r["event_type"], r["task_id"], r["execution_id"],
                                        r["timestamp"], r["actor"], r["details"])
                if recomputed != r["hash"]:
                    problems.append(f"#{seq}: content does not match its hash (edited)")
            if r["event_type"] == AuditEventType.AUDIT_CHAIN_STARTED.value:
                markers += 1
                try:
                    chained = json.loads(r["details"]).get("legacy_events_chained")
                except (ValueError, AttributeError):
                    chained = None
                if markers > 1:
                    problems.append(f"#{seq}: a second AUDIT_CHAIN_STARTED marker")
                elif chained != count - 1:  # rows before the marker
                    problems.append(f"#{seq}: AUDIT_CHAIN_STARTED is not directly after "
                                    "the legacy events it claims to cover")
            prev = r["hash"] or ""
            head = r["hash"] or GENESIS_HASH
            expected_seq += 1
        return problems, count, head

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

    # ── Atomic operations ─────────────────────────────────────────────────────
    #
    # Each method performs all required database writes inside ONE SQLite
    # transaction (``with conn:``).  A state-transition UPDATE and its
    # corresponding audit-event INSERT commit together or roll back together.
    #
    # These methods MUST NOT call update_task_status(), append_event(), or
    # any other helper that issues its own conn.commit(), because doing so
    # would split the work across multiple transactions and break atomicity.
    # Sequence allocation for the audit event is inlined here for the same
    # reason.

    def accept_recovery_atomic(
        self,
        task_id: str,
        actor: str = "relay",
    ) -> None:
        """Atomically transition RECOVERY_REQUIRED → READY and emit RECOVERY_ACCEPTED.

        Re-checks task status inside the transaction (CAS pattern). Raises
        InvalidTransitionError if the task is not RECOVERY_REQUIRED, or if a
        concurrent writer changed the status between the read and the UPDATE.

        Does NOT touch the filesystem, create an execution, increment attempts,
        or launch Claude. Pure database state + audit only.
        """
        conn = self._conn()
        now = _now_iso()
        with conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise InvalidTransitionError(f"Task {task_id!r} not found.")
            current = TaskStatus(row["status"])
            if current != TaskStatus.RECOVERY_REQUIRED:
                raise InvalidTransitionError(
                    f"accept_recovery_atomic: task {task_id!r} is {current.value!r}, "
                    "expected RECOVERY_REQUIRED."
                )
            cursor = conn.execute(
                "UPDATE tasks SET status=?, error=NULL, updated_at=? "
                "WHERE task_id=? AND status=?",
                (TaskStatus.READY.value, now, task_id, TaskStatus.RECOVERY_REQUIRED.value),
            )
            if cursor.rowcount == 0:
                raise InvalidTransitionError(
                    f"Concurrent modification: task {task_id!r} status changed "
                    "during RECOVERY_REQUIRED → READY transition."
                )
            _insert_chained_event(
                conn, AuditEvent.new_id(), AuditEventType.RECOVERY_ACCEPTED.value,
                task_id, None, now, actor,
                json.dumps({"previous_status": current.value}),
            )

    def retry_task_atomic(
        self,
        task_id: str,
        actor: str = "relay",
    ) -> None:
        """Atomically transition TIMEOUT → READY and emit TASK_RETRIED.

        Re-checks task status and attempt ceiling inside the transaction (CAS).
        Raises InvalidTransitionError if the task is not TIMEOUT, or if all
        allowed attempts are exhausted, or if a concurrent writer changed the
        status first.

        Does NOT create an execution or touch the filesystem.
        """
        conn = self._conn()
        now = _now_iso()
        with conn:
            row = conn.execute(
                "SELECT status, attempt_count, max_execution_attempts "
                "FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise InvalidTransitionError(f"Task {task_id!r} not found.")
            current = TaskStatus(row["status"])
            if current != TaskStatus.TIMEOUT:
                raise InvalidTransitionError(
                    f"retry_task_atomic: task {task_id!r} is {current.value!r}, "
                    "expected TIMEOUT."
                )
            attempt_count = row["attempt_count"]
            max_attempts = row["max_execution_attempts"]
            if attempt_count >= max_attempts:
                raise InvalidTransitionError(
                    f"retry_task_atomic: task {task_id!r} has exhausted "
                    f"{attempt_count}/{max_attempts} attempt(s)."
                )
            cursor = conn.execute(
                "UPDATE tasks SET status=?, error=NULL, updated_at=? "
                "WHERE task_id=? AND status=?",
                (TaskStatus.READY.value, now, task_id, TaskStatus.TIMEOUT.value),
            )
            if cursor.rowcount == 0:
                raise InvalidTransitionError(
                    f"Concurrent modification: task {task_id!r} status changed "
                    "during TIMEOUT → READY transition."
                )
            _insert_chained_event(
                conn, AuditEvent.new_id(), AuditEventType.TASK_RETRIED.value,
                task_id, None, now, actor,
                json.dumps({"attempt_count": attempt_count}),
            )

    def acknowledge_acceptance_atomic(
        self,
        task_id: str,
        actor: str,
        criteria: list[str],
        accept: bool,
        reason: str | None = None,
    ) -> None:
        """Atomically record a human decision on UNVERIFIED acceptance criteria.

        Batch 1 (D5): ACCEPTANCE_REQUIRED → COMPLETED (accept) or → FAILED
        (reject), with an ACCEPTANCE_ACKNOWLEDGED / ACCEPTANCE_REJECTED event in
        the same transaction.  The event records the human decision only; the
        automated verdicts in VERIFICATION_COMPLETED are never rewritten, so an
        UNVERIFIED criterion stays UNVERIFIED in the record.

        Raises InvalidTransitionError if the task is not ACCEPTANCE_REQUIRED or a
        concurrent writer changed its status first.
        """
        conn = self._conn()
        now = _now_iso()
        new_status = TaskStatus.COMPLETED if accept else TaskStatus.FAILED
        event_type = (
            AuditEventType.ACCEPTANCE_ACKNOWLEDGED if accept
            else AuditEventType.ACCEPTANCE_REJECTED
        )
        error = None if accept else (
            f"Acceptance rejected by {actor!r}: {reason or 'no reason given'}"
        )
        with conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise InvalidTransitionError(f"Task {task_id!r} not found.")
            current = TaskStatus(row["status"])
            if current != TaskStatus.ACCEPTANCE_REQUIRED:
                raise InvalidTransitionError(
                    f"acknowledge_acceptance_atomic: task {task_id!r} is "
                    f"{current.value!r}, expected ACCEPTANCE_REQUIRED."
                )
            cursor = conn.execute(
                "UPDATE tasks SET status=?, error=?, updated_at=? "
                "WHERE task_id=? AND status=?",
                (new_status.value, error, now, task_id,
                 TaskStatus.ACCEPTANCE_REQUIRED.value),
            )
            if cursor.rowcount == 0:
                raise InvalidTransitionError(
                    f"Concurrent modification: task {task_id!r} status changed "
                    f"during ACCEPTANCE_REQUIRED → {new_status.value} transition."
                )
            _insert_chained_event(
                conn, AuditEvent.new_id(), event_type.value,
                task_id, None, now, actor,
                json.dumps({
                        "criteria": criteria,
                        "decision": "ACCEPTED" if accept else "REJECTED",
                        "reason": reason,
                        "human_acknowledgement": True,
                    }),
            )

    def mark_stale_lease_atomic(
        self,
        execution_id: str,
        actor: str = "relay",
        expected_expires_at: datetime | None = None,
    ) -> bool:
        """Atomically handle database changes when recovery finds a stale lease.

        Within a single transaction:
        - Marks the execution INTERRUPTED if it is still in RUNNING state (CAS).
        - Releases the workspace lease (idempotent: skipped if already released).
        - Transitions the task RUNNING|VERIFYING → RECOVERY_REQUIRED via CAS.
          If the task is already RECOVERY_REQUIRED, or in any other state, its
          status is left unchanged (the call is idempotent on task state).
        - Emits a RECOVERY_SCAN_STALE_LEASE audit event unconditionally.

        Safe against repeated/concurrent recovery attempts: repeated calls on
        the same execution_id are no-ops on state (because of CAS guards and
        the released=0 condition on the lease) and each call records its own
        audit event.

        Batch 3 (E-10): when *expected_expires_at* is given (the expiry the
        recovery scan observed), nothing is changed unless the lease is still
        active with exactly that expiry.  A lease renewed by `relay run-task`
        after the scan read it is therefore never reclaimed.  Returns True if
        the stale lease was handled.
        """
        conn = self._conn()
        now = _now_iso()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_expires_at is not None:
                lease_row = conn.execute(
                    "SELECT expires_at FROM execution_leases "
                    "WHERE execution_id=? AND released=0",
                    (execution_id,),
                ).fetchone()
                if lease_row is None or (
                    _parse_dt(lease_row["expires_at"]) != expected_expires_at
                ):
                    return False
            ex_row = conn.execute(
                "SELECT task_id, status FROM executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if ex_row is None:
                return False
            task_id = ex_row["task_id"]
            ex_status = ExecutionStatus(ex_row["status"])

            # Mark execution INTERRUPTED only if still RUNNING (CAS — idempotent)
            if ex_status == ExecutionStatus.RUNNING:
                conn.execute(
                    "UPDATE executions SET status=?, error=?, ended_at=? "
                    "WHERE execution_id=? AND status=?",
                    (
                        ExecutionStatus.INTERRUPTED.value,
                        "Stale lease: worker did not renew the execution lease.",
                        now, execution_id, ExecutionStatus.RUNNING.value,
                    ),
                )

            # Release lease (idempotent: WHERE released=0 is a no-op if already 1)
            conn.execute(
                "UPDATE execution_leases SET released=1 "
                "WHERE execution_id=? AND released=0",
                (execution_id,),
            )

            # Transition task RUNNING|VERIFYING → RECOVERY_REQUIRED (CAS, idempotent)
            task_transitioned = False
            task_row = conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task_row is not None:
                task_status = TaskStatus(task_row["status"])
                if task_status in (TaskStatus.RUNNING, TaskStatus.VERIFYING):
                    cursor = conn.execute(
                        "UPDATE tasks SET status=?, updated_at=? "
                        "WHERE task_id=? AND status=?",
                        (
                            TaskStatus.RECOVERY_REQUIRED.value,
                            now, task_id, task_status.value,
                        ),
                    )
                    task_transitioned = cursor.rowcount > 0

            _insert_chained_event(
                conn, AuditEvent.new_id(), AuditEventType.RECOVERY_SCAN_STALE_LEASE.value,
                task_id, execution_id, now, actor,
                json.dumps({
                        "execution_id": execution_id,
                        "task_transitioned": task_transitioned,
                    }),
            )
        return True

    def mark_orphaned_task_atomic(
        self,
        task_id: str,
        reason: str,
        actor: str = "relay",
    ) -> bool:
        """Move a RUNNING/VERIFYING task with no active lease to RECOVERY_REQUIRED.

        Batch 3 (E-7): a task can be left RUNNING or VERIFYING after its lease is
        gone (verifier exception, interrupted CLI).  Lease-based recovery cannot
        see it.  In one transaction this re-checks that no active lease exists
        for the task (so a live execution is never touched), transitions the task
        via CAS, marks still-RUNNING executions INTERRUPTED, and emits a
        RECOVERY_REQUIRED event.  Returns True only if the task transitioned.
        """
        conn = self._conn()
        now = _now_iso()
        with conn:
            # IMMEDIATE: the "no active lease" check below and the update are one
            # serialised unit; no lease can be acquired between them.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return False
            current = TaskStatus(row["status"])
            if current not in (TaskStatus.RUNNING, TaskStatus.VERIFYING):
                return False
            active = conn.execute(
                "SELECT COUNT(*) FROM execution_leases WHERE task_id=? AND released=0",
                (task_id,),
            ).fetchone()[0]
            if active:
                return False
            cursor = conn.execute(
                "UPDATE tasks SET status=?, error=?, updated_at=? "
                "WHERE task_id=? AND status=?",
                (TaskStatus.RECOVERY_REQUIRED.value, reason, now, task_id, current.value),
            )
            if cursor.rowcount == 0:
                return False
            conn.execute(
                "UPDATE executions SET status=?, error=?, ended_at=? "
                "WHERE task_id=? AND status=?",
                (ExecutionStatus.INTERRUPTED.value, reason, now, task_id,
                 ExecutionStatus.RUNNING.value),
            )
            latest = conn.execute(
                "SELECT execution_id FROM executions WHERE task_id=? "
                "ORDER BY attempt_number DESC, started_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            _insert_chained_event(
                conn, AuditEvent.new_id(), AuditEventType.RECOVERY_REQUIRED.value,
                task_id, latest["execution_id"] if latest else None, now, actor,
                json.dumps({"previous_status": current.value, "reason": reason}),
            )
            return True

    def pre_launch_failed_atomic(
        self,
        execution_id: str,
        error: str,
        actor: str = "relay",
    ) -> None:
        """Atomically mark a RUNNING execution FAILED before Claude launched, and emit EXECUTION_FAILED.

        Used for pre-launch failures (auth misconfiguration, Popen failure) where
        the execution record was created as RUNNING but Claude never started.
        Commits the execution status update and the audit event in one transaction,
        eliminating the window where the DB could show a RUNNING execution after
        the audit event has already committed.

        Callers may safely call update_execution() afterwards — it will re-write
        the same FAILED status, which is idempotent.
        """
        conn = self._conn()
        now = _now_iso()
        with conn:
            row = conn.execute(
                "SELECT task_id FROM executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if row is None:
                return
            task_id = row["task_id"]
            conn.execute(
                "UPDATE executions SET status=?, ended_at=?, error=? "
                "WHERE execution_id=?",
                (ExecutionStatus.FAILED.value, now, error, execution_id),
            )
            _insert_chained_event(
                conn, AuditEvent.new_id(), AuditEventType.EXECUTION_FAILED.value,
                task_id, execution_id, now, actor,
                json.dumps({"error": error}),
            )
