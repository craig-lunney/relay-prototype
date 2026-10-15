"""Unit tests for A2A Protocol v0.2 task lifecycle service."""
from __future__ import annotations

import pytest

from tests.a2a_support import advance, fake_relay_lookup

from a2a.models import A2AProtocolState, A2ATask, A2ATaskTier
from a2a.service import A2AService, A2AInvalidTaskError, A2ATaskNotFoundError, A2ATransitionError
from a2a.state import A2AStateManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sm(tmp_path) -> A2AStateManager:
    return A2AStateManager(tmp_path / "a2a_svc.db", relay_status_lookup=fake_relay_lookup)


@pytest.fixture
def svc(sm) -> A2AService:
    return A2AService(sm)


def _create(svc: A2AService, **kwargs) -> A2ATask:
    return svc.create_task(
        title=kwargs.get("title", "Test task"),
        tier=kwargs.get("tier", A2ATaskTier.T2),
        github_issue_url=kwargs.get("url", "https://github.com/org/repo/issues/1"),
    )


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------

def test_create_returns_a2a_task(svc):
    task = _create(svc)
    assert isinstance(task, A2ATask)


def test_create_initial_state_is_draft(svc):
    assert _create(svc).state == A2AProtocolState.DRAFT


def test_create_sets_correct_fields(svc):
    task = svc.create_task(
        title="My task",
        tier=A2ATaskTier.T3,
        github_issue_url="https://github.com/x/y/issues/7",
    )
    assert task.title == "My task"
    assert task.tier == A2ATaskTier.T3
    assert task.github_issue_url == "https://github.com/x/y/issues/7"
    assert task.protocol_version == "0.2"


def test_create_generates_unique_ids(svc):
    ids = {_create(svc).task_id for _ in range(20)}
    assert len(ids) == 20


def test_create_id_format(svc):
    tid = _create(svc).task_id
    assert tid.startswith("A2A-")
    assert len(tid) == 12


def test_create_accepts_all_tiers(svc):
    for tier in A2ATaskTier:
        t = svc.create_task(title="t", tier=tier,
                             github_issue_url="https://github.com/o/r/issues/1")
        assert t.tier == tier


# ---------------------------------------------------------------------------
# get_task
# ---------------------------------------------------------------------------

def test_get_task_returns_correct_task(svc):
    t = _create(svc)
    r = svc.get_task(t.task_id)
    assert r.task_id == t.task_id
    assert r.title == t.title
    assert r.state == A2AProtocolState.DRAFT


def test_get_task_missing_raises_not_found(svc):
    with pytest.raises(A2ATaskNotFoundError) as exc_info:
        svc.get_task("A2A-NOTHERE")
    assert exc_info.value.task_id == "A2A-NOTHERE"


def test_get_task_not_found_error_message(svc):
    with pytest.raises(A2ATaskNotFoundError, match="A2A-NOTHERE"):
        svc.get_task("A2A-NOTHERE")


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------

def test_list_tasks_empty(svc):
    assert svc.list_tasks() == []


def test_list_tasks_returns_all_created(svc):
    for i in range(3):
        _create(svc, title=f"T{i}")
    assert len(svc.list_tasks()) == 3


def test_list_tasks_includes_correct_titles(svc):
    for i in range(3):
        _create(svc, title=f"Task {i}")
    titles = {t.title for t in svc.list_tasks()}
    assert titles == {"Task 0", "Task 1", "Task 2"}


def test_list_tasks_ordered_by_creation(svc):
    import time
    t1 = _create(svc, title="First")
    time.sleep(0.015)
    t2 = _create(svc, title="Second")
    listed = svc.list_tasks()
    assert listed[0].task_id == t1.task_id
    assert listed[1].task_id == t2.task_id


# ---------------------------------------------------------------------------
# advance — valid transitions
# ---------------------------------------------------------------------------

def test_advance_t1_draft_to_in_implementation(svc):
    t = _create(svc, tier=A2ATaskTier.T1)
    updated = advance(svc, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    assert updated.state == A2AProtocolState.IN_IMPLEMENTATION


def test_advance_t2_draft_to_speccing(svc):
    t = _create(svc, tier=A2ATaskTier.T2)
    updated = advance(svc, t.task_id, A2AProtocolState.SPECCING)
    assert updated.state == A2AProtocolState.SPECCING


def test_advance_returns_updated_task(svc):
    t = _create(svc)
    updated = advance(svc, t.task_id, A2AProtocolState.SPECCING)
    assert updated.task_id == t.task_id
    assert updated.state == A2AProtocolState.SPECCING


def test_advance_get_reflects_new_state(svc):
    t = _create(svc)
    advance(svc, t.task_id, A2AProtocolState.SPECCING)
    assert svc.get_task(t.task_id).state == A2AProtocolState.SPECCING


def test_advance_full_t2_happy_path(svc):
    t = _create(svc, tier=A2ATaskTier.T2)
    for s in [
        A2AProtocolState.SPECCING,
        A2AProtocolState.AWAITING_GATE_1,
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.UNDER_REVIEW,
        A2AProtocolState.AWAITING_GATE_2,
        A2AProtocolState.SHIPPED,
        A2AProtocolState.CLOSED,
    ]:
        advance(svc, t.task_id, s)
    assert svc.get_task(t.task_id).state == A2AProtocolState.CLOSED


def test_advance_full_t1_happy_path(svc):
    t = _create(svc, tier=A2ATaskTier.T1)
    for s in [
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.AWAITING_GATE_2,
        A2AProtocolState.SHIPPED,
        A2AProtocolState.CLOSED,
    ]:
        advance(svc, t.task_id, s)
    assert svc.get_task(t.task_id).state == A2AProtocolState.CLOSED


def test_advance_blocked_by_discovery_roundtrip(svc):
    t = _create(svc)
    for s in [
        A2AProtocolState.SPECCING,
        A2AProtocolState.AWAITING_GATE_1,
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.BLOCKED_BY_DISCOVERY,
        A2AProtocolState.IN_IMPLEMENTATION,
    ]:
        advance(svc, t.task_id, s)
    assert svc.get_task(t.task_id).state == A2AProtocolState.IN_IMPLEMENTATION


def test_advance_gate_1_rejection_returns_to_draft(svc):
    t = _create(svc)
    advance(svc, t.task_id, A2AProtocolState.SPECCING)
    advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_1)
    advance(svc, t.task_id, A2AProtocolState.DRAFT)
    assert svc.get_task(t.task_id).state == A2AProtocolState.DRAFT


# ---------------------------------------------------------------------------
# advance — invalid transitions
# ---------------------------------------------------------------------------

def test_advance_invalid_transition_raises_transition_error(svc):
    t = _create(svc)
    with pytest.raises(A2ATransitionError):
        advance(svc, t.task_id, A2AProtocolState.CLOSED)


def test_advance_transition_error_has_correct_states(svc):
    t = _create(svc)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.SHIPPED)
    err = exc_info.value
    assert err.from_state == A2AProtocolState.DRAFT
    assert err.to_state == A2AProtocolState.SHIPPED
    assert err.task_id == t.task_id


def test_advance_transition_error_has_reason(svc):
    t = _create(svc)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.CLOSED)
    assert exc_info.value.reason  # non-empty reason string


def test_advance_transition_error_message_names_states(svc):
    t = _create(svc)
    with pytest.raises(A2ATransitionError, match="DRAFT.*CLOSED"):
        advance(svc, t.task_id, A2AProtocolState.CLOSED)


def test_advance_missing_task_raises_not_found(svc):
    with pytest.raises(A2ATaskNotFoundError) as exc_info:
        advance(svc, "A2A-MISSING", A2AProtocolState.SPECCING)
    assert exc_info.value.task_id == "A2A-MISSING"


def test_advance_invalid_does_not_corrupt_state(svc):
    t = _create(svc)
    with pytest.raises(A2ATransitionError):
        advance(svc, t.task_id, A2AProtocolState.SHIPPED)
    assert svc.get_task(t.task_id).state == A2AProtocolState.DRAFT


def test_advance_closed_rejects_all_further_transitions(svc):
    t = _create(svc, tier=A2ATaskTier.T1)
    for s in [
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.AWAITING_GATE_2,
        A2AProtocolState.SHIPPED,
        A2AProtocolState.CLOSED,
    ]:
        advance(svc, t.task_id, s)
    for target in A2AProtocolState:
        with pytest.raises(A2ATransitionError):
            advance(svc, t.task_id, target)


# ---------------------------------------------------------------------------
# GitHub Issue URL validation (A2A-19)
# ---------------------------------------------------------------------------

_VALID_URL = "https://github.com/org/repo/issues/1"


def test_create_task_valid_url_accepted(svc):
    task = svc.create_task(title="t", tier=A2ATaskTier.T2,
                           github_issue_url=_VALID_URL)
    assert task.github_issue_url == _VALID_URL


@pytest.mark.parametrize("url, label", [
    ("", "empty string"),
    ("not a url", "garbage"),
    ("https://gitlab.com/org/repo/issues/1", "non-github host"),
    ("http://github.com/org/repo/issues/1", "http not https"),
    ("https://github.com/org/repo/pulls/1", "PR URL"),
    ("https://github.com/org/repo/issues", "missing issue number"),
    ("https://github.com/org/repo/issues/", "trailing slash only"),
    ("https://github.com/org/repo/issues/0", "zero issue number"),
    ("https://github.com/org/repo/issues/-1", "negative issue number"),
    ("https://github.com/org/repo/issues/abc", "non-numeric issue number"),
    ("https://github.com/org/repo/issues/1/comments", "extra path segment"),
    ("https://github.com/org/repo/issues/01", "leading zero"),
])
def test_create_task_invalid_url_rejected(svc, url, label):
    with pytest.raises(A2AInvalidTaskError):
        svc.create_task(title="t", tier=A2ATaskTier.T2, github_issue_url=url)


def test_invalid_task_error_has_reason(svc):
    with pytest.raises(A2AInvalidTaskError) as exc_info:
        svc.create_task(title="t", tier=A2ATaskTier.T2, github_issue_url="bad")
    assert exc_info.value.reason
    assert "bad" in exc_info.value.reason


def test_invalid_task_error_reason_names_expected_format(svc):
    with pytest.raises(A2AInvalidTaskError) as exc_info:
        svc.create_task(title="t", tier=A2ATaskTier.T2, github_issue_url="bad")
    assert "github.com" in exc_info.value.reason


def test_create_task_valid_url_large_issue_number(svc):
    task = svc.create_task(title="t", tier=A2ATaskTier.T2,
                           github_issue_url="https://github.com/org/repo/issues/99999")
    assert task.task_id.startswith("A2A-")


def test_create_task_url_not_persisted_on_invalid(svc):
    with pytest.raises(A2AInvalidTaskError):
        svc.create_task(title="t", tier=A2ATaskTier.T2,
                        github_issue_url="https://github.com/org/repo/pulls/1")
    assert svc.list_tasks() == []


# ---------------------------------------------------------------------------
# Task title validation (A2A-20)
# ---------------------------------------------------------------------------

_VALID_TITLE = "Fix OG description on brand pages"
_VALID_URL_FOR_TITLE = "https://github.com/org/repo/issues/1"


@pytest.mark.parametrize("bad_title", [
    "",
    "   ",
    "\t",
    "\n",
    " \t\n ",
])
def test_create_task_blank_title_rejected(svc, bad_title):
    with pytest.raises(A2AInvalidTaskError):
        svc.create_task(title=bad_title, tier=A2ATaskTier.T2,
                        github_issue_url=_VALID_URL_FOR_TITLE)


def test_create_task_blank_title_error_has_reason(svc):
    with pytest.raises(A2AInvalidTaskError) as exc_info:
        svc.create_task(title="", tier=A2ATaskTier.T2,
                        github_issue_url=_VALID_URL_FOR_TITLE)
    assert exc_info.value.reason


def test_create_task_blank_title_not_persisted(svc):
    with pytest.raises(A2AInvalidTaskError):
        svc.create_task(title="  ", tier=A2ATaskTier.T2,
                        github_issue_url=_VALID_URL_FOR_TITLE)
    assert svc.list_tasks() == []


def test_create_task_valid_title_accepted(svc):
    task = svc.create_task(title=_VALID_TITLE, tier=A2ATaskTier.T2,
                           github_issue_url=_VALID_URL_FOR_TITLE)
    assert task.title == _VALID_TITLE


# ---------------------------------------------------------------------------
# Tier-aware path enforcement (A2A-18)
# ---------------------------------------------------------------------------

# --- T1 forbidden states ----------------------------------------------------

def test_t1_cannot_advance_to_speccing(svc):
    # Graph permits DRAFT → SPECCING; tier enforcement must block it for T1.
    t = _create(svc, tier=A2ATaskTier.T1)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.SPECCING)
    err = exc_info.value
    assert err.task_id == t.task_id
    assert err.from_state == A2AProtocolState.DRAFT
    assert err.to_state == A2AProtocolState.SPECCING


def test_t1_cannot_advance_to_awaiting_gate_1(svc):
    t = _create(svc, tier=A2ATaskTier.T1)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_1)
    assert exc_info.value.to_state == A2AProtocolState.AWAITING_GATE_1


def test_t1_cannot_advance_to_under_review(svc):
    # Graph permits IN_IMPLEMENTATION → UNDER_REVIEW; tier enforcement must block it for T1.
    t = _create(svc, tier=A2ATaskTier.T1)
    advance(svc, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.UNDER_REVIEW)
    err = exc_info.value
    assert err.from_state == A2AProtocolState.IN_IMPLEMENTATION
    assert err.to_state == A2AProtocolState.UNDER_REVIEW


def test_t1_tier_violation_reason_names_state(svc):
    t = _create(svc, tier=A2ATaskTier.T1)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.SPECCING)
    assert "SPECCING" in exc_info.value.reason
    assert exc_info.value.reason  # non-empty


def test_t1_tier_violation_mentions_t1_path(svc):
    t = _create(svc, tier=A2ATaskTier.T1)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.SPECCING)
    assert "T1" in exc_info.value.reason


def test_t1_tier_violation_does_not_corrupt_state(svc):
    t = _create(svc, tier=A2ATaskTier.T1)
    with pytest.raises(A2ATransitionError):
        advance(svc, t.task_id, A2AProtocolState.SPECCING)
    assert svc.get_task(t.task_id).state == A2AProtocolState.DRAFT


# --- T2/T3 DRAFT → IN_IMPLEMENTATION shortcut blocked ----------------------

def test_t2_cannot_advance_draft_directly_to_in_implementation(svc):
    # Graph permits DRAFT → IN_IMPLEMENTATION; tier enforcement must block it for T2.
    t = _create(svc, tier=A2ATaskTier.T2)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    err = exc_info.value
    assert err.task_id == t.task_id
    assert err.from_state == A2AProtocolState.DRAFT
    assert err.to_state == A2AProtocolState.IN_IMPLEMENTATION


def test_t3_cannot_advance_draft_directly_to_in_implementation(svc):
    t = _create(svc, tier=A2ATaskTier.T3)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    err = exc_info.value
    assert err.from_state == A2AProtocolState.DRAFT
    assert err.to_state == A2AProtocolState.IN_IMPLEMENTATION


def test_t2_tier_violation_reason_names_speccing(svc):
    t = _create(svc, tier=A2ATaskTier.T2)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    assert "SPECCING" in exc_info.value.reason


def test_t2_tier_violation_reason_names_t2_t3(svc):
    t = _create(svc, tier=A2ATaskTier.T2)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    reason = exc_info.value.reason
    assert "T2" in reason or "T3" in reason


def test_t2_draft_shortcut_does_not_corrupt_state(svc):
    t = _create(svc, tier=A2ATaskTier.T2)
    with pytest.raises(A2ATransitionError):
        advance(svc, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    assert svc.get_task(t.task_id).state == A2AProtocolState.DRAFT


# --- T2/T3 valid IN_IMPLEMENTATION entries still work ----------------------

def test_t2_can_reach_in_implementation_via_gate_1(svc):
    # AWAITING_GATE_1 → IN_IMPLEMENTATION is not the DRAFT shortcut and must still pass.
    t = _create(svc, tier=A2ATaskTier.T2)
    advance(svc, t.task_id, A2AProtocolState.SPECCING)
    advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_1)
    advance(svc, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    assert svc.get_task(t.task_id).state == A2AProtocolState.IN_IMPLEMENTATION


def test_t2_can_reenter_in_implementation_from_revision_needed(svc):
    t = _create(svc, tier=A2ATaskTier.T2)
    for s in [
        A2AProtocolState.SPECCING,
        A2AProtocolState.AWAITING_GATE_1,
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.UNDER_REVIEW,
        A2AProtocolState.REVISION_NEEDED,
        A2AProtocolState.IN_IMPLEMENTATION,
    ]:
        advance(svc, t.task_id, s)
    assert svc.get_task(t.task_id).state == A2AProtocolState.IN_IMPLEMENTATION


def test_t2_can_reenter_in_implementation_from_blocked_by_discovery(svc):
    t = _create(svc, tier=A2ATaskTier.T2)
    for s in [
        A2AProtocolState.SPECCING,
        A2AProtocolState.AWAITING_GATE_1,
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.BLOCKED_BY_DISCOVERY,
        A2AProtocolState.IN_IMPLEMENTATION,
    ]:
        advance(svc, t.task_id, s)
    assert svc.get_task(t.task_id).state == A2AProtocolState.IN_IMPLEMENTATION


# --- T1 valid path still works ---------------------------------------------

def test_t1_valid_path_unaffected(svc):
    t = _create(svc, tier=A2ATaskTier.T1)
    for s in [
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.AWAITING_GATE_2,
        A2AProtocolState.SHIPPED,
        A2AProtocolState.CLOSED,
    ]:
        advance(svc, t.task_id, s)
    assert svc.get_task(t.task_id).state == A2AProtocolState.CLOSED


def test_t1_blocked_by_discovery_roundtrip_unaffected(svc):
    t = _create(svc, tier=A2ATaskTier.T1)
    for s in [
        A2AProtocolState.IN_IMPLEMENTATION,
        A2AProtocolState.BLOCKED_BY_DISCOVERY,
        A2AProtocolState.IN_IMPLEMENTATION,
    ]:
        advance(svc, t.task_id, s)
    assert svc.get_task(t.task_id).state == A2AProtocolState.IN_IMPLEMENTATION


# ---------------------------------------------------------------------------
# Tier-aware path enforcement — T2/T3 must not skip UNDER_REVIEW (F-1 fix)
# ---------------------------------------------------------------------------

def _advance_t2_t3_to_in_implementation(svc: A2AService, tier: A2ATaskTier) -> A2ATask:
    """Create a T2/T3 task and advance it to IN_IMPLEMENTATION via the valid path."""
    t = _create(svc, tier=tier)
    advance(svc, t.task_id, A2AProtocolState.SPECCING)
    advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_1)
    advance(svc, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    return svc.get_task(t.task_id)


def test_t2_cannot_skip_under_review(svc):
    # Graph permits IN_IMPLEMENTATION → AWAITING_GATE_2; tier enforcement must block it for T2.
    t = _advance_t2_t3_to_in_implementation(svc, A2ATaskTier.T2)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_2)
    err = exc_info.value
    assert err.task_id == t.task_id
    assert err.from_state == A2AProtocolState.IN_IMPLEMENTATION
    assert err.to_state == A2AProtocolState.AWAITING_GATE_2


def test_t3_cannot_skip_under_review(svc):
    t = _advance_t2_t3_to_in_implementation(svc, A2ATaskTier.T3)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_2)
    err = exc_info.value
    assert err.from_state == A2AProtocolState.IN_IMPLEMENTATION
    assert err.to_state == A2AProtocolState.AWAITING_GATE_2


def test_t2_skip_under_review_reason_names_constraint(svc):
    t = _advance_t2_t3_to_in_implementation(svc, A2ATaskTier.T2)
    with pytest.raises(A2ATransitionError) as exc_info:
        advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_2)
    reason = exc_info.value.reason
    assert "UNDER_REVIEW" in reason
    assert reason  # non-empty


def test_t2_skip_under_review_does_not_corrupt_state(svc):
    t = _advance_t2_t3_to_in_implementation(svc, A2ATaskTier.T2)
    with pytest.raises(A2ATransitionError):
        advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_2)
    assert svc.get_task(t.task_id).state == A2AProtocolState.IN_IMPLEMENTATION


def test_t2_valid_path_through_under_review(svc):
    # IN_IMPLEMENTATION → UNDER_REVIEW → AWAITING_GATE_2 must remain valid for T2.
    t = _advance_t2_t3_to_in_implementation(svc, A2ATaskTier.T2)
    advance(svc, t.task_id, A2AProtocolState.UNDER_REVIEW)
    advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_2)
    assert svc.get_task(t.task_id).state == A2AProtocolState.AWAITING_GATE_2


def test_t3_valid_path_through_under_review(svc):
    # IN_IMPLEMENTATION → UNDER_REVIEW → AWAITING_GATE_2 must remain valid for T3.
    t = _advance_t2_t3_to_in_implementation(svc, A2ATaskTier.T3)
    advance(svc, t.task_id, A2AProtocolState.UNDER_REVIEW)
    advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_2)
    assert svc.get_task(t.task_id).state == A2AProtocolState.AWAITING_GATE_2


def test_t1_can_advance_in_implementation_to_awaiting_gate_2(svc):
    # T1 shortcut IN_IMPLEMENTATION → AWAITING_GATE_2 must remain valid.
    t = _create(svc, tier=A2ATaskTier.T1)
    advance(svc, t.task_id, A2AProtocolState.IN_IMPLEMENTATION)
    advance(svc, t.task_id, A2AProtocolState.AWAITING_GATE_2)
    assert svc.get_task(t.task_id).state == A2AProtocolState.AWAITING_GATE_2


# ---------------------------------------------------------------------------
# Persistence goes through A2AStateManager (not reimplemented in service)
# ---------------------------------------------------------------------------

def test_create_is_visible_via_state_manager(tmp_path):
    """Data created by A2AService is readable directly from A2AStateManager."""
    db_path = tmp_path / "svc_sm.db"
    sm = A2AStateManager(db_path)
    svc = A2AService(sm)
    t = _create(svc)

    # Read via the same state manager (not the service)
    from_sm = sm.get_task(t.task_id)
    assert from_sm is not None
    assert from_sm.task_id == t.task_id
    assert from_sm.state == A2AProtocolState.DRAFT


def test_advance_is_visible_via_state_manager(tmp_path):
    """State transitions applied by A2AService are visible in A2AStateManager."""
    db_path = tmp_path / "svc_sm2.db"
    sm = A2AStateManager(db_path)
    svc = A2AService(sm)
    t = _create(svc)
    advance(svc, t.task_id, A2AProtocolState.SPECCING)

    from_sm = sm.get_task(t.task_id)
    assert from_sm.state == A2AProtocolState.SPECCING


def test_data_persists_across_service_instances(tmp_path):
    """A task created by one A2AService instance is readable by another."""
    db_path = tmp_path / "svc_cross.db"
    svc1 = A2AService(A2AStateManager(db_path))
    t = _create(svc1, title="Cross-instance task")
    advance(svc1, t.task_id, A2AProtocolState.SPECCING)

    svc2 = A2AService(A2AStateManager(db_path))
    r = svc2.get_task(t.task_id)
    assert r.state == A2AProtocolState.SPECCING
    assert r.title == "Cross-instance task"
