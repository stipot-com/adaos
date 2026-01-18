# NLU as Service Skills

AdaOS treats some NLU components as long-running **service skills** instead of in-process Python modules.
General service-skill docs: `docs/concepts/service-skills.md`.

## Why service skills for NLU?

- Avoid Python/ABI conflicts (e.g. Rasa requires Python <3.11).
- Isolate heavy dependencies per component.
- Uniform lifecycle: install -> start -> health -> restart -> observe.

## Event interface

The hub NLU pipeline uses:

- `nlp.intent.detect.request { text, webspace_id, request_id, _meta }`
- `nlp.intent.detected { intent, confidence, slots, text, webspace_id, request_id, via }`
- `nlp.intent.not_obtained { reason, text, webspace_id, request_id, via }`

## Rasa NLU service skill

- Skill: `.adaos/workspace/skills/rasa_nlu_service_skill`
- Config: `.adaos/workspace/skills/rasa_nlu_service_skill/skill.yaml`
- Supervisor: `src/adaos/services/skill/service_supervisor.py`
- Bridge: `src/adaos/services/nlu/rasa_service_bridge.py`

### Runtime / environment

- `runtime.kind: service`
- `runtime.env.mode: venv`
- `runtime.env.python: 3.10`

The supervisor creates `state/services/rasa_nlu_service_skill/venv` and installs dependencies from:
- `skill.yaml: dependencies`
- optional `requirements.in` in the skill root

### HTTP API (provided by the service)

- `GET /health`
- `POST /parse` `{ "text": "..." }`
- `POST /train` `{ "project_dir": "...", "out_dir": "...", "fixed_model_name": "interpreter_latest" }`

### Self-management (issues)

Rasa bridge records service issues when parsing fails or times out:
- issue types: `rasa_failed`, `rasa_timeout`
- storage: `state/services/rasa_nlu_service_skill/issues.json`

If `service.self_managed.doctor.enabled: true`, these issues can also trigger:
- `skill.service.doctor.request` events (with log tail)
- persisted reports via `state/services/rasa_nlu_service_skill/doctor_reports.json`

## Training flow

1. `adaos.services.nlu.data_registry.sync_from_scenarios_and_skills()` syncs NLU data into interpreter workspace files.
2. `adaos.services.nlu.rasa_training_bridge` (optional) triggers training by calling the service `/train`.

Enable auto-train with `ADAOS_NLU_AUTOTRAIN=1`.
