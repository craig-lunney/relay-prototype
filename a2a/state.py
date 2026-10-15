from __future__ import annotations

import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .models import (
    ALLOWED_A2A_TRANSITIONS,
    A2AProtocolState,
    A2ATask,
    A2ATaskTier,
)
from .protocol import RULES, A2ARole

# Accepts only: https://github.com/<owner>/<repo>/issues/<positive-integer>
# Rejects: http, non-github hosts, PR URLs, /issues/0, non-numeric, extra path segments.
GITHUB_ISSUE_URL_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/issues/[1-9][0-9]*$")

# Relay status that counts as implementation evidence (D1): independently
# verified and, where criteria were UNVERIFIED, human-acknowledged.
RELAY_EVIDENCE_STATUS = "COMPLETED"

# Placeholder actors that are not people or agents and never bind a role.
_UNBOUND_ACTORS = frozenset({"unattributed", "legacy-import"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


# Canonical actor names: ASCII letters/digits with single . _ @ - or space
# separators.  Anything else (control or invisible characters, combining marks,
# fillers, look-alike letters from other scripts) is refused rather than
# normalised, so two names that look the same are the same name.
_ACTOR_NAME_RE = re.compile(r"[a-z0-9]+(?:[ ._@-][a-z0-9]+)*")
_ACTOR_NAME_MAX = 64


def actor_key(actor: str) -> str:
    """Canonical form of an actor name used for role binding.

    "Claude", " claude " and "ｃｌａｕｄｅ" are the same actor: NFKC-normalised,
    invisible format characters removed, whitespace collapsed, case-folded.
    Use valid_actor_name() to refuse names that are not plain after this.
    """
    text = unicodedata.normalize("NFKC", actor or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return " ".join(text.split()).casefold()


def valid_actor_name(actor: str) -> bool:
    key = actor_key(actor)
    return (0 < len(key) <= _ACTOR_NAME_MAX
            and _ACTOR_NAME_RE.fullmatch(key) is not None)


class A2AInvalidTransitionError(Exception):
    pass


class A2AInvalidTaskDataError(ValueError):
    """Raised by the persistence layer when task data violates the protocol."""


class A2ARelayUnavailableError(Exception):
    """The Relay database could not be consulted (missing, unreadable, locked)."""


@dataclass(frozen=True)
class A2AAttestation:
    """Who is taking a protocol step, in which role, and on what evidence.

    Recorded verbatim in the append-only transition history.  These are claims
    made by the operator of the CLI, not authenticated identities.
    """
    actor: str
    role: A2ARole
    reason: str | None = None
    evidence_ref: str | None = None
    relay_task_id: str | None = None


@dataclass(frozen=True)
class A2ATransitionRecord:
    seq: int
    task_id: str
    from_state: A2AProtocolState | None
    to_state: A2AProtocolState
    actor: str
    role: A2ARole
    reason: str | None
    evidence_ref: str | None
    relay_task_id: str | None
    timestamp: datetime


@dataclass(frozen=True)
class A2AHumanVerification:
    task_id: str
    actor: str
    command: str
    output: str
    gate2_entry_seq: int
    recorded_at: datetime


@dataclass(frozen=True)
class A2ATierChange:
    task_id: str
    from_tier: A2ATaskTier
    to_tier: A2ATaskTier
    actor: str
    reason: str
    evidence_ref: str | None
    after_seq: int
    timestamp: datetime


RelayStatusLookup = Callable[[str], "str | None"]


def relay_status_from_db(relay_db_path: str | Path) -> RelayStatusLookup:
    """Return a lookup that reads a Relay task's status without writing to it.

    The Relay database is opened in SQLite's read-only URI mode on every call,
    so A2A never changes Relay data.  (SQLite may still create the usual
    -wal/-shm side files next to a WAL-mode database while reading it.)

    The lookup returns the status, None if the task does not exist, and raises
    A2ARelayUnavailableError if the database is missing, unreadable or locked.
    """
    path = Path(relay_db_path)

    def lookup(relay_task_id: str) -> str | None:
        if not path.is_file():
            raise A2ARelayUnavailableError(
                f"Relay database {str(path)!r} does not exist (check --relay-db)."
            )
        uri = path.resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                row = conn.execute(
                    "SELECT status FROM tasks WHERE task_id=?", (relay_task_id,)
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise A2ARelayUnavailableError(
                f"Relay database {str(path)!r} could not be read: {exc}."
            ) from exc
        return row[0] if row else None

    return lookup


_SCHEMA_SQL = """
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

            -- Batch 4: append-only transition history (the ledger).  Creation is
            -- recorded as a transition with from_state NULL.
            CREATE TABLE IF NOT EXISTS a2a_transitions (
                seq           INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id       TEXT NOT NULL REFERENCES a2a_tasks(task_id),
                from_state    TEXT,
                to_state      TEXT NOT NULL,
                actor         TEXT NOT NULL,
                role          TEXT NOT NULL,
                reason        TEXT,
                evidence_ref  TEXT,
                relay_task_id TEXT,
                timestamp     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_a2a_transitions_task
                ON a2a_transitions(task_id, seq);

            -- Batch 4 (D4): T3 human re-run records (protocol §9.7).
            CREATE TABLE IF NOT EXISTS a2a_human_verifications (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id         TEXT NOT NULL REFERENCES a2a_tasks(task_id),
                actor           TEXT NOT NULL,
                command         TEXT NOT NULL,
                output          TEXT NOT NULL,
                gate2_entry_seq INTEGER NOT NULL,
                recorded_at     TEXT NOT NULL
            );

            -- Batch 4: Human tier escalation at Gate 1 (§3, §10.3, §14).
            CREATE TABLE IF NOT EXISTS a2a_tier_changes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id      TEXT NOT NULL REFERENCES a2a_tasks(task_id),
                from_tier    TEXT NOT NULL,
                to_tier      TEXT NOT NULL,
                actor        TEXT NOT NULL,
                reason       TEXT NOT NULL,
                evidence_ref TEXT,
                after_seq    INTEGER NOT NULL,
                timestamp    TEXT NOT NULL
            );

            -- Defence against casual edits, not a security boundary: the ledger
            -- tables are append-only, and a task's tier can only move T2 -> T3
            -- (through escalate_tier, which records it).
            CREATE TRIGGER IF NOT EXISTS a2a_transitions_no_update
                BEFORE UPDATE ON a2a_transitions
                BEGIN SELECT RAISE(ABORT, 'a2a_transitions is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS a2a_transitions_no_delete
                BEFORE DELETE ON a2a_transitions
                BEGIN SELECT RAISE(ABORT, 'a2a_transitions is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS a2a_verifications_no_update
                BEFORE UPDATE ON a2a_human_verifications
                BEGIN SELECT RAISE(ABORT, 'a2a_human_verifications is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS a2a_verifications_no_delete
                BEFORE DELETE ON a2a_human_verifications
                BEGIN SELECT RAISE(ABORT, 'a2a_human_verifications is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS a2a_tier_changes_no_update
                BEFORE UPDATE ON a2a_tier_changes
                BEGIN SELECT RAISE(ABORT, 'a2a_tier_changes is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS a2a_tier_changes_no_delete
                BEFORE DELETE ON a2a_tier_changes
                BEGIN SELECT RAISE(ABORT, 'a2a_tier_changes is append-only'); END;
            DROP TRIGGER IF EXISTS a2a_tasks_tier_immutable;
            CREATE TRIGGER IF NOT EXISTS a2a_tasks_tier_escalation_only
                BEFORE UPDATE OF tier ON a2a_tasks
                WHEN NEW.tier IS NOT OLD.tier AND NOT (OLD.tier = 'T2' AND NEW.tier = 'T3')
                BEGIN SELECT RAISE(ABORT, 'a2a task tier can only be escalated T2 -> T3'); END;
        """
# Statements end with ";" at end of line (trigger bodies keep theirs inline).
_SCHEMA_STATEMENTS = [st for st in re.split(r";[ \t]*$", _SCHEMA_SQL, flags=re.M)
                      if st.strip()]


class A2AStateManager:
    """SQLite-backed persistence for A2A protocol tasks — the attested ledger.

    Every rule of the protocol that the ledger enforces is checked here, inside
    the same IMMEDIATE transaction that writes the change, so no caller can skip
    a check by using this class directly and no concurrent writer can slip
    between a check and the write.  (The only check made before the transaction
    is the read-only Relay status lookup, so a slow Relay database never holds
    the A2A write lock; COMPLETED is a terminal Relay status.)

    Thread-safe: each thread gets its own connection via threading.local.
    WAL mode is enabled so readers do not block writers.
    """

    def __init__(
        self,
        db_path: str | Path,
        relay_status_lookup: RelayStatusLookup | None = None,
    ):
        self.db_path = str(db_path)
        self._relay_status = relay_status_lookup
        self._local = threading.local()
        self._init_db()

    # ── Connection ─────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # Switching to WAL needs a brief exclusive lock and does not wait on
            # the busy timeout, so concurrent first opens can see "database is
            # locked".  Retry briefly rather than fail.
            for attempt in range(50):
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == 49:
                        raise
                    time.sleep(0.1)
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    # Schema version recorded in PRAGMA user_version.
    #   0: pre-ledger (a2a_tasks only)
    #   1: attested ledger (Batch 4)
    _SCHEMA_VERSION = 1

    def _init_db(self) -> None:
        """Create or upgrade the schema in ONE IMMEDIATE transaction.

        Table creation, the "did the ledger exist?" test, the one-time legacy
        import and the schema-version update are serialised together, so two
        processes opening an old database at the same moment cannot interleave
        (e.g. one creating the ledger tables and the other then recording the
        migration as done without importing the legacy tasks).
        """
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            had_ledger = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='a2a_transitions'"
            ).fetchone() is not None
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(a2a_transitions)")}
            if "tier" not in columns:
                conn.execute("ALTER TABLE a2a_transitions ADD COLUMN tier TEXT")
            if version < self._SCHEMA_VERSION:
                if not had_ledger:
                    self._import_legacy_tasks(conn)
                conn.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _import_legacy_tasks(conn: sqlite3.Connection) -> None:
        """One-time migration of a pre-ledger database (runs once, ever).

        Tasks that existed before the ledger get one 'legacy-import' creation row
        in their current state and tier, so their history chains from there.
        This happens only when the ledger table did not exist before: afterwards
        a task with no history can only have been inserted outside the ledger,
        and check_consistency reports it.
        """
        conn.execute(
            """
            INSERT INTO a2a_transitions (task_id, from_state, to_state, actor,
                                         role, reason, tier, timestamp)
            SELECT t.task_id, NULL, t.state, 'legacy-import', 'HUMAN',
                   'task existed before the ledger; imported in its current state',
                   t.tier, t.updated_at
            FROM a2a_tasks t
            WHERE NOT EXISTS (SELECT 1 FROM a2a_transitions h WHERE h.task_id = t.task_id)
            """
        )

    # ── Tasks ──────────────────────────────────────────────────────────────────

    def create_task(self, task: A2ATask, created_by: str | None = None) -> None:
        """Persist a new A2ATask in DRAFT and record its creation in the ledger.

        Validated here (not only in the service) so this public method cannot
        insert a task in a later state or with invalid data.  The creator is
        recorded as a HUMAN act (§2: the Human creates tasks) and binds that
        actor to the HUMAN role on this task.

        Raises A2AInvalidTaskDataError on invalid data, sqlite3.IntegrityError if
        the task_id already exists.
        """
        if task.state != A2AProtocolState.DRAFT:
            raise A2AInvalidTaskDataError(
                f"New tasks must start in DRAFT (got {task.state.value})."
            )
        if not task.title or not task.title.strip():
            raise A2AInvalidTaskDataError("title must not be empty or whitespace-only.")
        if not isinstance(task.tier, A2ATaskTier):
            raise A2AInvalidTaskDataError(f"Invalid tier {task.tier!r}.")
        if not GITHUB_ISSUE_URL_RE.match(task.github_issue_url):
            raise A2AInvalidTaskDataError(
                "github_issue_url must be a GitHub Issue URL of the form "
                "https://github.com/<owner>/<repo>/issues/<number> "
                f"(got {task.github_issue_url!r})."
            )
        creator = (created_by or "").strip() or "unattributed"
        if created_by is not None and created_by.strip():
            if actor_key(creator) in _UNBOUND_ACTORS or not valid_actor_name(creator):
                raise A2AInvalidTaskDataError(
                    f"Creator name {creator!r} is not allowed: use letters and digits "
                    "with single '.', '_', '@', '-' or space separators."
                )
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO a2a_tasks
                    (task_id, title, tier, github_issue_url, state,
                     created_at, updated_at, protocol_version)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    task.task_id, task.title, task.tier.value, task.github_issue_url,
                    task.state.value, task.created_at.isoformat(),
                    task.updated_at.isoformat(), task.protocol_version,
                ),
            )
            conn.execute(
                "INSERT INTO a2a_transitions (task_id, from_state, to_state, actor, role, "
                "reason, tier, timestamp) VALUES (?,?,?,?,?,?,?,?)",
                (task.task_id, None, A2AProtocolState.DRAFT.value, creator,
                 A2ARole.HUMAN.value, "task created", task.tier.value,
                 task.created_at.isoformat()),
            )

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

    def get_history(self, task_id: str) -> list[A2ATransitionRecord]:
        rows = self._conn().execute(
            "SELECT * FROM a2a_transitions WHERE task_id=? ORDER BY seq", (task_id,)
        ).fetchall()
        return [
            A2ATransitionRecord(
                seq=r["seq"], task_id=r["task_id"],
                from_state=A2AProtocolState(r["from_state"]) if r["from_state"] else None,
                to_state=A2AProtocolState(r["to_state"]), actor=r["actor"],
                role=A2ARole(r["role"]), reason=r["reason"],
                evidence_ref=r["evidence_ref"], relay_task_id=r["relay_task_id"],
                timestamp=_parse_dt(r["timestamp"]),
            )
            for r in rows
        ]

    def get_human_verifications(self, task_id: str) -> list[A2AHumanVerification]:
        rows = self._conn().execute(
            "SELECT * FROM a2a_human_verifications WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return [
            A2AHumanVerification(
                task_id=r["task_id"], actor=r["actor"], command=r["command"],
                output=r["output"], gate2_entry_seq=r["gate2_entry_seq"],
                recorded_at=_parse_dt(r["recorded_at"]),
            )
            for r in rows
        ]

    def get_tier_changes(self, task_id: str) -> list[A2ATierChange]:
        rows = self._conn().execute(
            "SELECT * FROM a2a_tier_changes WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return [
            A2ATierChange(
                task_id=r["task_id"], from_tier=A2ATaskTier(r["from_tier"]),
                to_tier=A2ATaskTier(r["to_tier"]), actor=r["actor"], reason=r["reason"],
                evidence_ref=r["evidence_ref"], after_seq=r["after_seq"],
                timestamp=_parse_dt(r["timestamp"]),
            )
            for r in rows
        ]

    # ── Transitions ────────────────────────────────────────────────────────────

    def transition_state(
        self,
        task_id: str,
        new_state: A2AProtocolState,
        attestation: A2AAttestation,
    ) -> A2ATask:
        """Validate and apply a protocol transition, recording it in the ledger.

        Inside one IMMEDIATE transaction this checks, in order: the task exists;
        the transition is in the protocol graph; the task's tier may take it;
        the attested role may trigger it; the actor has not acted in a different
        role on this task; the required reason / evidence is present; a cited
        Relay task is COMPLETED and has never been cited before; and, for T3
        tasks entering SHIPPED, that a human re-run was recorded in the current
        Gate 2 window.  Only then is the state updated (CAS) and the transition
        appended to the history.

        Raises A2AInvalidTransitionError on any violation; nothing is written.
        """
        relay_status = self._prefetch_relay_status(attestation.relay_task_id)
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT state, tier FROM a2a_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise A2AInvalidTransitionError(f"Task {task_id!r} not found.")
            current = A2AProtocolState(row["state"])
            tier = A2ATaskTier(row["tier"])
            self._check_transition(conn, task_id, tier, current, new_state,
                                   attestation, relay_status)

            now = _now_iso()
            cursor = conn.execute(
                "UPDATE a2a_tasks SET state=?, updated_at=? WHERE task_id=? AND state=?",
                (new_state.value, now, task_id, current.value),
            )
            if cursor.rowcount == 0:  # defence in depth; IMMEDIATE already serialises
                raise A2AInvalidTransitionError(
                    f"Concurrent modification: task {task_id!r} state changed "
                    f"during {current.value} → {new_state.value}. "
                    "Another writer committed first."
                )
            conn.execute(
                "INSERT INTO a2a_transitions (task_id, from_state, to_state, actor, role, "
                "reason, evidence_ref, relay_task_id, timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, current.value, new_state.value, attestation.actor.strip(),
                 attestation.role.value, attestation.reason, attestation.evidence_ref,
                 attestation.relay_task_id, now),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError(
                f"Task {task_id!r} disappeared immediately after a committed transition. "
                "This should never happen."
            )
        return updated

    def _prefetch_relay_status(self, relay_task_id: str | None):
        """Look up a cited Relay task before taking the A2A write lock.

        Returns ("ok", status-or-None), ("unconfigured", None) or
        ("unavailable", message).  The result is only used if the step
        actually requires Relay evidence.
        """
        if not (relay_task_id or "").strip():
            return ("none", None)
        if self._relay_status is None:
            return ("unconfigured", None)
        try:
            return ("ok", self._relay_status(relay_task_id))
        except A2ARelayUnavailableError as exc:
            return ("unavailable", str(exc))

    def _bound_roles(self, conn: sqlite3.Connection, task_id: str, actor: str) -> set[str]:
        """Roles the actor has already taken on this task (canonical actor match)."""
        key = actor_key(actor)
        roles: set[str] = set()
        for r in conn.execute(
            "SELECT actor, role FROM a2a_transitions WHERE task_id=?", (task_id,)
        ).fetchall():
            if actor_key(r["actor"]) == key and r["actor"] not in _UNBOUND_ACTORS:
                roles.add(r["role"])
        for r in conn.execute(
            "SELECT actor FROM a2a_human_verifications WHERE task_id=?", (task_id,)
        ).fetchall():
            if actor_key(r["actor"]) == key:
                roles.add(A2ARole.HUMAN.value)
        for r in conn.execute(
            "SELECT actor FROM a2a_tier_changes WHERE task_id=?", (task_id,)
        ).fetchall():
            if actor_key(r["actor"]) == key:
                roles.add(A2ARole.HUMAN.value)
        return roles

    def _check_actor(self, conn, task_id: str, actor: str, role: A2ARole) -> str:
        actor = (actor or "").strip()
        if not actor_key(actor):
            raise A2AInvalidTransitionError("An actor must be named for every transition.")
        if not valid_actor_name(actor):
            raise A2AInvalidTransitionError(
                f"Actor name {actor!r} is not allowed: use letters and digits with single "
                "'.', '_', '@', '-' or space separators (at most 64 characters)."
            )
        if actor_key(actor) in _UNBOUND_ACTORS:
            raise A2AInvalidTransitionError(
                f"{actor!r} is a reserved placeholder, not an actor name."
            )
        other_roles = self._bound_roles(conn, task_id, actor) - {role.value}
        if other_roles:
            raise A2AInvalidTransitionError(
                f"Actor {actor!r} already acted on this task as "
                f"{', '.join(sorted(other_roles))}; one actor may not take two "
                "protocol roles on the same task (no self-review or self-approval)."
            )
        return actor

    def _check_transition(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        tier: A2ATaskTier,
        current: A2AProtocolState,
        new_state: A2AProtocolState,
        att: A2AAttestation,
        relay_status,
    ) -> None:
        allowed = ALLOWED_A2A_TRANSITIONS.get(current, frozenset())
        if new_state not in allowed:
            raise A2AInvalidTransitionError(
                f"Transition {current.value} → {new_state.value} is not allowed "
                f"(allowed from {current.value}: "
                f"{sorted(s.value for s in allowed) or 'none'})."
            )
        rule = RULES[(current, new_state)]
        if tier not in rule.tiers:
            path = ("T1 tasks follow: DRAFT → IN_IMPLEMENTATION → AWAITING_GATE_2 "
                    "→ SHIPPED → CLOSED." if tier == A2ATaskTier.T1 else
                    "T2/T3 tasks follow: DRAFT → SPECCING → AWAITING_GATE_1 → "
                    "IN_IMPLEMENTATION → UNDER_REVIEW → AWAITING_GATE_2 → SHIPPED → CLOSED.")
            raise A2AInvalidTransitionError(
                f"{current.value} → {new_state.value} is not part of the "
                f"{tier.value} protocol path. {path}"
            )
        if att.role not in rule.roles:
            raise A2AInvalidTransitionError(
                f"{current.value} → {new_state.value} ({rule.trigger}) may only be "
                f"performed by {'/'.join(sorted(r.value for r in rule.roles))}, "
                f"not {att.role.value}."
            )
        self._check_actor(conn, task_id, att.actor, att.role)
        if rule.reason_required and not (att.reason or "").strip():
            raise A2AInvalidTransitionError(
                f"{current.value} → {new_state.value} requires a reason ({rule.trigger})."
            )
        if rule.evidence_required and not (att.evidence_ref or "").strip():
            raise A2AInvalidTransitionError(
                f"{current.value} → {new_state.value} requires an evidence reference "
                f"for its trigger ({rule.trigger}), e.g. the GitHub comment or PR URL."
            )
        if rule.relay_required:
            self._check_relay_evidence(conn, att.relay_task_id, relay_status)
        elif (att.relay_task_id or "").strip():
            raise A2AInvalidTransitionError(
                "A Relay task is cited only on the implementation step that hands work "
                f"to review; {current.value} → {new_state.value} does not take one."
            )
        if (tier == A2ATaskTier.T3 and current == A2AProtocolState.AWAITING_GATE_2
                and new_state == A2AProtocolState.SHIPPED):
            window = self._gate2_entry_seq(conn, task_id)
            recorded = conn.execute(
                "SELECT COUNT(*) FROM a2a_human_verifications "
                "WHERE task_id=? AND gate2_entry_seq=?",
                (task_id, window),
            ).fetchone()[0]
            if not recorded:
                raise A2AInvalidTransitionError(
                    "T3 Gate 2 may not proceed without a recorded human re-run of a "
                    "prescribed verification command (protocol §9.7). Run "
                    f"'a2a record-human-verification {task_id} ...' first."
                )

    def _check_relay_evidence(
        self, conn: sqlite3.Connection, relay_task_id: str | None, relay_status
    ) -> None:
        kind, value = relay_status
        if kind == "none":
            raise A2AInvalidTransitionError(
                "This step requires implementation evidence from Relay: cite the "
                "Relay task (--relay-task) whose independent verification passed."
            )
        if kind == "unconfigured":
            raise A2AInvalidTransitionError(
                "No Relay database is configured, so the cited Relay task cannot be "
                "checked. Pass --relay-db."
            )
        if kind == "unavailable":
            raise A2AInvalidTransitionError(value)
        if value is None:
            raise A2AInvalidTransitionError(
                f"Relay task {relay_task_id!r} was not found in the Relay database."
            )
        if value != RELAY_EVIDENCE_STATUS:
            raise A2AInvalidTransitionError(
                f"Relay task {relay_task_id!r} is {value}; only a COMPLETED Relay task "
                "(independently verified, with any UNVERIFIED criteria acknowledged) "
                "counts as implementation evidence."
            )
        other = conn.execute(
            "SELECT task_id FROM a2a_transitions WHERE relay_task_id=? LIMIT 1",
            (relay_task_id,),
        ).fetchone()
        if other:
            raise A2AInvalidTransitionError(
                f"Relay task {relay_task_id!r} was already cited as evidence (by A2A "
                f"task {other['task_id']}); each implementation, including a "
                "revision, needs its own verified Relay run."
            )

    @staticmethod
    def _gate2_entry_seq(conn: sqlite3.Connection, task_id: str) -> int:
        """Sequence number of the task's latest entry into AWAITING_GATE_2."""
        row = conn.execute(
            "SELECT MAX(seq) FROM a2a_transitions WHERE task_id=? AND to_state=?",
            (task_id, A2AProtocolState.AWAITING_GATE_2.value),
        ).fetchone()
        return row[0] or 0

    # ── T3 human re-run (protocol §9.7) ────────────────────────────────────────

    def record_human_verification(
        self, task_id: str, actor: str, command: str, output: str
    ) -> A2AHumanVerification:
        """Record a human's independent re-run of a prescribed verification command.

        Allowed only for T3 tasks in AWAITING_GATE_2, by an actor who has not
        acted on the task in a non-HUMAN role (and who is thereafter bound to
        HUMAN).  The record is bound to the current Gate 2 window: a revision
        loop back through AWAITING_GATE_2 requires a fresh re-run.  The software
        cannot verify that the human really ran the command; it records the
        human's attestation.
        """
        if not (command or "").strip() or not (output or "").strip():
            raise A2AInvalidTransitionError(
                "A human verification needs an actor, the exact command and its "
                "verbatim output."
            )
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT state, tier FROM a2a_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise A2AInvalidTransitionError(f"Task {task_id!r} not found.")
            if row["tier"] != A2ATaskTier.T3.value:
                raise A2AInvalidTransitionError(
                    f"Human re-run records apply to T3 tasks only (task is {row['tier']})."
                )
            if row["state"] != A2AProtocolState.AWAITING_GATE_2.value:
                raise A2AInvalidTransitionError(
                    f"A human re-run is recorded at Gate 2; task is {row['state']}."
                )
            try:
                actor = self._check_actor(conn, task_id, actor, A2ARole.HUMAN)
            except A2AInvalidTransitionError as exc:
                raise A2AInvalidTransitionError(
                    f"{exc} The re-run must be performed by a human."
                ) from None
            window = self._gate2_entry_seq(conn, task_id)
            now = _now_iso()
            conn.execute(
                "INSERT INTO a2a_human_verifications (task_id, actor, command, output, "
                "gate2_entry_seq, recorded_at) VALUES (?,?,?,?,?,?)",
                (task_id, actor, command, output, window, now),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return A2AHumanVerification(task_id, actor, command, output, window, _parse_dt(now))

    # ── Tier escalation (§3, §10.3, §14) ───────────────────────────────────────

    def escalate_tier(
        self, task_id: str, actor: str, reason: str, evidence_ref: str | None = None
    ) -> A2ATask:
        """Escalate a T2 task to T3 at Gate 1 (Human decision, recorded).

        §3: a task declaring an EXCLUSIVE resource is T3; §10.3: a significant
        amendment (e.g. a new EXCLUSIVE resource) returns the task to Gate 1,
        where §14 has the Human confirm the tier.  Escalation is one-way: a task
        cannot be moved to a lower tier, so T3 controls can never be shed.
        """
        if not (reason or "").strip():
            raise A2AInvalidTransitionError("A tier escalation requires a reason.")
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT state, tier FROM a2a_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise A2AInvalidTransitionError(f"Task {task_id!r} not found.")
            if row["tier"] != A2ATaskTier.T2.value:
                raise A2AInvalidTransitionError(
                    f"Only T2 tasks can be escalated (to T3); task is {row['tier']}."
                )
            if row["state"] != A2AProtocolState.AWAITING_GATE_1.value:
                raise A2AInvalidTransitionError(
                    "The tier is confirmed at Gate 1; escalate while the task is "
                    f"AWAITING_GATE_1 (it is {row['state']})."
                )
            actor = self._check_actor(conn, task_id, actor, A2ARole.HUMAN)
            now = _now_iso()
            after_seq = conn.execute(
                "SELECT MAX(seq) FROM a2a_transitions WHERE task_id=?", (task_id,)
            ).fetchone()[0] or 0
            conn.execute(
                "UPDATE a2a_tasks SET tier=?, updated_at=? WHERE task_id=?",
                (A2ATaskTier.T3.value, now, task_id),
            )
            conn.execute(
                "INSERT INTO a2a_tier_changes (task_id, from_tier, to_tier, actor, reason, "
                "evidence_ref, after_seq, timestamp) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, A2ATaskTier.T2.value, A2ATaskTier.T3.value, actor, reason,
                 evidence_ref, after_seq, now),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self.get_task(task_id)

    # ── Ledger consistency ─────────────────────────────────────────────────────

    def check_consistency(self) -> list[str]:
        """Replay every task's recorded history against the protocol rules.

        Reports: a task with no history (inserted outside the ledger); history
        that does not chain; steps outside the protocol graph or taken by a role,
        on a tier, or without the reason / evidence / Relay task the rule
        requires; an actor holding two roles; a T3 SHIPPED without a re-run in
        its Gate 2 window; a Relay task cited twice; a stored state or tier that
        differs from what the history implies (e.g. edited with raw SQL); and
        rows that cannot be parsed at all.
        """
        problems: list[str] = []
        conn = self._conn()
        for (tid,) in conn.execute("SELECT task_id FROM a2a_tasks ORDER BY rowid").fetchall():
            try:
                problems.extend(self._check_task(conn, tid))
            except (ValueError, KeyError, TypeError) as exc:
                problems.append(f"{tid}: ledger rows cannot be parsed ({exc}).")
        for r in conn.execute(
            "SELECT relay_task_id, COUNT(*) AS n FROM a2a_transitions "
            "WHERE relay_task_id IS NOT NULL GROUP BY relay_task_id HAVING n > 1"
        ).fetchall():
            problems.append(f"Relay task {r['relay_task_id']} is cited {r['n']} times.")
        return problems

    def _check_task(self, conn: sqlite3.Connection, tid: str) -> list[str]:
        problems: list[str] = []
        task = self.get_task(tid)
        history = self.get_history(tid)
        if not history:
            return [f"{tid}: no transition history recorded (inserted outside the ledger)."]
        changes = self.get_tier_changes(tid)
        reruns = self.get_human_verifications(tid)
        first_tier = conn.execute(
            "SELECT tier FROM a2a_transitions WHERE task_id=? ORDER BY seq LIMIT 1", (tid,)
        ).fetchone()["tier"]
        if first_tier is None:
            problems.append(f"{tid}: creation row does not record the tier.")
            first_tier = (changes[0].from_tier if changes else task.tier).value
        tier = A2ATaskTier(first_tier)
        roles_by_actor: dict[str, set[str]] = {}
        expected_from: A2AProtocolState | None = None
        gate2_seq = 0
        applied: set[int] = set()
        for rec in history:
            for i, ch in enumerate(changes):
                if i not in applied and ch.after_seq < rec.seq:
                    if ch.from_tier != tier:
                        problems.append(f"{tid}: tier change {ch.from_tier.value} -> "
                                        f"{ch.to_tier.value} does not start from {tier.value}.")
                    tier = ch.to_tier
                    applied.add(i)
            if rec.from_state != expected_from:
                problems.append(
                    f"{tid}: history step #{rec.seq} starts from "
                    f"{rec.from_state.value if rec.from_state else 'nothing'}, "
                    f"expected {expected_from.value if expected_from else 'nothing'}."
                )
            expected_from = rec.to_state
            if rec.to_state == A2AProtocolState.AWAITING_GATE_2:
                gate2_seq = rec.seq
            if rec.actor not in _UNBOUND_ACTORS:
                roles_by_actor.setdefault(actor_key(rec.actor), set()).add(rec.role.value)
            if rec.from_state is None:
                continue  # creation or legacy-import row
            rule = RULES.get((rec.from_state, rec.to_state))
            label = f"{tid}: step #{rec.seq} {rec.from_state.value} → {rec.to_state.value}"
            if rule is None:
                problems.append(f"{label} is not a protocol step.")
                continue
            if tier not in rule.tiers:
                problems.append(f"{label} is not on the {tier.value} path.")
            if rec.role not in rule.roles:
                problems.append(f"{label} was taken as {rec.role.value}, not allowed.")
            if rule.reason_required and not (rec.reason or "").strip():
                problems.append(f"{label} has no reason.")
            if rule.evidence_required and not (rec.evidence_ref or "").strip():
                problems.append(f"{label} has no evidence reference.")
            if rule.relay_required and not (rec.relay_task_id or "").strip():
                problems.append(f"{label} cites no Relay task.")
            if (rec.to_state == A2AProtocolState.SHIPPED and tier == A2ATaskTier.T3
                    and not any(v.gate2_entry_seq == gate2_seq for v in reruns)):
                problems.append(f"{label}: T3 shipped without a human re-run in its "
                                "Gate 2 window.")
        for i, ch in enumerate(changes):
            if i not in applied:
                tier = ch.to_tier  # escalated after the last step
        for actor in [v.actor for v in reruns] + [c.actor for c in changes]:
            roles_by_actor.setdefault(actor_key(actor), set()).add(A2ARole.HUMAN.value)
        for key, roles in roles_by_actor.items():
            if len(roles) > 1:
                problems.append(f"{tid}: actor {key!r} acted in several roles "
                                f"({', '.join(sorted(roles))}).")
        if tier != task.tier:
            problems.append(f"{tid}: stored tier {task.tier.value} differs from the tier "
                            f"implied by the ledger ({tier.value}).")
        if history[-1].to_state != task.state:
            problems.append(
                f"{tid}: stored state {task.state.value} does not match the "
                f"last recorded transition ({history[-1].to_state.value}); the state "
                "was changed outside the ledger."
            )
        return problems

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
