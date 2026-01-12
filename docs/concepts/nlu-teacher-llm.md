# NLU Teacher (LLM) — MVP

This document describes the minimal **LLM teacher-in-the-loop** implementation for AdaOS NLU.

## Pipeline (MVP)

1. Router emits `nlp.intent.detect.request` (text + webspace_id + request_id).
2. If `regex` matches → `nlp.intent.detected { via: "regex" }`.
3. Else try Rasa (service skill) → `nlp.intent.detected { via: "rasa" }`.
4. If no intent is obtained → `nlp.intent.not_obtained { via: "rasa", reason: ... }`.
5. Teacher bridge reacts to `nlp.intent.not_obtained` and emits:
   - `nlp.teacher.request { webspace_id, request }`
6. Teacher runtime creates a placeholder revision and stores teacher state for UI:
   - `data.nlu_teacher.revisions[]` (e.g. `status="pending"`).
7. LLM teacher consumes `nlp.teacher.request`, calls Root `/v1/llm/response`, and produces:
   - `nlp.teacher.candidate.proposed` (suggest new skill/scenario), and/or
   - `nlp.teacher.revision.proposed` (suggest NLU dataset revision),
   - also appends structured logs: `data.nlu_teacher.llm_logs[]`.

## Enable

Set env vars on hub:

- `ADAOS_NLU_TEACHER=1`
- `ADAOS_NLU_LLM_TEACHER=1`
- optional: `ADAOS_NLU_LLM_MODEL=gpt-4o-mini`
- optional: `ADAOS_NLU_LLM_TIMEOUT_S=20`

## Storage (YJS, MVP)

For MVP, teacher state is stored in YJS under `data.nlu_teacher` (projected per webspace):

- `revisions[]` — pending/proposed/applied/ignored revisions
- `candidates[]` — proposed skill/scenario candidates
- `llm_logs[]` — request/response logs (for debugging and UI)

This is intentionally not “durable storage”; later we can project from a persistent store into YJS.

## Apply (manual, MVP)

Apply is manual in MVP:

- emit `nlp.teacher.revision.apply` with:
  - `revision_id`
  - `intent`
  - `examples[]`
  - `slots`

The default Web UI provides this via the “NLU Teacher” modal, implemented as a declarative schema (`ui.list` + `ui.jsonViewer`) reading `data.nlu_teacher.*`.
