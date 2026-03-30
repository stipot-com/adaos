# Веб интеграция

##

# 1) роли и каналы

* **owner CLI**: `adaos dev root init`, `adaos dev root login`
* **owner browser**: `app.inimatic.com` (SPA), WebAuthn + Socket.IO
* **root api**: http + socket.io, proxy fallback
* **hub**: `adaos api serve` (socket.io клиент к root, e2e шины к браузеру через root-прокси при необходимости)

# 2) модель доверия (коротко)

* корневой **Root-CA** выпускает: `subnet_id`, `hub_id`, ключи/серты хаба.
* **Browser** не ставим mTLS; аутентификация — WebAuthn + связка с owner/subnet.
* **Transport browser↔hub**: *рекомендуемый безопасный вариант*: ключ пары для канала генерирует **браузер** и отдаёт **только публичный ключ** (или общий секрет по ECDH) в хаб через root. (Если делать «хаб генерит и шлёт приватный» — оставим как временный debug-режим, но сразу помечаем как insecure.)
* **Root** может проксировать сообщения (socket proxy) без доступа к контенту, если шифруем end-to-end (JOSE/Noise).

# 3) идентификаторы и хранилище (Redis/DB)

```
subnet:{subnet_id}
hub:{hub_id}
owner:{owner_id}
session:web:{sid}            -> { owner_id?, browser_key_id?, stage, exp }
device_code:{code}           -> { owner_id, hub_id, exp, bind_sid? }
webauthn:cred:{cred_id}      -> { owner_id, browser_pubkey, sign_count }
pairing:challenge:{sid}      -> { challenge, exp }
e2e:browser_pub:{sid}        -> { pubkey, alg, exp }
online:hub:{hub_id}          -> socket_id
online:browser:{sid}         -> socket_id
route:{sid}                  -> { hub_id, e2e=on|off, last_seen }
```

# 4) HTTP эндпойнты (root)

```
POST  /v1/owner/login/device-code
-> { device_code: "123-456", verify_uri: "https://app.inimatic.com/owner-auth", expires_in }

POST  /v1/owner/login/verify
body: { device_code, sid }  // sid = web session id
-> { ok, owner_id, subnet_id, hub_id? }

POST  /v1/owner/webauthn/registration/challenge
body: { sid }
-> { publicKeyCredentialCreationOptions }

POST  /v1/owner/webauthn/registration/finish
body: { sid, credential }
-> { browser_key_id }

POST  /v1/owner/webauthn/login/challenge
body: { sid }
-> { publicKeyCredentialRequestOptions }

POST  /v1/owner/webauthn/login/finish
body: { sid, credential }
-> { session_jwt, browser_key_id }

POST  /v1/browser/pairing/offer
body: { sid, e2e_pubkey? }  // браузер генерит и присылает свой pubkey (рекомендовано)
-> { routed_to_hub: bool }

POST  /v1/hub/pairing/accept     // вызывается хабом через сокет-ивент, root просто валидирует
body: { sid, hub_id, hub_e2e_pubkey? }
-> { ok }
```

# 5) Socket.IO пространства / события

**namespaces**

* `/hub` (аутентификация по hub_id + токен/сертификат)
* `/owner` (браузер, авторизуется по session_jwt после WebAuthn)

**events**

* hub→root: `hub.online {hub_id}`
* browser→root: `owner.online {sid}`
* root→hub: `pairing.request {sid, owner_id, subnet_id, browser_pub?}`
* hub→root: `pairing.accept {sid, hub_e2e_pub?}`
* root: связывает и создаёт `route:{sid} -> hub_id`
* e2e relay (если прямого нет): `relay.to_hub {sid, frame}` / `relay.to_browser {sid, frame}`
  (где `frame` уже зашифрован end-to-end браузер↔хаб)

# 6) сценарии (последовательности)

## А. первичное подключение owner browser (через 6-значный код)

1. **owner CLI**: `adaos dev root login`
   → root: `device_code` (TTL ~10 мин)

2. **browser**: открывает `app.inimatic.com`
   SPA получает `sid`, показывает QR сессии + поле ввода кода (если не авторизован).

3. **owner вводит device_code** → `POST /v1/owner/login/verify {code,sid}`
   root: связывает `sid ↔ owner_id` (и, если есть, выбранный `hub_id`), `stage=preauth`.

4. **WebAuthn регистрация**:

   * `POST /webauthn/registration/challenge {sid}`
   * браузер `navigator.credentials.create`
   * `POST /webauthn/registration/finish {sid, credential}`
     root сохраняет `webauthn:cred:{cred_id}` и `browser_pubkey` (из attestation).

5. **Логин WebAuthn** (сразу после регистрации либо при следующих входах):

   * `POST /webauthn/login/challenge {sid}`
   * `navigator.credentials.get`
   * `POST /webauthn/login/finish {sid, credential}`
     → `session_jwt` для сокета `/owner`.

6. **pairing c hub**:

   * browser (уже в `/owner`) вызывает `POST /browser/pairing/offer {sid, e2e_pub?}`
   * root ищет онлайн-хаб `hub:{hub_id}`; если оффлайн, ставит `route:{sid}` и «ожидание».
   * root→hub: `pairing.request {sid, ...}` по `/hub`.
   * hub подтверждает: `pairing.accept {sid, hub_e2e_pub?}`.
   * root фиксирует `route:{sid}->{hub_id}`, публикует обе стороны, начинает relay (если нет прямого p2p).

7. **установка E2E** (рекомендовано)

   * если обе стороны прислали `e2e_pub`, стороны делают ECDH и согласуют `session_key` (JOSE/Noise NK).
   * root больше не видит содержимого `frame`.

## B. последующие входы

1. browser открывает SPA → `sid`
2. WebAuthn **login** (challenge → assertion → `session_jwt`)
3. root проверяет `route:{sid}`:

   * если хаб онлайн — создаём сокет-мост и «прозрачно» восстанавливаем канал;
   * если нет — в статус-строке SPA показываем «hub offline» и подписываемся на `hub.online`.

## C. запуск хаба

* `adaos api serve` поднимает `/hub` сокет к root с mTLS/токеном.
* шлёт `hub.online {hub_id}`; root отмечает `online:hub:{hub_id}`.
* root проверяет ожидающие `route:{sid}` для этого `hub_id` и рассылает `pairing.request` (авто-rebind).

# 7) безопасность / защита

* **device_code**: 6 цифр, TTL 10 мин, одноразовый, rate-limit по IP/сид.
* **WebAuthn**: platform authenticator (отпечаток/face), RPID: `app.inimatic.com`. Сохраняем `sign_count` и проверяем.
* **E2E**: JOSE (ECDH-ES + A256GCM) или Noise NK/IK. Минимум — ECDH поверх X25519 + HKDF, nonce-счётчик.
* **replay-защита**: все кадры `frame` — с монотонным `ctr`, root отбрасывает дубликаты даже в relay-режиме.
* **binding к owner/subnet**: `sid` после `verify` привязывается к `owner_id` и `subnet_id`; любые pairing-операции валидируются.
* **инвальдация**: logout стирает `session_jwt`; при компрометации — revoke `webauthn:cred:{cred_id}` и все `route:{sid}`.
* **debug-режим (временный)**: если очень надо, «хаб генерит секрет для канала» → зашифровать его на webauthn-публичный браузера (через `webauthn/registration` мы знаем его) и передать как `sealed_secret`. Браузер расшифрует через SubtleCrypto, root не видит.

# 8) состояния сессии (state-машина)

```
NEW -> PREAUTH (device_code verified) 
-> WEBREG (webauthn challenge issued) 
-> AUTH (webauthn finished, session_jwt valid) 
-> PAIRED (route:{sid} bound to hub) 
-> ONLINE (both sockets up, e2e ready)
```

# 9) сообщения Socket.IO (минимум полей)

**/owner**

* `owner.online { sid, session_jwt, browser_key_id }`
* `relay.to_hub { sid, frame, ctr }`
* `status { route, hub_online, lag_ms }`

**/hub**

* `hub.online { hub_id, subnet_id, caps }`
* `pairing.request { sid, owner_id, subnet_id, browser_pub? }`
* `pairing.accept { sid, hub_e2e_pub? }`
* `relay.to_browser { sid, frame, ctr }`

# 10) UI логика SPA (вкладка public как «единственная» до авторизации)

1. нет ключа → поле ввода кода + QR текущего `sid`.
2. ввели код → прячем поле, запускаем WebAuthn регистрацию (если первый раз) → логин.
3. показываем статус-бар:

   * «auth ok • hub: connecting…»
   * «hub: online (latency 42ms)»
   * «hub: offline (auto-reconnect)»
4. если разрыв — не выкидываем пользователя, сидим в `AUTH`, ждём `hub.online`.

# 11) CLI контур (ожидаемые ответы)

* `adaos dev root init` → печатает `subnet_id`, `hub_id`, путь к ключам, время жизни.
* `adaos dev root login` → `Open app.inimatic.com/owner-auth and enter code 123-456 (valid 10m)`; при успешном verify — подсветка «owner browser paired».
* `adaos api serve` → лог `connected to root as hub:{hub_id}`; после первого `pairing.accept` — «paired with sid:…».

# 12) обработка сбоев

* device_code неверный/просрочен → унифицированная ошибка `invalid_device_code`.
* WebAuthn провал → `registration_required` / `assertion_failed`.
* hub недоступен → SPA продолжает в `AUTH`, route остаётся, периодический ping на root.
* ротация ключей хаба → route переустанавливается прозрачно; e2e заново согласуется.

# 13) что кодим прямо сейчас (короткий план работ)

1. **root api**: эндпойнты из §4 + хранение из §3; socket пространства `/hub`, `/owner`; relay.
2. **hub**: клиент `/hub`, обработка `pairing.request` → `pairing.accept`; e2e-согласование.
3. **browser (SPA)**: экран ввода кода + WebAuthn (register/login) + сокет `/owner` + статус-бар.
4. **cli**: `login` печатает код и крутит «waiting for verify…» до успеха; `api serve` — авто-reconnect.
5. **безопасный e2e**: форсируем «браузер генерит pubkey», «хаб публикует свой pubkey», JOSE/Noise.

супер, идём без «хаб генерит ключ для браузера». фиксирую план «после авторизации» и первый набор артефактов (ui-layout, сценарий desktop, skill icon-manager, расширение weather_skill, базовые админ-скиллы). всё — mvp-уровень, готово к кодингу.

# 1) post-auth поведение web-клиента

* сразу после `AUTH + PAIRED + ONLINE`: скрываем поле кода и qr, показываем **чистый контейнер рабочего стола**.
* верхняя полоска-статус (узкая): `owner • hub:{hub_id} • online (xx ms)`; при оффлайне — «offline (auto-reconnect)».
* корневой контейнер: **DesktopScenario** (см. §3), рендерит решётку иконок (icon-manager) + модалки.

# 2) расширение контракта skill-манифеста (для визуальных навыков)

добавим в `skills/<skill>/manifest.json`:

```json
{
  "id": "weather_skill",
  "version": "2.1.0",
  "capabilities": {
    "visual": true,
    "ui": {
      "icon": "sun-cloud",          // ключ из icon-сет или data:svg
      "title": "Погода",
      "entrypoint": "/v1/ui/panel", // http endpoint (через hub proxy)
      "modes": ["modal", "panel"],  // modal для всплывашки
      "sizeHints": { "w": 480, "h": 360 }
    },
    "streams": {
      "supportsDeclarativeInterest": true
    }
  },
  "permissions": {
    "ownerOnly": false
  }
}
```

# 3) сценарий «Рабочий стол» (DesktopScenario)

минимальный DSL (json) для сценария:

```json
{
  "id": "scenario.desktop",
  "version": "0.1.0",
  "uses": ["skill.icon_manager"],
  "layout": {
    "grid": { "cols": 4, "gap": 16, "padding": 24 },
    "statusBar": true
  },
  "behavior": {
    "sourceOfApps": "icon_manager:list_installed",
    "onIconClick": "icon_manager:open_modal(skill_id)"
  },
  "policy": {
    "allowSkills": ["*"],       // mvp — все визуальные
    "ownerOnlySkills": ["skill.logs", "skill.hub_status"]
  }
}
```

runtime поведения:

* при монтировании: `icon_manager.list_installed()` → отдаёт набор иконок с метаданными ui.
* по клику: `icon_manager.open_modal(skill_id)` → создаёт модал и встраивает ui указанного skill (через iframe-sandbox, см. §6).

# 4) skill «Менеджер иконок» (icon_manager)

## обязанности

* агрегировать установленные визуальные навыки (из registry/root).
* хранить user-prefs (пины, порядок, группы) per owner.
* отдавать решётку иконок, открывать модалки с ui навыка.

## http-api (через hub→root proxy)

```
GET  /v1/icons
-> [{ skill_id, title, icon, entrypoint, modes, sizeHints, pinned, order }]

POST /v1/icons/pin
{ skill_id, pinned: true/false }

POST /v1/icons/order
{ order: [ "weather_skill", "skill.logs", ... ] }

POST /v1/open
{ skill_id, mode: "modal"|"panel" }
-> { ticket, entryUrl } // см. §6 безопасная загрузка UI
```

минимальная модель хранения (на hub в sqlite):

```
icons(owner_id, skill_id, pinned, order)
```

# 5) weather_skill — декларативный интерес + ui-панель

## новые эндпойнты навыка

```
POST /v1/interest
{ 
  "streams": [
    {
      "topic": "weather.now",
      "area": { "lat": 55.75, "lon": 37.62, "radius_km": 50 },
      "period": "5m",          // желаемая частота
      "ttl": "8h",
      "format": "compact"      // hint
    }
  ]
}
-> { subscription_id, nextUpdateIn: "PT300S" }

GET /v1/ui/manifest
-> { title, icon, modes:["modal","panel"], sizeHints:{w:480,h:360} }

GET /v1/ui/panel
-> html/js (встраиваемый виджет; mvp — простая страница)
```

## event-bus (внутри subnet, для сценариев)

* topic: `weather.update`
* payload.schema (пример):

```json
{
  "type":"object",
  "required":["ts","place","temp_c","conditions"],
  "properties":{
    "ts":{"type":"string","format":"date-time"},
    "place":{"type":"string"},
    "temp_c":{"type":"number"},
    "conditions":{"type":"string"},
    "forecast":{"type":"array","items":{"type":"object","properties":{
      "ts":{"type":"string","format":"date-time"},
      "temp_c":{"type":"number"},
      "conditions":{"type":"string"}
    }}}
  }
}
```

* acl: `role:OWNER read`, `scenario.desktop read`, `skill.weather publish`.

# 6) как встраиваем ui навыка (безопасно)

**mvp: iframe-sandbox**:

* `icon_manager.open_modal` вызывает `/v1/open { skill_id }`.
* root/hub выдаёт **одноразовый ticket (jwt)** и `entryUrl` вида:

  ```
  https://api.inimatic.com/skill-ui/{skill_id}/v1/ui/panel?ticket=...
  ```

* браузер создаёт `<iframe sandbox="allow-scripts allow-same-origin">` на `entryUrl`.
* обмен данными с контейнером — через `postMessage` с проверкой origin и валидацией `ticket` (внутри iframe начальная загрузка валидирует ticket у root).
* CSP: запрещаем внешние источники по умолчанию; разрешаем только собственный домен навыка через прокси.

(позже можно перейти на web-components/Module Federation; для mvp iframe даёт изоляцию и простоту.)

# 7) админ-скиллы (owner-only)

1. **skill.logs** (просмотр логов)

   * manifest: `ownerOnly: true`, `visual: true`.
   * api:

     ```
     GET /v1/logs?level=info&tail=500
     GET /v1/logs/stream (sse/ws)
     GET /v1/ui/panel (таблица + live tail)
     ```

   * источник: логи hub (journald/docker logs) через адаптер.

2. **skill.hub_status**

   * показывает: uptime, cpu/mem, версии, online-сокеты, pending routes.
   * api:

     ```
     GET /v1/status/summary
     GET /v1/ui/panel
     ```

(дальше можно добавить `skill.registry` — управлять установкой/обновлением навыков.)

# 8) изменения в front (SPA)

* роут `/` → контейнер DesktopScenario.
* компоненты:

  * `StatusBar` (hub online, latency, reconnect spinner)
  * `IconGrid` (данные из icon_manager `/v1/icons`)
  * `ModalHost` (iframe tickets)
* состояние:

  * `authState`: AUTH/PAIRED/ONLINE
  * `desktopState`: icons[], pinned[], order[]
  * `modalState`: { open: bool, skill_id?, entryUrl?, sizeHints? }
* сокеты:

  * `/owner` для статуса и relay.
* поведение при оффлайне: блокируем открытие новых модалок, но не очищаем иконки.

# 9) протокол открытия модалки (события)

**browser → icon_manager**

```
open_modal(skill_id)
```

**icon_manager → root/hub**

```
POST /v1/open { skill_id } -> { ticket, entryUrl, sizeHints }
```

**browser**:

* создаёт iframe на `entryUrl`, подписывается на `message` события.
* первым делом iframe шлёт `hello {ticket}` → контейнер проверяет.
* далее обмен: `resize {w,h}` (по желанию), `request {api}` если нужно.

# 10) принятие интереса от weather_skill

* DesktopScenario при первом старте вызывает:

  ```
  POST hub:/skills/weather_skill/v1/interest 
  {
    "streams":[{ "topic":"weather.now", "area":{...}, "period":"5m", "ttl":"8h" }]
  }
  ```

* hub регистрирует subscription и пересылает обновления в шину `weather.update`.
* DesktopScenario (или сам weather_widget внутри iframe) слушает через relay (или запрашивает `GET /v1/state` раз в N минут, mvp-вариант).

# 11) права и безопасность

* визуальные навыки могут отдавать только свои `/v1/ui/*`.
* root выдаёт tickets c `aud=skill_ui` и `sub=owner:{owner_id}`, `skill_id`, exp=5m, one-time-use.
* skills не видят device_code/sid, только `owner_id` (scope-ограниченно) и свой `skill_id`.
* admin-скиллы помечены `ownerOnly`. DesktopScenario фильтрует их для других ролей.

# 12) backlog на код (минимум)

**root**

* [ ] выдача одноразовых tickets `/v1/skill-ui/open`
* [ ] registry визуальных навыков (чтение manifest из hub) + кэш
* [ ] proxy `/skill-ui/{skill_id}/v1/ui/panel` с проверкой ticket

**hub**

* [ ] sqlite: таблицы icons, skills
* [ ] adapter skills discovery (манифесты)
* [ ] icon_manager service + http controller
* [ ] weather_skill: `/v1/interest`, `/v1/ui/{manifest|panel}`, публикация `weather.update`

**spa**

* [ ] DesktopScenario container + StatusBar
* [ ] IconGrid: `GET /v1/icons`, pin/order
* [ ] ModalHost: iframe + ticket-flow + postMessage
* [ ] weather widget (mvp: встроенный iframe к weather_skill/panel)

**tests / приёмка**

* [ ] e2e: login → webauthn → desktop shows icons
* [ ] open weather modal → виден текущий прогноз (stub ok)
* [ ] offline hub → статус «offline», иконки остаются; при восстановлении — модалки открываются снова
* [ ] owner-only skills видны только owner

# 13) быстрые админ-идеи на потом

* «events tail» — live-лента шины событий.
* «skill manager» — обновление/enable/disable навыков.
* «network diag» — pings, latency, socket health.

если ок, в следующем шаге дам короткий openapi-фрагмент для `/v1/icons`, `/v1/open`, `/v1/skill-ui/open`, `/skills/weather_skill/v1/…` и шаблон манифеста навыка, чтобы сразу отдать codex.

--------------

# 1) post-auth поведение web-клиента

* сразу после `AUTH + PAIRED + ONLINE`: скрываем поле кода и qr, показываем **чистый контейнер рабочего стола**.
* верхняя полоска-статус (узкая): `owner • hub:{hub_id} • online (xx ms)`; при оффлайне — «offline (auto-reconnect)».
* корневой контейнер: **DesktopScenario** (см. §3), рендерит решётку иконок (icon-manager) + модалки.

# 2) расширение контракта skill-манифеста (для визуальных навыков)

добавим в `skills/<skill>/manifest.json`:

```json
{
  "id": "weather_skill",
  "version": "2.1.0",
  "capabilities": {
    "visual": true,
    "ui": {
      "icon": "sun-cloud",          // ключ из icon-сет или data:svg
      "title": "Погода",
      "entrypoint": "/v1/ui/panel", // http endpoint (через hub proxy)
      "modes": ["modal", "panel"],  // modal для всплывашки
      "sizeHints": { "w": 480, "h": 360 }
    },
    "streams": {
      "supportsDeclarativeInterest": true
    }
  },
  "permissions": {
    "ownerOnly": false
  }
}
```

# 3) сценарий «Рабочий стол» (DesktopScenario)

минимальный DSL (json) для сценария:

```json
{
  "id": "scenario.desktop",
  "version": "0.1.0",
  "uses": ["skill.icon_manager"],
  "layout": {
    "grid": { "cols": 4, "gap": 16, "padding": 24 },
    "statusBar": true
  },
  "behavior": {
    "sourceOfApps": "icon_manager:list_installed",
    "onIconClick": "icon_manager:open_modal(skill_id)"
  },
  "policy": {
    "allowSkills": ["*"],       // mvp — все визуальные
    "ownerOnlySkills": ["skill.logs", "skill.hub_status"]
  }
}
```

runtime поведения:

* при монтировании: `icon_manager.list_installed()` → отдаёт набор иконок с метаданными ui.
* по клику: `icon_manager.open_modal(skill_id)` → создаёт модал и встраивает ui указанного skill (через iframe-sandbox, см. §6).

# 4) skill «Менеджер иконок» (icon_manager)

## обязанности

* агрегировать установленные визуальные навыки (из registry/root).
* хранить user-prefs (пины, порядок, группы) per owner.
* отдавать решётку иконок, открывать модалки с ui навыка.

## http-api (через hub→root proxy)

```
GET  /v1/icons
-> [{ skill_id, title, icon, entrypoint, modes, sizeHints, pinned, order }]

POST /v1/icons/pin
{ skill_id, pinned: true/false }

POST /v1/icons/order
{ order: [ "weather_skill", "skill.logs", ... ] }

POST /v1/open
{ skill_id, mode: "modal"|"panel" }
-> { ticket, entryUrl } // см. §6 безопасная загрузка UI
```

минимальная модель хранения (на hub в sqlite):

```
icons(owner_id, skill_id, pinned, order)
```

# 5) weather_skill — декларативный интерес + ui-панель

## новые эндпойнты навыка

```
POST /v1/interest
{ 
  "streams": [
    {
      "topic": "weather.now",
      "area": { "lat": 55.75, "lon": 37.62, "radius_km": 50 },
      "period": "5m",          // желаемая частота
      "ttl": "8h",
      "format": "compact"      // hint
    }
  ]
}
-> { subscription_id, nextUpdateIn: "PT300S" }

GET /v1/ui/manifest
-> { title, icon, modes:["modal","panel"], sizeHints:{w:480,h:360} }

GET /v1/ui/panel
-> html/js (встраиваемый виджет; mvp — простая страница)
```

## event-bus (внутри subnet, для сценариев)

* topic: `weather.update`
* payload.schema (пример):

```json
{
  "type":"object",
  "required":["ts","place","temp_c","conditions"],
  "properties":{
    "ts":{"type":"string","format":"date-time"},
    "place":{"type":"string"},
    "temp_c":{"type":"number"},
    "conditions":{"type":"string"},
    "forecast":{"type":"array","items":{"type":"object","properties":{
      "ts":{"type":"string","format":"date-time"},
      "temp_c":{"type":"number"},
      "conditions":{"type":"string"}
    }}}
  }
}
```

* acl: `role:OWNER read`, `scenario.desktop read`, `skill.weather publish`.

# 6) как встраиваем ui навыка (безопасно)

**mvp: iframe-sandbox**:

* `icon_manager.open_modal` вызывает `/v1/open { skill_id }`.
* root/hub выдаёт **одноразовый ticket (jwt)** и `entryUrl` вида:

  ```
  https://api.inimatic.com/skill-ui/{skill_id}/v1/ui/panel?ticket=...
  ```

* браузер создаёт `<iframe sandbox="allow-scripts allow-same-origin">` на `entryUrl`.
* обмен данными с контейнером — через `postMessage` с проверкой origin и валидацией `ticket` (внутри iframe начальная загрузка валидирует ticket у root).
* CSP: запрещаем внешние источники по умолчанию; разрешаем только собственный домен навыка через прокси.

(позже можно перейти на web-components/Module Federation; для mvp iframe даёт изоляцию и простоту.)

# 7) админ-скиллы (owner-only)

1. **skill.logs** (просмотр логов)

   * manifest: `ownerOnly: true`, `visual: true`.
   * api:

     ```
     GET /v1/logs?level=info&tail=500
     GET /v1/logs/stream (sse/ws)
     GET /v1/ui/panel (таблица + live tail)
     ```

   * источник: логи hub (journald/docker logs) через адаптер.

2. **skill.hub_status**

   * показывает: uptime, cpu/mem, версии, online-сокеты, pending routes.
   * api:

     ```
     GET /v1/status/summary
     GET /v1/ui/panel
     ```

(дальше можно добавить `skill.registry` — управлять установкой/обновлением навыков.)

# 8) изменения в front (SPA)

* роут `/` → контейнер DesktopScenario.
* компоненты:

  * `StatusBar` (hub online, latency, reconnect spinner)
  * `IconGrid` (данные из icon_manager `/v1/icons`)
  * `ModalHost` (iframe tickets)
* состояние:

  * `authState`: AUTH/PAIRED/ONLINE
  * `desktopState`: icons[], pinned[], order[]
  * `modalState`: { open: bool, skill_id?, entryUrl?, sizeHints? }
* сокеты:

  * `/owner` для статуса и relay.
* поведение при оффлайне: блокируем открытие новых модалок, но не очищаем иконки.

# 9) протокол открытия модалки (события)

**browser → icon_manager**

```
open_modal(skill_id)
```

**icon_manager → root/hub**

```
POST /v1/open { skill_id } -> { ticket, entryUrl, sizeHints }
```

**browser**:

* создаёт iframe на `entryUrl`, подписывается на `message` события.
* первым делом iframe шлёт `hello {ticket}` → контейнер проверяет.
* далее обмен: `resize {w,h}` (по желанию), `request {api}` если нужно.

# 10) принятие интереса от weather_skill

* DesktopScenario при первом старте вызывает:

  ```
  POST hub:/skills/weather_skill/v1/interest 
  {
    "streams":[{ "topic":"weather.now", "area":{...}, "period":"5m", "ttl":"8h" }]
  }
  ```

* hub регистрирует subscription и пересылает обновления в шину `weather.update`.
* DesktopScenario (или сам weather_widget внутри iframe) слушает через relay (или запрашивает `GET /v1/state` раз в N минут, mvp-вариант).

# 11) права и безопасность

* визуальные навыки могут отдавать только свои `/v1/ui/*`.
* root выдаёт tickets c `aud=skill_ui` и `sub=owner:{owner_id}`, `skill_id`, exp=5m, one-time-use.
* skills не видят device_code/sid, только `owner_id` (scope-ограниченно) и свой `skill_id`.
* admin-скиллы помечены `ownerOnly`. DesktopScenario фильтрует их для других ролей.

# 12) backlog на код (минимум)

**root**

* [ ] выдача одноразовых tickets `/v1/skill-ui/open`
* [ ] registry визуальных навыков (чтение manifest из hub) + кэш
* [ ] proxy `/skill-ui/{skill_id}/v1/ui/panel` с проверкой ticket

**hub**

* [ ] sqlite: таблицы icons, skills
* [ ] adapter skills discovery (манифесты)
* [ ] icon_manager service + http controller
* [ ] weather_skill: `/v1/interest`, `/v1/ui/{manifest|panel}`, публикация `weather.update`

**spa**

* [ ] DesktopScenario container + StatusBar
* [ ] IconGrid: `GET /v1/icons`, pin/order
* [ ] ModalHost: iframe + ticket-flow + postMessage
* [ ] weather widget (mvp: встроенный iframe к weather_skill/panel)

**tests / приёмка**

* [ ] e2e: login → webauthn → desktop shows icons
* [ ] open weather modal → виден текущий прогноз (stub ok)
* [ ] offline hub → статус «offline», иконки остаются; при восстановлении — модалки открываются снова
* [ ] owner-only skills видны только owner

# 13) быстрые админ-идеи на потом

* «events tail» — live-лента шины событий.
* «skill manager» — обновление/enable/disable навыков.
* «network diag» — pings, latency, socket health.

--------------

## что фиксируем как принцип

* **веб-представление живёт у сценария**, а навыки — это «сервисы» и «виджеты», которые сценарий вызывает.
* сценарий может быть **многостраничным/многокомпонентным**: у него есть своё дерево маршрутов, состояние, права, тема.
* единый рендер-движок (ULM/USDL/UMF из прошлого ответа) остаётся, но **точкой входа становится сценарий**, а не навык.

### 1) три уровня UI (по сложности)

1. **UI-Markdown (ULM)** — сверхпростой, безопасный, сервер-драйвенный

   * подмножество markdown + мини-директивы (`:::card`, `:::form`, `@[action:id]`)
   * идеален для LLM: текст+кнопки+простые формы без верстки и js
   * рендерится в стандартные компоненты (Ionic/shadcn) автоматически

2. **UI-Schema (USDL)** — декларативный JSON (по духу: AdaptiveCards/Vega-Lite)

   * компонентная схема + биндинги к `viewModel` (JSONPath/JMESPath)
   * поддержка форм, таблиц с пагинацией, чартов (через vega-lite), шагов, модалок
   * дифф-обновления через JSON Patch/JSON Merge Patch
   * устойчиво к версионированию, хорошо валидируется JSON-Schema

3. **Микро-фронт (UMF)** — опционально для «тяжёлых» навыков

   * изолированный web component/Micro-frontend (iframe/cust. element)
   * capability-handshake + строгое API событий
   * реже нужен; позволяет «нарисовать всё», не ломая общую модель

> правило: каждый навык **обязан** уметь говорить на уровне 1, может повышать детальность до 2, и только при необходимости — 3.

### 2) событийная шина UI

унифицированные темы (названия условные):

* `ui.render` — от навыка → клиенту (ULM/USDL/UMF дескриптор)
* `ui.patch` — частичные обновления состояния/документа
* `ui.action` — от клиента → навыку (клик/submit/shortcut)
* `ui.query` — ленивые данные (таблицы/деревья/поиск)
* `ui.toast`/`ui.notify` — лёгкие уведомления
* `ui.route` — навигация (внутри «рабочих областей»: main, side, modal)
* `ui.state` — синхронизация viewModel (опционально, двунаправленно)

формат действия (минимум):

```json
{
  "type": "ui.action",
  "action_id": "save",
  "payload": {"form": {"title": "..." }},
  "context": {"skill": "notes", "path": "sheet/1", "session": "…"}
}
```

### 3) слой разметки (layout) и capability-negotiation

* **слоты**: `header`, `main`, `aside`, `modal`, `toast` — навык адресует, где отрисовать
* **capabilities** (от клиента): темы, шрифты, a11y, offline, vega-lite, maps
  навык может деградировать: если нет чартов → рендерит таблицу, если нет aside → вкладка в main.
* **theme tokens**: цвет/типографика как дизайн-токены, без произвольного CSS навыка

### 4) LLM-friendly ограничения

* без произвольного HTML/JS от навыка на уровнях 1–2
* фиксированный словарь компонентов + строгие схемы
* короткие, предсказуемые action_id и поля форм
* авто-валидация по JSON-Schema с понятными сообщениями об ошибках

### 5) мини-спека USDL (v0)

```json
{
  "version": "usdl/0.2",
  "layout": {"slot": "main"},
  "viewModel": {"items": [], "filter": ""},
  "components": [
    {"type": "Card", "title": "Список",
     "body": [
       {"type": "Input", "label": "поиск", "bind": "$.filter", "debounceMs": 300,
        "on": [{"event": "change", "action": "search"}]},
       {"type": "Table", "columns": [
          {"key": "name", "title": "Название"},
          {"key": "status", "title": "Статус"}
        ],
        "data": {"source": "lazy", "query": "items.list",
                 "params": {"q": "$.filter"}, "pageSize": 25},
        "on": [{"event": "rowClick", "action": "open"}]
       },
       {"type": "Button", "text": "Добавить", "action": "create", "variant": "primary"}
     ]
    }
  ],
  "actions": {
    "search": {"emit": "ui.query", "target": "items.list"},
    "open":   {"emit": "ui.action"},
    "create": {"emit": "ui.action"}
  }
}
```

* `bind` — JSONPath в `viewModel`
* `data.source: "lazy"` — клиент сам будет вызывать `ui.query` при скролле/странице
* чарт: `{"type":"Chart","spec":{"$vegaLite":{…}},"data":{"bind":"$.series"}}`

### 6) UI-Markdown (ULM) — пример

```
# заметки

:::card
введите заголовок

:::form id="new-note"
- title: input(required)
- tags: chips
[создать](@action:create submit="new-note")
:::

:::table query="notes.list" pageSize=20
* {{name}} — {{status}}
:::
```

клиент преобразует в USDL, вешает обработчики и генерит `ui.action/ui.query`.

### 7) жизненный цикл и состояние

* **server-driven UI**: навык шлёт первый `ui.render` (ULM/USDL), далее — `ui.patch`
* **viewModel** живёт на клиенте; навык присылает патчи (JSON Patch) для реактивности
* долгие операции → `progress`/`spinner` + отмена (`action: cancel`)

### 8) безопасность и изоляция

* уровни 1–2: без пользовательского JS; XSS практически исключён
* уровень 3 (UMF): iframe + CSP + postMessage API, список разрешённых capability
* запрет прямого доступа к токенам/куки; всё через событийную шину

### 9) доступность, локализация, офлайн

* все базовые компоненты имеют ARIA-роли; клавиатурная навигация по умолчанию
* `i18n` ключи внутри USDL: `"text": {"i18n": "notes.add"}`
* офлайн-кеш USDL/viewModel и отложенные `ui.action` (replay)

### 10) интеграция с текущим фронтом

* **рендерер**: React + Ionic/shadcn/ui (адаптер компонентов под USDL)
* **чарты**: vega-lite рендерер (без произвольного код-инъекта)
* **сборка**: один «UI-движок» в приложении; навыки ничего не билдят
* **DevTools**: правый инспектор USDL/ULM, лог `ui.*` событий, визуальный diff patch

### 11) мини-гайд для навыка (LLM-программист)

* всегда поддерживай **ULM** (на крайний случай)
* если нужна форма/таблица/чарт — отдавай **USDL** v0.2
* отправляй **краткие** `ui.patch`, не перерисовывай всё
* используй короткие `action_id` и валидируй вход по JSON-Schema
* для больших наборов — `ui.query` с пагинацией/фильтрами

### 12) пример: навык отвечает USDL

```json
{
  "type": "ui.render",
  "skill": "weather",
  "payload": {
    "version": "usdl/0.2",
    "layout": {"slot":"main"},
    "viewModel": {"city": "Berlin", "now": null},
    "components": [
      {"type":"Form","id":"f","fields":[
        {"name":"city","label":"город","component":"Input","bind":"$.city","required":true}
      ],
       "actions":[{"text":"показать","action":"fetch"}]
      },
      {"type":"Card","title":"погода сейчас",
       "body":[
         {"type":"Progress","when":"$.now==null"},
         {"type":"Markdown","when":"$.now!=null",
          "text":"**{{$.city}}**: {{$.now.temp}}°C, {{$.now.desc}}"}
       ]
      }
    ],
    "actions":{
      "fetch":{"emit":"ui.action","payload":{"op":"fetch","city":"$.city"}}
    }
  }
}
```

клиент отправит `ui.action(fetch)` → навык вернёт `ui.patch`:

```json
[
  {"op":"replace","path":"/viewModel/now","value":{"temp":14.5,"desc":"cloudy"}}
]
```

### 13) версионирование и эволюция

* `version: usdl/x.y` + capability-negotiation; рендерер умеет понизить фичи
* строгие JSON-Schema для каждой версии; автогенерация шпаргалок для LLM
* совместимость: старший минор — назад-совместим, мажор — через feature flags

## модель исполнения

* **Scenario App** = `scenario.yaml` + набор UI-документов (ULM/USDL) + статические ассеты (опционально UMF).
* **Router** на фронте монтирует сценарий в рабочую область (`/s/{scenarioId}/...`) и управляет его маршрутами.
* **ViewModel** сценария: единое дерево состояния; навыки получают/меняют только свои неймспейсы.
* **Event bus** остаётся: `ui.action/ui.query/ui.patch` идут от/к сценарию; сценарий проксирует к нужным навыкам.

### навигация и состояние

* сценарий объявляет **меню/дерево** (как «варианты использования»), а рендерер автоматически строит навигацию.
* маршруты: **страницы** и **вью-узлы** (компоненты). каждый узел имеет:

  * `usdl`/`ulm` документ,
  * `bind` к кусочку `viewModel`,
  * `guards` (права/условия видимости),
  * `prefetch` (ленивая загрузка).

#### композиция с навыками

* сценарий подтягивает навыки через **контракты**:

  * **action contracts**: `orders.create`, `orders.update`
  * **query contracts**: `orders.list`, `orders.byId`
  * **widget contracts** (если нужны встраиваемые готовые компоненты навыка)
* виджеты навыков встраиваются как **USDL-компоненты с типом `Widget`**, а данные идут по `ui.query`.

### схема сценария (черновик)

```yaml
# .adaos/scenarios/orders/scenario.yaml
name: orders
version: 0.3.0
title: «заказы»
entry: routes.list
theme:
  tokens: default
permissions:
  - skill: orders_service
    contracts: [orders.list, orders.create, orders.update]
  - skill: customers_service
    contracts: [customers.search]
viewModel:
  initial:
    filters: { q: "", status: "any" }
routes:
  list:
    path: /list
    title: «список»
    usdl: ui/list.usdl.json
    prefetch:
      - query: orders.list
        params: { q: $.filters.q, status: $.filters.status, page: 1 }
  details:
    path: /:orderId
    title: «детали»
    usdl: ui/details.usdl.json
    guards:
      - hasParam: orderId
    prefetch:
      - query: orders.byId
        params: { id: $route.orderId }
  create:
    path: /create
    title: «новый заказ»
    usdl: ui/create.usdl.json
menu:
  - route: routes.list
  - route: routes.create
```

### пример USDL-страницы (листинг)

```json
{
  "version": "usdl/0.2",
  "layout": {"slot":"main"},
  "viewBind": "$.filters",
  "components": [
    {"type":"Toolbar","items":[
      {"type":"Input","label":"поиск","bind":"$.q","debounceMs":300,
       "on":[{"event":"change","action":"reload"}]},
      {"type":"Select","label":"статус","bind":"$.status",
       "options":[{"id":"any","text":"любой"},{"id":"open","text":"открыт"},{"id":"closed","text":"закрыт"}],
       "on":[{"event":"change","action":"reload"}]},
      {"type":"Button","text":"создать","navigate":{"to":"routes.create"}}
    ]},
    {"type":"Table","id":"tbl",
     "columns":[
       {"key":"id","title":"№"},
       {"key":"customer","title":"клиент"},
       {"key":"total","title":"сумма"},
       {"key":"status","title":"статус"}
     ],
     "data":{"source":"lazy","query":"orders.list","params":{"q":"$.q","status":"$.status"}},
     "on":[{"event":"rowClick","navigate":{"to":"routes.details","params":{"orderId":"$row.id"}}}]
    }
  ],
  "actions":{
    "reload":{"emit":"ui.query","target":"orders.list","params":{"q":"$.q","status":"$.status"}}
  }
}
```

### права и безопасность

* сценарий явно **декларирует** какие контракты навыков использует → это и есть его **scope**.
* фронт применяет **CSP** и sandbox для UMF-страниц (если есть).
* доступ к данным — только через шину; прямых HTTP-ключей в USDL/ULM нет.

### мультисценарность

* одновременно можно открыть несколько сценариев в разных **workspaces** (вкладки/сплиты).
* глобальный «центральный» стор охраняет **изоляцию**: `viewModel` и кеши per-scenario.

### офлайн/история/глубокие ссылки

* отдельный **persist layer** per-scenario (краткоживущий кеш и pinned state).
* deep links: `/s/orders/123` восстанавливает нужный маршрут, делает `prefetch`.
* replay для действий, сделанных офлайн (маркируем действия идемпотентными, где возможно).

### i18n и темы

* все пользовательские строки в USDL — через i18n-ключи; ключи хранятся у сценария.
* тема сценария — палитра токенов; системные темы можно переопределять частично.

### DX: как с этим живёт llm-программист

* генерирует **scenario.yaml** (маршруты/меню/контракты/guards).
* пишет ULM/USDL файлы по маршрутам (чистая декларативка).
* описывает контракты навыков через JSON-Schema (вызовы `ui.action/ui.query`).
* рендерер и SDK дают **шаблоны** и автодополнение; ошибки валидации — человекочитаемые.

### автогенерация UI «по умолчанию»

* если у сценария есть только **контракты** и **schemas**, движок может **собрать черновик UI**:

  * генерация таблиц/форм по схемам,
  * базовое меню из списка маршрутов,
  * далее сценарий постепенно заменяет автосгенерированные страницы своими USDL.
