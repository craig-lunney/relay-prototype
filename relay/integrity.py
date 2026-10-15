"""
Workspace integrity manifest (Batch 1 — verification integrity).

Relay hashes every file in the workspace after the baseline has been
established and before the worker is launched.  At verification time the
workspace is hashed again and the two snapshots are compared.  This gives the
verifier a view of what the worker changed that does not depend on anything
the worker controls: git state (index, HEAD, .gitignore, .git/config), test
names, test counts, test outcomes or the worker's own report.

Protected paths (D3): for ordinary implementation tasks the worker must not
modify, delete or rename existing tests, and must not add or change anything
that alters how pytest collects or executes them.  Any such change is reported
as a protected violation and fails verification.  New test files may be added,
but they are excluded from the verification run (see ``new_python_files``) so
they cannot influence the baseline tests at import time.

Bytecode caches are the only workspace content treated as non-substantive.
That exclusion is safe only because the verifier never reads them: pytest is
run with ``-B -X pycache_prefix=<relay-owned temp dir>`` and
``-p no:cacheprovider`` (see relay/testing.py).
"""
from __future__ import annotations

import configparser
import fnmatch
import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

# Directories never descended into.  The verifier does not rely on git
# metadata for change detection, so .git content is irrelevant here.
_SKIP_DIRS = frozenset({".git"})

# Test-directory names whose entire contents are protected.
_TEST_DIR_NAMES = frozenset({"tests", "test"})

# Default pytest test-module patterns (python_files default).
_TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")

# Files whose whole content is pytest configuration.
_PYTEST_ONLY_CONFIG = frozenset({"pytest.ini", ".pytest.ini"})

# Python start-up hooks.  Not active from a workspace on a standard interpreter,
# but they exist only to alter interpreter behaviour, so they are protected
# defensively rather than trusted.
_STARTUP_HOOK_NAMES = frozenset({"sitecustomize.py", "usercustomize.py"})

# Kinds whose *addition* is forbidden.  New test files are allowed (D3 forbids
# altering existing tests, not adding tests); they are excluded from collection.
_ADD_FORBIDDEN_KINDS = frozenset({
    "conftest", "pytest config", "python startup hook", "test package __init__",
})


@dataclass
class WorkspaceSnapshot:
    """Content hashes of every workspace file plus pytest-config fingerprints."""
    files: dict[str, str] = field(default_factory=dict)
    pytest_config: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"files": self.files, "pytest_config": self.pytest_config}, sort_keys=True
        )

    @classmethod
    def from_json(cls, raw: str) -> "WorkspaceSnapshot":
        data = json.loads(raw)
        return cls(files=dict(data["files"]), pytest_config=dict(data["pytest_config"]))


@dataclass
class IntegrityReport:
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    ignored_artifacts: list[str] = field(default_factory=list)
    protected_violations: list[str] = field(default_factory=list)
    new_python_files: list[str] = field(default_factory=list)


# ── Snapshot ──────────────────────────────────────────────────────────────────

def snapshot_workspace(workspace: str | Path) -> WorkspaceSnapshot:
    """Hash every file under *workspace* (symlinks are recorded, never followed)."""
    root = Path(workspace)
    snap = WorkspaceSnapshot()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                snap.files[rel] = "symlink:" + os.readlink(full)
                continue
            data = full.read_bytes()
            snap.files[rel] = hashlib.sha256(data).hexdigest()
            fingerprint = _pytest_config_fingerprint(name, data)
            if fingerprint:
                snap.pytest_config[rel] = fingerprint
    return snap


def _pytest_config_fingerprint(name: str, data: bytes) -> str:
    """Fingerprint of the pytest section of a shared config file, or "" if none.

    Only the pytest section is compared so that legitimate edits to the rest of
    pyproject.toml / setup.cfg / tox.ini are not treated as violations.  A file
    that cannot be parsed is fingerprinted by its whole content, so any change
    to it counts.
    """
    try:
        if name == "pyproject.toml":
            section = tomllib.loads(data.decode("utf-8")).get("tool", {}).get("pytest")
        elif name in ("setup.cfg", "tox.ini"):
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(data.decode("utf-8"))
            key = "tool:pytest" if name == "setup.cfg" else "pytest"
            section = dict(parser[key]) if parser.has_section(key) else None
        else:
            return ""
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, configparser.Error):
        return "unparseable:" + hashlib.sha256(data).hexdigest()
    if section is None:
        return ""
    canonical = json.dumps(section, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Classification ────────────────────────────────────────────────────────────

def is_bytecode_artifact(path: str) -> bool:
    """True for interpreter/pytest cache files that the verifier never reads."""
    parts = PurePosixPath(path).parts
    if ".pytest_cache" in parts[:-1]:
        return True
    return "__pycache__" in parts[:-1] and parts[-1].endswith((".pyc", ".pyo"))


def _is_test_file(path: str, test_files: set[str]) -> bool:
    p = PurePosixPath(path)
    if path in test_files:
        return True
    if any(part in _TEST_DIR_NAMES for part in p.parts[:-1]):
        return True
    return p.suffix == ".py" and any(fnmatch.fnmatch(p.name, pat) for pat in _TEST_FILE_PATTERNS)


def _protected_kind(path: str, test_files: set[str], test_dirs: set[str]) -> str | None:
    p = PurePosixPath(path)
    if p.name == "conftest.py":
        return "conftest"
    if p.name in _PYTEST_ONLY_CONFIG:
        return "pytest config"
    if p.name in _STARTUP_HOOK_NAMES or p.suffix == ".pth":
        return "python startup hook"
    if p.name == "__init__.py" and str(p.parent) in test_dirs:
        return "test package __init__"
    if _is_test_file(path, test_files):
        return "test file"
    return None


def _ancestor_dirs(paths: Iterable[str]) -> set[str]:
    """Every directory (relative, "." for root) containing or above *paths*."""
    dirs: set[str] = set()
    for path in paths:
        for parent in PurePosixPath(path).parents:
            dirs.add(str(parent))
    return dirs


# ── Comparison ────────────────────────────────────────────────────────────────

def compare_snapshots(
    baseline: WorkspaceSnapshot,
    current: WorkspaceSnapshot,
    baseline_test_ids: Iterable[str] = (),
) -> IntegrityReport:
    """Compare *current* against the Relay-captured *baseline*."""
    report = IntegrityReport()
    test_files = {tid.split("::", 1)[0].replace("\\", "/") for tid in baseline_test_ids}

    all_paths = set(baseline.files) | set(current.files)
    anchors = [
        p for p in all_paths
        if PurePosixPath(p).name == "conftest.py" or _is_test_file(p, test_files)
    ]
    test_dirs = _ancestor_dirs(anchors)

    for path in sorted(all_paths):
        before = baseline.files.get(path)
        after = current.files.get(path)
        if before == after:
            continue
        if is_bytecode_artifact(path):
            report.ignored_artifacts.append(path)
            continue

        kind = _protected_kind(path, test_files, test_dirs)
        if before is None:
            report.added.append(path)
            if kind in _ADD_FORBIDDEN_KINDS:
                report.protected_violations.append(f"added: {path} ({kind})")
            if path.endswith(".py"):
                report.new_python_files.append(path)
        elif after is None:
            report.deleted.append(path)
            if kind:
                report.protected_violations.append(f"deleted: {path} ({kind})")
        else:
            report.changed.append(path)
            if kind:
                report.protected_violations.append(f"modified: {path} ({kind})")

    for path in sorted(set(baseline.pytest_config) | set(current.pytest_config)):
        if baseline.pytest_config.get(path, "") != current.pytest_config.get(path, ""):
            report.protected_violations.append(
                f"pytest configuration changed: {path}"
            )

    return report
