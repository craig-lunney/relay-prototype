"""Unit tests for the A2A Protocol v0.2 persistence layer."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from tests.a2a_support import fake_relay_lookup, transition

from a2a.models import A2AProtocolState, A2ATask, A2ATaskTier
from a2a.state import A2AInvalidTransitionError, A2AStateManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task(
    tier: A2ATaskTier = A2ATaskTier.T2,
    state: A2AProtocolState = A2AProtocolState.DRAFT,
    title: str = "Test task",
) -> A2ATask:
    return A2ATask(
        task_id=A2ATask.new_id(),
        title=title,
        tier=tier,
        github_issue_url="https://github.com/org/repo/issues/1",
        state=state,
    )


@pytest.fixture
def db(tmp_path) -> A2AStateManager:
    return A2AStateManager(tmp_path / "a2a_test.db", relay_status_lookup=fake_relay_lookup)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def test_create_and_get_roundtrip(db):
    t = _task()
    db.create_task(t)
    r = db.get_task(t.task_id)
    assert r is not None
    assert r.task_id == t.task_id
    assert r.title == t.title
    assert r.tier == t.tier
    assert r.github_issue_url == t.github_issue_url
    assert r.state == A2AProtocolState.DRAFT
    assert r.protocol_version == "0.2"


def test_all_fields_survive_roundtrip(db):
    t = A2ATask(
        task_id=A2ATask.new_id(),
        title="Exact title",
        tier=A2ATaskTier.T3,
        github_issue_url="https://github.com/x/y/issues/99",
        state=A2AProtocolState.DRAFT,
        protocol_version="0.2",
    )
    db.create_task(t)
    r = db.get_task(t.task_id)
    assert r.title == "Exact title"
    assert r.tier == A2ATaskTier.T3
    assert r.github_issue_url == "https://github.com/x/y/issues/99"
    assert r.protocol_version == "0.2"


def test_created_at_preserved(db):
    t = _task()
    db.create_task(t)
    r = db.get_task(t.task_id)
    assert abs((r.created_at - t.created_at).total_seconds()) < 1


def test_create_duplicate_raises_integrity_error(db):
    t = _task()
    db.create_task(t)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_task(t)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def test_get_nonexistent_returns_none(db):
    assert db.get_task("A2A-NOTFOUND") is None


def test_list_tasks_empty(db):
    assert db.list_tasks() == []


def test_list_tasks_returns_all(db):
    tasks = [_task(title=f"T{i}") for i in range(3)]
    for t in tasks:
        db.create_task(t)
    listed = db.list_tasks()
    assert len(listed) == 3
    assert {t.title for t in listed} == {"T0", "T1", "T2"}


def test_list_tasks_ordered_by_created_at(db):
    t1 = _task(title="First")
    db.create_task(t1)
    time.sleep(0.015)
    t2 = _task(title="Second")
    db.create_task(t2)
    listed = db.list_tasks()
    assert listed[0].title == "First"
    assert listed[1].title == "Second"


# ---------------------------------------------------------------------------
# Valid transitions — T1 path
# ---------------------------------------------------------------------------

def test_t1_draft_to_in_implementation(db):
    t = _task(tier=A2ATaskTier.T1)
    db.create_task(t)
    updated = transition(db, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    assert updated.state == A2AProtocolState.IN_IMPLEMENTATION


def test_t1_full_happy_path(db):
    t = _task(tier=A2ATaskTier.T1)
    db.create_task(t)
    for s in [
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.AWAITING_GATE_2,
        A2AProtocolState.SHIPPED,
        A2AProtocolState.CLOSED,
    ]:
        transition(db, t.task_id, s)
    assert db.get_task(t.task_id).state == A2AProtocolState.CLOSED


def test_t1_post_deploy_failure_and_retry(db):
    t = _task(tier=A2ATaskTier.T1)
    db.create_task(t)
    for s in [
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.AWAITING_GATE_2,
        A2AProtocolState.REVISION_NEEDED,
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.AWAITING_GATE_2,
        A2AProtocolState.SHIPPED,
        A2AProtocolState.CLOSED,
    ]:
        transition(db, t.task_id, s)
    assert db.get_task(t.task_id).state == A2AProtocolState.CLOSED


# ---------------------------------------------------------------------------
# Valid transitions — T2/T3 path
# ---------------------------------------------------------------------------

def test_t2_draft_to_speccing(db):
    t = _task(tier=A2ATaskTier.T2)
    db.create_task(t)
    updated = transition(db, t.task_id, A2AProtocolState.SPECCING)
    assert updated.state == A2AProtocolState.SPECCING


def test_t2_full_happy_path(db):
    t = _task(tier=A2ATaskTier.T2)
    db.create_task(t)
    for s in [
        A2AProtocolState.SPECCING,
        A2AProtocolState.AWAITING_GATE_1,
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.UNDER_REVIEW,
        A2AProtocolState.AWAITING_GATE_2,
        A2AProtocolState.SHIPPED,
        A2AProtocolState.CLOSED,
    ]:
        transition(db, t.task_id, s)
    assert db.get_task(t.task_id).state == A2AProtocolState.CLOSED


def test_gate_1_rejection_returns_to_draft(db):
    t = _task()
    db.create_task(t)
    transition(db, t.task_id, A2AProtocolState.SPECCING)
    transition(db, t.task_id, A2AProtocolState.AWAITING_GATE_1)
    transition(db, t.task_id, A2AProtocolState.DRAFT)
    assert db.get_task(t.task_id).state == A2AProtocolState.DRAFT


def test_revision_cycle_t2(db):
    t = _task()
    db.create_task(t)
    for s in [
        A2AProtocolState.SPECCING,
        A2AProtocolState.AWAITING_GATE_1,
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.UNDER_REVIEW,
        A2AProtocolState.REVISION_NEEDED,
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.UNDER_REVIEW,
        A2AProtocolState.AWAITING_GATE_2,
        A2AProtocolState.SHIPPED,
        A2AProtocolState.CLOSED,
    ]:
        transition(db, t.task_id, s)
    assert db.get_task(t.task_id).state == A2AProtocolState.CLOSED


# ---------------------------------------------------------------------------
# Valid transitions — BLOCKED_BY_DISCOVERY (all tiers)
# ---------------------------------------------------------------------------

def test_blocked_by_discovery_resolves_to_in_implementation(db):
    t = _task(tier=A2ATaskTier.T2)
    db.create_task(t)
    for s in [
        A2AProtocolState.SPECCING,
        A2AProtocolState.AWAITING_GATE_1,
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.BLOCKED_BY_DISCOVERY,
        A2AProtocolState.IN_IMPLEMENTATION,
    ]:
        transition(db, t.task_id, s)
    assert db.get_task(t.task_id).state == A2AProtocolState.IN_IMPLEMENTATION


def test_blocked_by_discovery_can_close(db):
    t = _task(tier=A2ATaskTier.T1)
    db.create_task(t)
    for s in [
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.BLOCKED_BY_DISCOVERY,
        A2AProtocolState.CLOSED,
    ]:
        transition(db, t.task_id, s)
    assert db.get_task(t.task_id).state == A2AProtocolState.CLOSED


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------

def test_invalid_transition_raises(db):
    t = _task()
    db.create_task(t)
    with pytest.raises(A2AInvalidTransitionError):
        transition(db, t.task_id, A2AProtocolState.CLOSED)


def test_invalid_transition_error_message_names_states(db):
    t = _task()
    db.create_task(t)
    with pytest.raises(A2AInvalidTransitionError, match="DRAFT.*CLOSED"):
        transition(db, t.task_id, A2AProtocolState.CLOSED)


def test_invalid_transition_does_not_corrupt_state(db):
    t = _task()
    db.create_task(t)
    with pytest.raises(A2AInvalidTransitionError):
        transition(db, t.task_id, A2AProtocolState.SHIPPED)
    assert db.get_task(t.task_id).state == A2AProtocolState.DRAFT


@pytest.mark.parametrize("bad_target", [
    A2AProtocolState.CLOSED,
    A2AProtocolState.SHIPPED,
    A2AProtocolState.AWAITING_GATE_2,
    A2AProtocolState.UNDER_REVIEW,
    A2AProtocolState.REVISION_NEEDED,
    A2AProtocolState.BLOCKED_BY_DISCOVERY,
    A2AProtocolState.AWAITING_GATE_1,
    A2AProtocolState.DRAFT,           # self-transition
])
def test_draft_rejects_invalid_targets(db, bad_target):
    t = _task()
    db.create_task(t)
    with pytest.raises(A2AInvalidTransitionError):
        transition(db, t.task_id, bad_target)


def test_closed_rejects_all_targets(db):
    t = _task(tier=A2ATaskTier.T1)
    db.create_task(t)
    for s in [
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.AWAITING_GATE_2,
        A2AProtocolState.SHIPPED,
        A2AProtocolState.CLOSED,
    ]:
        transition(db, t.task_id, s)
    for target in A2AProtocolState:
        with pytest.raises(A2AInvalidTransitionError):
            transition(db, t.task_id, target)


def test_transition_nonexistent_task_raises(db):
    with pytest.raises(A2AInvalidTransitionError, match="not found"):
        transition(db, "A2A-NOTFOUND", A2AProtocolState.SPECCING)


# ---------------------------------------------------------------------------
# Updates — return value, timestamps, immutable fields
# ---------------------------------------------------------------------------

def test_transition_returns_task_with_new_state(db):
    t = _task()
    db.create_task(t)
    updated = transition(db, t.task_id, A2AProtocolState.SPECCING)
    assert updated.task_id == t.task_id
    assert updated.state == A2AProtocolState.SPECCING


def test_transition_updates_updated_at(db):
    t = _task()
    db.create_task(t)
    before = db.get_task(t.task_id).updated_at
    time.sleep(0.015)
    transition(db, t.task_id, A2AProtocolState.SPECCING)
    after = db.get_task(t.task_id).updated_at
    assert after > before


def test_transition_preserves_created_at(db):
    t = _task()
    db.create_task(t)
    created = db.get_task(t.task_id).created_at
    transition(db, t.task_id, A2AProtocolState.SPECCING)
    assert db.get_task(t.task_id).created_at == created


def test_transition_preserves_immutable_fields(db):
    t = _task(tier=A2ATaskTier.T3, title="Immutable title")
    db.create_task(t)
    transition(db, t.task_id, A2AProtocolState.SPECCING)
    r = db.get_task(t.task_id)
    assert r.title == "Immutable title"
    assert r.tier == A2ATaskTier.T3
    assert r.github_issue_url == t.github_issue_url
    assert r.protocol_version == "0.2"


def test_get_reflects_latest_state_after_transition(db):
    t = _task()
    db.create_task(t)
    transition(db, t.task_id, A2AProtocolState.SPECCING)
    transition(db, t.task_id, A2AProtocolState.AWAITING_GATE_1)
    assert db.get_task(t.task_id).state == A2AProtocolState.AWAITING_GATE_1


# ---------------------------------------------------------------------------
# Persistence across database connections
# ---------------------------------------------------------------------------

def test_task_persists_across_connections(tmp_path):
    db_path = tmp_path / "a2a_persist.db"
    t = _task(title="Persisted task", tier=A2ATaskTier.T3)

    sm1 = A2AStateManager(db_path)
    sm1.create_task(t)
    transition(sm1, t.task_id, A2AProtocolState.SPECCING)

    sm2 = A2AStateManager(db_path)
    r = sm2.get_task(t.task_id)
    assert r is not None
    assert r.state == A2AProtocolState.SPECCING
    assert r.title == "Persisted task"
    assert r.tier == A2ATaskTier.T3


def test_list_persists_across_connections(tmp_path):
    db_path = tmp_path / "a2a_list.db"
    sm1 = A2AStateManager(db_path)
    for i in range(4):
        sm1.create_task(_task(title=f"P{i}"))

    sm2 = A2AStateManager(db_path)
    assert len(sm2.list_tasks()) == 4


def test_state_written_by_one_connection_visible_to_another(tmp_path):
    db_path = tmp_path / "a2a_cross.db"
    t = _task()
    sm1 = A2AStateManager(db_path)
    sm2 = A2AStateManager(db_path)
    sm1.create_task(t)
    transition(sm1, t.task_id, A2AProtocolState.SPECCING)
    transition(sm1, t.task_id, A2AProtocolState.AWAITING_GATE_1)
    r = sm2.get_task(t.task_id)
    assert r.state == A2AProtocolState.AWAITING_GATE_1


# ---------------------------------------------------------------------------
# CAS guard — concurrent modification protection
# ---------------------------------------------------------------------------

def test_concurrent_transition_one_wins(tmp_path):
    """Two threads race to apply the same DRAFT→SPECCING transition.

    Exactly one must succeed. The other must raise A2AInvalidTransitionError —
    either because it reads the already-updated state (validation rejects
    SPECCING→SPECCING) or because its UPDATE WHERE state='DRAFT' matches 0 rows
    (CAS guard). Either path keeps the stored state consistent.
    """
    db_path = tmp_path / "a2a_concurrent.db"
    setup = A2AStateManager(db_path)
    t = _task()
    setup.create_task(t)

    successes: list[int] = []
    failures: list[int] = []
    lock = threading.Lock()

    def attempt() -> None:
        sm = A2AStateManager(db_path)
        try:
            transition(sm, t.task_id, A2AProtocolState.SPECCING)
            with lock:
                successes.append(1)
        except A2AInvalidTransitionError:
            with lock:
                failures.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(successes) == 1, "Exactly one transition must succeed"
    assert len(failures) == 1, "Exactly one transition must be rejected"
    assert setup.get_task(t.task_id).state == A2AProtocolState.SPECCING
