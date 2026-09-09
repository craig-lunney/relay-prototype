from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    ALLOWED_A2A_TRANSITIONS,
    A2AProtocolState,
    A2ATask,
    A2ATaskTier,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class A2AInvalidTransitionError(Exception):
    pass


class A2AStateManager:
    """SQLite-backed persistence for A2A protocol tasks.

    Thread-safe: each thread gets its own connection via threading.local.
    WAL mode is enabled so readers do not block writers.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._local = threading.local()
        self._init_db()

    # ── Connection ─────────────────────────────────────────────────────────────

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
            CREATE TABLE IF NOT EXISTS a2a_tasks (
                task_id          TEXT PRIMARY KEY,
                title            TEXT NOT NULL,
                tier             TEXT NOT NULL,
                github_issue_url TEXT NOT NULL,
                state            TEXT NOT NULL,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                protocol_version TEXT NOT NULL DEFAULT '0.2'
            );

            CREATE INDEX IF NOT EXISTS idx_a2a_tasks_created_at
                ON a2a_tasks(created_at);
        """)
        conn.commit()

    # ── Tasks ──────────────────────────────────────────────────────────────────

    def create_task(self, task: A2ATask) -> None:
        """Persist a new A2ATask.

        Raises sqlite3.IntegrityError if a task with the same task_id already
        exists. Callers should use A2ATask.new_id() to generate unique IDs.
        """
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO a2a_tasks
                (task_id, title, tier, github_issue_url, state,
                 created_at, updated_at, protocol_version)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                task.task_id,
                task.title,
                task.tier.value,
                task.github_issue_url,
                task.state.value,
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
                task.protocol_version,
            ),
        )
        conn.commit()

    def get_task(self, task_id: str) -> A2ATask | None:
        """Return the A2ATask for task_id, or None if not found."""
        row = self._conn().execute(
            "SELECT * FROM a2a_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self) -> list[A2ATask]:
        """Return all tasks ordered by created_at ascending."""
        rows = self._conn().execute(
            "SELECT * FROM a2a_tasks ORDER BY created_at"
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def transition_state(
        self,
        task_id: str,
        new_state: A2AProtocolState,
    ) -> A2ATask:
        """Validate and apply a protocol state transition.

        Uses a compare-and-swap (CAS) pattern: validates against the current
        persisted state, then UPDATEs using WHERE state=<current>. A concurrent
        writer that changes state between the read and the UPDATE causes
        rowcount=0, which is raised as A2AInvalidTransitionError rather than
        silently committing an invalid transition.

        Returns the updated A2ATask on success.

        Raises A2AInvalidTransitionError if:
        - task_id does not exist
        - the transition is not in ALLOWED_A2A_TRANSITIONS for the current state
        - a concurrent writer committed a state change first
        """
        conn = self._conn()
        now = _now_iso()

        row = conn.execute(
            "SELECT state FROM a2a_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise A2AInvalidTransitionError(f"Task {task_id!r} not found.")

        current = A2AProtocolState(row["state"])
        allowed = ALLOWED_A2A_TRANSITIONS.get(current, frozenset())
        if new_state not in allowed:
            raise A2AInvalidTransitionError(
                f"Transition {current.value} → {new_state.value} is not allowed "
                f"(allowed from {current.value}: "
                f"{sorted(s.value for s in allowed) or 'none'})."
            )

        cursor = conn.execute(
            "UPDATE a2a_tasks SET state=?, updated_at=? WHERE task_id=? AND state=?",
            (new_state.value, now, task_id, current.value),
        )
        conn.commit()

        if cursor.rowcount == 0:
            raise A2AInvalidTransitionError(
                f"Concurrent modification: task {task_id!r} state changed "
                f"during {current.value} → {new_state.value}. "
                "Another writer committed first."
            )

        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError(
                f"Task {task_id!r} disappeared immediately after a committed transition. "
                "This should never happen."
            )
        return updated

    # ── Internal ───────────────────────────────────────────────────────────────

    def _row_to_task(self, row: sqlite3.Row) -> A2ATask:
        return A2ATask(
            task_id=row["task_id"],
            title=row["title"],
            tier=A2ATaskTier(row["tier"]),
            github_issue_url=row["github_issue_url"],
            state=A2AProtocolState(row["state"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            protocol_version=row["protocol_version"],
        )
