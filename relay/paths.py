from __future__ import annotations

import re
from pathlib import Path


class WorkspaceBoundaryError(Exception):
    """Raised when a path cannot be verified as within the authorised workspace."""


class InvalidTestSpecifierError(Exception):
    """Raised when a required_tests entry fails validation."""


# Characters that must not appear in a pytest test specifier.
# These are shell control characters that could cause confusion in log output,
# command strings, or argument parsing, and are never valid in file paths or
# pytest node identifiers.
_FORBIDDEN_CHARS = re.compile(r"[&|;<>$`\n\r\x00]")


def assert_within_workspace(candidate: str | Path, workspace: str | Path) -> Path:
    """
    Resolve both paths to canonical absolute form and verify that candidate
    is within the workspace boundary.

    Containment is determined by Path.relative_to(), which compares path
    *components* — never a string prefix. This correctly handles:

    * Sibling-prefix attacks: /workspace-evil is rejected when workspace=/workspace
      because the path components differ at the leaf level.
    * .. traversal: /workspace/../outside resolves to /outside, which is outside.
    * Relative paths: resolved against the workspace root before comparison.
    * Equivalent paths: /workspace/./sub/../sub resolves to /workspace/sub, accepted.
    * Symlink/junction resolution: Path.resolve() follows symlinks on all platforms
      to the extent supported by the OS; the canonical form is then compared.

    Relative candidates are resolved relative to the workspace root.

    Returns the canonical absolute candidate path on success.
    Raises WorkspaceBoundaryError if:
    - Either path cannot be canonicalized (OS error or malformed path)
    - The candidate does not fall within the workspace boundary

    Never uses string-prefix comparison for authorisation.
    """
    try:
        canonical_ws = Path(workspace).resolve()
    except (OSError, ValueError) as exc:
        raise WorkspaceBoundaryError(
            f"Cannot resolve workspace path {workspace!r}: {exc}"
        ) from exc

    try:
        p = Path(candidate)
        if not p.is_absolute():
            # Relative paths are resolved relative to the workspace root.
            p = canonical_ws / p
        canonical_candidate = p.resolve()
    except (OSError, ValueError) as exc:
        raise WorkspaceBoundaryError(
            f"Cannot resolve candidate path {candidate!r}: {exc}"
        ) from exc

    try:
        canonical_candidate.relative_to(canonical_ws)
    except ValueError:
        raise WorkspaceBoundaryError(
            f"Path {canonical_candidate!r} is outside the authorised "
            f"workspace {canonical_ws!r}."
        )

    return canonical_candidate


def validate_test_specifier(specifier: str, workspace: str | Path | None = None) -> str:
    """
    Validate a single pytest test specifier from required_tests.

    Accepted forms:
      test_file.py
      path/to/test_file.py
      test_file.py::test_function
      test_file.py::TestClass::test_method

    The path component is everything before the first '::'.
    Node-ID components (after '::') are passed through without further parsing.

    Raises InvalidTestSpecifierError if:
    - specifier is empty
    - specifier starts with '-'  (would be interpreted as a pytest option)
    - specifier contains shell metacharacters or control characters
    - the path component escapes the workspace (when workspace is given)
    - the path component is absolute and outside the workspace (when workspace is given)

    When workspace is None the path boundary check is skipped (policy-time validation,
    before any workspace exists).

    The subprocess invocation uses shell=False and the '--' separator, so these checks
    are defence-in-depth rather than the sole injection barrier.

    Returns specifier unchanged on success.
    """
    if not specifier:
        raise InvalidTestSpecifierError("Test specifier must not be empty.")

    if specifier.startswith("-"):
        raise InvalidTestSpecifierError(
            f"Test specifier {specifier!r} begins with '-', which would be "
            "interpreted as a pytest option. Test specifiers must be file paths "
            "or node IDs, not option flags."
        )

    if _FORBIDDEN_CHARS.search(specifier):
        raise InvalidTestSpecifierError(
            f"Test specifier {specifier!r} contains shell metacharacters or "
            "control characters, which are not valid in test specifiers."
        )

    # The path component is everything before the first '::' separator.
    path_part = specifier.split("::")[0]

    if workspace is not None:
        try:
            assert_within_workspace(path_part, workspace)
        except WorkspaceBoundaryError as exc:
            raise InvalidTestSpecifierError(
                f"Test path {path_part!r} in specifier {specifier!r} "
                f"is outside the authorised workspace: {exc}"
            ) from exc

    if "\\" in specifier:
        raise InvalidTestSpecifierError(
            f"Test specifier {specifier!r} contains a backslash. "
            "Use forward slashes for path separators in test specifiers."
        )

    return specifier


def validate_required_tests(
    required_tests: list[str],
    workspace: str | Path | None = None,
) -> list[str]:
    """
    Validate all entries in required_tests.

    Calls validate_test_specifier() on each entry.
    Returns the list unchanged if all entries are valid.
    Raises InvalidTestSpecifierError naming the first invalid entry.
    """
    for spec in required_tests:
        validate_test_specifier(spec, workspace)
    return required_tests
