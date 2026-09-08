"""
Tests for P1-I authentication compatibility fixes.

These tests verify:
  AC-1  Command construction: '--' separator, prompt position, auth settings
  AC-2  Environment isolation: RELAY_API_KEY_FILE never enters the worker env
  AC-3  Auth failure modes: fail-closed when RELAY_API_KEY_FILE is unconfigured
  AC-4  Helper script: lives in relay/ (not workspace / worker_config), standalone
  AC-5  _build_auth_settings() unit tests

The autouse fixture in conftest.py sets RELAY_API_KEY_FILE for all tests.
Tests that verify failure paths call monkeypatch.delenv() after the fixture runs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from relay.executor import (
    RELAY_WORKER_CONFIG_DIR,
    ExecutionEngine,
    _build_auth_settings,
    _worker_environment,
)
from relay.models import ExecutionStatus


# ── Shared test helpers ───────────────────────────────────────────────────────

def _make_spy_popen():
    """Return (factory, captured_list) — intercepts subprocess.Popen calls."""
    captured: list[dict] = []

    def factory(cmd, **kwargs):
        captured.append({"cmd": list(cmd), "env": kwargs.get("env"), "kwargs": kwargs})
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    return factory, captured


def _run_and_capture(engine, sample_task, tmp_path):
    """Run engine.run() under a spy Popen; return (execution, captured)."""
    spy, captured = _make_spy_popen()
    with patch("subprocess.Popen", side_effect=spy):
        execution = engine.run(sample_task, tmp_path / "ws")
    return execution, captured


def _parse_settings_json(cmd: list[str]) -> dict:
    """Extract and parse the JSON value immediately after --settings in cmd."""
    assert "--settings" in cmd, "--settings not found in command"
    idx = cmd.index("--settings")
    return json.loads(cmd[idx + 1])


# ── AC-1: Command construction ────────────────────────────────────────────────

def test_ac1_prompt_via_stdin_not_command_arg(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """The task prompt must NOT appear in the command argument vector.

    P1-I/Fix1: On Windows, multi-line strings in command-line arguments are
    unreliable (newlines in argv are not preserved by CommandLineToArgvW).
    The prompt is delivered via stdin (communicate(input=prompt)) instead.
    This also prevents task content from appearing in process listings.
    """
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured, "subprocess.Popen was never called"
    cmd = captured[0]["cmd"]
    full_cmd_str = " ".join(cmd)
    assert sample_task.objective not in full_cmd_str, (
        "Task objective appeared in the command argument vector. "
        "Prompt must be delivered via stdin, not as a command-line argument."
    )


def test_ac1_stdin_pipe_established(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """subprocess.Popen must be called with stdin=subprocess.PIPE.

    This ensures the prompt can be written to the worker process via
    communicate(input=prompt) without waiting for terminal input.
    """
    import subprocess as _subprocess
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    popen_kwargs = captured[0]["kwargs"]
    assert popen_kwargs.get("stdin") == _subprocess.PIPE, (
        f"subprocess.Popen was called with stdin={popen_kwargs.get('stdin')!r}. "
        "Expected stdin=subprocess.PIPE so the prompt can be delivered via communicate()."
    )


def test_ac1_last_cmd_element_is_worker_config(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """The last element of the command must be the worker config path (via --add-dir).

    With the stdin delivery approach, there is no trailing '--' separator or prompt
    argument in the command vector. The worker config is the last meaningful argument.
    """
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    assert "--add-dir" in cmd
    add_dir_idx = cmd.index("--add-dir")
    worker_config_in_cmd = cmd[add_dir_idx + 1]
    assert cmd[-1] == worker_config_in_cmd, (
        f"Last command element {cmd[-1]!r} is not the worker config path. "
        "No trailing prompt or '--' separator should follow --add-dir."
    )


def test_ac1_settings_flag_present(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """--settings must be present so Claude can receive the apiKeyHelper."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    assert "--settings" in cmd, (
        "--settings is missing from the Claude command. "
        "Claude cannot receive the apiKeyHelper configuration."
    )


def test_ac1_settings_json_has_api_key_helper(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """The --settings JSON must contain an apiKeyHelper entry."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    settings = _parse_settings_json(cmd)
    assert "apiKeyHelper" in settings, (
        f"apiKeyHelper key missing from --settings JSON. Got keys: {list(settings.keys())}"
    )
    assert isinstance(settings["apiKeyHelper"], str), (
        "apiKeyHelper value must be a command string."
    )
    assert settings["apiKeyHelper"].strip(), "apiKeyHelper command must not be empty."


def test_ac1_api_key_helper_uses_sys_executable(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """apiKeyHelper command must use sys.executable (the relay interpreter)."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    settings = _parse_settings_json(cmd)
    helper_cmd = settings["apiKeyHelper"]

    # sys.executable is embedded as a double-quoted path component.
    assert sys.executable in helper_cmd, (
        f"sys.executable ({sys.executable!r}) not found in apiKeyHelper command: "
        f"{helper_cmd!r}. A hardcoded path was used instead."
    )


def test_ac1_api_key_helper_references_helper_script(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """apiKeyHelper command must reference api_key_helper.py."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    settings = _parse_settings_json(cmd)
    helper_cmd = settings["apiKeyHelper"]

    assert "api_key_helper.py" in helper_cmd, (
        f"api_key_helper.py not referenced in apiKeyHelper command: {helper_cmd!r}"
    )


def test_ac1_raw_key_not_in_settings_json(
    tmp_db, audit, policy, sample_task, tmp_path, _relay_api_key_file
):
    """The raw API key value must never appear in the --settings JSON."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    # Read the fake key that the autouse fixture wrote.
    fake_key = _relay_api_key_file.read_text(encoding="ascii").strip()
    settings_str = cmd[cmd.index("--settings") + 1]

    assert fake_key not in settings_str, (
        "Raw API key content appeared inside the --settings JSON. "
        "The key must reach Claude only via the apiKeyHelper runtime call."
    )


def test_ac1_raw_key_not_in_full_command(
    tmp_db, audit, policy, sample_task, tmp_path, _relay_api_key_file
):
    """The raw API key value must not appear anywhere in the command list."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    fake_key = _relay_api_key_file.read_text(encoding="ascii").strip()
    full_cmd_str = " ".join(cmd)

    assert fake_key not in full_cmd_str, (
        "Raw API key content appeared in the subprocess command. "
        "Keys must never be embedded in command arguments."
    )


def test_ac1_settings_before_add_dir(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """--settings must appear before --add-dir so the auth flag is processed first."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    assert "--settings" in cmd and "--add-dir" in cmd
    settings_idx = cmd.index("--settings")
    add_dir_idx = cmd.index("--add-dir")
    assert settings_idx < add_dir_idx, (
        f"--settings ({settings_idx}) must precede --add-dir ({add_dir_idx}). "
        "Current ordering may affect argument parsing."
    )


def test_ac1_add_dir_value_is_worker_config(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """--add-dir must point at the Relay worker config directory (P1-A regression)."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    assert "--add-dir" in cmd
    idx = cmd.index("--add-dir")
    actual = Path(cmd[idx + 1]).resolve()
    expected = RELAY_WORKER_CONFIG_DIR.resolve()
    assert actual == expected, (
        f"--add-dir value {actual!r} != expected worker config {expected!r}. "
        "P1-A worker config isolation regression."
    )


def test_ac1_bare_still_present(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """--bare must remain in the command after P1-I auth changes (P1-A regression)."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    assert "--bare" in cmd, (
        "--bare is missing from the Claude command after P1-I changes. "
        "P1-A CLAUDE.md auto-discovery isolation regression."
    )


# ── AC-2: Environment isolation ───────────────────────────────────────────────

def test_ac2_relay_api_key_file_excluded_from_worker_env(monkeypatch, tmp_path):
    """RELAY_API_KEY_FILE must not appear in the curated worker environment dict."""
    # The autouse fixture has already set RELAY_API_KEY_FILE.
    env = _worker_environment()
    assert "RELAY_API_KEY_FILE" not in env, (
        "RELAY_API_KEY_FILE appeared in the curated worker environment. "
        "The API key file path must not cross the Relay → worker boundary."
    )


def test_ac2_relay_api_key_file_excluded_at_popen_boundary(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """RELAY_API_KEY_FILE must be absent from the env= dict passed to subprocess.Popen."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    popen_env = captured[0].get("env") or {}
    assert "RELAY_API_KEY_FILE" not in popen_env, (
        "RELAY_API_KEY_FILE appeared in the Popen env= at the subprocess boundary. "
        "The key file path must not reach the worker process."
    )


def test_ac2_anthropic_api_key_still_excluded(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """ANTHROPIC_API_KEY must remain excluded from the worker env (P1-G regression)."""
    captured_envs: list[dict] = []

    def spy_popen(cmd, **kwargs):
        captured_envs.append(dict(kwargs.get("env") or {}))
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=spy_popen):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-regression-test"}):
            engine.run(sample_task, tmp_path / "ws")

    assert captured_envs
    assert "ANTHROPIC_API_KEY" not in captured_envs[0], (
        "ANTHROPIC_API_KEY appeared in the Popen env after P1-I changes. "
        "P1-G credential exclusion regression."
    )


# ── AC-3: Auth failure modes (fail-closed) ────────────────────────────────────

def test_ac3_missing_env_var_fails_closed(
    tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
):
    """When RELAY_API_KEY_FILE is not set, execution must fail without calling Popen."""
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)

    popen_called = []
    def spy_popen(cmd, **kwargs):
        popen_called.append(True)
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=spy_popen):
        execution = engine.run(sample_task, tmp_path / "ws")

    assert execution.status == ExecutionStatus.FAILED, (
        f"Expected FAILED when RELAY_API_KEY_FILE is missing, got {execution.status}"
    )
    assert not popen_called, (
        "subprocess.Popen was called despite missing RELAY_API_KEY_FILE. "
        "Auth check must fail closed before the subprocess is launched."
    )


def test_ac3_nonexistent_file_fails_closed(
    tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
):
    """When RELAY_API_KEY_FILE points to a nonexistent file, execution must fail closed."""
    monkeypatch.setenv("RELAY_API_KEY_FILE", str(tmp_path / "no_such_key_file.txt"))

    popen_called = []
    def spy_popen(cmd, **kwargs):
        popen_called.append(True)
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=spy_popen):
        execution = engine.run(sample_task, tmp_path / "ws")

    assert execution.status == ExecutionStatus.FAILED, (
        f"Expected FAILED when key file is missing, got {execution.status}"
    )
    assert not popen_called, (
        "subprocess.Popen was called despite pointing at a nonexistent key file."
    )


def test_ac3_error_message_mentions_relay_api_key_file(
    tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
):
    """Failure error message must name RELAY_API_KEY_FILE to aid diagnosis."""
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)

    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=MagicMock()):
        execution = engine.run(sample_task, tmp_path / "ws")

    assert execution.error is not None
    assert "RELAY_API_KEY_FILE" in execution.error, (
        f"Error message does not mention RELAY_API_KEY_FILE: {execution.error!r}. "
        "Operators need clear guidance on which variable to set."
    )


def test_ac3_error_message_does_not_contain_key_material(
    tmp_db, audit, policy, sample_task, tmp_path, monkeypatch, _relay_api_key_file
):
    """If auth fails, the error message must not contain the key content."""
    # Point to a real file so we test the "file readable but wrong content" scenario.
    # Even in other failure paths the key content should never leak into error messages.
    bad_key_file = tmp_path / "bad_key.txt"
    bad_key_file.write_text("sk-ant-sensitive-key-material", encoding="ascii")
    monkeypatch.setenv("RELAY_API_KEY_FILE", str(bad_key_file))

    # Override helper_script resolution to simulate a packaging error.
    with patch("relay.executor.Path.is_file", side_effect=lambda self=None: False):
        flags, error = _build_auth_settings()

    # Whether it fails or not, verify the key content is absent from any error.
    if error is not None:
        assert "sk-ant-sensitive-key-material" not in error, (
            "Raw key material appeared in the _build_auth_settings error message."
        )


def test_ac3_missing_env_emits_failed_status_in_db(
    tmp_db, audit, policy, sample_task, tmp_path, monkeypatch
):
    """Execution FAILED due to missing auth must be persisted to the state DB."""
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)

    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=MagicMock()):
        execution = engine.run(sample_task, tmp_path / "ws")

    saved = tmp_db.get_execution(execution.execution_id)
    assert saved is not None
    assert saved.status == ExecutionStatus.FAILED


# ── AC-4: Helper script location and independence ─────────────────────────────

def test_ac4_helper_not_in_worker_config_dir():
    """api_key_helper.py must NOT live inside the Relay worker config directory."""
    worker_config = RELAY_WORKER_CONFIG_DIR
    helper_in_worker_config = worker_config / "api_key_helper.py"
    assert not helper_in_worker_config.exists(), (
        f"api_key_helper.py found inside worker_config at {helper_in_worker_config}. "
        "The helper must not be in the directory visible to the worker process."
    )


def test_ac4_helper_in_relay_package_dir():
    """api_key_helper.py must exist in the relay package directory (next to executor.py)."""
    from relay import executor
    relay_pkg_dir = Path(executor.__file__).parent
    helper = relay_pkg_dir / "api_key_helper.py"
    assert helper.is_file(), (
        f"api_key_helper.py not found in relay package dir {relay_pkg_dir}. "
        "This is a packaging error."
    )


def test_ac4_helper_imports_only_stdlib():
    """api_key_helper.py must not import from the relay package or any third-party lib."""
    from relay import executor
    relay_pkg_dir = Path(executor.__file__).parent
    helper = relay_pkg_dir / "api_key_helper.py"
    source = helper.read_text(encoding="utf-8")

    # Simple structural check: no 'from relay' or 'import relay'.
    assert "from relay" not in source, (
        "api_key_helper.py imports from the relay package. "
        "The helper must be standalone and workspace-independent."
    )
    assert "import relay" not in source, (
        "api_key_helper.py imports the relay module. "
        "The helper must be standalone."
    )


def test_ac4_helper_reads_no_env_vars():
    """api_key_helper.py must not read environment variables."""
    from relay import executor
    relay_pkg_dir = Path(executor.__file__).parent
    helper = relay_pkg_dir / "api_key_helper.py"
    source = helper.read_text(encoding="utf-8")

    assert "os.environ" not in source, (
        "api_key_helper.py references os.environ. "
        "The helper must be solely driven by its argv[1] argument."
    )
    assert "os.getenv" not in source, (
        "api_key_helper.py calls os.getenv. "
        "The helper must be solely driven by its argv[1] argument."
    )


def test_ac4_apiKeyHelper_command_contains_key_file_path(
    tmp_db, audit, policy, sample_task, tmp_path, _relay_api_key_file
):
    """The apiKeyHelper command must include the key file path as the final argument."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    settings = _parse_settings_json(cmd)
    helper_cmd = settings["apiKeyHelper"]

    # The resolved key file path must appear in the helper command.
    expected_path = str(_relay_api_key_file.resolve())
    assert expected_path in helper_cmd, (
        f"Key file path {expected_path!r} not found in apiKeyHelper command: "
        f"{helper_cmd!r}"
    )


# ── AC-5: _build_auth_settings() unit tests ───────────────────────────────────

def test_build_auth_settings_returns_flags_when_configured(tmp_path, monkeypatch):
    """_build_auth_settings() returns a non-empty flag list when configured correctly."""
    key_file = tmp_path / "test_api_key.txt"
    key_file.write_text("sk-ant-fake-unit-test-key", encoding="ascii")
    monkeypatch.setenv("RELAY_API_KEY_FILE", str(key_file))

    flags, error = _build_auth_settings()

    assert error is None, f"Expected no error, got: {error!r}"
    assert flags is not None, "Expected flags list, got None"
    assert isinstance(flags, list), f"Expected list, got {type(flags)}"
    assert len(flags) > 0, "Flags list must not be empty"


def test_build_auth_settings_returns_error_when_env_var_missing(monkeypatch):
    """_build_auth_settings() returns (None, error) when RELAY_API_KEY_FILE is absent."""
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)

    flags, error = _build_auth_settings()

    assert flags is None, f"Expected None flags, got: {flags!r}"
    assert error is not None, "Expected an error message"
    assert isinstance(error, str) and error.strip(), "Error must be a non-empty string"


def test_build_auth_settings_returns_error_when_file_missing(tmp_path, monkeypatch):
    """_build_auth_settings() returns (None, error) when the key file does not exist."""
    monkeypatch.setenv("RELAY_API_KEY_FILE", str(tmp_path / "nonexistent.txt"))

    flags, error = _build_auth_settings()

    assert flags is None, f"Expected None flags, got: {flags!r}"
    assert error is not None


def test_build_auth_settings_flags_are_settings_pair(tmp_path, monkeypatch):
    """_build_auth_settings() must return exactly ['--settings', <json>] on success."""
    key_file = tmp_path / "test_api_key.txt"
    key_file.write_text("sk-ant-fake-unit-test-key", encoding="ascii")
    monkeypatch.setenv("RELAY_API_KEY_FILE", str(key_file))

    flags, error = _build_auth_settings()

    assert error is None
    assert flags is not None
    assert len(flags) == 2, f"Expected 2-element list ['--settings', json], got {flags!r}"
    assert flags[0] == "--settings"
    # Second element must be valid JSON.
    parsed = json.loads(flags[1])
    assert isinstance(parsed, dict)


def test_build_auth_settings_json_is_valid(tmp_path, monkeypatch):
    """The --settings value from _build_auth_settings() must be valid JSON."""
    key_file = tmp_path / "test_api_key.txt"
    key_file.write_text("sk-ant-fake-unit-test-key", encoding="ascii")
    monkeypatch.setenv("RELAY_API_KEY_FILE", str(key_file))

    flags, error = _build_auth_settings()
    assert error is None and flags is not None

    try:
        parsed = json.loads(flags[1])
    except json.JSONDecodeError as exc:
        pytest.fail(f"--settings value is not valid JSON: {exc}")

    assert "apiKeyHelper" in parsed


def test_build_auth_settings_error_names_relay_api_key_file(monkeypatch):
    """The error message from a missing env var must name RELAY_API_KEY_FILE."""
    monkeypatch.delenv("RELAY_API_KEY_FILE", raising=False)

    flags, error = _build_auth_settings()

    assert error is not None
    assert "RELAY_API_KEY_FILE" in error, (
        f"Error message does not mention RELAY_API_KEY_FILE: {error!r}"
    )


def test_build_auth_settings_spaces_in_path_handled(tmp_path, monkeypatch):
    """_build_auth_settings() must handle key file paths that contain spaces."""
    spaced_dir = tmp_path / "my keys dir"
    spaced_dir.mkdir()
    key_file = spaced_dir / "api key.txt"
    key_file.write_text("sk-ant-fake-key", encoding="ascii")
    monkeypatch.setenv("RELAY_API_KEY_FILE", str(key_file))

    flags, error = _build_auth_settings()

    # A path with spaces is valid as long as it has no double-quotes.
    # The implementation wraps each component in double quotes.
    assert error is None, f"Space in key file path caused unexpected error: {error!r}"
    assert flags is not None


# ── AC-6: --allowedTools permission mechanism (P1-I) ─────────────────────────
#
# These tests verify the narrowly-scoped tool allowlist added to unblock the
# worker from denied Bash/pytest calls in --print --bare mode.  They also act
# as regression guards for the safety boundaries that must not widen.

_APPROVED_ALLOWED_TOOLS_VALUE = "Read Edit Bash(python -m pytest *) Bash(pytest *)"


def _get_allowed_tools_value(cmd: list[str]) -> str:
    """Return the value immediately after --allowedTools in cmd, or '' if absent."""
    if "--allowedTools" not in cmd:
        return ""
    idx = cmd.index("--allowedTools")
    return cmd[idx + 1] if idx + 1 < len(cmd) else ""


def test_ac6_allowed_tools_present_exactly_once(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """--allowedTools must appear exactly once in the command."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    count = cmd.count("--allowedTools")
    assert count == 1, (
        f"Expected --allowedTools to appear exactly once, found {count} times. "
        f"Command: {cmd}"
    )


def test_ac6_allowed_tools_value_is_exact(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """The --allowedTools value must match exactly the approved string."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    value = _get_allowed_tools_value(cmd)
    assert value == _APPROVED_ALLOWED_TOOLS_VALUE, (
        f"--allowedTools value {value!r} does not match approved value "
        f"{_APPROVED_ALLOWED_TOOLS_VALUE!r}. "
        "Do not widen or narrow permissions without explicit approval."
    )


def test_ac6_read_permission_present(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """Read must be in the --allowedTools value."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    value = _get_allowed_tools_value(cmd)
    tools = value.split()
    assert "Read" in tools, f"'Read' missing from --allowedTools: {value!r}"


def test_ac6_edit_permission_present(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """Edit must be in the --allowedTools value."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    value = _get_allowed_tools_value(cmd)
    tools = value.split()
    assert "Edit" in tools, f"'Edit' missing from --allowedTools: {value!r}"


def test_ac6_pytest_bash_permission_present(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """Both pytest Bash patterns must be in the --allowedTools value."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    value = _get_allowed_tools_value(cmd)
    assert "Bash(python -m pytest *)" in value, (
        f"'Bash(python -m pytest *)' missing from --allowedTools: {value!r}"
    )
    assert "Bash(pytest *)" in value, (
        f"'Bash(pytest *)' missing from --allowedTools: {value!r}"
    )


def test_ac6_no_dangerously_skip_permissions(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """--dangerously-skip-permissions must never appear in the command."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    full_cmd_str = " ".join(cmd)
    assert "--dangerously-skip-permissions" not in full_cmd_str, (
        "--dangerously-skip-permissions found in Claude command. "
        "This flag is explicitly prohibited by the P1-I safety boundary."
    )


def test_ac6_no_bypass_permissions_mode(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """--permission-mode bypassPermissions must not appear in the command."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    full_cmd_str = " ".join(cmd)
    assert "bypassPermissions" not in full_cmd_str, (
        "'bypassPermissions' found in Claude command. "
        "This permission mode is explicitly prohibited."
    )


def test_ac6_no_auto_permission_mode(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """--permission-mode auto must not appear in the command."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    # Check that --permission-mode is absent entirely, OR that its value is not 'auto'.
    if "--permission-mode" in cmd:
        idx = cmd.index("--permission-mode")
        mode_value = cmd[idx + 1] if idx + 1 < len(cmd) else ""
        assert mode_value != "auto", (
            f"--permission-mode auto found in Claude command. "
            "auto mode is equivalent to bypassPermissions and is prohibited."
        )


def _parse_allowed_tools_tokens(value: str) -> list[str]:
    """Parse --allowedTools value into individual tool tokens.

    Uses a regex that matches a word followed by an optional parenthesised group,
    so patterns like 'Bash(python -m pytest *)' (which contain spaces inside the
    parentheses) are treated as a single token rather than split on whitespace.
    """
    import re
    return re.findall(r'\w+(?:\([^)]*\))?', value)


def test_ac6_no_broader_bash_permission(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """No Bash pattern broader than the two approved pytest patterns may be present.

    Checks that bare 'Bash', 'Bash(*)', 'PowerShell', and other broad patterns
    are absent from the --allowedTools value.
    """
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    value = _get_allowed_tools_value(cmd)

    # Parse tokens respecting parenthesised groups with internal spaces.
    tokens = _parse_allowed_tools_tokens(value)

    bash_tokens = [t for t in tokens if t.lower().startswith("bash")]
    approved_bash = {"Bash(python -m pytest *)", "Bash(pytest *)"}
    for token in bash_tokens:
        assert token in approved_bash, (
            f"Unapproved Bash permission {token!r} found in --allowedTools. "
            f"Only {approved_bash} are permitted."
        )

    # PowerShell must not be in the allowlist.
    assert "PowerShell" not in value, (
        "PowerShell found in --allowedTools. "
        "PowerShell execution was not approved for the worker."
    )

    # No bare 'Bash' token (without parenthesised scope).
    assert "Bash" not in tokens, (
        "Bare 'Bash' token found in --allowedTools — this permits all Bash commands. "
        "Only the two scoped pytest patterns are approved."
    )


def test_ac6_bare_flag_preserved(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """--bare must remain after adding --allowedTools (P1-A regression guard)."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    assert "--bare" in cmd, (
        "--bare is missing after adding --allowedTools. "
        "P1-A CLAUDE.md auto-discovery isolation regression."
    )


def test_ac6_api_key_helper_settings_preserved(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """The apiKeyHelper --settings mechanism must be intact after adding --allowedTools."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    assert "--settings" in cmd, "--settings missing after adding --allowedTools"
    settings = _parse_settings_json(cmd)
    assert "apiKeyHelper" in settings, (
        f"apiKeyHelper missing from --settings JSON after adding --allowedTools. "
        f"Got: {list(settings.keys())}"
    )


def test_ac6_prompt_still_via_stdin_not_command_arg(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """Prompt delivery must remain via stdin — no '--' positional separator in cmd.

    The P1-I/Fix1 stdin approach must not be reverted to positional-argument delivery.
    """
    import subprocess as _subprocess

    engine = ExecutionEngine(tmp_db, audit, policy)

    spy_kwargs: list[dict] = []

    def spy_popen(cmd, **kwargs):
        spy_kwargs.append(kwargs)
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    with patch("subprocess.Popen", side_effect=spy_popen):
        engine.run(sample_task, tmp_path / "ws")

    assert spy_kwargs, "subprocess.Popen was never called"
    kwargs = spy_kwargs[0]

    # stdin must still be PIPE.
    assert kwargs.get("stdin") == _subprocess.PIPE, (
        f"stdin={kwargs.get('stdin')!r} — expected PIPE. "
        "P1-I/Fix1 stdin delivery regression."
    )

    # No standalone '--' separator should exist (prompt is not a positional arg).
    _, captured = _make_spy_popen()
    engine2 = ExecutionEngine(tmp_db, audit, policy)
    _, captured2 = _run_and_capture(engine2, sample_task, tmp_path)
    if captured2:
        cmd = captured2[0]["cmd"]
        assert "--" not in cmd, (
            "'--' separator found in Claude command. "
            "Prompt must be delivered via stdin, not as a positional argument."
        )


def test_ac6_worker_env_hardening_unchanged(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """RELAY_API_KEY_FILE and ANTHROPIC_API_KEY must remain excluded from worker env."""
    captured_envs: list[dict] = []

    def spy_popen(cmd, **kwargs):
        captured_envs.append(dict(kwargs.get("env") or {}))
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    engine = ExecutionEngine(tmp_db, audit, policy)
    with patch("subprocess.Popen", side_effect=spy_popen):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-env-regression"}):
            engine.run(sample_task, tmp_path / "ws")

    assert captured_envs
    env = captured_envs[0]
    assert "RELAY_API_KEY_FILE" not in env, (
        "RELAY_API_KEY_FILE in Popen env after --allowedTools addition. "
        "P1-G env hardening regression."
    )
    assert "ANTHROPIC_API_KEY" not in env, (
        "ANTHROPIC_API_KEY in Popen env after --allowedTools addition. "
        "P1-G credential exclusion regression."
    )


def test_ac6_raw_api_key_not_in_allowed_tools_value(
    tmp_db, audit, policy, sample_task, tmp_path, _relay_api_key_file
):
    """Raw API key content must not appear anywhere in the --allowedTools value."""
    engine = ExecutionEngine(tmp_db, audit, policy)
    _, captured = _run_and_capture(engine, sample_task, tmp_path)

    assert captured
    cmd = captured[0]["cmd"]
    value = _get_allowed_tools_value(cmd)
    fake_key = _relay_api_key_file.read_text(encoding="ascii").strip()

    assert fake_key not in value, (
        "Raw API key content found inside --allowedTools value. "
        "Key material must never appear in tool permission strings."
    )
