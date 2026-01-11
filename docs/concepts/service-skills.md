# Service Skills (managed processes)

AdaOS supports **service skills**: skills that run as **external long-running processes** managed by the hub (instead of in-process Python handlers).

This is the main tool for integrating components with:
- incompatible Python/ABI requirements,
- heavy dependencies,
- their own HTTP servers (e.g. NLU engines).

---

## 1) What is a service skill?

A service skill is a normal skill folder (`skill.yaml`) with:

- `runtime.kind: service`
- `service.host`, `service.port`
- `service.command` (argv; hub prepends selected python where needed)

The hub discovers and manages these skills via:
- `src/adaos/services/skill/service_supervisor.py`

---

## 2) Service lifecycle

### Auto-start

During hub boot, AdaOS starts all discovered service skills:
- `src/adaos/services/bootstrap.py`

Also, when a skill gets (re)activated or rolled back, AdaOS restarts the service (if it is a service skill):
- `src/adaos/services/skill/service_supervisor_runtime.py`

### Health

For each service skill, the supervisor can perform HTTP health checks:
- `service.healthcheck.path` (default `/health`)
- `service.healthcheck.timeout_ms` (default `1000`)

---

## 3) Isolation / environment

Service skills can run in an isolated venv:

- `runtime.env.mode: venv`
- optional `runtime.env.venv_dir` (default: `state/services/<skill>/venv`)
- dependencies:
  - `skill.yaml: dependencies` (pip requirement strings)
  - optional `requirements.in` file inside the skill root

---

## 4) Self-managed services (issues + self-heal)

Service skills may opt-in to **self-management**:

```yaml
service:
  self_managed:
    enabled: true
    crash:
      max_in_window: 3
      window_s: 60
      cooloff_s: 30
    health:
      interval_s: 10
      failures_before_issue: 3
    hooks:
      on_issue: handlers.main:on_issue
      on_self_heal: handlers.main:on_self_heal
      timeout_s: 5.0
```

### Issue detector

The supervisor detects:
- crash-loop (many crashes within a time window) + cooloff
- repeated healthcheck failures

When an issue is recorded:
- it is persisted to `state/services/<skill>/issues.json`
- event is emitted: `skill.service.issue { skill, issue }`

### Doctor requests and reports (in-process)

If `service.self_managed.doctor.enabled: true`, the supervisor emits:
- `skill.service.doctor.request` (with service status + log tail).

AdaOS also includes an in-process doctor consumer:
- `src/adaos/services/skill/service_doctor_runtime.py`

It turns `doctor.request` into persisted reports:
- `state/services/<skill>/doctor_reports.json`
- event: `skill.service.doctor.report { skill, report }`

### Self-heal hooks

If enabled, the supervisor may call skill-provided hooks inside the service venv:
- `hooks.on_issue` is called when an issue is detected
- `hooks.on_self_heal` is called when the supervisor decides to attempt a self-heal

Entrypoints are `module:function` and are executed with `PYTHONPATH=<skill_root>` so `handlers.*` is importable.

---

## 5) API (service supervisor)

Hub API endpoints:

- `GET /api/services`
- `GET /api/services/{name}`
- `POST /api/services/{name}/start`
- `POST /api/services/{name}/stop`
- `POST /api/services/{name}/restart`

Self-management:

- `GET /api/services/{name}/issues`
- `POST /api/services/{name}/issue` (manual injection)
- `POST /api/services/{name}/self-heal`
- `GET /api/services/{name}/doctor/requests`
- `POST /api/services/{name}/doctor/request`
- `GET /api/services/{name}/doctor/reports`

---

## 6) Events (service supervisor)

Emitted by the platform:

- `skill.service.started { skill, pid }`
- `skill.service.ready { skill, pid }`
- `skill.service.stopped { skill, pid }`
- `skill.service.crashed { skill, code }`
- `skill.service.issue { skill, issue }`
- `skill.service.doctor.request { id, ts, skill, reason, issue?, service, log_tail[] }`
- `skill.service.doctor.report { skill, report }`
