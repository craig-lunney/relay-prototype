"""
Tests for P1-G: Execution Environment Hardening.

Security property enforced: the Claude worker subprocess must not inherit
arbitrary parent-process environment variables.  Relay constructs an explicit
curated environment using a per-platform allowlist.  Variables not on the
list — including credential-bearing variables and attacker-injected variables
— are excluded by default.  The boundary is allowlist-based, not blacklist-based.

Test groups:
  G1 — Credential / secret variables are excluded
  G2 — Arbitrary / attacker-controlled variables are excluded
  G3 — Required execution variables are preserved
  G4 — The actual subprocess.Popen call receives an explicit env= mapping
  G5 — P1-A worker configuration isolation is unaffected by P1-G changes
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from relay.executor import (
    RELAY_WORKER_CONFIG_DIR,
    ExecutionEngine,
    _WORKER_ENV_ALLOWLIST_POSIX,
    _WORKER_ENV_ALLOWLIST_WINDOWS,
    _worker_environment,
)


def _current_allowlist() -> frozenset[str]:
    return (
        _WORKER_ENV_ALLOWLIST_WINDOWS
        if sys.platform == "win32"
        else _WORKER_ENV_ALLOWLIST_POSIX
    )


def _make_spy_popen():
    """Return (factory, captured_list) for intercepting subprocess.Popen."""
    captured: list[dict] = []

    def factory(cmd, **kwargs):
        captured.append({"cmd": list(cmd), "env": kwargs.get("env"), "kwargs": kwargs})
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    return factory, captured


# ── G1: Credential / secret exclusion ────────────────────────────────────────

def test_g1_anthropic_api_key_excluded():
    """ANTHROPIC_API_KEY must not cross the Relay → worker environment boundary."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-secret-value"}):
        env = _worker_environment()
    assert "ANTHROPIC_API_KEY" not in env, (
        "ANTHROPIC_API_KEY appeared in the worker environment. "
        "Credentials could reach the untrusted Claude subprocess."
    )


def test_g1_database_url_excluded():
    """DATABASE_URL must not cross the Relay → worker environment boundary."""
    with patch.dict(os.environ, {"DATABASE_URL": "postgres://user:password@host/db"}):
        env = _worker_environment()
    assert "DATABASE_URL" not in env


def test_g1_relay_db_path_excluded():
    """RELAY_DB_PATH must not cross the Relay → worker environment boundary."""
    with patch.dict(os.environ, {"RELAY_DB_PATH": "/var/secrets/relay.db"}):
        env = _worker_environment()
    assert "RELAY_DB_PATH" not in env


def test_g1_multiple_credential_variables_all_excluded():
    """A representative batch of credential-bearing variables must all be excluded."""
    secrets = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DATABASE_URL": "postgres://secret@host/db",
        "RELAY_DB_PATH": "/hidden/relay.db",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-key",
        "OPENAI_API_KEY": "sk-openai-secret",
        "GITHUB_TOKEN": "ghp_secret_token",
        "SECRET_KEY": "django-or-flask-secret",
        "PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----",
        "STRIPE_SECRET_KEY": "sk_live_secretstripe",
    }
    with patch.dict(os.environ, secrets):
        env = _worker_environment()
    for key in secrets:
        assert key not in env, (
            f"{key!r} appeared in the worker environment — "
            f"credentials can reach the untrusted Claude subprocess."
        )


# ── G2: Arbitrary / attacker-controlled variable exclusion ───────────────────

def test_g2_attacker_controlled_variable_excluded():
    """
    A clearly arbitrary attacker-controlled variable must not cross the boundary.

    This tests the allowlist property: the variable is not on the list, so it
    must be absent regardless of whether it looks dangerous.
    """
    with patch.dict(os.environ, {"RELAY_ATTACKER_CONTROLLED": "should-not-cross"}):
        env = _worker_environment()
    assert "RELAY_ATTACKER_CONTROLLED" not in env


def test_g2_unknown_variables_excluded_by_default():
    """Variables not on the allowlist are excluded — the allowlist is exhaustive."""
    injected = {
        "MY_CUSTOM_TOOL_VAR": "custom-value",
        "RELAY_INTERNAL_SECRET": "internal",
        "OPERATOR_SESSION_TOKEN": "tok-xyz-abc",
        "WORKSPACE_OVERRIDE": "/attacker/path",
        "CLAUDE_CONFIG_DIR": "/attacker/claude-config",
    }
    with patch.dict(os.environ, injected):
        env = _worker_environment()
    for key in injected:
        assert key not in env, (
            f"Non-allowlisted variable {key!r} appeared in the worker environment. "
            f"The allowlist is not the sole boundary."
        )


def test_g2_only_allowlisted_keys_can_appear():
    """
    Every key present in the worker environment must be on the platform allowlist.

    This is the definitive allowlist check: if this test passes, it is impossible
    for an unknown variable to cross the boundary regardless of what the parent
    environment contains.
    """
    allowlist = _current_allowlist()
    env = _worker_environment()
    for key in env:
        assert key in allowlist, (
            f"Key {key!r} is in the worker environment but NOT on the allowlist. "
            f"An unknown variable crossed the boundary."
        )


# ── G3: Required environment preservation ────────────────────────────────────

def test_g3_path_preserved_when_present():
    """PATH must survive into the worker environment — required to locate executables."""
    test_path = "C:\\Windows\\System32;C:\\Program Files\\Git\\bin"
    with patch.dict(os.environ, {"PATH": test_path}):
        env = _worker_environment()
    assert "PATH" in env, (
        "PATH was not propagated to the worker environment. "
        "The worker cannot locate executables without PATH."
    )
    assert env["PATH"] == test_path


def test_g3_temp_directories_preserved_when_present():
    """
    At least one of TEMP/TMP must survive when present in the parent environment.
    Required for temporary file operations by the Claude subprocess.
    """
    temp_dir = "C:\\Users\\testuser\\AppData\\Local\\Temp"
    with patch.dict(os.environ, {"TEMP": temp_dir, "TMP": temp_dir}):
        env = _worker_environment()
    assert "TEMP" in env or "TMP" in env, (
        "Neither TEMP nor TMP was propagated to the worker environment. "
        "Temporary file operations may fail."
    )


def test_g3_result_is_a_dict_of_strings():
    """
    Worker environment must be a plain dict[str, str].
    Popen's env= parameter requires exactly this type.
    """
    env = _worker_environment()
    assert isinstance(env, dict), f"Expected dict, got {type(env)}"
    for k, v in env.items():
        assert isinstance(k, str), f"Key {k!r} is not a str (type: {type(k)})"
        assert isinstance(v, str), f"Value for {k!r} is not a str (type: {type(v)})"


def test_g3_environment_is_non_empty():
    """
    The curated environment must not be empty — a completely empty environment
    prevents normal execution (no PATH, no TEMP, etc.).
    """
    env = _worker_environment()
    assert len(env) > 0, (
        "Worker environment is empty. The allowlist produced no variables. "
        "The subprocess cannot execute correctly."
    )


# ── G4: Actual subprocess.Popen boundary ─────────────────────────────────────

def test_g4_popen_receives_explicit_env_kwarg(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """
    The Claude subprocess.Popen call must receive an explicit env= mapping.

    If env=None is passed to Popen, the subprocess inherits the full parent
    environment — this is exactly the vulnerability P1-G closes.  This test
    fails if the implementation accidentally reverts to implicit inheritance.
    """
    spy, captured = _make_spy_popen()
    engine = ExecutionEngine(tmp_db, audit, policy)

    with patch("subprocess.Popen", side_effect=spy):
        engine.run(sample_task, tmp_path / "ws")

    assert captured, "subprocess.Popen was never called — no boundary to test"
    env_kwarg = captured[0].get("env")

    assert env_kwarg is not None, (
        "subprocess.Popen was called with env=None (implicit inheritance). "
        "The full parent environment is reaching the Claude worker. P1-G boundary is broken."
    )
    assert isinstance(env_kwarg, dict), (
        f"env= is not a dict: {type(env_kwarg)}. Popen requires dict[str, str]."
    )


def test_g4_popen_env_excludes_secrets_at_boundary(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """
    At the actual Popen call boundary, secrets injected into the parent
    environment must be absent from the env= dict passed to Popen.
    """
    captured_envs: list[dict] = []

    def spy_popen(cmd, **kwargs):
        captured_envs.append(dict(kwargs.get("env") or {}))
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    engine = ExecutionEngine(tmp_db, audit, policy)

    injected_secrets = {
        "ANTHROPIC_API_KEY": "sk-ant-popen-boundary-test",
        "DATABASE_URL": "postgres://user:secret@db/prod",
        "RELAY_DB_PATH": "/secrets/relay-prod.db",
        "RELAY_ATTACKER_CONTROLLED": "should-not-cross-popen",
    }

    with patch("subprocess.Popen", side_effect=spy_popen):
        with patch.dict(os.environ, injected_secrets):
            engine.run(sample_task, tmp_path / "ws")

    assert captured_envs, "subprocess.Popen was never called"
    actual_env = captured_envs[0]

    for key in injected_secrets:
        assert key not in actual_env, (
            f"{key!r} appeared in the Popen env= at the subprocess boundary. "
            f"P1-G boundary is broken — secret reached the Claude worker."
        )


def test_g4_popen_env_only_contains_allowlisted_keys(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """
    Every key in the Popen env= must be on the platform allowlist.
    Verifies the allowlist property holds at the real execution boundary.
    """
    captured_envs: list[dict] = []

    def spy_popen(cmd, **kwargs):
        captured_envs.append(dict(kwargs.get("env") or {}))
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc

    allowlist = _current_allowlist()
    engine = ExecutionEngine(tmp_db, audit, policy)

    with patch("subprocess.Popen", side_effect=spy_popen):
        engine.run(sample_task, tmp_path / "ws")

    assert captured_envs
    actual_env = captured_envs[0]

    for key in actual_env:
        assert key in allowlist, (
            f"Key {key!r} is in the Popen env= but not on the allowlist. "
            f"An unknown variable crossed the Popen boundary."
        )


# ── G5: P1-A worker configuration isolation regression ───────────────────────

def test_g5_bare_flag_present_after_p1g(tmp_db, audit, policy, sample_task, tmp_path):
    """
    P1-A regression: --bare must remain in the Claude command after P1-G changes.
    P1-G must not disrupt P1-A's CLAUDE.md auto-discovery prevention.
    """
    spy, captured = _make_spy_popen()
    engine = ExecutionEngine(tmp_db, audit, policy)

    with patch("subprocess.Popen", side_effect=spy):
        engine.run(sample_task, tmp_path / "ws")

    assert captured, "subprocess.Popen was never called"
    cmd = captured[0]["cmd"]
    assert "--bare" in cmd, (
        "--bare is missing from the Claude command after P1-G changes. "
        "P1-A CLAUDE.md isolation regression."
    )


def test_g5_add_dir_points_to_relay_config_after_p1g(
    tmp_db, audit, policy, sample_task, tmp_path
):
    """
    P1-A regression: --add-dir must point to the Relay worker config directory
    after P1-G changes.  P1-G must not affect the worker configuration path.
    """
    spy, captured = _make_spy_popen()
    engine = ExecutionEngine(tmp_db, audit, policy)

    with patch("subprocess.Popen", side_effect=spy):
        engine.run(sample_task, tmp_path / "ws")

    assert captured
    cmd = captured[0]["cmd"]

    assert "--add-dir" in cmd, (
        "--add-dir missing from command after P1-G changes. P1-A regression."
    )
    idx = cmd.index("--add-dir")
    actual_dir = Path(cmd[idx + 1]).resolve()
    expected_dir = RELAY_WORKER_CONFIG_DIR.resolve()
    assert actual_dir == expected_dir, (
        f"--add-dir points to {actual_dir!r} after P1-G changes; "
        f"expected Relay worker config at {expected_dir!r}. P1-A regression."
    )
