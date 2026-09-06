"""
P1-C path canonicalisation and workspace boundary tests.

Tests C1–C7 exercise assert_within_workspace() directly using real filesystem
paths (tmp_path fixture) rather than mocked Path methods, so OS-level resolution
behaviour (symlinks, drive-letter normalisation, separator canonicalisation) is
exercised as it would be in production.

C8 (recovery boundary regression) is in test_recovery.py because it requires
the full state/recovery fixture stack.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from relay.paths import WorkspaceBoundaryError, assert_within_workspace


# ── C1: Normal in-workspace path ──────────────────────────────────────────────

def test_c1_normal_in_workspace_path(tmp_path):
    """A canonical path inside the workspace is accepted."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "subdir").mkdir()
    (workspace / "subdir" / "file.txt").write_text("hello")

    result = assert_within_workspace(workspace / "subdir" / "file.txt", workspace)

    assert result.is_absolute()
    assert result == (workspace / "subdir" / "file.txt").resolve()


# ── C2: Workspace root ────────────────────────────────────────────────────────

def test_c2_workspace_root_is_accepted(tmp_path):
    """The workspace root itself is within the workspace boundary."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = assert_within_workspace(workspace, workspace)

    assert result == workspace.resolve()


# ── C3: Sibling-prefix attack ─────────────────────────────────────────────────

def test_c3_sibling_prefix_attack_rejected(tmp_path):
    """
    A path that SHARES a string prefix with the workspace but is NOT inside
    it must be rejected.

    workspace:        tmp/workspace
    candidate:        tmp/workspace-evil

    A string startswith() check on 'workspace' would incorrectly accept
    'workspace-evil'. Path component comparison correctly rejects it.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "workspace-evil"
    sibling.mkdir()  # exists — worst case for string-prefix checks

    with pytest.raises(WorkspaceBoundaryError, match="outside"):
        assert_within_workspace(sibling, workspace)


def test_c3_sibling_prefix_attack_rejected_nonexistent(tmp_path):
    """Sibling-prefix rejection works even when the sibling does not exist."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "workspace-evil"  # does NOT exist

    with pytest.raises(WorkspaceBoundaryError, match="outside"):
        assert_within_workspace(sibling, workspace)


# ── C4: .. traversal ─────────────────────────────────────────────────────────

def test_c4_dotdot_traversal_rejected(tmp_path):
    """A path using .. to escape the workspace is rejected after resolution."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # workspace/../outside resolves to tmp_path/outside — outside the workspace
    candidate = workspace / ".." / "outside"

    with pytest.raises(WorkspaceBoundaryError, match="outside"):
        assert_within_workspace(candidate, workspace)


def test_c4_dotdot_within_workspace_accepted(tmp_path):
    """A .. that stays within the workspace resolves correctly and is accepted."""
    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)

    # workspace/sub/../sub is still within workspace
    candidate = workspace / "sub" / ".." / "sub"

    result = assert_within_workspace(candidate, workspace)
    assert result == (workspace / "sub").resolve()


# ── C5: Relative path inside workspace ───────────────────────────────────────

def test_c5_relative_path_inside_workspace_accepted(tmp_path):
    """A relative path that resolves inside the workspace is accepted."""
    workspace = tmp_path / "workspace"
    (workspace / "subdir").mkdir(parents=True)

    # "subdir" is relative — resolved against workspace root
    result = assert_within_workspace("subdir", workspace)

    assert result == (workspace / "subdir").resolve()


def test_c5_relative_path_nested_accepted(tmp_path):
    """A multi-level relative path that resolves inside the workspace is accepted."""
    workspace = tmp_path / "workspace"
    (workspace / "a" / "b").mkdir(parents=True)

    result = assert_within_workspace("a/b", workspace)
    assert result == (workspace / "a" / "b").resolve()


# ── C6: Relative path escaping the workspace ─────────────────────────────────

def test_c6_relative_path_escaping_rejected(tmp_path):
    """A relative path that resolves outside the workspace is rejected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # "../outside" relative to workspace resolves to tmp_path/outside
    with pytest.raises(WorkspaceBoundaryError, match="outside"):
        assert_within_workspace("../outside", workspace)


def test_c6_relative_dotdot_chain_rejected(tmp_path):
    """Multiple .. components that collectively escape the workspace are rejected."""
    workspace = tmp_path / "workspace"
    (workspace / "deep" / "dir").mkdir(parents=True)

    # "deep/dir/../../.." goes workspace -> deep -> dir -> workspace -> tmp_path
    with pytest.raises(WorkspaceBoundaryError, match="outside"):
        assert_within_workspace("deep/dir/../../..", workspace)


# ── C7: Equivalent canonical paths ───────────────────────────────────────────

def test_c7_equivalent_paths_resolve_consistently(tmp_path):
    """
    Different textual representations of the same location are treated
    consistently: both accepted when inside, both rejected when outside.
    """
    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)

    # Three textually different paths that all resolve to workspace/sub
    forms = [
        workspace / "sub",
        workspace / "." / "sub",
        workspace / "sub" / ".." / "sub",
    ]

    canonical = (workspace / "sub").resolve()
    for form in forms:
        result = assert_within_workspace(form, workspace)
        assert result == canonical, f"Form {form!r} resolved to {result!r}, expected {canonical!r}"


def test_c7_equivalent_paths_outside_all_rejected(tmp_path):
    """All textual variants of an outside path are consistently rejected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    forms = [
        outside,
        workspace / ".." / "outside",
    ]

    for form in forms:
        with pytest.raises(WorkspaceBoundaryError, match="outside"):
            assert_within_workspace(form, workspace)


# ── Canonicalization failure ──────────────────────────────────────────────────

def test_fail_closed_on_bad_workspace(tmp_path):
    """
    If the workspace path cannot be resolved (e.g. empty string treated as
    current directory), the function raises WorkspaceBoundaryError rather than
    silently accepting or falling back to string comparison.
    Note: Path("").resolve() succeeds (returns cwd), so this test verifies the
    containment logic for a degenerate case, not an OS-level exception.
    A candidate clearly outside cwd is still rejected.
    """
    # Use a candidate that is definitely not within Path("").resolve() (cwd)
    # on a standard system — e.g. a system temp path above the cwd.
    import tempfile
    system_tmp = Path(tempfile.gettempdir()).resolve()
    cwd = Path("").resolve()

    if not str(system_tmp).startswith(str(cwd)):
        # system_tmp is outside cwd — should be rejected
        with pytest.raises(WorkspaceBoundaryError, match="outside"):
            assert_within_workspace(system_tmp, "")
    # If cwd happens to be inside system_tmp (e.g. /tmp/pytest-xxx), skip.
