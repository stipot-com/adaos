# NLU in AdaOS

This document describes the current production MVP direction for intent detection in AdaOS.

## MVP baseline

- Pipeline: `regex` -> `rasa (service-skill)` -> `teacher (LLM in the loop)`
- System boundary: NLU runtime code is one; only **data** varies per scenario/skill.
- Transport: intent detection is integrated into AdaOS event bus (not CLI-only).

## Event flow (high level)

1. UI / Telegram / Voice publishes:
   - `nlp.intent.detect.request { text, webspace_id, request_id, _meta... }`
2. `nlu.pipeline` tries regex rules:
   - built-in rules (`nlu.pipeline`)
   - dynamic rules (`data.nlu.regex_rules`)
3. If regex does not match:
   - `nlp.intent.detect.rasa` is emitted (delegates to the Rasa service skill)
4. If an intent is found:
   - `nlp.intent.detected { intent, confidence, slots, text, webspace_id, request_id, via }`
5. If intent is not obtained:
   - `nlp.intent.not_obtained { reason, text, via, webspace_id, request_id }`
   - Router emits a human-friendly `io.out.chat.append` and records the request for NLU Teacher.
6. If teacher is enabled:
   - `nlp.teacher.request { webspace_id, request }` is emitted for teacher runtimes.

## Rasa as a service-skill

Rasa is treated as a **service-type skill** (separate Python/venv, managed lifecycle) to avoid dependency conflicts with the hub runtime.

The hub supervises:

- health checks
- crash frequency
- request failures/timeouts

Issues can trigger:

- `skill.service.issue`
- `skill.service.doctor.request` -> `skill.service.doctor.report` (LLM doctor can be plugged later)

## Teacher-in-the-loop (LLM)

When `regex` and `rasa` do not produce an intent, AdaOS calls an LLM teacher to:

- propose a **dataset revision** (existing intent + new examples + slots), or
- propose a **regex rule** to improve the `regex` stage, or
- propose a **new capability** (skill / scenario candidate), or
- decide to ignore (non-actionable).

Teacher receives scenario + skill context, including:

- current scenario NLU (`scenario.json:nlu`)
- installed catalog (apps/widgets + origins)
- existing dynamic regex rules (`data.nlu.regex_rules`)
- built-in regex rules (`nlu.pipeline`)
- selected skill-level NLU artifacts (e.g. `interpreter/intents.yml`)

Teacher state is projected into YJS under `data.nlu_teacher.*` for UI inspection (MVP; not durable storage).

## Web UI: NLU Teacher

In the default web desktop scenario the NLU Teacher UI is a schema-driven modal:

- Tabs: **User requests** / **Candidates**
- Grouping: entries are grouped by `request_id`
- Logs: request groups show event payloads inline (raw JSON)
- Apply actions:
  - `nlp.teacher.revision.apply`
  - `nlp.teacher.regex_rule.apply` (stores a dynamic rule under `data.nlu.regex_rules`, used by `nlu.pipeline`)

## Later (not MVP)

- Rhasspy / offline NLU
- Retriever-style NLU (graph/context retrieval)
- Multi-step, stateful NLU workflows across scenarios
