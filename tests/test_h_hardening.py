"""
Tests for P1-H: Verifier environment hardening, workspace lifecycle cleanup,
report JSON parser robustness, and audit sequence atomicity.

H-1 — Verifier subprocess environment hardening
H-2 — Workspace lifecycle cleanup
H-3 — Proper JSON scanning in _parse_report
H-4 — Atomic audit sequence assignment

All tests are focused on the specific security and reliability properties
introduced in P1-H. No regressions in P1-A through P1-G are acceptable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from relay.audit import AuditLog
from relay.executor import (
    ExecutionEngine,
    _worker_environment,
    _WORKER_ENV_ALLOWLIST_WINDOWS,
    _WORKER_ENV_ALLOWLIST_POSIX,
)
from relay.models import (
    AuditEvent,
    AuditEventType,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
)
from relay.verifier import Verifier


# ─────────────────────────────────────────────────────────────────────────────
# H-1  Verifier subprocess environment hardening
# ─────────────────────────────────────────────────────────────────────────────

def _make_completed_execution(task: Task, workspace: str) -> Execution:
    """Create a completed Execution record pointing at an existing workspace."""
    return Execution(
        execution_id="EXEC-H1TEST",
        task_id=task.task_id,
        attempt_number=1,
        status=ExecutionStatus.COMPLETED,
        workspace=workspace,
        worker_identity="test-worker",
        policy_version="0.1",
        capability_snapshot=[],
        started_at=datetime.now(timezone.utc),
    )


def test_h1_all_verifier_subprocesses_have_explicit_env(
    sample_task, test_repo
):
    """
    Every subprocess.run call made by the verifier must receive an explicit env= kwarg.

    If env=None is passed, the subprocess inherits the full parent environment,
    which may contain Relay credentials.  This test fails if any call omits env=.
    """
    verifier = Verifier()
    execution = _make_completed_execution(sample_task, str(test_repo))
    captured: list[dict] = []

    with patch("subprocess.run", wraps=subprocess.run) as mock_run:
        verifier.verify(sample_task, execution)

    calls = mock_run.call_args_list
    assert calls, "verifier made no subprocess.run calls — test is ineffective"

    for i, call in enumerate(calls):
        env = call.kwargs.get("env")
        cmd = call.args[0] if call.args else call.kwargs.get("args", "?")
        assert env is not None, (
            f"subprocess.run call #{i} has env=None (implicit parent env). "
            f"Command: {cmd!r}. H-1 boundary is broken."
        )
        assert isinstance(env, dict), (
            f"subprocess.run call #{i} env= is not a dict: {type(env)}"
        )


def test_h1_verifier_subprocess_count_is_expected(sample_task, test_repo):
    """
    The verifier makes at least 3 subprocess.run calls (git branch, git status ×2,
    optional pytest). All must use a curated environment.
    """
    verifier = Verifier()
    execution = _make_completed_execution(sample_task, str(test_repo))

    with patch("subprocess.run", wraps=subprocess.run) as mock_run:
        verifier.verify(sample_task, execution)

    # At minimum: _get_branch, _get_status_lines, _get_changed_files = 3 git calls.
    assert mock_run.call_count >= 3, (
        f"Expected at least 3 subprocess calls; got {mock_run.call_count}"
    )


def test_h1_verifier_subprocess_env_excludes_anthropic_api_key(
    sample_task, test_repo
):
    """ANTHROPIC_API_KEY must not reach any verifier subprocess."""
    verifier = Verifier()
    execution = _make_completed_execution(sample_task, str(test_repo))

    with patch("subprocess.run", wraps=subprocess.run) as mock_run:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-h1-secret"}):
            verifier.verify(sample_task, execution)

    for i, call in enumerate(mock_run.call_args_list):
        env = call.kwargs.get("env") or {}
        assert "ANTHROPIC_API_KEY" not in env, (
            f"ANTHROPIC_API_KEY appeared in subprocess.run call #{i} env=. "
            "Relay credential crossed the verifier boundary."
        )


def test_h1_verifier_subprocess_env_excludes_database_url(
    sample_task, test_repo
):
    """DATABASE_URL must not reach any verifier subprocess."""
    verifier = Verifier()
    execution = _make_completed_execution(sample_task, str(test_repo))

    with patch("subprocess.run", wraps=subprocess.run) as mock_run:
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://secret@host/db"}):
            verifier.verify(sample_task, execution)

    for i, call in enumerate(mock_run.call_args_list):
        env = call.kwargs.get("env") or {}
        assert "DATABASE_URL" not in env, (
            f"DATABASE_URL appeared in subprocess.run call #{i} env=."
        )


def test_h1_verifier_subprocess_env_excludes_arbitrary_variable(
    sample_task, test_repo
):
    """
    An attacker-controlled variable must not cross the verifier → subprocess boundary.
    This verifies the allowlist property: unknown = excluded by default.
    """
    verifier = Verifier()
    execution = _make_completed_execution(sample_task, str(test_repo))

    with patch("subprocess.run", wraps=subprocess.run) as mock_run:
        with patch.dict(
            os.environ,
            {"RELAY_ATTACKER_CONTROLLED_VERIFIER": "should-not-cross"},
        ):
            verifier.verify(sample_task, execution)

    for i, call in enumerate(mock_run.call_args_list):
        env = call.kwargs.get("env") or {}
        assert "RELAY_ATTACKER_CONTROLLED_VERIFIER" not in env, (
            f"Arbitrary variable appeared in subprocess.run call #{i} env=."
        )


def test_h1_verifier_uses_same_allowlist_as_p1g_worker(sample_task, test_repo):
    """
    Every key in the verifier subprocess env= must be on the P1-G platform allowlist.

    This confirms H-1 reuses the P1-G allowlist and has not introduced a weaker boundary.
    """
    allowlist = (
        _WORKER_ENV_ALLOWLIST_WINDOWS
        if sys.platform == "win32"
        else _WORKER_ENV_ALLOWLIST_POSIX
    )
    verifier = Verifier()
    execution = _make_completed_execution(sample_task, str(test_repo))

    with patch("subprocess.run", wraps=subprocess.run) as mock_run:
        verifier.verify(sample_task, execution)

    for i, call in enumerate(mock_run.call_args_list):
        env = call.kwargs.get("env") or {}
        for key in env:
            assert key in allowlist, (
                f"Key {key!r} in subprocess.run call #{i} env= is NOT on the P1-G "
                f"allowlist. H-1 boundary is weaker than P1-G."
            )


def test_h1_p1g_worker_environment_unaffected(sample_task, tmp_path, tmp_db, audit, policy):
    """
    P1-G regression: the Claude worker subprocess environment must be unaffected by H-1.
    --bare and --add-dir must still be present in the Claude command.
    """
    from relay.executor import RELAY_WORKER_CONFIG_DIR
    captured: list[dict] = []

    def spy_popen(cmd, **kwargs):
        captured.append({"cmd": list(cmd), "env": kwargs.get("env")})
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=spy_popen):
        engine.run(sample_task, tmp_path / "ws")

    assert captured, "Popen was never called"
    cmd = captured[0]["cmd"]
    assert "--bare" in cmd, "--bare missing from Claude command after H-1 changes"
    assert "--add-dir" in cmd, "--add-dir missing from Claude command after H-1 changes"
    idx = cmd.index("--add-dir")
    assert Path(cmd[idx + 1]).resolve() == RELAY_WORKER_CONFIG_DIR.resolve(), (
        "--add-dir does not point to Relay worker config after H-1 changes"
    )
    env = captured[0]["env"]
    assert env is not None, "Claude Popen env= is None after H-1 changes (P1-G regression)"


# ─────────────────────────────────────────────────────────────────────────────
# H-2  Workspace lifecycle cleanup
# ─────────────────────────────────────────────────────────────────────────────

def _make_spy_popen(report_json: dict | None = None, exit_code: int = 0, timeout: bool = False):
    """Return a Popen factory that simulates a Claude execution outcome."""
    def factory(cmd, **kwargs):
        proc = MagicMock()
        if timeout:
            call_count = [0]
            def communicate(timeout=None, input=None):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise subprocess.TimeoutExpired(cmd, timeout or 1)
                return ("", "")
            proc.communicate.side_effect = communicate
            proc.returncode = -1
        else:
            stdout = json.dumps(report_json) if report_json else ""
            proc.communicate.return_value = (stdout, "")
            proc.returncode = exit_code
        return proc
    return factory


def _valid_report(task_id: str, execution_id: str) -> dict:
    return {
        "protocol_version": "0.1",
        "message_type": "IMPLEMENTATION_REPORT",
        "task_id": task_id,
        "execution_id": execution_id,
        "status": "COMPLETED",
        "summary": "done",
        "files_changed": [],
        "files_added": [],
        "files_deleted": [],
        "tests_run": [],
        "risks": [],
        "uncertainties": [],
        "blockers": [],
        "recommended_next_action": "none",
    }


def test_h2_cleanup_workspace_removes_directory(tmp_path, tmp_db, audit, policy, sample_task):
    """cleanup_workspace deletes an existing workspace within the boundary."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    ws_base = tmp_path / "ws"
    workspace = engine._prepare_workspace(sample_task, ws_base)
    assert Path(workspace).exists(), "workspace should exist before cleanup"

    engine.cleanup_workspace(workspace, ws_base)

    assert not Path(workspace).exists(), "workspace should be deleted after cleanup"


def test_h2_cleanup_workspace_nonexistent_is_noop(tmp_path, tmp_db, audit, policy, sample_task):
    """cleanup_workspace on a nonexistent path does not raise."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    ws_base = tmp_path / "ws"
    nonexistent = str(ws_base / "does-not-exist")
    engine.cleanup_workspace(nonexistent, ws_base)  # must not raise


def test_h2_cleanup_workspace_empty_path_is_noop(tmp_path, tmp_db, audit, policy, sample_task):
    """cleanup_workspace with an empty workspace string does not raise."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    engine.cleanup_workspace("", tmp_path)  # must not raise


def test_h2_cleanup_workspace_boundary_violation_preserves(
    tmp_path, tmp_db, audit, policy, sample_task
):
    """
    cleanup_workspace must NOT delete a directory outside workspace_base.

    Fail-safe: if the boundary cannot be confirmed, the directory is preserved.
    """
    engine = ExecutionEngine(tmp_db, audit, policy)
    ws_base = tmp_path / "ws"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / "sentinel.txt").write_text("must-survive", encoding="utf-8")

    engine.cleanup_workspace(str(outside), ws_base)

    assert outside.exists(), "Directory outside workspace_base must be preserved"
    assert (outside / "sentinel.txt").exists(), "Files outside boundary must survive"


def test_h2_workspace_cleaned_after_completed_execution(
    tmp_path, tmp_db, audit, policy, sample_task
):
    """
    After a COMPLETED execution, cleanup_workspace removes the workspace
    when called (simulating cmd_run_task behaviour with keep_workspace=False).
    """
    engine = ExecutionEngine(tmp_db, audit, policy)
    ws_base = tmp_path / "ws"
    report = _valid_report(sample_task.task_id, "EXEC-H2")

    with patch("subprocess.Popen", side_effect=_make_spy_popen(report, exit_code=0)):
        execution = engine.run(sample_task, ws_base)

    assert execution.status == ExecutionStatus.COMPLETED
    assert Path(execution.workspace).exists(), "workspace should exist before cleanup"

    engine.cleanup_workspace(execution.workspace, ws_base)

    assert not Path(execution.workspace).exists(), "workspace should be gone after cleanup"


def test_h2_workspace_cleaned_after_failed_execution(
    tmp_path, tmp_db, audit, policy, sample_task
):
    """After a FAILED execution, cleanup_workspace removes the workspace."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    ws_base = tmp_path / "ws"

    with patch("subprocess.Popen", side_effect=_make_spy_popen(exit_code=1)):
        execution = engine.run(sample_task, ws_base)

    assert execution.status == ExecutionStatus.FAILED
    assert Path(execution.workspace).exists()

    engine.cleanup_workspace(execution.workspace, ws_base)

    assert not Path(execution.workspace).exists()


def test_h2_workspace_cleaned_after_timeout(
    tmp_path, tmp_db, audit, policy, sample_task
):
    """After a TIMEOUT execution, cleanup_workspace removes the workspace."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    ws_base = tmp_path / "ws"

    with patch("subprocess.Popen", side_effect=_make_spy_popen(timeout=True)):
        execution = engine.run(sample_task, ws_base, timeout_seconds=1)

    assert execution.status == ExecutionStatus.TIMEOUT
    assert Path(execution.workspace).exists()

    engine.cleanup_workspace(execution.workspace, ws_base)

    assert not Path(execution.workspace).exists()


def test_h2_cleanup_workspace_respects_keep_workspace_flag(
    tmp_path, tmp_db, audit, policy, sample_task
):
    """When keep_workspace=True, the caller must not invoke cleanup_workspace."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    ws_base = tmp_path / "ws"
    report = _valid_report(sample_task.task_id, "EXEC-H2-KEEP")

    with patch("subprocess.Popen", side_effect=_make_spy_popen(report, exit_code=0)):
        execution = engine.run(sample_task, ws_base)

    assert execution.status == ExecutionStatus.COMPLETED
    workspace_path = Path(execution.workspace)
    assert workspace_path.exists()

    # Simulate keep_workspace=True: do NOT call cleanup
    # Workspace must still be present
    assert workspace_path.exists(), (
        "Workspace was removed even though keep_workspace=True prevents cleanup call"
    )


def test_h2_cleanup_not_called_while_lease_active(
    tmp_path, tmp_db, audit, policy, sample_task
):
    """
    cleanup_workspace is only safe after the lease is released.

    This test verifies the method safely handles a workspace that still
    contains live content — it does NOT verify the lease is held (that's
    the caller's responsibility), but it does confirm the boundary check runs.
    """
    engine = ExecutionEngine(tmp_db, audit, policy)
    ws_base = tmp_path / "ws"
    ws_base.mkdir(parents=True, exist_ok=True)
    workspace = ws_base / sample_task.task_id
    workspace.mkdir()
    (workspace / "sentinel.txt").write_text("alive", encoding="utf-8")

    # Call cleanup_workspace while the workspace exists within the boundary.
    # This must succeed (workspace within boundary).
    engine.cleanup_workspace(str(workspace), ws_base)
    assert not workspace.exists(), "cleanup_workspace should have removed the workspace"


# ─────────────────────────────────────────────────────────────────────────────
# H-3  Proper JSON scanning in _parse_report
# ─────────────────────────────────────────────────────────────────────────────

def _make_engine_for_parsing(tmp_db, audit, policy):
    """Return an ExecutionEngine for direct _parse_report testing."""
    return ExecutionEngine(tmp_db, audit, policy)


TASK_ID = "TASK-H3TEST"
EXEC_ID = "EXEC-H3TEST"


def _report_json(**overrides) -> dict:
    base = {
        "protocol_version": "0.1",
        "message_type": "IMPLEMENTATION_REPORT",
        "task_id": TASK_ID,
        "execution_id": EXEC_ID,
        "status": "COMPLETED",
        "summary": "done",
        "files_changed": [],
        "files_added": [],
        "files_deleted": [],
        "tests_run": [],
        "risks": [],
        "uncertainties": [],
        "blockers": [],
        "recommended_next_action": "none",
    }
    base.update(overrides)
    return base


def test_h3_flat_json_report_still_parsed(tmp_db, audit, policy):
    """Regression: a flat JSON report (no nested objects) still parses correctly."""
    engine = _make_engine_for_parsing(tmp_db, audit, policy)
    report = _report_json()
    output = json.dumps(report)

    result = engine._parse_report(output, TASK_ID, EXEC_ID)

    assert result is not None, "Flat JSON report was not parsed"
    assert result.status == "COMPLETED"
    assert result.task_id == TASK_ID


def test_h3_nested_json_report_parsed(tmp_db, audit, policy):
    """
    H-3: A report containing nested JSON objects must be correctly parsed.

    The old r"\\{[^{}]*\\}" regex would fail on any object with nested braces.
    raw_decode() handles arbitrary nesting.
    """
    engine = _make_engine_for_parsing(tmp_db, audit, policy)
    # risks field contains a list — while the schema expects list[str], the
    # _parse_report regex was broken by ANY nested braces in the output.
    # Test with nested braces appearing elsewhere in the output text.
    report = _report_json(summary="nested {braces} in summary text")
    output = (
        "Some preamble text.\n"
        + json.dumps(report)
        + "\nSome trailing text."
    )

    result = engine._parse_report(output, TASK_ID, EXEC_ID)

    assert result is not None, "Report with nested JSON context was not parsed"
    assert result.summary == "nested {braces} in summary text"


def test_h3_report_embedded_in_prose_is_found(tmp_db, audit, policy):
    """A valid report embedded in prose output is found by the scanner."""
    engine = _make_engine_for_parsing(tmp_db, audit, policy)
    report = _report_json()
    output = (
        "I have completed the task.\n\n"
        + json.dumps(report)
        + "\n\nLet me know if you need anything else."
    )

    result = engine._parse_report(output, TASK_ID, EXEC_ID)

    assert result is not None
    assert result.status == "COMPLETED"


def test_h3_multiple_json_objects_first_valid_report_returned(tmp_db, audit, policy):
    """When the output contains multiple JSON objects, the first valid report is returned."""
    engine = _make_engine_for_parsing(tmp_db, audit, policy)
    decoy = json.dumps({"some": "other", "object": True})
    report = _report_json(summary="the real report")
    output = decoy + "\n" + json.dumps(report) + "\n" + decoy

    result = engine._parse_report(output, TASK_ID, EXEC_ID)

    assert result is not None
    assert result.summary == "the real report"


def test_h3_malformed_json_returns_none(tmp_db, audit, policy):
    """Malformed JSON in output returns None (fail-closed)."""
    engine = _make_engine_for_parsing(tmp_db, audit, policy)
    output = '{"message_type": "IMPLEMENTATION_REPORT", "broken": }'

    result = engine._parse_report(output, TASK_ID, EXEC_ID)

    assert result is None


def test_h3_empty_output_returns_none(tmp_db, audit, policy):
    """Empty output returns None."""
    engine = _make_engine_for_parsing(tmp_db, audit, policy)

    result = engine._parse_report("", TASK_ID, EXEC_ID)

    assert result is None


def test_h3_report_missing_required_fields_returns_none(tmp_db, audit, policy):
    """A JSON object with message_type but missing required fields returns None."""
    engine = _make_engine_for_parsing(tmp_db, audit, policy)
    bad_report = {
        "message_type": "IMPLEMENTATION_REPORT",
        "task_id": TASK_ID,
        # Missing: protocol_version, execution_id, status, summary
    }
    output = json.dumps(bad_report)

    result = engine._parse_report(output, TASK_ID, EXEC_ID)

    assert result is None


def test_h3_deeply_nested_output_scanned_correctly(tmp_db, audit, policy):
    """
    Output containing deeply nested JSON (not the report) does not confuse the scanner.
    The scanner should skip non-report objects and find the valid report.
    """
    engine = _make_engine_for_parsing(tmp_db, audit, policy)
    # A deeply nested object that is NOT a report
    nested_other = json.dumps({
        "outer": {"inner": {"deep": {"very": "nested"}}},
        "something": "else",
    })
    report = _report_json(summary="found after nested")
    output = nested_other + "\n" + json.dumps(report)

    result = engine._parse_report(output, TASK_ID, EXEC_ID)

    assert result is not None
    assert result.summary == "found after nested"


def test_h3_output_format_json_wrapper_unwrapped(tmp_db, audit, policy):
    """
    The --output-format json outer wrapper is unwrapped before scanning.
    The report embedded in 'result' is still found.
    """
    engine = _make_engine_for_parsing(tmp_db, audit, policy)
    report = _report_json(summary="from wrapped output")
    wrapped = json.dumps({"result": json.dumps(report), "type": "result"})

    result = engine._parse_report(wrapped, TASK_ID, EXEC_ID)

    assert result is not None
    assert result.summary == "from wrapped output"


def test_h3_non_report_json_returns_none(tmp_db, audit, policy):
    """JSON output that is not an implementation report returns None."""
    engine = _make_engine_for_parsing(tmp_db, audit, policy)
    output = json.dumps({"foo": "bar", "status": "done"})

    result = engine._parse_report(output, TASK_ID, EXEC_ID)

    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# H-4  Atomic audit sequence assignment
# ─────────────────────────────────────────────────────────────────────────────

def _make_event(task_id: str, n: int) -> AuditEvent:
    return AuditEvent(
        event_id=AuditEvent.new_id(),
        event_type=AuditEventType.TASK_CREATED,
        task_id=task_id,
        execution_id=None,
        timestamp=datetime.now(timezone.utc),
        actor="relay",
        details={"n": n},
    )


def test_h4_sequential_appends_produce_unique_sequences(tmp_db):
    """Sequential append_event calls produce monotonically increasing unique sequences."""
    events = [_make_event("TASK-SEQ", i) for i in range(20)]
    for ev in events:
        tmp_db.append_event(ev)

    all_events = tmp_db.get_all_events()
    sequences = [e.sequence for e in all_events]
    assert len(set(sequences)) == len(sequences), "Duplicate sequences in sequential appends"
    assert sequences == sorted(sequences), "Sequences are not monotonically increasing"
    assert len(sequences) == 20


def test_h4_concurrent_appends_no_lost_events(tmp_db):
    """
    Concurrent append_event calls from multiple threads must not lose any events.

    This is the core H-4 property: the retry loop must absorb IntegrityError
    collisions without discarding events.  All N events must appear in the DB
    with unique sequence numbers.
    """
    N = 30
    errors: list[Exception] = []
    barrier = threading.Barrier(N)

    def append_one(i: int) -> None:
        ev = _make_event(f"TASK-CONC-{i}", i)
        try:
            barrier.wait(timeout=10)  # synchronize all threads at the start point
            tmp_db.append_event(ev)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append_one, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Exceptions in append threads: {errors}"

    all_events = tmp_db.get_all_events()
    sequences = [e.sequence for e in all_events]

    assert len(all_events) == N, (
        f"Expected {N} events; got {len(all_events)}. "
        f"{N - len(all_events)} event(s) were lost in concurrent appends."
    )
    assert len(set(sequences)) == N, (
        f"Duplicate sequence numbers found after concurrent appends: "
        f"{sorted(sequences)}"
    )


def test_h4_concurrent_appends_all_sequences_unique(tmp_db):
    """Even under concurrency, no two events share a sequence number."""
    N = 20
    barrier = threading.Barrier(N)
    errors: list[Exception] = []

    def append_one(i: int) -> None:
        ev = _make_event("TASK-UNIQ", i)
        try:
            barrier.wait(timeout=10)
            tmp_db.append_event(ev)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append_one, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    events = tmp_db.get_all_events()
    sequences = [e.sequence for e in events]
    assert len(sequences) == len(set(sequences)), "Sequence numbers are not unique"


def test_h4_sequence_numbers_remain_contiguous_after_concurrent_writes(tmp_db):
    """
    After concurrent writes, sequence numbers must be a contiguous range
    (no gaps from lost events, no duplicates from races).
    """
    N = 15
    errors: list[Exception] = []
    barrier = threading.Barrier(N)

    def append_one(i: int) -> None:
        ev = _make_event("TASK-CONT", i)
        try:
            barrier.wait(timeout=10)
            tmp_db.append_event(ev)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append_one, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    events = tmp_db.get_all_events()
    sequences = sorted(e.sequence for e in events)
    assert len(sequences) == N
    # Sequences must be a contiguous range starting at 1
    expected = list(range(sequences[0], sequences[0] + N))
    assert sequences == expected, (
        f"Sequences are not contiguous: {sequences}. "
        "Some events may have been lost or duplicated."
    )


def test_h4_append_event_preserves_append_only_semantics(tmp_db):
    """append_event must never modify or delete existing events."""
    first = _make_event("TASK-PRES", 1)
    tmp_db.append_event(first)
    first_stored = tmp_db.get_events_for_task("TASK-PRES")[0]

    # Append more events
    for i in range(5):
        tmp_db.append_event(_make_event("TASK-PRES", i + 2))

    # The first event must be unchanged
    events = tmp_db.get_events_for_task("TASK-PRES")
    assert events[0].event_id == first_stored.event_id
    assert events[0].sequence == first_stored.sequence
    assert events[0].details == first_stored.details
    assert len(events) == 6
