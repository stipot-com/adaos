# Сценарии и целевое состояние (draft)

> NOTE: этот документ — черновик для сохранения целостной структуры MkDocs.
> Идеи будут уточняться по мере развития `ScenarioRuntime` и Prompt IDE.

Долгосрочная цель эволюции сценариев в AdaOS — сместиться от чисто процедурных
flow вида «выполни эти шаги» к подходу **целевого состояния** (target state):

- описываем *что* должно быть в результате (данные, UI, активные навыки);
- рантайм решает *как* к этому прийти (reconciliation, projections, workflows).

Сегодня эта идея частично проявляется в:

- `data_projections` в `scenario.yaml`, которые описывают, как логические слоты
  (`scope/slot`) сопоставляются с конкретными backend’ами (Yjs, KV, SQL);
- секции `ui` в `scenario.json` для desktop‑сценариев — описывает желаемую модель UI
  (widgets, модалки, каталоги), которая проецируется в Yjs;
- секции `workflow` (Prompt IDE) — описывает высокоуровневые состояния и действия,
  а не жёстко прошитый control‑flow.

Для текущих деталей реализации см.:

- `docs/scenarios.md` — манифесты сценариев и `data_projections`;
- `docs/concepts/scenario-first-launch.md` — первый запуск сценария и конвейер desktop‑UI;
- `src/adaos/sdk/scenarios/runtime.py` — процедурный `ScenarioRuntime`;
- `src/adaos/services/scenario/webspace_runtime.py` — проекция desktop/webspace;
- `src/adaos/services/scenario/workflow_runtime.py` — workflow‑проекция и интеграция Prompt IDE.

