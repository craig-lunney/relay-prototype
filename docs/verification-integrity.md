# Verification Integrity (Batch 1)

This document describes how Relay's verifier establishes what a worker changed,
which changes are forbidden, how acceptance criteria are reported, and which
risks remain. It covers behaviour introduced in Batch 1; the overall picture is
in the README and docs/limitations.md.

## Why

Before Batch 1 the verifier compared test **names** and **outcomes** only. A
worker could leave a bug unfixed, redefine every test as `pass` with the same
name, and receive `passed = True`. The same class of bypass worked by editing
assertions, injecting a `conftest.py`, or planting a doctored bytecode cache
for an unchanged test file. All of these are now covered by end-to-end
regression tests in `tests/test_verification_integrity.py`.

## Integrity manifest

`ExecutionEngine._capture_baseline` (in `relay/executor.py`) records, after baseline test collection and before the worker is launched:

- the baseline test node IDs (P1-J, unchanged), and
- a **workspace integrity manifest**: a SHA-256 hash of every file in the workspace (except `.git/`), plus a fingerprint of the pytest section of `pyproject.toml`, `setup.cfg` and `tox.ini`.

The manifest is persisted on the execution (`executions.baseline_manifest`) before the worker starts.

At verification the workspace is hashed again and compared (`relay/integrity.py`). That comparison, not `git status`, is the source of the changed, added and deleted file lists. Git state (index, HEAD, `.gitignore`, `.git/config`) is worker-writable and is not trusted for change detection.

If the manifest is missing or could not be captured for an execution that had a baseline, verification **fails closed** and the test suite is not run.

## Protected paths (decision D3)

For ordinary implementation tasks, each of the following fails verification. It is listed in `protected_violations` and recorded as an error:

| Change | Examples |
|---|---|
| Modifying, deleting or renaming an existing test file | `test_*.py`, `*_test.py`, any file under a `tests/` or `test/` directory, any file that contained a baseline-collected test |
| Adding or changing a `conftest.py` | — |
| Adding or changing pytest-only configuration | `pytest.ini`, `.pytest.ini` |
| Changing the pytest section of a shared config file | `[tool.pytest...]` in `pyproject.toml`, `[tool:pytest]` in `setup.cfg`, `[pytest]` in `tox.ini`. Edits elsewhere in those files are allowed. An unparseable file counts as changed if its content changes. |
| Adding or changing Python start-up hooks | `sitecustomize.py`, `usercustomize.py`, `*.pth` |
| Adding an `__init__.py` in a directory that contains, or is above, a test file or `conftest.py` | — |

When a protected violation is found, the verifier **does not run the test suite**: the outcome is already FAILED, so no worker-influenced test code is executed.

**New test files** may be added. They are excluded from the verification run with pytest `--ignore`, so they cannot change the behaviour of the baseline tests (for example by patching application code at import time). They can still be imported by application code.

**There is no bypass for test-maintenance tasks.** A task whose purpose is to change existing tests cannot currently pass verification. Supporting one needs a separate, explicitly authorised task type; it has not been implemented.

## New files (H-9)

A task may declare `expected_new_files`: a list of workspace-relative paths, matched exactly with no globs. The paths are validated at `create-task`; absolute paths, `..`, glob characters and shell metacharacters are rejected.

Any other new file fails verification (`unexpected_files`). The default is an empty list, so a task that creates files must declare them.

`expected_new_files` **cannot** authorise a protected addition such as `conftest.py`, pytest configuration or a `.pth` file.

## Bytecode caches

The only workspace content treated as non-substantive is `*.pyc` and `*.pyo` files inside `__pycache__/`, and anything inside `.pytest_cache/`. These are listed in `ignored_artifacts` rather than hidden.

Ignoring them is safe only because the verifier never reads them. Both Relay pytest runs, baseline collection and verification, use:

```
python -B -X pycache_prefix=<fresh Relay temp dir> -m pytest -p no:cacheprovider ...
```

Before Batch 1, pytest would load a planted assertion-rewritten pyc whose header matched the unchanged test file's mtime and size. This is reproduced by the `poison_pyc` regression test.

The flags are passed on the command line, so the subprocess environment remains exactly the P1-G allowlist.

## Acceptance criteria (decision D5)

Every criterion receives a verdict in `acceptance_results`:

| Verdict | Meaning |
|---|---|
| `PASS` | Checked by Relay and satisfied |
| `FAIL` | Checked by Relay and not satisfied, including when the tests could not run |
| `UNVERIFIED` | Not established by automated verification |

**Which criteria Relay checks:**
- Only statements that the required test suite passes are checked automatically, and they are matched as whole normalised phrases, for example "All tests pass", "The test suite passes" or "Tests must not fail". The full list is in `relay/verifier.py:_TEST_SUITE_CRITERIA`.
- Anything else is `UNVERIFIED`. That includes compound criteria such as "add a regression test; all tests pass".
- Classification depends only on the operator-authored task text, so worker output cannot move a criterion from `FAIL` to `UNVERIFIED`.
- The pre-Batch-1 rule that inverted criteria mentioning "fail" has been removed.

**What happens after verification:**
- **Verification failed:** `FAILED`, as before. A `FAIL` verdict fails verification.
- **Verification passed with no `UNVERIFIED` criteria:** `COMPLETED`, as before.
- **Verification passed with `UNVERIFIED` criteria:** the task enters **`ACCEPTANCE_REQUIRED`**. The workspace is kept for inspection, and the task is not `COMPLETED` until an operator decides:

```
relay acknowledge-criteria TASK-XXXXXXXX --actor <name> [--reason <text>]
relay acknowledge-criteria TASK-XXXXXXXX --actor <name> --reject [--reason <text>]
```

Accepting moves the task to `COMPLETED`; rejecting moves it to `FAILED`. The decision is recorded as a separate `ACCEPTANCE_ACKNOWLEDGED` or `ACCEPTANCE_REJECTED` audit event, attributed to `--actor`, in the same transaction as the state change.

The `VERIFICATION_COMPLETED` event is never rewritten, so an acknowledged criterion is still recorded as `UNVERIFIED` by Relay. A human acknowledgement is not an automated pass.

Only tasks whose automated verification passed can be in `ACCEPTANCE_REQUIRED`, so a criterion with a `FAIL` verdict can never be acknowledged.

`relay status` lists the criteria that are awaiting acknowledgement.

## Trust boundaries during verification (investigation D)

**What runs, and as whom:**
- **Verification still executes worker-written application code** as the operator's OS account. pytest imports the modified application modules.
- The subprocess environment is the P1-G allowlist, so no credential environment variables are passed. The process still has the user's full filesystem access, including the Relay database, the Relay source and the API key file.
- This is **not an OS-level sandbox**, and nothing in Batch 1 provides one.

**Mitigations in Batch 1:**
- No test code runs when integrity cannot be established or protected files were altered.
- Protected tests and configuration are hash-verified before they are executed.
- The verifier never loads worker-planted bytecode or `.pytest_cache` state.
- Worker-added test modules are not collected.
- The verifier no longer runs `git status` in the workspace when a manifest is available. `git status` can run programs configured in the worker-writable `.git/config`, such as `core.fsmonitor` and clean filters. The remaining git call is `git branch --show-current`.

**Remaining:** a malicious worker can still put code in application modules that runs during verification with the operator's privileges. It can also do this during its own session, through the allowlisted `pytest` command. Closing this requires running the worker and the verifier under a separate low-privilege identity or container. That is a sandboxing design for a later batch.

## Remaining limitations

- **Test-support code outside test directories is not protected.** Examples are a pytest plugin module named in configuration (`-p myplugin`) or a fixtures package outside `tests/`.
- **Custom test-file naming.** Files are protected if they match the default patterns, live in a `tests/` or `test/` directory, or contained a baseline-collected test. A new file matching a custom `python_files` pattern is not recognised as a test file, although it is still excluded from collection because it is a new `.py` file.
- **Application code can special-case test inputs.** Verification is only as strong as the protected tests.
- **The database is unprotected from same-user processes.** The CLI verifies against the manifest held in memory since before the worker ran, so tampering with the stored copy during a run does not change that run's verdict. But the database itself, including the audit log, has no tamper evidence.
- **Hashing cost.** Every workspace file is hashed twice per execution.
- **Acknowledgement is all-or-nothing and unauthenticated.** It covers all `UNVERIFIED` criteria together, and `--actor` is unauthenticated, as for `approve-task`.
- **Workspaces left in `ACCEPTANCE_REQUIRED` are not cleaned up automatically.**
