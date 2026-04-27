# Codex MCP для test hub

## Рекомендуемое подключение VS Code Codex

Для текущего сценария VS Code Codex предпочтительно прямое remote MCP-подключение к root-published HTTP endpoint:

- MCP URL: `https://<zone>.api.inimatic.com/v1/root/mcp`
- bearer token env var: `ADAOS_ROOT_MCP_AUTH`

Практический порядок такой:

1. Через `infra_access_skill` выпустите свежий MCP session lease для нужного target.
2. Возьмите из ответа `mcp_http_url`.
3. Сохраните `access_token` в переменную окружения ОС `ADAOS_ROOT_MCP_AUTH`.
4. В настройке MCP сервера для VS Code Codex укажите этот URL и эту env var.

На Windows:

```powershell
setx ADAOS_ROOT_MCP_AUTH "mcp_..."
```

Важно:

- `setx` обновляет окружение только для новых процессов
- уже открытые VS Code, Codex, терминалы и MCP helper-процессы продолжают жить со старым bearer
- после ротации bearer может понадобиться полный перезапуск VS Code, если Codex продолжает использовать старый токен
- после переключения zone с legacy backend-local Root MCP surface на canonical proxied root surface нужно выпустить свежий bearer; старые backend-local MCP session lease не должны считаться валидными

Перед подключением Codex bearer удобно проверить вручную:

```powershell
curl -i https://ru.api.inimatic.com/v1/root/mcp/foundation `
  -H "Authorization: Bearer $env:ADAOS_ROOT_MCP_AUTH"
```

и:

```powershell
curl -i https://ru.api.inimatic.com/v1/root/mcp `
  -H "Authorization: Bearer $env:ADAOS_ROOT_MCP_AUTH" `
  -H "Content-Type: application/json" `
  -d '{"jsonrpc":"2.0","id":"1","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

## Локальный bridge MVP

Этот документ описывает текущий MVP-сценарий подключения Codex в VS Code к test hub через AdaOS Root MCP.

Текущая реализация намеренно сделана как локальный `stdio` bridge:

`Codex в VS Code -> local bridge -> RootMcpClient -> Root MCP API -> managed test hub`

Такой путь сохраняет SDK внутренним, не создаёт ложного прямого SDK surface и соответствует текущему состоянию `Phase 1` для `Root MCP Foundation`.

## Что покрывает этот MVP

- scoped-подключение Codex к одному managed target, обычно `hub:<subnet_id>`
- базовые read-first operational methods для target
- выдачу токена через опубликованный target-side surface `infra_access_skill`
- хранение profile и token files в workspace-local каталоге `.adaos/mcp/`

Сейчас bridge публикует такие tools:

- `foundation`
- `get_architecture_catalog`
- `get_sdk_metadata`
- `get_template_catalog`
- `get_public_skill_registry`
- `get_public_scenario_registry`
- `list_managed_targets`
- `get_managed_target`
- `get_operational_surface`
- `get_status`
- `get_runtime_summary`
- `get_activity_log`
- `get_capability_usage_summary`
- `get_logs`
- `run_healthchecks`
- `recent_audit`
- `get_yjs_load_mark_history`
- `get_yjs_logs`
- `get_skill_logs`
- `get_adaos_logs`
- `get_events_logs`
- `get_subnet_info`
- `get_subnet_analysis_health`
- `get_subnet_timeline`
- `get_subnet_diagnostics`

## Почему это локальный bridge, а не direct remote MCP

Codex умеет регистрировать MCP servers напрямую, но текущий AdaOS root surface пока является Root MCP API foundation, а не полноценным remote MCP transport.

Кроме того, текущий AdaOS scope выражается через `root_url + subnet_id + zone + bounded access token`, тогда как нативный remote HTTP MCP flow у Codex лучше подходит под модель `url + bearer`.

Поэтому текущий MVP использует локальный bridge-процесс, который стартует сам Codex. Bridge читает workspace-local profile и token file, а затем переводит MCP tool calls в вызовы `RootMcpClient`.

## Предварительные условия

Перед настройкой убедитесь, что:

- workspace подключён к нужному root
- test hub виден на root как managed target
- target публикует `infra_access_skill`, если нужны operational tools
- выполнен `adaos dev root login`, либо у вас есть `ROOT_TOKEN` / `ADAOS_ROOT_TOKEN`

Для `get_logs` и `run_healthchecks` target сейчас должен публиковать `infra_access_skill` с `execution_mode=local_process`.

## Рекомендуемая настройка

### 1. Подготовить bridge profile для Codex

Запустите:

```powershell
adaos dev root mcp prepare-codex
```

По умолчанию команда:

- вычислит `target_id` как `hub:<subnet_id>`
- выпустит bounded MCP access token для `codex-vscode`
- запишет profile file в `.adaos/mcp/adaos-test-hub.profile.json`
- запишет token file в `.adaos/mcp/adaos-test-hub.token`
- выведет точную команду `codex mcp add ...` для регистрации bridge

Полезные варианты:

```powershell
adaos dev root mcp prepare-codex --target-id hub:test-subnet --ttl-seconds 14400
adaos dev root mcp prepare-codex --owner-token $env:ROOT_TOKEN
adaos dev root mcp prepare-codex --apply-codex
```

`--apply-codex` сразу обновит `~/.codex/config.toml`.

### 2. Зарегистрировать bridge в Codex

Скопируйте команду, которую напечатал `prepare-codex`, либо используйте ту же форму вручную:

```powershell
codex mcp add adaos-test-hub --env ADAOS_MCP_PROFILE=D:\git\adaos\.adaos\mcp\adaos-test-hub.profile.json -- D:\git\adaos\.venv\Scripts\python.exe -m adaos dev root mcp serve
```

Ключевые части:

- `ADAOS_MCP_PROFILE`
  указывает bridge на workspace-local profile JSON
- Python command запускает локальный `stdio` bridge

Сам токен не хранится в `~/.codex/config.toml`; bridge читает его из token file, на который ссылается profile.

## Каталоги workspace

Локальный bridge теперь использует явное разделение директорий:

- `.adaos/mcp/`
  profile и token files для Codex bridge
- `.adaos/state/root_mcp/`
  state и cache Root MCP: descriptor cache, session registry, control reports
- `.adaos/logs/`
  локальные логи AdaOS-процессов на текущей машине; это не cache ответов MCP

### 3. Проверить регистрацию в Codex

```powershell
codex mcp list
codex mcp get adaos-test-hub
```

После этого откройте Codex в VS Code и дайте простой запрос, например:

```text
Use the AdaOS test-hub MCP tools to inspect the target operational surface, status, and runtime summary.
```

В tool trace вы увидите namespaced tools вида:

- `mcp__adaos-test-hub__get_operational_surface`
- `mcp__adaos-test-hub__get_status`
- `mcp__adaos-test-hub__get_runtime_summary`

## Ротация и обновление

Чтобы перевыпустить токен, достаточно снова запустить:

```powershell
adaos dev root mcp prepare-codex --apply-codex
```

Bridge читает token file на каждом вызове, поэтому при ротации токена не нужно заново менять server definition, если путь к profile остаётся тем же.

Чтобы удалить регистрацию из Codex:

```powershell
codex mcp remove adaos-test-hub
```

## Troubleshooting

### Target не зарегистрирован

По умолчанию команда работает с `--ensure-target`, поэтому может создать минимальную запись test target на root. Если реальное состояние target всё равно не появляется, проверьте control reports и target registration path.

### Не выпускается токен

`prepare-codex` использует target-scoped путь `hub.issue_access_token`. Если это не работает, обычно target ещё не публикует token management через `infra_access_skill`.

Проверьте:

- `get_operational_surface`
- target control reports на root
- draft/installed state `infra_access_skill` на hub

### Не работают logs или healthchecks

Сейчас эти методы зависят от `execution_mode=local_process`. Если target публикует только `reported_only`, status и observability tools будут работать, а bounded execution tools — нет.

Выделенные log-tools `get_adaos_logs`, `get_events_logs`, `get_skill_logs` и `get_yjs_logs` теперь по умолчанию используют `scope=subnet_active`. Это нужно, чтобы пустые `root_local` ответы не маскировали рабочий hub-root путь observability. `scope=root_local` стоит указывать только тогда, когда нужны именно логи локальной машины, где запущен bridge или root service.

Эти log-tools теперь также возвращают явные блоки `provenance` и `health`. Так проще понять, читаете ли вы root-local логи, healthy subnet-active aggregation или degraded/partial subnet-active результат.

Для Phase 7 анализа подсети теперь лучше начинать с `get_subnet_analysis_health`. Этот метод сводит в одну оценку доверие к snapshot state, session registry, audit history и `subnet_active` log channels перед более глубоким разбором memory или incident проблем.

`get_activity_log` теперь лучше воспринимать как компактный audit-derived activity feed. Когда нужна более структурированная история, стоит использовать `get_subnet_timeline`, где события уже разложены по классам: control-report ingest, profile ops, session activity и target operations.

`get_subnet_diagnostics` стоит использовать, когда нужны typed pressure-oriented projections вместо сырых логов. Теперь он сводит компактное состояние route backlog и pending ack, pressure по выбранному YJS webspace и недавние root-ingested memory-profile sessions для текущей подсети.

Session-management views теперь нормализуют expired MCP session leases при чтении, поэтому в обычных list/get сценариях просроченные lease больше не должны оставаться видимыми как `active`.

## Текущие границы MVP

Этот MVP пока намеренно не включает:

- direct remote MCP transport от root к Codex
- arbitrary shell access
- unrestricted deploy или rollback
- публичный SDK import path для внешних MCP clients

Это остаётся задачей следующих фаз `Root MCP Foundation`.
