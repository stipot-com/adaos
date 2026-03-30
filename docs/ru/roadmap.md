# AdaOS — дорожная карта по группам (осень 2025)

### 0. Сквозные контракты

* [ ] **Skill Manifest** (inputs/outputs/events/permissions/health/telemetry).
* [ ] **Scenario DSL** (узлы, связи, условия, таймауты, ошибки).
* [ ] **Event Bus Contract** (`event_id`, `payload.schema`, `trace_id`, `ttl`, `acl`).
* [ ] **Observability Schema** (логи, метрики, трассы, объяснения).

---

### 1. Ядро (Core)

* [ ] Адресное пространство памяти (state, secrets, cache, assets).
* [ ] Реестр событий + схемы + подписи.
* [ ] IPC / Message Bus (pub/sub, rpc, приоритеты).
  *DoD:* p95 publish→consume ≤ N мс, trace_id сквозной.
* [ ] Планировщик (scheduler) задач/сценариев.
  *DoD:* приоритеты соблюдаются, starvation-free, таймеры точность ≤ ±Δ мс.

---

### 2. Исполнение (Runtime)

* [ ] Runtime навыков (lifecycle init/start/stop/health, restart/backoff).
* [ ] Runtime сценариев (оркестратор событий/ретраев, state-store).
* [ ] Каркас SDK для сценариев (hooks, контекст, валидация).

---

### 3. I/O слой

* [ ] Унифицированный SDK I/O (messages, files, streams).
* [ ] Маршруты ввода-вывода (routing graph).
* [ ] Streaming SDK (аудио/видео, back-pressure).
* [ ] Web-интеграция (multi-browser подключение, WS).
* [ ] Драйверы/адаптеры устройств (`os://device/{capability}`, health-checks).
  *DoD:* hot-swap источника звука без рестарта навыка; стрим с back-pressure не рвёт кадры.

---

### 4. API & Discovery

* [ ] REST v0.2 (`/skills`, `/scenarios`, `/runtime`, `/events`, `/telemetry`).
* [ ] CLI: `adaos api serve`, `adaos events tail`.
* [ ] Service Discovery & Node Identity (heartbeats, registry).
* [ ] Слоистая маршрутизация в подсети.

---

### 5. Безопасность и доверие

* [ ] PKI, выдача сертификатов, trust-root.
* [ ] ACL для событий/портов.
* [ ] Sandbox навыков (изоляция, квоты ресурсов).
* [ ] Policy Engine (RBAC/ABAC, quotas).
* [ ] Регистрация/онбординг агента через телефон (QR → cert, ключ в браузере).
  *DoD:* устройство подключается <60с, получает cert, видит только разрешённые порты, возможен revoke.

---

### 6. UX и сценарии

* [ ] Интерпретатор намерений (NL → формальный вызов).
* [ ] UI-shell (список навыков/сценариев, запуск/стоп, секреты, логи).
* [ ] Визуализация графа сценариев (read-only).
* [ ] Редактор сценариев (graph edit + валидация).
* [ ] Сценарий первого запуска (guided setup).
* [ ] Сценарий развития (установка/удаление, on_development режим).
* [ ] Маркетплейс навыков/сценариев.

---

### 7. DevOps и Supply Chain

* [ ] Шаблоны навыков/сценариев + генерация скелета.
* [ ] CI/CD: тесты, контракт-чеки, подпись артефактов (SBOM, sigstore).
* [ ] Частные/публичные репозитории (fork, sparse-ветки, PR).
* [ ] DevOps сценария через сценарий (self-hosting pipelines).
* [ ] Schema & Contract Versioning (semver, миграции state/dsl).
  *DoD:* событие vN и vN+1 совместимы; автотесты контрактов в CI.

---

### 8. Monitoring & Resilience

* [ ] OpenTelemetry интеграция (logs, metrics, traces).
* [ ] Базовый дашборд (состояние сценариев/навыков).
* [ ] SLA/SLO + Error Budgets (метрики для bus/runtime).
* [ ] Data Retention & Privacy (TTL логов/стримов, шреддинг секретов).
* [ ] Персистентность хаба на Inimatic (state, recovery).
  *DoD:* после рестарта восстанавливаются сценарии/подписки; e2e trace доступен в дашборде.

---

### 9. Мобильность и распределённость

* [ ] Android-агент (микрофон, динамик, камера, онбординг).
* [ ] Linux/Windows адаптеры (минимальные).
* [ ] iOS клиент (минимум audio I/O).
* [ ] Multi-tenant режим (профили пользователей, ACL).
* [ ] Кластер/подсеть (роутинг, лидерство, репликация состояния).
  *DoD:* падение узла переносит сценарий на другой без потери сообщений.
