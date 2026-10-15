"""
Batch 4 — A2A attested ledger (decisions D1, D2, D4).

Regression coverage for the audit's A2A findings:
  D1  one caller could drive any task DRAFT → SHIPPED → CLOSED: no actor, no
      role authority, no trigger evidence.
  D2  T3 human re-run (protocol §9.7) existed only as a tier label.
  D3  tier rules lived only in the service; A2AStateManager.transition_state and
      create_task bypassed them.
  D4  the service checked tier rules outside the CAS transaction (TOCTOU).
  D5  §7.2 / §10.3 return-to-Gate-1 transitions were missing.
  D6  no transition history.
  D1 (link)  nothing tied protocol review to a verified Relay execution.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from a2a.models import ALLOWED_A2A_TRANSITIONS, A2AProtocolState as S, A2ATask, A2ATaskTier
from a2a.protocol import RULES, A2ARole
from a2a.service import A2AService, A2ATransitionError
from a2a.state import (
    A2AAttestation,
    A2ARelayUnavailableError,
    A2AInvalidTaskDataError,
    A2AInvalidTransitionError,
    A2AStateManager,
    relay_status_from_db,
)
from tests.a2a_support import EVIDENCE, fake_relay_lookup, make_relay_db, transition

ROOT = Path(__file__).parent.parent
URL = "https://github.com/org/repo/issues/7"
H, G, C = A2ARole.HUMAN, A2ARole.CHATGPT, A2ARole.CLAUDE


@pytest.fixture
def sm(tmp_path) -> A2AStateManager:
    return A2AStateManager(tmp_path / "a2a.db", relay_status_lookup=fake_relay_lookup)


def _new(sm, tier=A2ATaskTier.T2) -> str:
    task = A2ATask(task_id=A2ATask.new_id(), title="t", tier=tier, github_issue_url=URL)
    sm.create_task(task, created_by="craig")
    return task.task_id


def _walk(sm, task_id, *states):
    for s in states:
        transition(sm, task_id, s)


T2_TO_IMPL = (S.SPECCING, S.AWAITING_GATE_1, S.IN_IMPLEMENTATION)


# ── Protocol table matches the graph ──────────────────────────────────────────

def test_every_graph_edge_has_a_rule_and_vice_versa():
    edges = {(a, b) for a, targets in ALLOWED_A2A_TRANSITIONS.items() for b in targets}
    assert edges == set(RULES)


def test_return_to_gate_1_transitions_exist():
    assert S.AWAITING_GATE_1 in ALLOWED_A2A_TRANSITIONS[S.IN_IMPLEMENTATION]
    assert S.AWAITING_GATE_1 in ALLOWED_A2A_TRANSITIONS[S.BLOCKED_BY_DISCOVERY]


# ── Role authority (D1) ───────────────────────────────────────────────────────

@pytest.mark.parametrize("path,step,wrong_role", [
    ((), S.SPECCING, C),                                   # only the Human submits a Brief
    ((S.SPECCING,), S.AWAITING_GATE_1, C),                 # only ChatGPT posts the spec
    ((S.SPECCING, S.AWAITING_GATE_1), S.IN_IMPLEMENTATION, G),   # Gate 1 is Human-only
    ((S.SPECCING, S.AWAITING_GATE_1), S.IN_IMPLEMENTATION, C),
])
def test_wrong_role_is_rejected_and_nothing_written(sm, path, step, wrong_role):
    tid = _new(sm)
    _walk(sm, tid, *path)
    before = sm.get_task(tid).state
    with pytest.raises(A2AInvalidTransitionError, match="may only be performed by"):
        sm.transition_state(tid, step, A2AAttestation(
            actor="someone", role=wrong_role, reason="r", evidence_ref=EVIDENCE))
    assert sm.get_task(tid).state == before
    assert len(sm.get_history(tid)) == 1 + len(path)


def test_claude_cannot_ship_or_close(sm):
    tid = _new(sm, A2ATaskTier.T1)
    _walk(sm, tid, S.IN_IMPLEMENTATION, S.AWAITING_GATE_2)
    with pytest.raises(A2AInvalidTransitionError):
        sm.transition_state(tid, S.SHIPPED, A2AAttestation(
            actor="claude", role=C, evidence_ref=EVIDENCE))


def test_same_actor_cannot_take_two_roles_on_one_task(sm):
    """An implementer may not also act as reviewer or approver."""
    tid = _new(sm)
    _walk(sm, tid, *T2_TO_IMPL)
    sm.transition_state(tid, S.UNDER_REVIEW, A2AAttestation(
        actor="bot", role=C, evidence_ref=EVIDENCE, relay_task_id="TASK-RELAYOK-1"))
    with pytest.raises(A2AInvalidTransitionError, match="two\\s+protocol roles"):
        sm.transition_state(tid, S.AWAITING_GATE_2, A2AAttestation(
            actor="bot", role=G, evidence_ref=EVIDENCE))
    assert sm.get_task(tid).state == S.UNDER_REVIEW


def test_actor_is_required(sm):
    tid = _new(sm)
    with pytest.raises(A2AInvalidTransitionError, match="actor"):
        sm.transition_state(tid, S.SPECCING, A2AAttestation(actor="  ", role=H))


# ── Trigger evidence and reasons ──────────────────────────────────────────────

def test_spec_posting_requires_evidence(sm):
    tid = _new(sm)
    _walk(sm, tid, S.SPECCING)
    with pytest.raises(A2AInvalidTransitionError, match="evidence reference"):
        sm.transition_state(tid, S.AWAITING_GATE_1, A2AAttestation(actor="chatgpt", role=G))


def test_gate_1_rejection_requires_reason(sm):
    tid = _new(sm)
    _walk(sm, tid, S.SPECCING, S.AWAITING_GATE_1)
    with pytest.raises(A2AInvalidTransitionError, match="requires a reason"):
        sm.transition_state(tid, S.DRAFT, A2AAttestation(actor="craig", role=H))


def test_history_records_every_attested_step(sm):
    tid = _new(sm)
    _walk(sm, tid, *T2_TO_IMPL)
    history = sm.get_history(tid)
    assert [(r.from_state, r.to_state) for r in history] == [
        (None, S.DRAFT), (S.DRAFT, S.SPECCING), (S.SPECCING, S.AWAITING_GATE_1),
        (S.AWAITING_GATE_1, S.IN_IMPLEMENTATION),
    ]
    assert [r.role for r in history] == [H, H, G, H]
    assert history[0].actor == "craig"
    assert history[2].evidence_ref == EVIDENCE


def test_history_is_append_only(sm):
    tid = _new(sm)
    conn = sqlite3.connect(sm.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE a2a_transitions SET actor='forged' WHERE task_id=?", (tid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM a2a_transitions WHERE task_id=?", (tid,))
    conn.close()


# ── State layer enforces tier rules itself (D3) ───────────────────────────────

@pytest.mark.parametrize("tier,path,step", [
    (A2ATaskTier.T2, (), S.IN_IMPLEMENTATION),             # skip Gate 1
    (A2ATaskTier.T3, (), S.IN_IMPLEMENTATION),
    (A2ATaskTier.T2, T2_TO_IMPL, S.AWAITING_GATE_2),       # skip review
    (A2ATaskTier.T1, (), S.SPECCING),
])
def test_state_layer_rejects_tier_violations(sm, tier, path, step):
    tid = _new(sm, tier)
    _walk(sm, tid, *path)
    rule = RULES.get((sm.get_task(tid).state, step))
    role = sorted(rule.roles, key=lambda r: r.value)[0] if rule else H
    with pytest.raises(A2AInvalidTransitionError, match="protocol path"):
        sm.transition_state(tid, step, A2AAttestation(
            actor="x", role=role, evidence_ref=EVIDENCE, relay_task_id="TASK-RELAYOK-9"))


@pytest.mark.parametrize("field,value", [
    ("state", S.SHIPPED), ("title", "  "), ("github_issue_url", "not-a-url"),
])
def test_state_layer_create_validates(sm, field, value):
    task = A2ATask(task_id=A2ATask.new_id(), title="t", tier=A2ATaskTier.T2,
                   github_issue_url=URL)
    setattr(task, field, value)
    with pytest.raises(A2AInvalidTaskDataError):
        sm.create_task(task)
    assert sm.list_tasks() == []


def test_tier_cannot_be_changed_in_the_database(sm):
    tid = _new(sm, A2ATaskTier.T3)
    conn = sqlite3.connect(sm.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE a2a_tasks SET tier='T1' WHERE task_id=?", (tid,))
    conn.close()


def test_check_detects_state_changed_outside_ledger(sm):
    tid = _new(sm)
    assert sm.check_consistency() == []
    conn = sqlite3.connect(sm.db_path)
    conn.execute("UPDATE a2a_tasks SET state='SHIPPED' WHERE task_id=?", (tid,))
    conn.commit()
    conn.close()
    problems = sm.check_consistency()
    assert len(problems) == 1 and "outside the ledger" in problems[0]


# ── Relay evidence link (D1) ──────────────────────────────────────────────────

def _to_impl(sm, tier):
    tid = _new(sm, tier)
    _walk(sm, tid, *(T2_TO_IMPL if tier != A2ATaskTier.T1 else (S.IN_IMPLEMENTATION,)))
    return tid


@pytest.mark.parametrize("tier,target", [
    (A2ATaskTier.T2, S.UNDER_REVIEW), (A2ATaskTier.T3, S.UNDER_REVIEW),
    (A2ATaskTier.T1, S.AWAITING_GATE_2),
])
def test_review_requires_cited_relay_task(sm, tier, target):
    tid = _to_impl(sm, tier)
    with pytest.raises(A2AInvalidTransitionError, match="evidence from Relay"):
        sm.transition_state(tid, target, A2AAttestation(
            actor="claude", role=C, evidence_ref=EVIDENCE))


def test_relay_task_must_be_completed_in_the_real_relay_db(tmp_path):
    relay_db = tmp_path / "relay.db"
    make_relay_db(relay_db, completed=["TASK-DONE0001"],
                  other={"TASK-WAIT0001": "ACCEPTANCE_REQUIRED", "TASK-FAIL0001": "FAILED"})
    sm = A2AStateManager(tmp_path / "a2a.db", relay_status_lookup=relay_status_from_db(relay_db))
    for relay_id, message in [("TASK-NOPE0001", "not found"),
                              ("TASK-WAIT0001", "ACCEPTANCE_REQUIRED"),
                              ("TASK-FAIL0001", "FAILED")]:
        tid = _to_impl(sm, A2ATaskTier.T2)
        with pytest.raises(A2AInvalidTransitionError, match=message):
            sm.transition_state(tid, S.UNDER_REVIEW, A2AAttestation(
                actor="claude", role=C, evidence_ref=EVIDENCE, relay_task_id=relay_id))
    tid = _to_impl(sm, A2ATaskTier.T2)
    sm.transition_state(tid, S.UNDER_REVIEW, A2AAttestation(
        actor="claude", role=C, evidence_ref=EVIDENCE, relay_task_id="TASK-DONE0001"))
    assert sm.get_history(tid)[-1].relay_task_id == "TASK-DONE0001"


def test_relay_db_is_opened_read_only(tmp_path):
    relay_db = tmp_path / "relay.db"
    state = make_relay_db(relay_db, completed=["TASK-DONE0001"])
    state._conn().close()
    before = relay_db.stat().st_mtime_ns, relay_db.read_bytes()
    lookup = relay_status_from_db(relay_db)
    assert lookup("TASK-DONE0001") == "COMPLETED"
    assert lookup("TASK-NOPE0001") is None
    # Relay data is never changed; SQLite may add only its WAL side files.
    assert (relay_db.stat().st_mtime_ns, relay_db.read_bytes()) == before
    assert {p.name for p in tmp_path.iterdir()} <= {
        "relay.db", "relay.db-wal", "relay.db-shm", "fake_relay_api_key.txt"}


def test_missing_or_unreadable_relay_db_is_reported_distinctly(tmp_path):
    with pytest.raises(A2ARelayUnavailableError, match="does not exist"):
        relay_status_from_db(tmp_path / "missing.db")("TASK-X")
    assert not (tmp_path / "missing.db").exists()
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(A2ARelayUnavailableError, match="could not be read"):
        relay_status_from_db(garbage)("TASK-X")
    sm = A2AStateManager(tmp_path / "a2a.db",
                         relay_status_lookup=relay_status_from_db(tmp_path / "missing.db"))
    tid = _new(sm)
    for s in T2_TO_IMPL:
        transition(sm, tid, s)
    with pytest.raises(A2AInvalidTransitionError, match="does not exist"):
        sm.transition_state(tid, S.UNDER_REVIEW, A2AAttestation(
            actor="claude", role=C, evidence_ref=EVIDENCE, relay_task_id="TASK-X"))


def test_one_relay_task_cannot_evidence_two_a2a_tasks(sm):
    first, second = _to_impl(sm, A2ATaskTier.T2), _to_impl(sm, A2ATaskTier.T2)
    att = A2AAttestation(actor="claude", role=C, evidence_ref=EVIDENCE,
                         relay_task_id="TASK-RELAYOK-SHARED")
    sm.transition_state(first, S.UNDER_REVIEW, att)
    with pytest.raises(A2AInvalidTransitionError, match="already cited"):
        sm.transition_state(second, S.UNDER_REVIEW, att)


def test_no_relay_db_configured_fails_closed(tmp_path):
    sm = A2AStateManager(tmp_path / "a2a.db")
    tid = _new(sm)
    for s in T2_TO_IMPL:
        transition(sm, tid, s)
    with pytest.raises(A2AInvalidTransitionError, match="No Relay database"):
        sm.transition_state(tid, S.UNDER_REVIEW, A2AAttestation(
            actor="claude", role=C, evidence_ref=EVIDENCE, relay_task_id="TASK-X"))


# ── T3 human re-run (D4 / protocol §9.7) ──────────────────────────────────────

def _t3_at_gate_2(sm):
    tid = _to_impl(sm, A2ATaskTier.T3)
    _walk(sm, tid, S.UNDER_REVIEW, S.AWAITING_GATE_2)
    return tid


def _ship(sm, tid):
    return sm.transition_state(tid, S.SHIPPED, A2AAttestation(
        actor="craig", role=H, evidence_ref=EVIDENCE))


def test_t3_cannot_ship_without_human_rerun(sm):
    tid = _t3_at_gate_2(sm)
    with pytest.raises(A2AInvalidTransitionError, match="human re-run"):
        _ship(sm, tid)
    assert sm.get_task(tid).state == S.AWAITING_GATE_2


def test_t3_ships_after_recorded_rerun(sm):
    tid = _t3_at_gate_2(sm)
    sm.record_human_verification(tid, "craig", "pytest -q", "17 passed")
    assert _ship(sm, tid).state == S.SHIPPED
    [v] = sm.get_human_verifications(tid)
    assert (v.actor, v.command, v.output) == ("craig", "pytest -q", "17 passed")


def test_rerun_from_an_earlier_gate_2_window_does_not_count(sm):
    tid = _t3_at_gate_2(sm)
    sm.record_human_verification(tid, "craig", "pytest -q", "17 passed")
    _walk(sm, tid, S.REVISION_NEEDED, S.IN_IMPLEMENTATION)
    sm.transition_state(tid, S.UNDER_REVIEW, A2AAttestation(
        actor="claude", role=C, evidence_ref=EVIDENCE, relay_task_id=f"TASK-RELAYOK-{tid}"))
    transition(sm, tid, S.AWAITING_GATE_2)
    with pytest.raises(A2AInvalidTransitionError, match="human re-run"):
        _ship(sm, tid)


@pytest.mark.parametrize("setup,actor,message", [
    ("t2", "craig", "T3 tasks only"),
    ("t3_impl", "craig", "recorded at Gate 2"),
    ("t3_gate2", "claude", "must be performed by a human"),
])
def test_rerun_recording_rules(sm, setup, actor, message):
    if setup == "t2":
        tid = _to_impl(sm, A2ATaskTier.T2)
    elif setup == "t3_impl":
        tid = _to_impl(sm, A2ATaskTier.T3)
    else:
        tid = _t3_at_gate_2(sm)
    with pytest.raises(A2AInvalidTransitionError, match=message):
        sm.record_human_verification(tid, actor, "pytest -q", "ok")
    assert sm.get_human_verifications(tid) == []


def test_t1_and_t2_ship_without_rerun(sm):
    t1 = _to_impl(sm, A2ATaskTier.T1)
    transition(sm, t1, S.AWAITING_GATE_2)
    assert _ship(sm, t1).state == S.SHIPPED


# ── Service and CLI end to end ────────────────────────────────────────────────

def test_service_maps_ledger_errors(sm):
    svc = A2AService(sm)
    task = svc.create_task("t", A2ATaskTier.T2, URL, created_by="craig")
    with pytest.raises(A2ATransitionError) as err:
        svc.advance(task.task_id, S.SPECCING, A2AAttestation(actor="claude", role=C))
    assert err.value.from_state == S.DRAFT and "HUMAN" in err.value.reason


def _a2a(tmp_path, *argv):
    return subprocess.run(
        [sys.executable, "-m", "a2a", "--db", str(tmp_path / "a2a.db"),
         "--relay-db", str(tmp_path / "relay.db"), *argv],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )


def test_cli_full_t3_walkthrough_with_real_relay_evidence(tmp_path):
    make_relay_db(tmp_path / "relay.db", completed=["TASK-CLIDONE1"])
    created = _a2a(tmp_path, "create-task", "--title", "Rotate cron", "--tier", "T3",
                   "--issue-url", URL, "--actor", "craig")
    assert created.returncode == 0, created.stderr
    tid = created.stdout.split()[1]
    steps = [
        ("SPECCING", ["--actor", "craig", "--role", "HUMAN"]),
        ("AWAITING_GATE_1", ["--actor", "chatgpt", "--role", "CHATGPT", "--evidence", EVIDENCE]),
        ("IN_IMPLEMENTATION", ["--actor", "craig", "--role", "HUMAN"]),
        ("UNDER_REVIEW", ["--actor", "claude", "--role", "CLAUDE", "--evidence", EVIDENCE,
                          "--relay-task", "TASK-CLIDONE1"]),
        ("AWAITING_GATE_2", ["--actor", "chatgpt", "--role", "CHATGPT", "--evidence", EVIDENCE]),
    ]
    for state, extra in steps:
        r = _a2a(tmp_path, "advance", tid, "--to", state, *extra)
        assert r.returncode == 0, (state, r.stderr)

    ship = ["advance", tid, "--to", "SHIPPED", "--actor", "craig", "--role", "HUMAN",
            "--evidence", EVIDENCE]
    refused = _a2a(tmp_path, *ship)
    assert refused.returncode == 1
    assert "human re-run" in refused.stderr and "No state was changed" in refused.stderr

    rec = _a2a(tmp_path, "record-human-verification", tid, "--actor", "craig",
               "--command", "pytest -q", "--output", "17 passed")
    assert rec.returncode == 0, rec.stderr
    assert _a2a(tmp_path, *ship).returncode == 0

    shown = _a2a(tmp_path, "get-task", tid)
    assert "[SHIPPED]" in shown.stdout
    assert "by claude [CLAUDE]" in shown.stdout and "relay task: TASK-CLIDONE1" in shown.stdout
    assert "Human re-run by craig" in shown.stdout
    assert _a2a(tmp_path, "check").returncode == 0


def test_cli_advance_requires_actor_and_role(tmp_path):
    created = _a2a(tmp_path, "create-task", "--title", "t", "--tier", "T2",
                   "--issue-url", URL, "--actor", "craig")
    tid = created.stdout.split()[1]
    r = _a2a(tmp_path, "advance", tid, "--to", "SPECCING")
    assert r.returncode == 2 and "--actor" in r.stderr


# ── Independent-review findings on Batch 4 ────────────────────────────────────

@pytest.mark.parametrize("variant", ["Claude", " claude ", "CLAUDE", "ｃｌａｕｄｅ",
                                     "clau​de", "claude\t"])
def test_actor_name_variants_are_the_same_actor(sm, variant):
    tid = _new(sm, A2ATaskTier.T1)
    transition(sm, tid, S.IN_IMPLEMENTATION)
    sm.transition_state(tid, S.AWAITING_GATE_2, A2AAttestation(
        actor="claude", role=C, evidence_ref=EVIDENCE, relay_task_id="TASK-RELAYOK-V1"))
    with pytest.raises(A2AInvalidTransitionError, match=r"two\s+protocol roles"):
        sm.transition_state(tid, S.SHIPPED, A2AAttestation(
            actor=variant, role=H, evidence_ref=EVIDENCE))


def test_relay_task_cannot_be_recited_after_revision(sm):
    tid = _to_impl(sm, A2ATaskTier.T2)

    def cite(rid):
        return sm.transition_state(tid, S.UNDER_REVIEW, A2AAttestation(
            actor="claude", role=C, evidence_ref=EVIDENCE, relay_task_id=rid))

    cite("TASK-RELAYOK-R1")
    _walk(sm, tid, S.REVISION_NEEDED, S.IN_IMPLEMENTATION)
    with pytest.raises(A2AInvalidTransitionError, match="already cited"):
        cite("TASK-RELAYOK-R1")
    cite("TASK-RELAYOK-R2")   # a fresh verified run for the revision is accepted
    assert sm.get_task(tid).state == S.UNDER_REVIEW


def test_relay_task_on_a_non_implementation_step_is_refused(sm):
    tid = _new(sm)
    with pytest.raises(A2AInvalidTransitionError, match="does not take one"):
        sm.transition_state(tid, S.SPECCING, A2AAttestation(
            actor="craig", role=H, relay_task_id="TASK-RELAYOK-X"))


def test_rejected_transition_leaves_no_open_transaction(sm):
    tid = _new(sm)
    with pytest.raises(A2AInvalidTransitionError):
        sm.transition_state(tid, S.SHIPPED, A2AAttestation(actor="craig", role=H))
    assert sm._conn().in_transaction is False


def test_rerun_actor_is_bound_to_human(sm):
    tid = _t3_at_gate_2(sm)
    sm.record_human_verification(tid, "mallory", "pytest -q", "ok")
    _walk(sm, tid, S.REVISION_NEEDED)
    with pytest.raises(A2AInvalidTransitionError, match=r"two\s+protocol roles"):
        sm.transition_state(tid, S.IN_IMPLEMENTATION, A2AAttestation(actor="Mallory", role=C))


def test_fresh_rerun_in_new_window_allows_shipping(sm):
    tid = _t3_at_gate_2(sm)
    sm.record_human_verification(tid, "craig", "pytest -q", "17 passed")
    _walk(sm, tid, S.REVISION_NEEDED, S.IN_IMPLEMENTATION, S.UNDER_REVIEW, S.AWAITING_GATE_2)
    with pytest.raises(A2AInvalidTransitionError, match="human re-run"):
        _ship(sm, tid)
    sm.record_human_verification(tid, "craig", "pytest -q", "17 passed")
    assert _ship(sm, tid).state == S.SHIPPED


def test_t2_ships_without_rerun(sm):
    tid = _to_impl(sm, A2ATaskTier.T2)
    _walk(sm, tid, S.UNDER_REVIEW, S.AWAITING_GATE_2)
    assert _ship(sm, tid).state == S.SHIPPED


def test_claude_cannot_close(sm):
    tid = _new(sm, A2ATaskTier.T1)
    _walk(sm, tid, S.IN_IMPLEMENTATION, S.AWAITING_GATE_2, S.SHIPPED)
    with pytest.raises(A2AInvalidTransitionError, match="may only be performed by HUMAN"):
        sm.transition_state(tid, S.CLOSED, A2AAttestation(actor="claude", role=C))


@pytest.mark.parametrize("path,target", [
    (T2_TO_IMPL, S.AWAITING_GATE_1),                                  # §7.2
    (T2_TO_IMPL + (S.BLOCKED_BY_DISCOVERY,), S.AWAITING_GATE_1),      # §10.3
    (T2_TO_IMPL + (S.UNDER_REVIEW, S.REVISION_NEEDED), S.CLOSED),     # §12.4 INCOMPLETE
])
def test_return_and_close_decisions_need_a_reason(sm, path, target):
    tid = _new(sm)
    _walk(sm, tid, *path)
    with pytest.raises(A2AInvalidTransitionError, match="requires a reason"):
        sm.transition_state(tid, target, A2AAttestation(actor="craig", role=H,
                                                        evidence_ref=EVIDENCE))
    sm.transition_state(tid, target, A2AAttestation(actor="craig", role=H,
                                                    reason="decided", evidence_ref=EVIDENCE))
    assert sm.get_task(tid).state == target


def test_return_to_gate_1_is_not_a_t1_step(sm):
    tid = _to_impl(sm, A2ATaskTier.T1)
    with pytest.raises(A2AInvalidTransitionError, match="protocol path"):
        sm.transition_state(tid, S.AWAITING_GATE_1, A2AAttestation(
            actor="craig", role=H, reason="conflict"))


# Tier escalation (§3, §10.3, §14)

def test_t2_escalates_to_t3_at_gate_1_and_then_needs_rerun(sm):
    tid = _new(sm)
    _walk(sm, tid, S.SPECCING, S.AWAITING_GATE_1)
    task = sm.escalate_tier(tid, "craig", "crontab now touched", EVIDENCE)
    assert task.tier == A2ATaskTier.T3
    _walk(sm, tid, S.IN_IMPLEMENTATION, S.UNDER_REVIEW, S.AWAITING_GATE_2)
    with pytest.raises(A2AInvalidTransitionError, match="human re-run"):
        _ship(sm, tid)
    [change] = sm.get_tier_changes(tid)
    assert (change.from_tier, change.to_tier, change.actor) == (
        A2ATaskTier.T2, A2ATaskTier.T3, "craig")
    assert sm.check_consistency() == []


@pytest.mark.parametrize("setup,actor,reason,message", [
    ("t2_draft", "craig", "r", "AWAITING_GATE_1"),
    ("t3_gate1", "craig", "r", "Only T2"),
    ("t2_gate1", "craig", " ", "requires a reason"),
    ("t2_gate1", "chatgpt", "r", r"two\s+protocol roles"),
])
def test_escalation_rules(sm, setup, actor, reason, message):
    tier = A2ATaskTier.T3 if setup.startswith("t3") else A2ATaskTier.T2
    tid = _new(sm, tier)
    if setup.endswith("gate1"):
        _walk(sm, tid, S.SPECCING, S.AWAITING_GATE_1)
    with pytest.raises(A2AInvalidTransitionError, match=message):
        sm.escalate_tier(tid, actor, reason)
    assert sm.get_task(tid).tier == tier
    assert sm.get_tier_changes(tid) == []


def test_tier_cannot_be_lowered_in_the_database(sm):
    tid = _new(sm, A2ATaskTier.T3)
    conn = sqlite3.connect(sm.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE a2a_tasks SET tier='T2' WHERE task_id=?", (tid,))
    conn.close()


# Legacy databases and history replay

def test_legacy_database_is_imported_and_consistent(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE a2a_tasks (task_id TEXT PRIMARY KEY, title TEXT NOT NULL,"
                     " tier TEXT NOT NULL, github_issue_url TEXT NOT NULL, state TEXT NOT NULL,"
                     " created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                     " protocol_version TEXT NOT NULL DEFAULT '0.2')")
        conn.execute("INSERT INTO a2a_tasks VALUES ('A2A-OLD00001','old','T3',?, "
                     "'AWAITING_GATE_2','2026-09-01T00:00:00+00:00',"
                     "'2026-09-02T00:00:00+00:00','0.2')", (URL,))
    sm = A2AStateManager(db, relay_status_lookup=fake_relay_lookup)
    [row] = sm.get_history("A2A-OLD00001")
    assert (row.from_state, row.to_state, row.actor) == (None, S.AWAITING_GATE_2,
                                                         "legacy-import")
    assert sm.check_consistency() == []
    sm.record_human_verification("A2A-OLD00001", "craig", "pytest -q", "ok")
    _ship(sm, "A2A-OLD00001")
    assert sm.check_consistency() == []
    A2AStateManager(db)  # re-opening does not import twice
    assert len(sm.get_history("A2A-OLD00001")) == 2


def _forge(sm, tid, frm, to, actor, role, evidence=None, relay=None):
    conn = sqlite3.connect(sm.db_path)
    conn.execute("INSERT INTO a2a_transitions (task_id, from_state, to_state, actor, role, "
                 "evidence_ref, relay_task_id, timestamp) VALUES (?,?,?,?,?,?,?,?)",
                 (tid, frm.value, to.value, actor, role.value, evidence, relay,
                  "2026-10-16T00:00:00+00:00"))
    conn.execute("UPDATE a2a_tasks SET state=? WHERE task_id=?", (to.value, tid))
    conn.commit()
    conn.close()


def test_check_replays_forged_history_against_the_rules(sm):
    tid = _new(sm, A2ATaskTier.T3)
    _forge(sm, tid, S.DRAFT, S.SPECCING, "claude", C)
    problems = " | ".join(sm.check_consistency())
    assert "taken as CLAUDE" in problems


def test_check_flags_t3_shipped_without_rerun(sm):
    tid = _t3_at_gate_2(sm)
    _forge(sm, tid, S.AWAITING_GATE_2, S.SHIPPED, "craig", H, evidence=EVIDENCE)
    assert any("without a human re-run" in p for p in sm.check_consistency())


# CLI details

def test_cli_output_file_is_recorded_verbatim(tmp_path):
    make_relay_db(tmp_path / "relay.db", completed=["TASK-CLIDONE2"])
    tid = _a2a(tmp_path, "create-task", "--title", "t", "--tier", "T3", "--issue-url", URL,
               "--actor", "craig").stdout.split()[1]
    for state, extra in [
        ("SPECCING", ["--actor", "craig", "--role", "HUMAN"]),
        ("AWAITING_GATE_1", ["--actor", "chatgpt", "--role", "CHATGPT", "--evidence", EVIDENCE]),
        ("IN_IMPLEMENTATION", ["--actor", "craig", "--role", "HUMAN"]),
        ("UNDER_REVIEW", ["--actor", "claude", "--role", "CLAUDE", "--evidence", EVIDENCE,
                          "--relay-task", "TASK-CLIDONE2"]),
        ("AWAITING_GATE_2", ["--actor", "chatgpt", "--role", "CHATGPT", "--evidence", EVIDENCE]),
    ]:
        assert _a2a(tmp_path, "advance", tid, "--to", state, *extra).returncode == 0
    out = tmp_path / "out.txt"
    out.write_bytes(b"line one\r\nline two\r\n")
    r = _a2a(tmp_path, "record-human-verification", tid, "--actor", " craig ",
             "--command", "pytest -q", "--output-file", str(out))
    assert r.returncode == 0 and "by craig." in r.stdout
    [v] = A2AStateManager(tmp_path / "a2a.db").get_human_verifications(tid)
    assert v.output == "line one\r\nline two\r\n"
    shown = _a2a(tmp_path, "get-task", tid).stdout
    assert "| line two" in shown and "Gate 2 window" in shown


def test_cli_database_error_is_reported_without_traceback(tmp_path):
    bad = tmp_path / "no-such-dir" / "a2a.db"
    r = subprocess.run([sys.executable, "-m", "a2a", "--db", str(bad), "list-tasks"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr and "database problem" in r.stderr


def test_cli_escalate_tier(tmp_path):
    tid = _a2a(tmp_path, "create-task", "--title", "t", "--tier", "T2", "--issue-url", URL,
               "--actor", "craig").stdout.split()[1]
    _a2a(tmp_path, "advance", tid, "--to", "SPECCING", "--actor", "craig", "--role", "HUMAN")
    _a2a(tmp_path, "advance", tid, "--to", "AWAITING_GATE_1", "--actor", "chatgpt",
         "--role", "CHATGPT", "--evidence", EVIDENCE)
    r = _a2a(tmp_path, "escalate-tier", tid, "--actor", "craig", "--reason", "crontab")
    assert r.returncode == 0 and "now T3" in r.stdout
    assert "Tier T2 -> T3 by craig" in _a2a(tmp_path, "get-task", tid).stdout


# ── Second review pass on Batch 4 ─────────────────────────────────────────────

def _raw(sm, sql, *params):
    conn = sqlite3.connect(sm.db_path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def test_task_inserted_outside_the_ledger_is_flagged_even_after_reopening(tmp_path):
    """The legacy import is a one-time migration: a history-less task inserted
    later must never be 'imported' into a clean-looking history."""
    db = tmp_path / "a2a.db"
    sm = A2AStateManager(db, relay_status_lookup=fake_relay_lookup)
    _raw(sm, "INSERT INTO a2a_tasks VALUES ('A2A-FORGED01','forged','T3',?,'SHIPPED',"
             "'2026-10-16T00:00:00+00:00','2026-10-16T00:00:00+00:00','0.2')", URL)
    reopened = A2AStateManager(db, relay_status_lookup=fake_relay_lookup)
    assert reopened.get_history("A2A-FORGED01") == []
    assert any("A2A-FORGED01" in p and "outside the ledger" in p
               for p in reopened.check_consistency())
    r = subprocess.run([sys.executable, "-m", "a2a", "--db", str(db), "check"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "A2A-FORGED01" in r.stderr


@pytest.mark.parametrize("name", [
    "clau\x00de", "cla͏ude", "claude️", "ㅤclaude", "claudeᅟ",
    "⠀claude", "сlaude",            # last: Cyrillic 'с' look-alike
    "a" * 65, "-lead", "trail.",
])
def test_unplain_actor_names_are_refused(sm, name):
    tid = _new(sm)
    with pytest.raises(A2AInvalidTransitionError, match="not allowed"):
        sm.transition_state(tid, S.SPECCING, A2AAttestation(actor=name, role=H))
    assert len(sm.get_history(tid)) == 1


@pytest.mark.parametrize("name", ["craig", "Craig Paterson", "chatgpt-4o", "c.p@lunneys",
                                  "claude_code"])
def test_plain_actor_names_are_accepted(sm, name):
    tid = _new(sm)
    sm.transition_state(tid, S.SPECCING, A2AAttestation(actor=name, role=H))


@pytest.mark.parametrize("creator", ["legacy-import", "unattributed", "bad​name!"])
def test_reserved_or_unplain_creator_is_refused(sm, creator):
    task = A2ATask(task_id=A2ATask.new_id(), title="t", tier=A2ATaskTier.T2,
                   github_issue_url=URL)
    with pytest.raises(A2AInvalidTaskDataError):
        sm.create_task(task, created_by=creator)


def test_tier_lowered_with_trigger_dropped_is_detected(sm):
    tid = _new(sm, A2ATaskTier.T3)
    _raw(sm, "DROP TRIGGER a2a_tasks_tier_escalation_only")
    _raw(sm, "UPDATE a2a_tasks SET tier='T2' WHERE task_id=?", tid)
    assert any("tier" in p and tid in p for p in sm.check_consistency())


def test_check_reports_unparseable_rows_instead_of_crashing(sm):
    tid = _new(sm)
    _raw(sm, "UPDATE a2a_tasks SET created_at='not-a-date' WHERE task_id=?", tid)
    problems = sm.check_consistency()
    assert any(tid in p and "cannot be parsed" in p for p in problems)


def test_check_flags_creator_who_later_acted_in_another_role(sm):
    tid = _new(sm)   # created by craig (HUMAN)
    _forge(sm, tid, S.DRAFT, S.SPECCING, "Craig", C)
    assert any("several roles" in p for p in sm.check_consistency())


def test_concurrent_first_opens_of_a_legacy_db_import_exactly_once(tmp_path):
    """Third review pass: schema creation, the legacy import and the version
    bump are one transaction, so racing first opens can neither skip the
    import nor fail on the WAL switch."""
    import threading

    for round_ in range(8):
        db = tmp_path / f"legacy-{round_}.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE a2a_tasks (task_id TEXT PRIMARY KEY, title TEXT NOT NULL,"
                         " tier TEXT NOT NULL, github_issue_url TEXT NOT NULL,"
                         " state TEXT NOT NULL, created_at TEXT NOT NULL,"
                         " updated_at TEXT NOT NULL,"
                         " protocol_version TEXT NOT NULL DEFAULT '0.2')")
            conn.execute("INSERT INTO a2a_tasks VALUES ('A2A-L1','t','T2',?,'DRAFT',"
                         "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','0.2')",
                         (URL,))
        errors: list[BaseException] = []

        def open_db():
            try:
                A2AStateManager(db)
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=open_db) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        sm = A2AStateManager(db)
        assert sm.check_consistency() == []
        assert [r.actor for r in sm.get_history("A2A-L1")] == ["legacy-import"]
