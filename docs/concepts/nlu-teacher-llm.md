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
- catalog of apps/widgets (with origins) + installed ids
- built-in regex rules (`nlu.pipeline`)
- existing regex rules (from skills/scenarios + legacy per-webspace cache)
- routing hints (`intent_routes`: scenario intent -> callSkill topic -> skill)
- system actions visible in the current scenario (`system_actions`) and a published host action catalog (`host_actions`)
- skill manifests (`skills_manifest`: tools/events/llm_policy summary for installed skills)

Goal: prefer improving existing intents (regex rule / dataset revision) over creating a new capability, when possible.

## Apply

Apply can be triggered from UI or programmatically:

- apply a proposed dataset revision:
  - `nlp.teacher.revision.apply { revision_id, intent, examples[], slots }`
- apply a teacher candidate:
  - `nlp.teacher.candidate.apply { candidate_id, target? }`
  - for `regex_rule` candidates the runtime delegates to `nlp.teacher.regex_rule.apply { intent, pattern, target? }`

## Where regex rules are stored

The teacher does not “bake” regexes into the hub code. A rule is stored as data owned by a workspace artifact:

- **Skill-owned** (preferred): `.adaos/workspace/skills/<skill>/skill.yaml` → `nlu.regex_rules[]`
- **Scenario-owned**: `.adaos/workspace/scenarios/<scenario>/scenario.json` → `nlu.regex_rules[]`
- **Legacy runtime cache**: mirrored into YJS `data.nlu.regex_rules[]` so it starts matching immediately after Apply.

Every rule has a stable identity: `id="rx.<uuid>"`.

## Target selection (skill vs scenario)

When the teacher proposes a regex rule, it should also propose a storage target:

- Prefer the skill that actually handles the intent (derived from scenario intent `callSkill` actions + skill `events.subscribe`).
- If the intent triggers host/system behavior (`callHost`), the target is usually the scenario.

Apply supports a UI override (“Apply to Scenario”), in addition to an LLM-suggested target.

## Auto-apply policy (trusted skills)

Skills can opt into automatic application of teacher-proposed regex rules:

- `skill.yaml: llm_policy.autoapply_nlu_teacher: true`

If enabled and the candidate target is that skill, the hub auto-emits `nlp.teacher.candidate.apply` after a candidate is proposed.

## Observability: regex usage journal

Each time the dynamic regex stage matches, the hub appends a JSONL record to:

- `state/nlu/regex_usage.jsonl`

This is intended for later cleanup/optimization (identify dead rules, consolidate duplicates, etc.).

## Example: improve existing intent via regex rule

Utterance: `Покажи температуру в Берлине`

Assume built-in weather regex only matches `погода` / `weather`, so the regex stage misses the intent.

Expected teacher decision:

- `decision="propose_regex_rule"`
- `regex_rule.intent="desktop.open_weather"`
- `regex_rule.pattern` should be a Python regex with named capture groups, e.g. `(?P<city>...)`
- `target` should usually be the owning skill (e.g. `{"type":"skill","id":"weather_skill"}`)

After you click **Apply** (UI emits `nlp.teacher.candidate.apply`):

- the rule is persisted into the chosen owner (skill/scenario) and mirrored into `data.nlu.regex_rules`
- the next time the same utterance is sent, `nlu.pipeline` should resolve it as `via="regex.dynamic"` without calling the LLM
