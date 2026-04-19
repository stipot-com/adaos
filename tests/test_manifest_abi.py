from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, ValidationError


def _load_schema(name: str) -> dict:
    path = Path(__file__).resolve().parents[1] / "src" / "adaos" / "abi" / name
    return json.loads(path.read_text(encoding="utf-8"))


def test_skill_schema_accepts_runtime_activation_policy() -> None:
    schema = _load_schema("skill.schema.json")
    payload = {
        "name": "infrascope_skill",
        "version": "0.9.0",
        "runtime": {
            "python": "3.11",
            "activation": {
                "mode": "lazy",
                "startup_allowed": False,
                "background_refresh": False,
                "when": {
                    "scenarios_active": ["infrascope"],
                    "client_presence": True,
                    "webspace_scope": "active",
                    "webspaces": ["default"],
                },
            },
        },
    }

    Draft7Validator(schema).validate(payload)


def test_skill_schema_rejects_unknown_activation_mode() -> None:
    schema = _load_schema("skill.schema.json")
    payload = {
        "name": "bad_skill",
        "version": "1.0.0",
        "runtime": {
            "activation": {
                "mode": "hot",
            }
        },
    }

    with pytest.raises(ValidationError):
        Draft7Validator(schema).validate(payload)


def test_scenario_schema_accepts_runtime_skill_bindings() -> None:
    schema = _load_schema("scenario.schema.json")
    payload = {
        "id": "infrascope",
        "version": "0.6.0",
        "depends": ["legacy_skill"],
        "runtime": {
            "skills": {
                "required": ["infrascope_skill"],
                "optional": ["telemetry_skill"],
            }
        },
    }

    Draft7Validator(schema).validate(payload)


def test_scenario_schema_rejects_unknown_runtime_skill_field() -> None:
    schema = _load_schema("scenario.schema.json")
    payload = {
        "id": "infrascope",
        "version": "0.6.0",
        "runtime": {
            "skills": {
                "required": ["infrascope_skill"],
                "preferred": ["telemetry_skill"],
            }
        },
    }

    with pytest.raises(ValidationError):
        Draft7Validator(schema).validate(payload)
