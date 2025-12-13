# Skills and scenarios

This document gives a high-level view of how **skills** and **scenarios** fit together in AdaOS.

- Skills implement reusable business capabilities and expose tools/event handlers.
- Scenarios orchestrate skills, define data flows, and (for desktop cases) attach UI.

See:

- `docs/skills.md` – how to structure and describe a skill (`skill.yaml`, `handlers/main.py`).
- `docs/scenarios.md` – how to structure and describe a scenario (`scenario.yaml`, `scenario.json`).

At runtime:

- The **scenario** selects which skills are relevant (`depends`).
- The **web desktop** scenario (`web_desktop`) loads scenario + skill UI contributions and projects them into Yjs (`WebspaceScenarioRuntime`).
- The **Prompt IDE** scenario (`prompt_engineer_scenario`) uses the workflow section and tools from `prompt_engineer_skill`
  to drive the dev workflow.

