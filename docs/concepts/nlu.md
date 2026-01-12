# NLU in AdaOS

This document describes the current production MVP direction for intent detection in AdaOS.

## MVP baseline

- **Pipeline**: `regex` → `rasa (service-skill)` → `teacher (LLM in the loop)`
- **System boundary**: the interpreter code is **one**, only **data** varies per scenario/skill.
- **Transport**: intent detection is integrated into AdaOS event bus (not CLI-only).

## Event flow (high level)

1. UI / Telegram / Voice publishes a request:
   - `nlp.intent.detect.request` `{ text, webspace_id, request_id, _meta... }`
2. `nlu.pipeline` tries:
   - **regex** rules (fast, deterministic)
   - if not matched → emits `nlp.intent.detect.rasa` (delegates to Rasa service)
3. If an intent is found:
   - `nlp.intent.detected` `{ intent, confidence, slots, text, webspace_id, request_id, via }`
4. If intent is not obtained:
   - `nlp.intent.not_obtained` `{ reason, text, via, webspace_id, request_id }`
   - UI gets a human-friendly message from router (and the request is recorded for training).
5. If teacher is enabled:
   - `nlp.teacher.request` is emitted for LLM/teacher processing.
   - teacher may propose:
     - `nlp.teacher.revision.suggested` (revise NLU dataset)
     - `nlp.teacher.candidate.proposed` (new skill/scenario candidate for future implementation)

## Rasa as a service-skill

Rasa is treated as a **service-type skill** (separate Python/venv, managed lifecycle) to avoid dependency conflicts with the hub runtime.

Key properties:

- **Autostart / supervision**: hub starts the service skill and monitors:
  - health checks,
  - crash frequency,
  - request failures / timeouts.
- **Issues + doctor loop**:
  - hub emits `skill.service.issue` on repeated problems,
  - hub emits `skill.service.doctor.request` with status/log tail,
  - a doctor component can produce `skill.service.doctor.report` (LLM doctor can be plugged later).

## Teacher-in-the-loop (LLM)

When `regex` and `rasa` do not produce an intent, AdaOS can call an LLM teacher to:

- propose a **new NLU revision** (intent name + examples + slots), or
- propose a **candidate feature** (new skill / scenario) for future implementation, or
- decide to ignore (non-actionable message).

Teacher data is projected into `data.nlu_teacher.*` for UI inspection.

## Web UI: “NLU Teacher”

In the default web desktop scenario the NLU Teacher UI is implemented as a declarative schema modal that reads:

- `data/nlu_teacher/revisions`
- `data/nlu_teacher/candidates`
- `data/nlu_teacher/llm_logs`

No domain-specific Angular components are required: only generic widgets (`ui.list`, `ui.jsonViewer`) + schema.

## Debugging

To reduce console noise, frontend debug logs are muted by default.

- Enable verbose UI console output: set `localStorage['adaos.debug'] = '1'` and reload.

## Later (not MVP)

- Rhasspy / offline NLU
- Retriever-style NLU (graph/context retrieval)
- Multi-step, stateful NLU workflows across scenarios

