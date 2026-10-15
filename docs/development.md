# Development Guide

## Layout

```
relay/                  Execution and verification
  cli.py                `relay` CLI; outcome binding (run-task), approvals,
                        acknowledgement, recovery commands
  executor.py           Lease, workspace prep, baseline + integrity manifest,
                        Claude invocation (flags, curated env, apiKeyHelper)
  verifier.py           Independent verification (H-1..H-10), acceptance verdicts
  integrity.py          Workspace hash manifest, protected-path rules
  testing.py            Hardened pytest invocation and output parsing
  state.py              SQLite state, CAS transitions, atomic multi-write operations
  recovery.py           Stale-lease and orphaned-task detection
  policy.py             Capabilities, production-path check, base_ref, approval
  paths.py              Workspace containment, specifier and path validation
  schemas.py            Runtime task/report validation (matches schemas/*.json)
  audit.py, models.py   Audit events, dataclasses, enums, transition graph
a2a/                    Protocol ledger (see docs/a2a-dev.md)
tests/                  pytest suite
  fake_worker.py        Scripted stand-in for the Claude CLI (not collected)
  a2a_support.py        Helpers that walk the A2A protocol with correct attestations
demo/acceptance_demo.py End-to-end acceptance demonstration
test-repository/        The disposable target used by tests and the demo
```

## Running tests

```bash
python -m pytest -q                         # full suite (~5 min)
python -m pytest -q tests/test_verification_integrity.py   # one area
```

The suite never launches the real Claude CLI. Unit tests mock
`subprocess.Popen`; end-to-end tests put a shim named after the Claude binary in
front of `tests/fake_worker.py` and drive the real CLI. To make sure nothing can
reach a real `claude` while developing, remove it from PATH for the test run.

## Conventions that matter here

- **Fail closed.** Missing data, unparseable output, an unexpected exception:
  the outcome must be FAILED / RECOVERY_REQUIRED / "no state changed", never a
  silent pass.
- **State changes that must agree go in one transaction.** Use the existing
  `*_atomic` / `BEGIN IMMEDIATE` pattern in `relay/state.py` and
  `a2a/state.py`; CAS on the expected current status.
- **Enforce at the lowest public layer.** The A2A state manager, not the
  service, enforces protocol rules; Relay's verifier, not the worker's report,
  decides outcomes.
- **Never trust worker-writable inputs in verification.** Git state, test files,
  pytest config, bytecode caches and the worker report are all worker-writable.
- **Subprocess environments are allowlisted** (`_worker_environment`); pass
  options as command-line flags rather than adding environment variables.
- **Tests that fake `ExecutionEngine.run` must honour its contract**: the
  execution is recorded and its lease is held when `run()` returns
  (`tests/conftest.py::hold_worker_lease`).
- **The frozen protocol is not edited.** `docs/a2a-protocol-v0.2.md` is
  authoritative; implementation choices where it is ambiguous are documented in
  `docs/a2a-dev.md` and `docs/limitations.md`.

## Adding an adversarial regression test

Add a behaviour to `tests/fake_worker.py` (`BEHAVIOURS` dict), then use the
`relay_e2e` fixture in `tests/test_verification_integrity.py`:

```python
def test_my_attack_is_rejected(self, relay_e2e):
    out = relay_e2e("my_behaviour")
    assert out.status == TaskStatus.FAILED
```

Write the test first and confirm it fails against the unfixed code.
