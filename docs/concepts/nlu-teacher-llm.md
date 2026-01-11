# NLU Teacher (LLM) — MVP

This document describes the minimal **LLM teacher-in-the-loop** implementation for AdaOS NLU.

## Pipeline

1. `nlp.intent.detect.request` (text + webspace_id + request_id)
2. If regex succeeds → `nlp.intent.detected via=regex`
3. Else try Rasa service → `nlp.intent.detected via=rasa`
4. Else → `nlp.intent.not_obtained via=rasa reason=...`
5. Teacher bridge persists request and emits:
   - `nlp.teacher.request { webspace_id, request }`
6. Teacher runtime creates a placeholder revision:
   - `data.nlu_teacher.revisions[]` with `status=pending`
7. LLM teacher consumes `nlp.teacher.request`, calls Root `/v1/llm/response`, and updates:
   - `status=proposed` with `proposal.intent/examples/slots`, or
   - `status=ignored`, or
   - appends `data.nlu_teacher.candidates[]` (skill/scenario candidates).

## Enable

Set env vars on hub:

- `ADAOS_NLU_TEACHER=1`
- `ADAOS_NLU_LLM_TEACHER=1`
- optional: `ADAOS_NLU_LLM_MODEL=gpt-4o-mini`
- optional: `ADAOS_NLU_LLM_TIMEOUT_S=20`

## Storage (YJS)

All state is stored in `data.nlu_teacher`:

- `items[]`: raw not-obtained samples
- `revisions[]`: pending/proposed/applied/ignored revisions
- `dataset[]`: applied items (status=`revision` for now)
- `candidates[]`: proposed skill/scenario candidates

## Apply

Apply is manual for MVP:

- emit `nlp.teacher.revision.apply` (or call the API):
  - `POST /api/nlu/teacher/{webspace_id}/revision/apply`

