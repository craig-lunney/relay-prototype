"""
Batch 2 — packaging, entry points, and task-schema consistency.

Regression coverage for audit findings:
  P0-3  pyproject named a build backend that does not exist; `python -m a2a.cli`
        exited silently (no __main__ guard).
  P0-4  the shipped sample_task.json was rejected by the runtime validator, and
        schemas/task.schema.json disagreed with relay/schemas.py.
  G     `relay create-task` crashed with a traceback on a missing or malformed file.
"""
from __future__ import annotations

import codecs
import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from relay.schemas import _TASK_INPUT_SCHEMA, validate_task_input

ROOT = Path(__file__).parent.parent


def test_build_backend_is_importable():
    pytest.importorskip("setuptools")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    backend = pyproject["build-system"]["build-backend"]
    module = importlib.import_module(backend.split(":")[0])
    assert hasattr(module, "build_editable"), f"{backend} cannot build an editable install"


def test_console_scripts_point_at_real_functions():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for name, target in pyproject["project"]["scripts"].items():
        module_name, func = target.split(":")
        assert callable(getattr(importlib.import_module(module_name), func)), name


@pytest.mark.parametrize("module", ["relay", "relay.cli", "a2a", "a2a.cli"])
def test_python_dash_m_prints_help(module):
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_shipped_sample_task_is_accepted():
    validate_task_input(json.loads((ROOT / "sample_task.json").read_text(encoding="utf-8")))


_TYPES = {str: "string", list: "array"}
# JSON-schema keywords that have a runtime counterpart (or are documentation).
_ALLOWED_KEYWORDS = {"type", "items", "enum", "minLength", "default", "description"}


def test_json_schema_matches_runtime_validator():
    """Both directions: every constraint in either schema exists in the other."""
    from relay.policy import ALLOWED_CAPABILITIES

    js = json.loads((ROOT / "schemas" / "task.schema.json").read_text(encoding="utf-8"))
    runtime = _TASK_INPUT_SCHEMA
    assert set(js["properties"]) == set(runtime["properties"])
    assert set(js["required"]) == set(runtime["required"])
    assert js["additionalProperties"] is False and runtime["additional_properties"] is False
    for field, spec in runtime["properties"].items():
        jprop = js["properties"][field]
        assert set(jprop) <= _ALLOWED_KEYWORDS, (field, set(jprop) - _ALLOWED_KEYWORDS)
        assert jprop["type"] == _TYPES[spec["type"]], field
        assert set(jprop.get("enum", [])) == set(spec.get("enum", [])), field
        assert jprop.get("minLength") == spec.get("min_length"), field
        if spec["type"] is list:
            assert jprop["items"]["type"] == _TYPES[spec["items"]], field
            if field != "requested_actions":
                assert set(jprop["items"]) == {"type"}, field
    # The one item-level enum is enforced by policy, not the structural validator.
    assert set(js["properties"]["requested_actions"]["items"]["enum"]) == ALLOWED_CAPABILITIES


@pytest.mark.parametrize("field", ["project_id", "repository", "objective"])
def test_empty_required_strings_rejected(field):
    from relay.schemas import ValidationError

    data = {"project_id": "p", "repository": "r", "objective": "o", field: ""}
    with pytest.raises(ValidationError):
        validate_task_input(data)


def test_unknown_protocol_version_rejected():
    from relay.schemas import ValidationError

    with pytest.raises(ValidationError):
        validate_task_input({"project_id": "p", "repository": "r", "objective": "o",
                             "protocol_version": "9.9"})


@pytest.mark.parametrize("content,message", [
    (None, "Cannot read task file"),
    (b"{not json", "is not valid JSON"),
    ('{"a": 1}'.encode("utf-16"), "is not UTF-8 encoded"),
])
def test_create_task_reports_bad_file_without_traceback(tmp_path, content, message):
    task_file = tmp_path / "task.json"
    if content is not None:
        task_file.write_bytes(content)
    result = subprocess.run(
        [sys.executable, "-m", "relay", "--db", str(tmp_path / "r.db"),
         "create-task", str(task_file)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1
    assert message in result.stderr
    assert "No task was created" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "r.db").exists(), "a bad file must not create the database"


def test_create_task_accepts_utf8_bom(tmp_path):
    sample = json.loads((ROOT / "sample_task.json").read_text(encoding="utf-8"))
    sample["repository"] = str(tmp_path)
    task_file = tmp_path / "task.json"
    task_file.write_bytes(codecs.BOM_UTF8 + json.dumps(sample).encode("utf-8"))
    result = subprocess.run(
        [sys.executable, "-m", "relay", "--db", str(tmp_path / "r.db"),
         "create-task", str(task_file)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Status: READY" in result.stdout
