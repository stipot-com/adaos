# NLU Teacher (LLM) MVP

This document describes the minimal teacher-in-the-loop implementation for AdaOS NLU.

## Pipeline (MVP)

1. Router emits `nlp.intent.detect.request` (`text` + `webspace_id` + `request_id`).
2. `nlu.pipeline` tries:
   - built-in + dynamic `regex` (fast, deterministic)
   - if not matched -> delegates to Rasa service (`nlp.intent.detect.rasa`)
3. If intent is found -> `nlp.intent.detected { via: "regex" | "regex.dynamic" | "rasa" }`.
4. If intent is not obtained -> `nlp.intent.not_obtained { reason, via, ... }`.
5. Teacher bridge reacts to `nlp.intent.not_obtained` and emits:
   - `nlp.teacher.request { webspace_id, request }`
6. Teacher runtimes store state for UI inspection (YJS, per webspace):
   - `data.nlu_teacher.events[]` (includes `llm.request` / `llm.response`)
   - `data.nlu_teacher.candidates[]` (regex rules / skill candidates / scenario candidates)
   - `data.nlu_teacher.revisions[]` (proposed dataset revisions)
   - `data.nlu_teacher.llm_logs[]` (request/response logs; debugging)
7. Teacher state is also persisted on disk so it survives YJS reload/reset:
   - `.adaos/state/skills/nlu_teacher/<webspace_id>.json`

## Enable

Set env vars on hub:

- `ADAOS_NLU_TEACHER=1`
- `ADAOS_NLU_LLM_TEACHER=1`
- optional: `ADAOS_NLU_LLM_MODEL=gpt-4o-mini`
- optional: `ADAOS_NLU_LLM_TIMEOUT_S=20`

## Teacher context (inputs)

LLM teacher receives a compact context snapshot (per webspace), including:

- current scenario id
- scenario-level NLU (`scenario.json:nlu`)
- catalog of apps/widgets (with origins)
- installed ids
- built-in regex rules (`nlu.pipeline`)
- dynamic regex rules (`data.nlu.regex_rules`)
- selected skill NLU artifacts (e.g. `interpreter/intents.yml`)

Goal: prefer improving existing intents (regex rule / dataset revision) over creating a new capability, when possible.

## Apply (manual, MVP)

Apply is manual in MVP:

- apply a proposed dataset revision:
  - `nlp.teacher.revision.apply { revision_id, intent, examples[], slots }`
- apply a teacher candidate (preferred UI command):
  - `nlp.teacher.candidate.apply { candidate_id }`
  - for `regex_rule` candidates the runtime delegates to `nlp.teacher.regex_rule.apply { intent, pattern }`

The default Web UI provides Apply buttons via the schema-driven **NLU Teacher** modal.

## Example: improve existing intent via regex rule

Utterance: `покажи температуру в Москве`

Assume built-in weather regex only matches `погода` / `weather`, so the regex stage misses the intent.

Expected teacher decision:

- `decision="propose_regex_rule"`
- `regex_rule.intent="desktop.open_weather"`
- `regex_rule.pattern` should be a Python regex with named capture groups, e.g. `(?P<city>...)`

After you click **Apply** on the regex-rule candidate (UI emits `nlp.teacher.candidate.apply`):

- a dynamic rule is stored under `data.nlu.regex_rules`
- the next time the same utterance is sent, `nlu.pipeline` should resolve it as `via="regex.dynamic"` without calling the LLM
