# Scenarios and target state (draft)

> NOTE: This document is a placeholder to keep the MkDocs navigation consistent. The underlying ideas are
> still evolving and will likely be refined as the ScenarioRuntime and Prompt IDE mature.

The long‑term goal for scenarios in AdaOS is to shift from purely procedural flows (“run these steps”) towards a more
**target‑state** approach:

- Describe *what* the system should look like (data, UI, active skills).
- Let the runtime determine *how* to get there (reconciliation, projections, workflows).

Today this is partially reflected in:

- `data_projections` in `scenario.yaml`, which describe how conceptual data slots (`scope/slot`) map to concrete
  storage backends (Yjs, KV, SQL).
- The `ui` section in `scenario.json` for desktop scenarios, which describes the desired UI model (widgets, modals,
  catalog) projected into Yjs.
- The `workflow` section (used by Prompt IDE), which describes high‑level states and actions rather than hard‑coded
  control flow.

For current implementation details, refer to:

- `docs/scenarios.md` – scenario manifests and data projections.
- `src/adaos/sdk/scenarios/runtime.py` – procedural ScenarioRuntime.
- `src/adaos/services/scenario/webspace_runtime.py` – desktop/webspace projection logic.
- `src/adaos/services/scenario/workflow_runtime.py` – workflow projection and Prompt IDE integration.

