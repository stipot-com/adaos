# SDK Control Plane

В текущем коде control plane распределен между:

- локальным HTTP API
- CLI-командами, которые разрешают и вызывают этот API
- SDK и service helper'ами, которые дают стабильные операции для более высокоуровневых flow

## Что реализовано сейчас

- install, update, runtime prepare и activation flow для навыков
- install и sync flow для сценариев
- status, reliability, join, role и member-update flow для узлов
- service supervision и issue reporting
- webspace и desktop control для Yjs-backed state
- canonical self-object access через SDK-first control-plane helpers
- canonical skill/scenario object access через SDK-first helpers до расширения внешнего API
- canonical reliability projection access для LLM и skills, включая runtime component objects и action metadata
- canonical neighborhood projection access поверх subnet-directory peers, root connectivity и nearby capacity snapshots
- canonical workspace, profile, browser-session, device, quota и local capacity objects через SDK-first helpers
- canonical kind и relation registries в `adaos.services.system_model.model`, чтобы SDK, API и LLM projections использовали один и тот же словарь
- shared governance и action-role defaults, чтобы SDK-facing objects последовательно несли owner, visibility и role hints
- local inventory projection, объединяющая node, workspace, browser, device, skill, scenario, capacity и selected reliability-derived root/quota objects для LLM-oriented reasoning

## Связанные модули

- `adaos.apps.cli.active_control`
- `adaos.apps.api.*`
- `adaos.sdk.manage.*`
- `adaos.sdk.control_plane`
- `adaos.sdk.data.control_plane`
- `adaos.services.system_model.*`
- `adaos.services.reliability`
- runtime services в `adaos.services.*`
