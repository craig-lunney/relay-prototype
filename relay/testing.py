"""
Shared test-execution utilities for executor (baseline capture) and verifier.

PROTOTYPE: Uses text-based pytest output parsing.
Migrate to --json-report in a future milestone once the plugin is available
in the worker environment.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Strip ANSI colour/cursor escape sequences from pytest output.
_ANSI = re.compile(r"\x1b\[[0-9;]*[mGKHFJABCDEFsu]")

# Match a test outcome line from `pytest -v` output.
# Greedy on the node-ID portion so parameterised tails ([param0] etc.) are
# captured inside the ID, not split off.  The optional suffix handles pytest's
# progress indicator (e.g. "[ 41%]") introduced in pytest 9.x.
_OUTCOME_RE = re.compile(
    r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAILED|XPASS)(?:\s+\[\s*\d+%\])?\s*$"
)

# Match a collected test ID from `pytest --collect-only -q` output.
# Lines look like: "test_file.py::test_name" or "<no tests ran>" etc.
# Require "::" to be present to avoid capturing summary lines.
_COLLECTED_RE = re.compile(r"^(.+::.+)$")


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def collect_tests(
    workspace: str | Path,
    required_tests: list[str],
    env: dict[str, str],
) -> tuple[int | None, list[str], str, list[str]]:
    """
    Run ``pytest --collect-only -q`` on *required_tests*.

    Uses subprocess.Popen directly (not subprocess.run) so that existing
    executor tests can mock subprocess.Popen independently of subprocess.run.

    Returns ``(exit_code, collected_node_ids, raw_output, errors)``.
    *collected_node_ids* is empty when collection fails or nothing is found.
    """
    if not required_tests:
        return 0, [], "", []

    errors: list[str] = []
    # Pin rootdir to the workspace so node IDs are always workspace-relative,
    # regardless of any pytest config files in ancestor directories.
    args = ["--collect-only", "-q", f"--rootdir={workspace}", "--"] + required_tests

    for prefix in (["python", "-m", "pytest"], ["pytest"]):
        cmd = prefix + args
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                errors.append("Baseline collection timed out after 60s.")
                return -1, [], (out or "") + (err or ""), errors
            raw = (out or "") + (err or "")
            ids = _parse_collection_output(raw)
            return proc.returncode, ids, raw, errors
        except FileNotFoundError:
            continue

    errors.append("Neither 'python -m pytest' nor 'pytest' found during baseline collection.")
    return None, [], "", errors


def run_test_suite(
    workspace: str | Path,
    required_tests: list[str],
    env: dict[str, str],
    timeout: int = 120,
) -> tuple[int | None, str, list[str]]:
    """
    Run ``pytest -v --tb=short`` on *required_tests*.

    Uses subprocess.run so that existing verifier tests can mock it via
    ``patch("subprocess.run", ...)``.

    Returns ``(exit_code, combined_output, errors)``.
    """
    if not required_tests:
        return None, "", []

    errors: list[str] = []
    # Pin rootdir to the workspace so node IDs are always workspace-relative,
    # matching the format produced by collect_tests().
    args = ["-v", "--tb=short", f"--rootdir={workspace}", "--"] + required_tests

    for prefix in (["python", "-m", "pytest"], ["pytest"]):
        cmd = prefix + args
        try:
            result = subprocess.run(
                cmd,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            return result.returncode, result.stdout + result.stderr, errors
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            errors.append(f"Test run timed out after {timeout}s.")
            return -1, "TIMEOUT", errors

    errors.append("Neither 'python -m pytest' nor 'pytest' found.")
    return None, "", errors


def _parse_collection_output(raw: str) -> list[str]:
    """
    Extract node IDs from ``--collect-only -q`` output.

    Only lines that contain ``::`` are treated as node IDs; summary lines
    (e.g. "5 tests collected") are ignored.  ANSI codes are stripped first.
    """
    ids: list[str] = []
    for line in strip_ansi(raw).splitlines():
        line = line.strip()
        m = _COLLECTED_RE.match(line)
        if m and "::" in line:
            ids.append(m.group(1))
    return ids


def parse_test_results(raw: str) -> dict[str, str] | None:
    """
    Parse per-test outcomes from ``pytest -v`` output.

    Returns a dict mapping node_id → outcome string (PASSED, FAILED, ERROR,
    SKIPPED, XFAILED, XPASS).

    Fail-closed rules:
    - Returns ``None`` if the output is unparseable (no outcome lines found
      despite a non-empty input).
    - Returns ``None`` if any node ID maps to two different outcomes (conflict).
    - Returns an empty dict (not None) for genuinely empty output (no tests ran).

    ANSI codes are stripped before parsing.  Parameterised IDs such as
    ``test_foo[a-1]`` are returned as-is; callers doing prefix-based node-ID
    matching must strip the ``[...]`` suffix themselves.
    """
    clean = strip_ansi(raw)
    results: dict[str, str] = {}
    found_any = False

    for line in clean.splitlines():
        m = _OUTCOME_RE.match(line.rstrip())
        if not m:
            continue
        found_any = True
        node_id, outcome = m.group(1).strip(), m.group(2)
        if node_id in results and results[node_id] != outcome:
            # Conflicting outcomes for the same ID — fail closed.
            return None
        results[node_id] = outcome

    # If raw is non-empty but we found no outcome lines, the output is
    # unparseable.  Fail closed rather than returning an empty dict that
    # would pass the "zero required tests" path.
    if raw.strip() and not found_any:
        return None

    return results
