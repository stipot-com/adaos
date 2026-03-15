from __future__ import annotations


def describe_system_actions() -> list[dict]:
    """
    Public host/system actions that can be triggered by scenarios (callHost) or UI.

    This list is intentionally conservative and mirrors what the hub accepts from the UI
    websocket gateway. It is provided to the LLM NLU Teacher as "capabilities context".
    """
    return [
        {
            "action": "desktop.scenario.set",
            "description": "Switch current desktop scenario for a webspace.",
            "params": {"scenario_id": "string", "webspace_id": "string"},
        },
        {
            "action": "desktop.toggleInstall",
            "description": "Toggle install of an app/widget on the desktop.",
            "params": {"type": "\"app\"|\"widget\"", "id": "string", "webspace_id": "string"},
        },
        {
            "action": "desktop.webspace.reload",
            "description": "Reload the current webspace UI/data projections.",
            "params": {"webspace_id": "string"},
        },
        {
            "action": "desktop.webspace.reset",
            "description": "Reset the current webspace UI/data projections (currently same as reload).",
            "params": {"webspace_id": "string"},
        },
        {
            "action": "scenario.workflow.action",
            "description": "Trigger a scenario workflow action (Prompt IDE etc).",
            "params": {"action": "string", "object_type": "string", "object_id": "string", "webspace_id": "string"},
        },
        {
            "action": "scenario.workflow.set_state",
            "description": "Set scenario workflow state (Prompt IDE etc).",
            "params": {"state": "string", "object_type": "string", "object_id": "string", "webspace_id": "string"},
        },
        {
            "action": "nlp.teacher.candidate.apply",
            "description": "Apply an NLU Teacher candidate (regex_rule/skill/scenario plan item).",
            "params": {"candidate_id": "string", "webspace_id": "string", "target?": "{type,id}"},
        },
        {
            "action": "nlp.teacher.revision.apply",
            "description": "Apply an NLU Teacher revision to scenario NLU examples.",
            "params": {"revision_id": "string", "intent": "string", "examples": "string[]", "slots": "object"},
        },
    ]

