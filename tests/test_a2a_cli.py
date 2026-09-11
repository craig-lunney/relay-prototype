"""Unit tests for the A2A Protocol v0.2 CLI."""
from __future__ import annotations

import argparse
import time

import pytest

from a2a.cli import cmd_advance, cmd_create_task, cmd_get_task, cmd_list_tasks
from a2a.models import A2AProtocolState, A2ATaskTier
from a2a.service import A2AService
from a2a.state import A2AStateManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _args(**kwargs) -> argparse.Namespace:
    """Build an args namespace with sensible defaults."""
    defaults = dict(
        db=None,              # caller must override
        title="Test task",
        tier="T2",
        issue_url="https://github.com/org/repo/issues/1",
        task_id=None,
        to_state=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _create_in_db(db_path: str, title: str = "Task", tier: str = "T2") -> str:
    """Create a task directly via A2AService; return its task_id."""
    svc = A2AService(A2AStateManager(db_path))
    t = svc.create_task(
        title=title,
        tier=A2ATaskTier(tier),
        github_issue_url="https://github.com/org/repo/issues/1",
    )
    return t.task_id


# ---------------------------------------------------------------------------
# create-task
# ---------------------------------------------------------------------------

def test_create_task_prints_task_id(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    cmd_create_task(_args(db=db))
    out = capsys.readouterr().out
    assert "A2A-" in out


def test_create_task_prints_draft_state(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    cmd_create_task(_args(db=db))
    assert "DRAFT" in capsys.readouterr().out


def test_create_task_prints_title(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    cmd_create_task(_args(db=db, title="My special task"))
    assert "My special task" in capsys.readouterr().out


def test_create_task_prints_tier(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    cmd_create_task(_args(db=db, tier="T3"))
    assert "T3" in capsys.readouterr().out


def test_create_task_prints_issue_url(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    url = "https://github.com/x/y/issues/99"
    cmd_create_task(_args(db=db, issue_url=url))
    assert url in capsys.readouterr().out


def test_create_task_persists_to_db(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    cmd_create_task(_args(db=db, title="Persisted"))
    out = capsys.readouterr().out
    # Extract task_id from output ("Created: A2A-XXXXXXXX")
    task_id = next(tok for tok in out.split() if tok.startswith("A2A-"))
    sm = A2AStateManager(db)
    stored = sm.get_task(task_id)
    assert stored is not None
    assert stored.title == "Persisted"
    assert stored.state == A2AProtocolState.DRAFT


def test_create_task_all_tiers(tmp_path, capsys):
    for tier in ("T1", "T2", "T3"):
        db = str(tmp_path / f"a2a_{tier}.db")
        cmd_create_task(_args(db=db, tier=tier))
        assert tier in capsys.readouterr().out


def test_create_task_db_is_configurable(tmp_path, capsys):
    db_a = str(tmp_path / "alpha.db")
    db_b = str(tmp_path / "beta.db")
    cmd_create_task(_args(db=db_a, title="Alpha"))
    cmd_create_task(_args(db=db_b, title="Beta"))
    tasks_a = A2AStateManager(db_a).list_tasks()
    tasks_b = A2AStateManager(db_b).list_tasks()
    assert len(tasks_a) == 1
    assert len(tasks_b) == 1
    assert tasks_a[0].title == "Alpha"
    assert tasks_b[0].title == "Beta"


# ---------------------------------------------------------------------------
# get-task
# ---------------------------------------------------------------------------

def test_get_task_prints_task_id(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    cmd_get_task(_args(db=db, task_id=task_id))
    assert task_id in capsys.readouterr().out


def test_get_task_prints_state(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    cmd_get_task(_args(db=db, task_id=task_id))
    assert "DRAFT" in capsys.readouterr().out


def test_get_task_prints_issue_url(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    cmd_get_task(_args(db=db, task_id=task_id))
    assert "github.com" in capsys.readouterr().out


def test_get_task_missing_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit) as exc_info:
        cmd_get_task(_args(db=db, task_id="A2A-NOTFOUND"))
    assert exc_info.value.code == 1


def test_get_task_missing_prints_to_stderr(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit):
        cmd_get_task(_args(db=db, task_id="A2A-NOTFOUND"))
    err = capsys.readouterr().err
    assert "A2A-NOTFOUND" in err
    assert "Error" in err


def test_get_task_missing_no_stdout(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit):
        cmd_get_task(_args(db=db, task_id="A2A-NOTFOUND"))
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# list-tasks
# ---------------------------------------------------------------------------

def test_list_tasks_empty_prints_message(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    cmd_list_tasks(_args(db=db))
    assert "No tasks" in capsys.readouterr().out


def test_list_tasks_single_task(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    _create_in_db(db, title="Only task")
    cmd_list_tasks(_args(db=db))
    assert "Only task" in capsys.readouterr().out


def test_list_tasks_multiple_shows_all(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    for title in ("Alpha", "Beta", "Gamma"):
        _create_in_db(db, title=title)
        time.sleep(0.015)
    cmd_list_tasks(_args(db=db))
    out = capsys.readouterr().out
    assert "Alpha" in out
    assert "Beta" in out
    assert "Gamma" in out


def test_list_tasks_shows_task_ids(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    cmd_list_tasks(_args(db=db))
    assert task_id in capsys.readouterr().out


def test_list_tasks_empty_exit_zero(tmp_path):
    db = str(tmp_path / "a2a.db")
    # Must not raise SystemExit
    cmd_list_tasks(_args(db=db))


# ---------------------------------------------------------------------------
# advance
# ---------------------------------------------------------------------------

def test_advance_prints_new_state(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    cmd_advance(_args(db=db, task_id=task_id, to_state="SPECCING"))
    assert "SPECCING" in capsys.readouterr().out


def test_advance_prints_task_id(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    cmd_advance(_args(db=db, task_id=task_id, to_state="SPECCING"))
    assert task_id in capsys.readouterr().out


def test_advance_updates_state_in_db(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    cmd_advance(_args(db=db, task_id=task_id, to_state="SPECCING"))
    capsys.readouterr()
    stored = A2AStateManager(db).get_task(task_id)
    assert stored.state == A2AProtocolState.SPECCING


def test_advance_t1_to_in_implementation(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T1")
    cmd_advance(_args(db=db, task_id=task_id, to_state="IN_IMPLEMENTATION"))
    assert "IN_IMPLEMENTATION" in capsys.readouterr().out


def test_advance_full_t2_path(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T2")
    for state in ("SPECCING", "AWAITING_GATE_1", "IN_IMPLEMENTATION",
                  "UNDER_REVIEW", "AWAITING_GATE_2", "SHIPPED", "CLOSED"):
        cmd_advance(_args(db=db, task_id=task_id, to_state=state))
        capsys.readouterr()
    assert A2AStateManager(db).get_task(task_id).state == A2AProtocolState.CLOSED


def test_advance_invalid_transition_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    with pytest.raises(SystemExit) as exc_info:
        cmd_advance(_args(db=db, task_id=task_id, to_state="CLOSED"))
    assert exc_info.value.code == 1


def test_advance_invalid_transition_prints_to_stderr(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    with pytest.raises(SystemExit):
        cmd_advance(_args(db=db, task_id=task_id, to_state="CLOSED"))
    err = capsys.readouterr().err
    assert "Error" in err
    assert len(err.strip()) > 0


def test_advance_invalid_transition_no_stdout(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    with pytest.raises(SystemExit):
        cmd_advance(_args(db=db, task_id=task_id, to_state="CLOSED"))
    assert capsys.readouterr().out == ""


def test_advance_invalid_transition_does_not_corrupt_state(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db)
    with pytest.raises(SystemExit):
        cmd_advance(_args(db=db, task_id=task_id, to_state="SHIPPED"))
    capsys.readouterr()
    assert A2AStateManager(db).get_task(task_id).state == A2AProtocolState.DRAFT


def test_advance_missing_task_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit) as exc_info:
        cmd_advance(_args(db=db, task_id="A2A-NOTFOUND", to_state="SPECCING"))
    assert exc_info.value.code == 1


def test_advance_missing_task_prints_to_stderr(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit):
        cmd_advance(_args(db=db, task_id="A2A-NOTFOUND", to_state="SPECCING"))
    err = capsys.readouterr().err
    assert "A2A-NOTFOUND" in err
    assert "Error" in err


# ---------------------------------------------------------------------------
# create-task — invalid Issue URL (A2A-19)
# ---------------------------------------------------------------------------

def test_create_task_invalid_url_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit) as exc_info:
        cmd_create_task(_args(db=db, issue_url="not-a-github-issue-url"))
    assert exc_info.value.code == 1


def test_create_task_invalid_url_prints_to_stderr(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit):
        cmd_create_task(_args(db=db, issue_url="https://gitlab.com/org/repo/issues/1"))
    err = capsys.readouterr().err
    assert "Error" in err
    assert len(err.strip()) > 0


def test_create_task_invalid_url_no_stdout(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit):
        cmd_create_task(_args(db=db, issue_url="https://github.com/org/repo/pulls/1"))
    assert capsys.readouterr().out == ""


def test_create_task_invalid_url_no_task_persisted(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit):
        cmd_create_task(_args(db=db, issue_url="bad-url"))
    capsys.readouterr()
    assert A2AStateManager(db).list_tasks() == []


def test_create_task_pr_url_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit) as exc_info:
        cmd_create_task(_args(db=db, issue_url="https://github.com/org/repo/pulls/5"))
    assert exc_info.value.code == 1


def test_create_task_zero_issue_number_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit) as exc_info:
        cmd_create_task(_args(db=db, issue_url="https://github.com/org/repo/issues/0"))
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# create-task — invalid title (A2A-20)
# ---------------------------------------------------------------------------

def test_create_task_empty_title_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit) as exc_info:
        cmd_create_task(_args(db=db, title=""))
    assert exc_info.value.code == 1


def test_create_task_empty_title_prints_to_stderr(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit):
        cmd_create_task(_args(db=db, title=""))
    err = capsys.readouterr().err
    assert "Error" in err
    assert len(err.strip()) > 0


def test_create_task_empty_title_no_stdout(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit):
        cmd_create_task(_args(db=db, title="   "))
    assert capsys.readouterr().out == ""


def test_create_task_empty_title_no_task_persisted(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    with pytest.raises(SystemExit):
        cmd_create_task(_args(db=db, title="\t"))
    capsys.readouterr()
    assert A2AStateManager(db).list_tasks() == []


# ---------------------------------------------------------------------------
# advance — tier-violation paths at CLI level (A2A-18 gap, closed in A2A-19)
# ---------------------------------------------------------------------------

def test_advance_t1_to_speccing_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T1")
    with pytest.raises(SystemExit) as exc_info:
        cmd_advance(_args(db=db, task_id=task_id, to_state="SPECCING"))
    assert exc_info.value.code == 1


def test_advance_t1_to_speccing_prints_to_stderr(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T1")
    with pytest.raises(SystemExit):
        cmd_advance(_args(db=db, task_id=task_id, to_state="SPECCING"))
    err = capsys.readouterr().err
    assert "Error" in err


def test_advance_t1_to_speccing_no_stdout(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T1")
    with pytest.raises(SystemExit):
        cmd_advance(_args(db=db, task_id=task_id, to_state="SPECCING"))
    assert capsys.readouterr().out == ""


def test_advance_t1_to_awaiting_gate_1_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T1")
    with pytest.raises(SystemExit) as exc_info:
        cmd_advance(_args(db=db, task_id=task_id, to_state="AWAITING_GATE_1"))
    assert exc_info.value.code == 1


def test_advance_t1_to_under_review_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T1")
    # advance T1 task to IN_IMPLEMENTATION (valid), then attempt UNDER_REVIEW (tier-forbidden)
    cmd_advance(_args(db=db, task_id=task_id, to_state="IN_IMPLEMENTATION"))
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc_info:
        cmd_advance(_args(db=db, task_id=task_id, to_state="UNDER_REVIEW"))
    assert exc_info.value.code == 1


def test_advance_t1_tier_violation_does_not_corrupt_state(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T1")
    with pytest.raises(SystemExit):
        cmd_advance(_args(db=db, task_id=task_id, to_state="SPECCING"))
    capsys.readouterr()
    assert A2AStateManager(db).get_task(task_id).state == A2AProtocolState.DRAFT


def test_advance_t2_draft_to_in_implementation_exits_1(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T2")
    with pytest.raises(SystemExit) as exc_info:
        cmd_advance(_args(db=db, task_id=task_id, to_state="IN_IMPLEMENTATION"))
    assert exc_info.value.code == 1


def test_advance_t2_draft_to_in_implementation_prints_to_stderr(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T2")
    with pytest.raises(SystemExit):
        cmd_advance(_args(db=db, task_id=task_id, to_state="IN_IMPLEMENTATION"))
    err = capsys.readouterr().err
    assert "Error" in err


def test_advance_t2_draft_to_in_implementation_does_not_corrupt_state(tmp_path, capsys):
    db = str(tmp_path / "a2a.db")
    task_id = _create_in_db(db, tier="T2")
    with pytest.raises(SystemExit):
        cmd_advance(_args(db=db, task_id=task_id, to_state="IN_IMPLEMENTATION"))
    capsys.readouterr()
    assert A2AStateManager(db).get_task(task_id).state == A2AProtocolState.DRAFT


# ---------------------------------------------------------------------------
# Parser — argument structure
# ---------------------------------------------------------------------------

def test_parser_requires_subcommand():
    from a2a.cli import _build_parser
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_create_task_requires_title():
    from a2a.cli import _build_parser
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["create-task", "--tier", "T2",
                           "--issue-url", "https://github.com/x/y/issues/1"])


def test_parser_create_task_requires_tier():
    from a2a.cli import _build_parser
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["create-task", "--title", "t",
                           "--issue-url", "https://github.com/x/y/issues/1"])


def test_parser_advance_requires_to():
    from a2a.cli import _build_parser
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["advance", "A2A-12345678"])


def test_parser_db_default():
    from a2a.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["list-tasks"])
    assert args.db == "a2a.db"


def test_parser_db_override():
    from a2a.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["--db", "/tmp/custom.db", "list-tasks"])
    assert args.db == "/tmp/custom.db"


def test_parser_create_task_requires_issue_url():
    from a2a.cli import _build_parser
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["create-task", "--title", "t", "--tier", "T2"])
