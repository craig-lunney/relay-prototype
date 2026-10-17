"""
Tests for the worker prompt's tool and command guidance (Batch 8).

The v1 real run (docs/acceptance/real-run-2026-10-16.md) fixed the bug early,
then spent six turns on `python -c` checks and a `_verify.py` file that the
allowlist denied, and hit --max-turns.  The prompt now tells the worker what
the allowlist permits, that numeric criteria are checked by the human, and to
stop once the tests pass.  These tests keep that guidance present and in step
with the --allowedTools value Relay actually passes.
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

from relay.executor import (
    WORKER_ALLOWED_TOOLS,
    ExecutionEngine,
    _allowed_shell_commands,
)


def _capture_launch(engine, task, tmp_path) -> tuple[list[str], str]:
    """Run engine.run() under a spy Popen; return (cmd, prompt sent on stdin)."""
    launches: list[tuple[list[str], MagicMock]] = []

    def spy(cmd, **kwargs):
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        launches.append((list(cmd), proc))
        return proc

    with patch("subprocess.Popen", side_effect=spy):
        engine.run(task, tmp_path / "ws")

    assert launches, "subprocess.Popen was never called"
    cmd, proc = launches[0]
    prompt = proc.communicate.call_args.kwargs["input"]
    return cmd, prompt


def _allowed_tools_in(cmd: list[str]) -> str:
    return cmd[cmd.index("--allowedTools") + 1]


def _shell_line(prompt: str) -> str:
    lines = [l for l in prompt.splitlines() if "The only shell commands allowed are" in l]
    assert len(lines) == 1, f"Expected one allowed-commands line, found {lines!r}"
    return lines[0]


# ── Allowlist parsing ─────────────────────────────────────────────────────────

def test_allowed_shell_commands_from_fixed_allowlist():
    assert _allowed_shell_commands() == ["python -m pytest", "pytest"]


def test_allowed_shell_commands_ignores_non_bash_tools():
    assert _allowed_shell_commands("Read Edit") == []
    assert _allowed_shell_commands("Read Bash(git status)") == ["git status"]


# ── Prompt content ────────────────────────────────────────────────────────────

def test_prompt_has_tools_and_commands_section(tmp_db, audit, policy, sample_task):
    engine = ExecutionEngine(tmp_db, audit, policy)
    prompt = engine._build_prompt(sample_task, "EXEC-TEST")
    assert "TOOLS AND COMMANDS" in prompt


def test_prompt_names_only_the_pytest_commands(tmp_db, audit, policy, sample_task):
    engine = ExecutionEngine(tmp_db, audit, policy)
    line = _shell_line(engine._build_prompt(sample_task, "EXEC-TEST"))
    named = re.findall(r"`([^`]*?) \.\.\.`", line)
    assert named == ["python -m pytest", "pytest"], line


def test_prompt_says_other_commands_and_scripts_are_denied(
    tmp_db, audit, policy, sample_task
):
    engine = ExecutionEngine(tmp_db, audit, policy)
    prompt = engine._build_prompt(sample_task, "EXEC-TEST")
    assert "Every other command will be denied" in prompt
    assert "python -c" in prompt
    assert "ad-hoc" in prompt and "will also be denied" in prompt


def test_prompt_leaves_non_test_criteria_to_the_human(
    tmp_db, audit, policy, sample_task
):
    engine = ExecutionEngine(tmp_db, audit, policy)
    prompt = engine._build_prompt(sample_task, "EXEC-TEST")
    assert 'other than "the test suite passes" are checked by the human' in prompt
    assert "Do not try to verify them by other means" in prompt


def test_prompt_says_stop_once_tests_pass(tmp_db, audit, policy, sample_task):
    engine = ExecutionEngine(tmp_db, audit, policy)
    prompt = engine._build_prompt(sample_task, "EXEC-TEST")
    assert "Stop once the required tests pass" in prompt


def test_guidance_comes_before_report_template(tmp_db, audit, policy, sample_task):
    """The report template stays last so the report remains the final instruction."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    prompt = engine._build_prompt(sample_task, "EXEC-TEST")
    assert prompt.index("TOOLS AND COMMANDS") < prompt.index('"message_type"')


# ── Prompt matches the allowlist Relay actually launches with ─────────────────

def test_launched_prompt_matches_launched_allowlist(
    tmp_db, audit, policy, sample_task, tmp_path
):
    engine = ExecutionEngine(tmp_db, audit, policy)
    cmd, prompt = _capture_launch(engine, sample_task, tmp_path)

    value = _allowed_tools_in(cmd)
    assert value == WORKER_ALLOWED_TOOLS
    assert f"Your tool allowlist is exactly: {value}" in prompt

    named = re.findall(r"`([^`]*?) \.\.\.`", _shell_line(prompt))
    assert named == _allowed_shell_commands(value)
    for pattern in re.findall(r"Bash\([^)]*\)", value):
        assert pattern[len("Bash("):-len(" *)")] in named, pattern
