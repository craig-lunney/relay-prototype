from __future__ import annotations

from typing import Any


class ValidationError(ValueError):
    """Raised when structured input fails schema validation. Fail-closed."""


# ── Schema definitions ────────────────────────────────────────────────────────
#
# Schema keys:
#   name                 str   — used in error messages
#   required             list  — fields that must be present
#   properties           dict  — field -> {"type": T, "items": T, "enum": frozenset}
#   additional_properties bool — False means unexpected fields are rejected
#
# Security note on additional_properties:
#   Task input is a controlled definition format.  Unexpected fields (e.g.
#   "approved": true) are rejected — they may indicate injection attempts.
#
#   ImplementationReport is agent-controlled evidence.  Extra fields are
#   allowed and explicitly ignored.  No extra field can grant authority:
#   all decisions (approval, completion, status) are made by Relay code,
#   never by inspecting arbitrary report fields.

_TASK_INPUT_SCHEMA: dict[str, Any] = {
    "name": "task_input",
    "additional_properties": False,
    "required": ["project_id", "repository", "objective"],
    "properties": {
        "project_id":          {"type": str},
        "repository":          {"type": str},
        "objective":           {"type": str},
        "base_ref":            {"type": str},
        "working_ref":         {"type": str},
        "requirements":        {"type": list, "items": str},
        "acceptance_criteria": {"type": list, "items": str},
        "requested_actions":   {"type": list, "items": str},
        "forbidden_actions":   {"type": list, "items": str},
        "required_tests":           {"type": list, "items": str},
        "expected_deleted_tests":   {"type": list, "items": str},
        "expected_new_files":       {"type": list, "items": str},
        "approval_policy":          {"type": str, "enum": frozenset({"auto", "manual"})},
    },
}

_REPORT_SCHEMA: dict[str, Any] = {
    "name": "implementation_report",
    "additional_properties": True,
    "required": [
        "protocol_version", "message_type", "task_id", "execution_id",
        "status", "summary",
    ],
    "properties": {
        "protocol_version":        {"type": str},
        "message_type":            {"type": str},
        "task_id":                 {"type": str},
        "execution_id":            {"type": str},
        "status":                  {"type": str},
        "summary":                 {"type": str},
        "files_changed":           {"type": list, "items": str},
        "files_added":             {"type": list, "items": str},
        "files_deleted":           {"type": list, "items": str},
        "tests_run":               {"type": list, "items": str},
        "risks":                   {"type": list, "items": str},
        "uncertainties":           {"type": list, "items": str},
        "blockers":                {"type": list, "items": str},
        "recommended_next_action": {"type": str},
    },
}


# ── Core validator ────────────────────────────────────────────────────────────

def _validate(data: Any, schema: dict[str, Any]) -> None:
    """Validate data against a schema. Raises ValidationError on any violation."""
    name: str = schema["name"]

    if not isinstance(data, dict):
        raise ValidationError(
            f"{name}: expected a mapping, got {type(data).__name__}"
        )

    required: list[str] = schema.get("required", [])
    properties: dict[str, dict] = schema.get("properties", {})
    allow_additional: bool = schema.get("additional_properties", True)

    for field in required:
        if field not in data:
            raise ValidationError(
                f"{name}: required field {field!r} is missing"
            )

    for field, spec in properties.items():
        if field not in data:
            continue
        value = data[field]
        expected_type: type = spec["type"]
        if not isinstance(value, expected_type):
            raise ValidationError(
                f"{name}: field {field!r} must be {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )
        if expected_type is list and "items" in spec:
            item_type: type = spec["items"]
            for i, item in enumerate(value):
                if not isinstance(item, item_type):
                    raise ValidationError(
                        f"{name}: field {field!r}[{i}] must be "
                        f"{item_type.__name__}, got {type(item).__name__}"
                    )
        if "enum" in spec and value not in spec["enum"]:
            raise ValidationError(
                f"{name}: field {field!r} value {value!r} is not one of "
                f"{sorted(spec['enum'])}"
            )

    if not allow_additional:
        known = set(properties)
        extra = set(data) - known
        if extra:
            raise ValidationError(
                f"{name}: unexpected field(s) {sorted(extra)} — "
                "task input must not contain unrecognised fields"
            )


# ── Public validators ─────────────────────────────────────────────────────────

def validate_task_input(data: Any) -> None:
    """
    Validate raw task definition JSON before Task construction and state creation.

    Enforces: required fields present, correct types, no unexpected fields,
    valid approval_policy value.

    Unexpected fields are rejected because task input is a controlled definition
    format — an unknown field such as "approved": true may indicate an injection
    attempt and must not silently pass into the Task object.

    Does NOT evaluate business rules (e.g. allowed capabilities, production repo
    patterns) — that is the responsibility of PolicyEngine.validate_task().

    Raises ValidationError on any structural violation.
    """
    _validate(data, _TASK_INPUT_SCHEMA)


def validate_report(data: Any) -> None:
    """
    Validate a parsed ImplementationReport dict before it is consumed.

    The report is agent-controlled evidence, not authority. Extra fields
    (e.g. "approved": true, "policy_version": "unrestricted") are ALLOWED
    by the schema and explicitly IGNORED — they carry no decision authority.
    All authoritative decisions are made by Relay code from independently
    verified facts, never by inspecting report fields.

    Structural validation ensures:
      - Required fields are present and correctly typed.
      - List fields (files_changed, tests_run, etc.) contain strings only.

    Raises ValidationError if required structure is absent or types are wrong.
    """
    _validate(data, _REPORT_SCHEMA)
