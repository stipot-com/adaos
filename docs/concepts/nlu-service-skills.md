# NLU as Service Skills

AdaOS treats some components as long-running **service skills** instead of in-process Python modules.

## Motivation

- Avoid Python/ABI conflicts (e.g. Rasa requires Python `<3.11`).
- Allow heavy components to run in isolated per-skill environments.
- Provide a uniform lifecycle: install → start → healthcheck → restart → observe.

## Key Events

- Input: `nlp.intent.detect` `{ text, webspace_id?, _meta? }`
- Output: `nlp.intent.detected` `{ intent, confidence, slots, text, webspace_id?, _raw? }`

## Rasa NLU Service Skill

- Skill: `.adaos/workspace/skills/rasa_nlu_service_skill`
- Runtime:
  - `runtime.kind: service`
  - `runtime.env.mode: venv`
  - `runtime.env.python: 3.10`
- API:
  - `GET /health`
  - `POST /parse` `{ "text": "..." }`
  - `POST /train` `{ "project_dir": "...", "out_dir": "...", "fixed_model_name": "interpreter_latest" }`

## Supervisor

`src/adaos/services/skill/service_supervisor.py` discovers service skills in the skills workspace and:

- creates a per-skill venv (if configured),
- installs dependencies from `skill.yaml.dependencies` and optional `requirements.in`,
- starts the service process,
- checks `/health`,
- restarts on crash.

## Training Flow

1. `adaos.services.nlu.data_registry.sync_from_scenarios_and_skills()` updates the interpreter workspace files (pure Python).
2. `adaos.services.nlu.rasa_training_bridge` optionally triggers training by calling the Rasa service `/train`.

Enable auto-train with `ADAOS_NLU_AUTOTRAIN=1`.

