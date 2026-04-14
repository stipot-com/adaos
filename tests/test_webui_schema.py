from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError


def _load_schema() -> dict:
    path = Path(__file__).resolve().parents[1] / "src" / "adaos" / "abi" / "webui.v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_webui_schema_accepts_staged_load_hints() -> None:
    schema = _load_schema()
    payload = {
        "apps": [
            {
                "id": "prompt_ide",
                "title": "Prompt IDE",
                "load": {"structure": "visible", "data": "interaction", "focus": "primary"},
            }
        ],
        "widgets": [
            {
                "id": "chat_widget",
                "type": "ui.chat",
                "load": {
                    "structure": "visible",
                    "data": "deferred",
                    "focus": "off_focus",
                    "offFocusReadyState": "hydrating",
                },
            }
        ],
        "registry": {
            "modals": {
                "prompt_modal": {
                    "title": "Prompt",
                    "load": {
                        "structure": "interaction",
                        "data": "deferred",
                        "focus": "off_focus",
                        "offFocusReadyState": "hydrating",
                    },
                    "schema": {
                        "id": "prompt_modal",
                        "load": {"structure": "interaction", "data": "deferred", "focus": "off_focus"},
                        "layout": {"type": "single", "areas": [{"id": "main"}]},
                        "widgets": [
                            {
                                "id": "prompt_widget",
                                "type": "ui.chat",
                                "area": "main",
                                "load": {"structure": "visible", "data": "deferred", "focus": "off_focus"},
                            }
                        ],
                    },
                }
            }
        },
    }

    Draft202012Validator(schema).validate(payload)


def test_webui_schema_rejects_scheduler_specific_load_details() -> None:
    schema = _load_schema()
    payload = {
        "widgets": [
            {
                "id": "chat_widget",
                "type": "ui.chat",
                "load": {"structure": "visible", "scheduler": "critical_path"},
            }
        ]
    }

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)
