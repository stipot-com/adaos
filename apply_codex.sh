 (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF' 
diff --git a/README.md b/README.md
index 80bd0783c9505a5369b69100660490ef47cebbc0..a58ba8a267947b54ebea5188ab3b938bc0617716 100644
--- a/README.md
+++ b/README.md
@@ -380,62 +380,82 @@ adaos skill update weather_skill
 project_root/
 ├── .env                     # Конфигурация окружения (SKILLS_REPO_URL и т.д.)
 ├── docker-compose.yml
 ├── requirements.txt
 ├── setup.py
 ├── README.md
 │
 ├── src/adaos/
     ├── agent/                  # Runtime
     │   ├── core/              # State machine, skill_loader
     │   ├── db/                # SQLite persistence
     │   ├── i18n/              # TTS / UI strings
     │   └── audio/             # asr, tts, wake_word (если используется)
     │
     ├── sdk/                   # CLI и DevTools
     │   ├── cli/               # Typer CLI
     │   ├── skills/            # Компиляторы / шаблоны / генерация
     │   ├── llm/               # Подключение и промпты
     │   ├── locales/           # Локализация CLI
     │   └── utils/             # Общие утилиты (git, env)
     │
     ├── common/                # Опционально: конфиг, логгер, схемы
     └── skills_templates/      # YAML/intent-шаблоны
 │
 └── .adaos/                  # Рабочая среда пользователя (динамическая)
+    ├── workspace/           # Редактируемые копии
+    │   ├── skills/          # Локальные исходники навыков
+    │   └── scenarios/       # Локальные исходники сценариев
+    ├── skills/              # Кэш реестра навыков (read-only sparse checkout)
+    ├── scenarios/           # Кэш реестра сценариев (read-only sparse checkout)
     ├── skill_db.sqlite      # База данных навыков (версии, установленные навыки)
     ├── models/              # Локальные модели (ASR и др.)
     │   └── vosk-model-small-ru-0.22/
     ├── runtime/             # Логи и тесты
     │   ├── logs/
     │   └── tests/
-    └── skills/              # Установленные навыки (sparse-checkout из monorepo)
-        ├── test_skill2/
-        │   ├── skill.yaml
-        │   ├── handlers/
-        │   └── intents/
-        └── test_skill3/
+
+### Root / Forge интеграция
+
+Сервер Root принимает обращения от CLI через REST API. По умолчанию используются dev-настройки:
+
+* `ROOT_TOKEN` — токен для `POST /v1/*` (значение по умолчанию: `dev-root-token`).
+* `ROOT_BASE` (`root_base` в `~/.adaos/root-cli.json`) — базовый URL Root (`http://127.0.0.1:3030`).
+* `ADAOS_BASE` и `ADAOS_TOKEN` — адрес и токен локального AdaOS моста, передаются в заголовках при `GET /adaos/healthz`.
+
+CLI автоматически выполняет preflight-проверки, регистрирует subnet/node и отправляет архив черновика:
+
+```bash
+adaos skill install weather_skill
+adaos skill create demo --template python-minimal
+adaos skill push demo
+
+adaos scenario create morning
+adaos scenario push morning
+```
+
+Для сброса кэша реестра используйте `adaos repo reset skills` или `adaos repo reset scenarios` — команда выполнит `git fetch`/`reset --hard`/`clean -fdx` внутри `.adaos/skills` или `.adaos/scenarios`.
 
 ## **Схема локальной версии (PlantUML)**
 
 ```plantuml
 @startuml Local_AdaOS_MVP
 !includeurl https://raw.githubusercontent.com/plantuml-stdlib/C4-PlantUML/master/C4_Container.puml
 
 Container(user, "Пользователь", "Голос или текст", "Формулирует запросы")
 Container(llm, "LLM Client", "OpenAI / litellm", "Генерация тестов и навыков по запросу")
 Container(testrunner, "Test Runner", "PyYAML + pytest", "Прогоняет тесты BDD для навыков")
 Container(git, "Git Repo", "GitPython", "Версионирование навыков")
 Container(sqlite, "Skill DB", "SQLite + SQLAlchemy", "Метаданные навыков и версий")
 Container(runtime, "Skill Runtime", "importlib + watchdog", "Запуск навыков и проверка прав")
 Container(logs, "Логирование", "logging + rich", "Хранение логов и ошибок")
 
 Rel(user, llm, "Говорит/пишет запрос")
 Rel(llm, testrunner, "Создает тест")
 Rel(testrunner, runtime, "Проверяет в песочнице")
 Rel(testrunner, git, "Сохраняет успешный навык")
 Rel(testrunner, sqlite, "Записывает метаданные")
 Rel(runtime, sqlite, "Читает активные версии навыков")
 Rel(runtime, logs, "Пишет логи работы и ошибок")
 
 @enduml
 ```
diff --git a/docs/guides/cli..md b/docs/guides/cli..md
index 4b45f86db323c801cd47b462e99738639e64949f..f82972494cf2ee71180e386bd39bb9c16b484dc4 100644
--- a/docs/guides/cli..md
+++ b/docs/guides/cli..md
@@ -8,51 +8,51 @@ adaos skill list [--fs]
 adaos skill install <name>
 adaos skill uninstall <name>
 adaos skill sync
 adaos skill push <name> -m "msg"
 adaos skill reconcile-fs-to-db
 
 # Сценарии
 adaos scenario create <sid> [--template template]
 adaos scenario list [--fs]
 adaos scenario install <sid>
 adaos scenario uninstall <sid>
 adaos scenario sync
 adaos scenario push <sid> -m "msg"
 
 # Секреты
 adaos secret set/get/list/delete/export/import
 adaos secret set <KEY> <VALUE> [--scope profile|global]
 adaos secret get <KEY> [--show] [--scope ...]
 adaos secret list [--scope ...]
 adaos secret delete <KEY> [--scope ...]
 adaos secret export [--show] [--scope ...]
 adaos secret import <file.json> [--scope ...]
 
 # Песочница
 adaos sandbox profiles
-adaos sandbox run "<cmd>" --profile handler --cwd ~/.adaos/skills/<skill> --inherit-env --env DEBUG=1
+adaos sandbox run "<cmd>" --profile handler --cwd ~/.adaos/workspace/skills/<skill> --inherit-env --env DEBUG=1
 ```
 
 ## Поведение по умолчанию
 
 * CLI сам выполняет `prepare_environment()` (кроме `reset`) — создаёт каталоги и схему БД.
 * Для сетевых операций (git) используется `SecureGitClient` и `NetPolicy`.
 * Для файловых операций — `FSPolicy` + безопасные функции записи/удаления.
 
 ## Песочница
 
 ```bash
 adaos sandbox profiles
 adaos sandbox run "<команда>" \
   --profile handler \
-  --cwd ~/.adaos/skills/weather_skill \
+  --cwd ~/.adaos/workspace/skills/weather_skill \
   --inherit-env \
   --env DEBUG=1 --env LOG_LEVEL=info
-adaos sandbox run "<команда>" --profile handler --cwd \~/.adaos/skills/<skill> --inherit-env --env DEBUG=1
+adaos sandbox run "<команда>" --profile handler --cwd \~/.adaos/workspace/skills/<skill> --inherit-env --env DEBUG=1
 # Явные лимиты перекрывают профиль:
 adaos sandbox run "python -c 'while True: pass'" --cpu 1 --wall 10
 
 ## Временно отложено (deferred)
 
 * Генерация навыков из LLM, шаблоны, `push/rollback`, валидация/prep.
 * Свободная установка из произвольных репозиториев.
diff --git a/docs/guides/scenarios.md b/docs/guides/scenarios.md
index ff4d4dc7e1d6886d0d9bd80cccddfe54356cbe7d..b671c3bc64e011c412ceee8181bf54490a9e2a5d 100644
--- a/docs/guides/scenarios.md
+++ b/docs/guides/scenarios.md
@@ -1,48 +1,50 @@
 # Сценарии
 
 Аналогично Skills:
 
-- `{BASE_DIR}/scenarios` — моно-репо.
+- `{BASE_DIR}/workspace/scenarios` — рабочие директории сценариев.
+- `{BASE_DIR}/scenarios` — read-only кэш монорепозитория.
 - Таблицы `scenarios` и `scenario_versions`.
 
 CLI:
 
 ```bash
 # создание каркаса сценария по шаблону из монорепозитория
 adaos scenario create demo --template template
 
 # исполнение сценария и печать результатов
 adaos scenario run greet_on_boot
 
 # валидация структуры (проверка зарегистрированных действий)
 adaos scenario validate greet_on_boot
 
-# запуск тестов сценария (pytest из .adaos/scenarios/<id>/tests)
+# запуск тестов сценария (pytest из .adaos/workspace/scenarios/<id>/tests)
 adaos scenario test greet_on_boot
 
 # запустить тесты для всех сценариев
 adaos scenario test
 ```
 
 CLI использует URL монорепозитория, заданный в `SCENARIOS_MONOREPO_URL`, чтобы
-создавать и обновлять локальные каталоги сценариев в `{BASE_DIR}/scenarios`.
+поддерживать кэш `{BASE_DIR}/scenarios` и выдавать файлы в рабочие каталоги
+`{BASE_DIR}/workspace/scenarios`.
 
 ## Модель данных (SQLite)
 
 - Таблица `scenarios`:
 
   - `name` (PK), `active_version`, `repo_url`, `installed`, `last_updated`
 - Таблица `scenario_versions` — как у навыков.
 
 ## Хранилище кода
 
-- Путь: `{BASE_DIR}/scenarios` — **одно git-репо** (моно-репо сценариев).
+- Путь: `{BASE_DIR}/scenarios` — **одно git-репо** (моно-репо сценариев, read-only кэш).
 - Выборка подпапок через `git sparse-checkout` по БД.
 
 ## Сервис и CLI
 
 Сервис: `services/scenario/manager.py` (аналогично `SkillManager`).
 
 CLI подкоманды, связанные с управлением хранилищем и реестром, доступны в SDK
 через `adaos.sdk.manage.scenarios.*`. Для локальных сценариев основной поток
 работы идёт через команды `run`, `validate` и `test` выше.
diff --git a/docs/scenarios.md b/docs/scenarios.md
index 65ee6e4ce49b483392be252354af433a66a8e247..af9e1fdc1d898778fa50fe32661b765b838fd396 100644
--- a/docs/scenarios.md
+++ b/docs/scenarios.md
@@ -46,45 +46,45 @@ steps:
       path: ~/media
   - skill: indexer
     action: update_index
   - skill: web-server
     action: serve
     args:
       port: 8080
 ```
 
 ---
 
 ## Запуск сценария
 
 CLI:
 
 ```bash
 adaos scenario run catalog-media
 ```
 
 Runtime:
 
 ```python
 from adaos.sdk.scenarios.runtime import ScenarioRuntime, ensure_runtime_context
 
 ensure_runtime_context("~/.adaos")
-ScenarioRuntime().run_from_file("~/.adaos/scenarios/catalog-media/scenario.yaml")
+ScenarioRuntime().run_from_file("~/.adaos/workspace/scenarios/catalog-media/scenario.yaml")
 ```
 
 ---
 
 ## Репозитории
 
 - Как и навыки, сценарии могут храниться в монорепозитории (sparse-checkout)
   или в отдельных git-репозиториях.
 - Источник истины: SQLite-реестр сценариев.
 
 ---
 
 ## Best practices
 
 - Делайте шаги сценария атомарными (один action = одна операция).
 - Используйте `id` навыков и сценариев, а не пути к файлам.
 - Добавляйте автотесты для сценариев.
 - Поддерживайте семантическую версию (`0.1.0`, `0.2.0` …).
 - Документируйте сценарий (`description`) — это помогает при публикации.
diff --git a/src/adaos/adapters/fs/path_provider.py b/src/adaos/adapters/fs/path_provider.py
index 0b9a79ed972c942f6bd3d5b7042240ca09b1901a..2ce0462e387f4f5e0a6e751f5366d140dfade3be 100644
--- a/src/adaos/adapters/fs/path_provider.py
+++ b/src/adaos/adapters/fs/path_provider.py
@@ -17,77 +17,99 @@ class PathProvider:
     def from_settings(cls, settings: Settings) -> "PathProvider":
         return cls(base=Path(settings.base_dir).expanduser().resolve())
 
     # совместимость со старым стилем: PathProvider(settings)
     def __init__(self, settings: Settings | str | Path):
         base = settings.base_dir
         package_dir = settings.package_dir
         object.__setattr__(self, "base", base.expanduser().resolve())
         object.__setattr__(self, "package_dir", package_dir.expanduser().resolve())
 
     # --- базовые каталоги ---
     def locales_dir(self) -> Path:
         """Package-level locales shipped with AdaOS (see ``get_ctx().paths``)."""
 
         return (self.package_dir / "locales").resolve()
 
     def skill_templates_dir(self) -> Path:
         return (self.package_dir / "skills_templates").resolve()
 
     def scenario_templates_dir(self) -> Path:
         return (self.package_dir / "scenario_templates").resolve()
 
     def base_dir(self) -> Path:
         return self.base
 
-    def skills_dir(self) -> Path:
+    # --- workspace helpers ---
+
+    def workspace_dir(self) -> Path:
+        return (self.base / "workspace").resolve()
+
+    def skills_workspace_dir(self) -> Path:
+        return (self.workspace_dir() / "skills").resolve()
+
+    def scenarios_workspace_dir(self) -> Path:
+        return (self.workspace_dir() / "scenarios").resolve()
+
+    # --- registry caches ---
+
+    def skills_cache_dir(self) -> Path:
         return (self.base / "skills").resolve()
 
-    def scenarios_dir(self) -> Path:
+    def scenarios_cache_dir(self) -> Path:
         return (self.base / "scenarios").resolve()
 
+    def skills_dir(self) -> Path:
+        return self.skills_workspace_dir()
+
+    def scenarios_dir(self) -> Path:
+        return self.scenarios_workspace_dir()
+
     def models_dir(self) -> Path:
         return (self.base / "models").resolve()
 
     def logs_dir(self) -> Path:
         return (self.base / "logs").resolve()
 
     def cache_dir(self) -> Path:
         return (self.base / "cache").resolve()
 
     def state_dir(self) -> Path:
         return (self.base / "state").resolve()
 
     def locales_base_dir(self) -> Path:
         """Base directory for runtime locales exposed via ``get_ctx().paths``."""
 
         return (self.base / "i18n").resolve()
 
     def skills_locales_dir(self) -> Path:
         """Skill locales managed through the global context."""
 
         return self.locales_base_dir()
 
     def scenarios_locales_dir(self) -> Path:
         """Scenario locales managed through the global context."""
 
         return self.locales_base_dir()
 
     def tmp_dir(self) -> Path:
         return (self.base / "tmp").resolve()
 
     def ensure_tree(self) -> None:
         for p in (
             self.locales_dir(),
             self.locales_base_dir(),
             self.base_dir(),
-            self.skills_dir(),
-            self.scenarios_dir(),
+            self.workspace_dir(),
+            self.skills_workspace_dir(),
+            self.scenarios_workspace_dir(),
+            self.skills_cache_dir(),
+            self.scenarios_cache_dir(),
             self.skill_templates_dir(),
             self.scenario_templates_dir(),
             self.models_dir(),
             self.logs_dir(),
             self.cache_dir(),
             self.state_dir(),
             self.tmp_dir(),
         ):
             p.mkdir(parents=True, exist_ok=True)
diff --git a/src/adaos/adapters/scenarios/git_repo.py b/src/adaos/adapters/scenarios/git_repo.py
index fa5804ee7cee7fa1ae28feee34c5e44af9a21a45..59f183b99afb6993fb7ca98841009cf6189cfd6a 100644
--- a/src/adaos/adapters/scenarios/git_repo.py
+++ b/src/adaos/adapters/scenarios/git_repo.py
@@ -76,116 +76,128 @@ def _read_catalog(paths: PathProvider) -> list[str]:
     return []
 
 
 @dataclass
 class GitScenarioRepository(ScenarioRepository):
     """
     Унифицированный адаптер сценариев:
       - monorepo mode: если задан monorepo_url (и, опционально, monorepo_branch)
       - fs mode (multi-repo): если monorepo_url не задан — каждый сценарий отдельным git-репо
     """
 
     def __init__(
         self,
         *,
         paths: PathProvider,
         git: GitClient,
         url: Optional[str] = None,
         branch: Optional[str] = None,
     ):
         self.paths = paths
         self.git = git
         self.monorepo_url = url
         self.monorepo_branch = branch
 
     def _root(self) -> Path:
-        return Path(self.paths.scenarios_dir())
+        cache_dir = getattr(self.paths, "scenarios_cache_dir", None)
+        if cache_dir is not None:
+            cache = cache_dir() if callable(cache_dir) else cache_dir
+        else:
+            base = getattr(self.paths, "scenarios_dir")
+            cache = base() if callable(base) else base
+        root = Path(cache)
+        root.mkdir(parents=True, exist_ok=True)
+        return root
+
+    def _scenario_dir(self, root: Path, name: str) -> Path:
+        return _safe_join(root, f"scenarios/{name}")
 
     def _ensure_monorepo(self) -> None:
         if os.getenv("ADAOS_TESTING") == "1":
             return
         self.git.ensure_repo(str(self._root()), self.monorepo_url, branch=self.monorepo_branch)
 
     def ensure(self) -> None:
         if self.monorepo_url:
             self._ensure_monorepo()
         else:
             self._root().mkdir(parents=True, exist_ok=True)
 
     # --- list / get ---
 
     def list(self) -> list[SkillMeta]:
         self.ensure()
         items: List[SkillMeta] = []
         root = self._root()
-        if not root.exists():
+        scenarios_root = root / "scenarios"
+        if not scenarios_root.exists():
             return items
-        for ch in sorted(root.iterdir()):
+        for ch in sorted(scenarios_root.iterdir()):
             if ch.is_dir() and not ch.name.startswith("."):
                 items.append(_read_manifest(ch))
         return items
 
     def get(self, scenario_id: str) -> Optional[SkillMeta]:
         self.ensure()
-        p = self._root() / scenario_id
+        p = self._root() / "scenarios" / scenario_id
         if p.exists():
             m = _read_manifest(p)
             if m.id.value == scenario_id:
                 return m
         for m in self.list():
             if m.id.value == scenario_id:
                 return m
         return None
 
     # --- install ---
 
     def install(
         self,
         ref: str,
         *,
         branch: Optional[str] = None,
         dest_name: Optional[str] = None,
     ) -> SkillMeta:
         """
         monorepo mode: ref = имя сценария (подкаталог монорепо); URL запрещён.
         fs mode:      ref = полный git URL; dest_name опционален.
         """
         self.ensure()
         root = self._root()
 
         if self.monorepo_url:
             name = ref.strip()
             if not _NAME_RE.match(name):
                 raise ValueError("invalid scenario name")
             catalog = set(_read_catalog(self.paths))
             if catalog and name not in catalog:
                 raise ValueError(f"scenario '{name}' not found in catalog")
             self.git.sparse_init(str(root), cone=False)
-            self.git.sparse_add(str(root), name)
+            self.git.sparse_add(str(root), f"scenarios/{name}")
             self.git.pull(str(root))
-            p = _safe_join(root, name)
+            p = self._scenario_dir(root, name)
             if not p.exists():
                 raise FileNotFoundError(f"scenario '{name}' not present after sync")
             return _read_manifest(p)
 
         # fs mode
         if not _looks_like_url(ref):
             raise ValueError("ожидаю полный Git URL (multi-repo). " "если вы в монорежиме — задайте ADAOS_SCENARIOS_MONOREPO_URL и вызывайте install(<scenario_name>)")
         root.mkdir(parents=True, exist_ok=True)
         name = dest_name or _repo_basename_from_url(ref)
         dest = root / name
         self.git.ensure_repo(str(dest), ref, branch=branch)
         return _read_manifest(dest)
 
     # --- uninstall ---
 
     def uninstall(self, scenario_id: str) -> None:
         self.ensure()
-        p = _safe_join(self._root(), scenario_id)
+        p = self._scenario_dir(self._root(), scenario_id)
         if not p.exists():
             raise FileNotFoundError(f"scenario '{scenario_id}' not found")
         if remove_tree:
             ctx = getattr(self.paths, "ctx", None)
             fs = getattr(ctx, "fs", None) if ctx else None
             remove_tree(str(p), fs=fs)  # type: ignore[arg-type]
         else:
             shutil.rmtree(p)
diff --git a/src/adaos/adapters/skills/git_repo.py b/src/adaos/adapters/skills/git_repo.py
index 1505e74e59448c1f730f557b5d6c2bb0db1abf0c..e3e99a00ca7caf4720d43dc99bca0618bc621297 100644
--- a/src/adaos/adapters/skills/git_repo.py
+++ b/src/adaos/adapters/skills/git_repo.py
@@ -80,117 +80,129 @@ def _read_catalog(paths: PathProvider) -> list[str]:
 
 @dataclass
 class GitSkillRepository(SkillRepository):
     """
     Унифицированный адаптер навыков:
       - monorepo mode: если задан monorepo_url (и опц. monorepo_branch)
       - fs mode (multi-repo): если monorepo_url не задан
     """
 
     def __init__(
         self,
         *,
         paths: PathProvider,
         git: GitClient,
         monorepo_url: Optional[str] = None,
         monorepo_branch: Optional[str] = None,
     ):
         self.paths = paths
         self.git = git
         self.monorepo_url = monorepo_url
         self.monorepo_branch = monorepo_branch
 
     # --- common
 
     def _root(self) -> Path:
-        return Path(self.paths.skills_dir())
+        cache_dir = getattr(self.paths, "skills_cache_dir", None)
+        if cache_dir is not None:
+            cache = cache_dir() if callable(cache_dir) else cache_dir
+        else:
+            base = getattr(self.paths, "skills_dir")
+            cache = base() if callable(base) else base
+        root = Path(cache)
+        root.mkdir(parents=True, exist_ok=True)
+        return root
+
+    def _skill_dir(self, root: Path, name: str) -> Path:
+        return _safe_join(root, f"skills/{name}")
 
     def _ensure_monorepo(self) -> None:
         if os.getenv("ADAOS_TESTING") == "1":
             return
         self.git.ensure_repo(str(self._root()), self.monorepo_url, branch=self.monorepo_branch)
 
     def ensure(self) -> None:
         if self.monorepo_url:
             self._ensure_monorepo()
         else:
             self._root().mkdir(parents=True, exist_ok=True)
 
     # --- listing / get
 
     def list(self) -> list[SkillMeta]:
         self.ensure()
         result: List[SkillMeta] = []
         root = self._root()
-        if not root.exists():
+        skills_root = root / "skills"
+        if not skills_root.exists():
             return result
-        for child in sorted(root.iterdir()):
+        for child in sorted(skills_root.iterdir()):
             if not child.is_dir() or child.name.startswith("."):
                 continue
             meta = _read_manifest(child)
             result.append(meta)
         return result
 
     def get(self, skill_id: str) -> Optional[SkillMeta]:
         self.ensure()
         root = self._root()
-        direct = root / skill_id
+        direct = root / "skills" / skill_id
         if direct.exists():
             m = _read_manifest(direct)
             if m and m.id.value == skill_id:
                 return m
         for m in self.list():
             if m.id.value == skill_id:
                 return m
         return None
 
     # --- install
 
     def install(
         self,
         ref: str,
         *,
         branch: Optional[str] = None,
         dest_name: Optional[str] = None,
     ) -> SkillMeta:
         """
         monorepo mode: ref = skill name (подкаталог); URL запрещён.
         fs mode:      ref = git URL; dest_name опционален.
         """
         self.ensure()
         root = self._root()
 
         if self.monorepo_url:
             # monorepo: ожидаем имя скилла из каталога
             name = ref.strip()
             if not _NAME_RE.match(name):
                 raise ValueError("invalid skill name")
             """ catalog = set(_read_catalog(self.paths))
             if catalog and name not in catalog:
                 raise ValueError(f"skill '{name}' not found in catalog") """
             # sparse checkout только нужного подкаталога
             self.git.sparse_init(str(root), cone=False)
-            self.git.sparse_add(str(root), name)
+            self.git.sparse_add(str(root), f"skills/{name}")
             self.git.pull(str(root))
-            p = _safe_join(root, name)
+            p = self._skill_dir(root, name)
             if not p.exists():
                 raise FileNotFoundError(f"skill '{name}' not present after sync")
             return _read_manifest(p)
 
         # fs mode: ожидаем полный URL
         if not _looks_like_url(ref):
             raise ValueError("ожидаю полный Git URL (multi-repo). " "если вы в монорежиме — задайте ADAOS_SKILLS_MONOREPO_URL и вызывайте install(<skill_name>)")
         root.mkdir(parents=True, exist_ok=True)
         name = dest_name or _repo_basename_from_url(ref)
         dest = root / name
         self.git.ensure_repo(str(dest), ref, branch=branch)
         return _read_manifest(dest)
 
     def uninstall(self, skill_id: str) -> None:
         self.ensure()
-        p = _safe_join(self._root(), skill_id)
+        p = self._skill_dir(self._root(), skill_id)
         if not p.exists():
             raise FileNotFoundError(f"skill '{skill_id}' not found")
         if remove_tree:
             remove_tree(str(p), fs=getattr(self.paths, "ctx", None).fs if getattr(self.paths, "ctx", None) else None)  # type: ignore[attr-defined]
         else:
             shutil.rmtree(p)
diff --git a/src/adaos/apps/cli/app.py b/src/adaos/apps/cli/app.py
index ace158ab2a81d37126d78c58086d7ef781bfb0a2..a05a2b20a210e0ca4bbf88defcf7591b9bad2542 100644
--- a/src/adaos/apps/cli/app.py
+++ b/src/adaos/apps/cli/app.py
@@ -1,50 +1,50 @@
 # src/adaos/apps/cli/app.py
 from __future__ import annotations
 
 import os, traceback
 import sys
 import shutil
 from pathlib import Path
 from typing import Optional
 
 from dotenv import load_dotenv, find_dotenv
 import typer
 
 # загружаем .env один раз (для переменных вроде ADAOS_TTS/ADAOS_STT)
 load_dotenv(find_dotenv())
 
 from adaos.sdk.manage.environment import prepare_environment
 
 # контекст и настройки (PR-2)
 from adaos.services.settings import Settings
 from adaos.apps.bootstrap import init_ctx, get_ctx, reload_ctx
 from adaos.apps.cli.i18n import _
 from adaos.services.agent_context import get_ctx
 
 # общие подкоманды
-from adaos.apps.cli.commands import monitor, skill, runtime, llm, tests as tests_cmd, api, scenario, sdk_export as _sdk_export
+from adaos.apps.cli.commands import monitor, skill, runtime, llm, tests as tests_cmd, api, scenario, sdk_export as _sdk_export, repo
 
 # интеграции
 from adaos.apps.cli.commands import native
 from adaos.apps.cli.commands import ovos as ovos_cmd
 from adaos.apps.cli.commands import rhasspy as rhasspy_cmd
 from adaos.apps.cli.commands import secret
 from adaos.apps.cli.commands import sandbox as sandbox_cmd
 
 app = typer.Typer(help=_("cli.help"))
 
 # -------- вспомогательные --------
 
 
 def _run_safe(func):
     def wrapper(*args, **kwargs):
         try:
             return func(*args, **kwargs)
         except Exception as e:
             if os.getenv("ADAOS_CLI_DEBUG") == "1":
                 traceback.print_exc()
             raise
 
     return wrapper
 
 
@@ -160,43 +160,44 @@ def switch_tts(mode: str = typer.Argument(..., help="native | ovos | rhasspy")):
 
 @switch_app.command("stt")
 def switch_stt(mode: str = typer.Argument(..., help="vosk | rhasspy | ovos | native")):
     mode = mode.strip().lower()
     if mode not in {"vosk", "rhasspy", "ovos", "native"}:
         raise typer.BadParameter("Allowed: vosk, rhasspy, ovos, native")
     _write_env_var("ADAOS_STT", mode)
     typer.echo(f"[AdaOS] ADAOS_STT set to '{mode}'. Reloading ...")
     _restart_self()
 
 
 @app.command("where")
 def where():
     ctx = get_ctx()
     print("base_dir:", ctx.settings.base_dir)
 
 
 # -------- подкоманды --------
 
 app.add_typer(skill.app, name="skill", help=_("cli.help_skill"))
 app.add_typer(tests_cmd.app, name="tests", help=_("cli.help_test"))
 app.add_typer(runtime.app, name="runtime", help=_("cli.help_runtime"))
 app.add_typer(llm.app, name="llm", help=_("cli.help_llm"))
 app.add_typer(api.app, name="api")
 app.add_typer(monitor.app, name="monitor")
+app.add_typer(repo.app, name="repo", help=_("cli.repo.help"))
 app.add_typer(scenario.app, name="scenario", help=_("cli.help_scenario"))
 app.add_typer(switch_app, name="switch", help="Переключение профилей интеграций")
 app.add_typer(secret.app, name="secret")
 app.add_typer(sandbox_cmd.app, name="sandbox")
 app.add_typer(_sdk_export.app, name="sdk")
 
 # ---- Фильтрация интеграций по ENV ----
 _tts = _read("ADAOS_TTS", "native")
 if _tts == "ovos":
     app.add_typer(ovos_cmd.app, name="ovos", help="OVOS-интеграция")
 elif _tts == "rhasspy":
     app.add_typer(rhasspy_cmd.app, name="rhasspy", help="Rhasspy-интеграция")
 else:
     # корневые команды «нативного» профиля
     app.add_typer(native.app, name="", help="Нативные команды (по умолчанию)")
 
 if __name__ == "__main__":
     app()
diff --git a/src/adaos/apps/cli/commands/repo.py b/src/adaos/apps/cli/commands/repo.py
new file mode 100644
index 0000000000000000000000000000000000000000..85d2ab4c36d6b9dd4ba01cca91a0e6f4f2600f03
--- /dev/null
+++ b/src/adaos/apps/cli/commands/repo.py
@@ -0,0 +1,56 @@
+from __future__ import annotations
+
+import subprocess
+from pathlib import Path
+
+import typer
+
+from adaos.apps.cli.i18n import _
+from adaos.services.agent_context import get_ctx
+
+app = typer.Typer(help=_("cli.repo.help"))
+
+
+def _resolve_cache(kind: str) -> Path:
+    ctx = get_ctx()
+    attr_name = f"{kind}_cache_dir"
+    attr = getattr(ctx.paths, attr_name, None)
+    if attr is not None:
+        value = attr() if callable(attr) else attr
+    else:
+        fallback = getattr(ctx.paths, f"{kind}_dir")
+        value = fallback() if callable(fallback) else fallback
+    return Path(value).expanduser().resolve()
+
+
+def _run_git(repo: Path, args: list[str]) -> None:
+    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
+    if proc.returncode != 0:
+        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
+        raise RuntimeError(detail)
+
+
+@app.command("reset")
+def reset(
+    kind: str = typer.Argument(..., help=_("cli.repo.reset.help")),
+):
+    normalized = kind.strip().lower()
+    if normalized not in {"skills", "scenarios"}:
+        raise typer.BadParameter("kind must be 'skills' or 'scenarios'")
+
+    repo_path = _resolve_cache(normalized)
+    git_dir = repo_path / ".git"
+    if not git_dir.exists():
+        typer.secho(_("cli.repo.reset.not_initialized", kind=normalized, path=repo_path), fg=typer.colors.RED)
+        raise typer.Exit(1)
+
+    typer.echo(_("cli.repo.reset.running", kind=normalized, path=repo_path))
+    try:
+        _run_git(repo_path, ["fetch", "origin"])
+        _run_git(repo_path, ["reset", "--hard", "origin/main"])
+        _run_git(repo_path, ["clean", "-fdx"])
+    except RuntimeError as err:
+        typer.secho(_("cli.repo.reset.failed", reason=str(err)), fg=typer.colors.RED)
+        raise typer.Exit(1)
+
+    typer.secho(_("cli.repo.reset.done", path=repo_path), fg=typer.colors.GREEN)
diff --git a/src/adaos/apps/cli/commands/scenario.py b/src/adaos/apps/cli/commands/scenario.py
index 6572e861c01ac0415e58626f69911bbeefbd5926..4c706a8d347ad696d49e9227bfbdbd4b6bc4478c 100644
--- a/src/adaos/apps/cli/commands/scenario.py
+++ b/src/adaos/apps/cli/commands/scenario.py
@@ -1,78 +1,90 @@
 """Typer commands for managing and executing scenarios."""
 
 from __future__ import annotations
 
 import json
 import os
 import subprocess
 import traceback
 from pathlib import Path
 from typing import Optional
 
 import typer
 
 from adaos.adapters.db import SqliteScenarioRegistry
 from adaos.apps.cli.i18n import _
 from adaos.services.agent_context import get_ctx
 from adaos.services.scenario.manager import ScenarioManager
 from adaos.services.scenario.scaffold import create as scaffold_create
 from adaos.sdk.scenarios.runtime import ScenarioRuntime, ensure_runtime_context, load_scenario
 from adaos.apps.cli.root_ops import (
     RootCliError,
+    archive_bytes_to_b64,
     assert_safe_name,
     create_zip_bytes,
     ensure_registration,
     load_root_cli_config,
+    push_scenario_draft,
     run_preflight_checks,
-    store_scenario_draft,
 )
 
 app = typer.Typer(help=_("cli.help_scenario"))
 
 
 def _run_safe(func):
     """Wrap Typer callbacks to surface tracebacks when ADAOS_CLI_DEBUG=1."""
 
     def wrapper(*args, **kwargs):
         try:
             return func(*args, **kwargs)
         except Exception:
             if os.getenv("ADAOS_CLI_DEBUG") == "1":
                 traceback.print_exc()
             raise
 
     return wrapper
 
 
 def _mgr() -> ScenarioManager:
     ctx = get_ctx()
     repo = ctx.scenarios_repo
     reg = SqliteScenarioRegistry(ctx.sql)
     return ScenarioManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)
 
 
+def _workspace_root() -> Path:
+    ctx = get_ctx()
+    attr = getattr(ctx.paths, "scenarios_workspace_dir", None)
+    if attr is not None:
+        value = attr() if callable(attr) else attr
+    else:
+        base = getattr(ctx.paths, "scenarios_dir")
+        value = base() if callable(base) else base
+    return Path(value).expanduser().resolve()
+
+
 @_run_safe
 @app.command("list")
 def list_cmd(
     json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
     show_fs: bool = typer.Option(False, "--fs", help=_("cli.option.fs")),
 ):
     """List installed scenarios from the registry."""
 
     mgr = _mgr()
     rows = mgr.list_installed()
 
     if json_output:
         payload = {
             "scenarios": [
                 {
                     "name": r.name,
                     "version": getattr(r, "active_version", None) or "unknown",
                 }
                 for r in rows
                 if bool(getattr(r, "installed", True))
             ]
         }
         typer.echo(json.dumps(payload, ensure_ascii=False))
         return
 
@@ -179,84 +191,89 @@ def push_cmd(
             run_preflight_checks(config, dry_run=False, echo=typer.echo)
         except RootCliError as err:
             typer.secho(str(err), fg=typer.colors.RED)
             raise typer.Exit(1)
 
     try:
         config = ensure_registration(config, dry_run=dry_run, subnet_name=subnet_name, echo=typer.echo)
     except RootCliError as err:
         typer.secho(str(err), fg=typer.colors.RED)
         raise typer.Exit(1)
 
     scenario_dir = _resolve_scenario_dir(scenario_name)
     target_name = name_override or scenario_dir.name
     try:
         assert_safe_name(target_name)
     except RootCliError as err:
         typer.secho(str(err), fg=typer.colors.RED)
         raise typer.Exit(1)
 
     try:
         archive_bytes = create_zip_bytes(scenario_dir)
     except RootCliError as err:
         typer.secho(str(err), fg=typer.colors.RED)
         raise typer.Exit(1)
 
+    archive_b64 = archive_bytes_to_b64(archive_bytes)
+
     try:
-        stored = store_scenario_draft(
+        stored = push_scenario_draft(
+            config,
             node_id=config.node_id,
             name=target_name,
-            archive_bytes=archive_bytes,
+            archive_b64=archive_b64,
             dry_run=dry_run,
             echo=typer.echo,
         )
     except RootCliError as err:
         typer.secho(str(err), fg=typer.colors.RED)
         raise typer.Exit(1)
 
     if dry_run:
         typer.echo(_("cli.scenario.push.root.dry_run", path=stored))
     else:
         typer.secho(_("cli.scenario.push.root.success", path=stored), fg=typer.colors.GREEN)
 
 
 def _scenario_root() -> Path:
-    ctx_base = Path.cwd()
-    candidate = ctx_base / ".adaos" / "scenarios"
+    cwd = Path.cwd()
+    candidate = cwd / ".adaos" / "workspace" / "scenarios"
     if candidate.exists():
         return candidate
-    return ctx_base
+    legacy = cwd / ".adaos" / "scenarios"
+    if legacy.exists():
+        return legacy
+    return _workspace_root()
 
 
 def _resolve_scenario_dir(target: str) -> Path:
     candidate = Path(target).expanduser()
     if candidate.is_file():
         candidate = candidate.parent
     if candidate.exists():
         return candidate.resolve()
-    ctx = get_ctx()
-    base = Path(ctx.paths.scenarios_dir())
+    base = _workspace_root()
     candidate = (base / target).resolve()
     if candidate.is_file():
         candidate = candidate.parent
     if candidate.exists():
         return candidate
     raise typer.BadParameter(_("cli.scenario.push.not_found", name=target))
 
 
 def _scenario_path(scenario_id: str, override: Optional[str]) -> Path:
     if override:
         return Path(override).expanduser().resolve()
     root = _scenario_root()
     if (root / "scenario.yaml").exists():
         return (root / "scenario.yaml").resolve()
     candidate = root / scenario_id / "scenario.yaml"
     if candidate.exists():
         return candidate.resolve()
     raise FileNotFoundError(_("cli.scenario.run.not_found", scenario_id=scenario_id))
 
 
 def _base_dir_for(path: Path) -> Path:
     for parent in path.parents:
         if parent.name == ".adaos":
             return parent
     return path.parent
diff --git a/src/adaos/apps/cli/commands/skill.py b/src/adaos/apps/cli/commands/skill.py
index f53a1c74adeadab1bef25a8522bacf838e8422c5..5610e76de353a62583ffaf928b7022863280f4d7 100644
--- a/src/adaos/apps/cli/commands/skill.py
+++ b/src/adaos/apps/cli/commands/skill.py
@@ -33,56 +33,66 @@ from adaos.apps.cli.root_ops import (
     run_preflight_checks,
 )
 
 app = typer.Typer(help=_("cli.help_skill"))
 
 
 def _run_safe(func):
     def wrapper(*args, **kwargs):
         try:
             return func(*args, **kwargs)
         except Exception as e:
             if os.getenv("ADAOS_CLI_DEBUG") == "1":
                 traceback.print_exc()
             raise
 
     return wrapper
 
 
 def _mgr() -> SkillManager:
     ctx = get_ctx()
     repo = ctx.skills_repo
     reg = SqliteSkillRegistry(ctx.sql)
     return SkillManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=getattr(ctx, "bus", None), caps=ctx.caps)
 
 
+def _workspace_root() -> Path:
+    ctx = get_ctx()
+    attr = getattr(ctx.paths, "skills_workspace_dir", None)
+    if attr is not None:
+        value = attr() if callable(attr) else attr
+    else:
+        base = getattr(ctx.paths, "skills_dir")
+        value = base() if callable(base) else base
+    return Path(value).expanduser().resolve()
+
+
 def _resolve_skill_path(target: str) -> Path:
     candidate = Path(target).expanduser()
     if candidate.exists():
         return candidate.resolve()
-    ctx = get_ctx()
-    root = Path(ctx.paths.skills_dir())
+    root = _workspace_root()
     candidate = (root / target).resolve()
     if candidate.exists():
         return candidate
     raise typer.BadParameter(_("cli.skill.push.not_found", name=target))
 
 
 @_run_safe
 @app.command("list")
 def list_cmd(
     json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
     show_fs: bool = typer.Option(False, "--fs", help=_("cli.option.fs")),
 ):
     """
     Список установленных навыков из реестра.
     JSON-формат: {"skills": [{"name": "...", "version": "..."}, ...]}
     """
     mgr = _mgr()
     rows = mgr.list_installed()  # SkillRecord[]
 
     if json_output:
         payload = {
             "skills": [
                 {
                     "name": r.name,
                     # тестам важен только name, но version полезно оставить
diff --git a/src/adaos/apps/cli/root_ops.py b/src/adaos/apps/cli/root_ops.py
index e5631e3d3a785d0f0a95a25998c9f2c594b2f400..de2c862f77f8509ab5de9220ef318ab4a0c929f0 100644
--- a/src/adaos/apps/cli/root_ops.py
+++ b/src/adaos/apps/cli/root_ops.py
@@ -1,31 +1,30 @@
 from __future__ import annotations
 
 import base64
 import io
 import json
-import time
 import zipfile
 from dataclasses import asdict, dataclass, fields, replace
 from pathlib import Path
 from typing import Callable, Optional
 
 import httpx
 
 from adaos.apps.bootstrap import get_ctx
 
 DEFAULT_ROOT_BASE = "http://127.0.0.1:3030"
 DEFAULT_ROOT_TOKEN = "dev-root-token"
 DEFAULT_ADAOS_BASE = "http://127.0.0.1:8777"
 DEFAULT_ADAOS_TOKEN = "dev-local-token"
 CONFIG_FILENAME = "root-cli.json"
 FORGE_ROOT = Path("/tmp/forge")
 DRY_RUN_PLACEHOLDER_TS = "<ts>"
 
 
 class RootCliError(RuntimeError):
     """Raised for CLI integration failures with the Root service."""
 
 
 @dataclass
 class RootCliConfig:
     root_base: str = DEFAULT_ROOT_BASE
@@ -265,61 +264,72 @@ def push_skill_draft(
     target_node = node_id or "<node-id>"
     if dry_run:
         forge_path = FORGE_ROOT / target_node / "skills" / name / f"{DRY_RUN_PLACEHOLDER_TS}_{name}.zip"
         echo(
             f"[dry-run] POST {url} payload={{'node_id': '{target_node}', 'name': '{name}', 'archive_b64': '<omitted>'}}"
         )
         return str(forge_path)
 
     if not node_id:
         raise RootCliError("Node ID is not configured; run registration first")
 
     response = _request(
         "POST",
         url,
         config=config,
         json_body={"node_id": node_id, "name": name, "archive_b64": archive_b64},
         timeout=30.0,
     )
     data = response.json()
     stored = data.get("stored")
     if not stored:
         raise RootCliError("Root did not return stored path")
     return stored
 
 
-def store_scenario_draft(
+def push_scenario_draft(
+    config: RootCliConfig,
     *,
     node_id: Optional[str],
     name: str,
-    archive_bytes: bytes,
+    archive_b64: str,
     dry_run: bool,
     echo: Callable[[str], None],
 ) -> str:
+    url = _root_url(config, "/v1/scenarios/draft")
     target_node = node_id or "<node-id>"
-    base_dir = FORGE_ROOT / target_node / "scenarios" / name
     if dry_run:
-        predicted = base_dir / f"{DRY_RUN_PLACEHOLDER_TS}_{name}.zip"
-        echo(f"[dry-run] Would store scenario archive at {predicted}")
-        return str(predicted)
+        forge_path = FORGE_ROOT / target_node / "scenarios" / name / f"{DRY_RUN_PLACEHOLDER_TS}_{name}.zip"
+        echo(
+            f"[dry-run] POST {url} payload={{'node_id': '{target_node}', 'name': '{name}', 'archive_b64': '<omitted>'}}"
+        )
+        return str(forge_path)
 
     if not node_id:
         raise RootCliError("Node ID is not configured; run registration first")
 
-    base_dir.mkdir(parents=True, exist_ok=True)
-    filename = base_dir / f"{int(time.time())}_{name}.zip"
-    filename.write_bytes(archive_bytes)
-    return str(filename)
+    response = _request(
+        "POST",
+        url,
+        config=config,
+        json_body={"node_id": node_id, "name": name, "archive_b64": archive_b64},
+        timeout=30.0,
+    )
+    data = response.json()
+    stored = data.get("stored")
+    if not stored:
+        raise RootCliError("Root did not return stored path")
+    return stored
 
 
 __all__ = [
     "RootCliConfig",
     "RootCliError",
     "archive_bytes_to_b64",
     "assert_safe_name",
     "create_zip_bytes",
     "ensure_registration",
     "load_root_cli_config",
     "push_skill_draft",
+    "push_scenario_draft",
     "run_preflight_checks",
-    "store_scenario_draft",
 ]
diff --git a/src/adaos/config/const.py b/src/adaos/config/const.py
index c733ff7ec45102e8e8173d3295f38210c314bc35..206e328412190a83d39829c047d5a5d05a4b6a3c 100644
--- a/src/adaos/config/const.py
+++ b/src/adaos/config/const.py
@@ -1,12 +1,14 @@
 # src/adaos/config/const.py
 from __future__ import annotations
 
 # ЖЁСТКИЕ значения по умолчанию (меняются разработчиками в коде/сборке)
-SKILLS_MONOREPO_URL: str | None = "https://github.com/stipot/adaoskills.git"
+REGISTRY_URL: str = "https://github.com/stipot-com/adaos-registry.git"
+
+SKILLS_MONOREPO_URL: str | None = REGISTRY_URL
 SKILLS_MONOREPO_BRANCH: str | None = "main"
 
-SCENARIOS_MONOREPO_URL: str | None = "https://github.com/stipot/adaosscens.git"
+SCENARIOS_MONOREPO_URL: str | None = REGISTRY_URL
 SCENARIOS_MONOREPO_BRANCH: str | None = "main"
 
 # Разрешить ли .env/ENV менять монорепо (ТОЛЬКО для dev-сборок!)
 ALLOW_ENV_MONOREPO_OVERRIDE: bool = False
diff --git a/src/adaos/integrations/inimatic/backend/app.ts b/src/adaos/integrations/inimatic/backend/app.ts
index 8af4bd059bf26dcdae904ed2b7c32de1bc0ad598..35ae6648f415782fc708c30ba394d31629250ab2 100644
--- a/src/adaos/integrations/inimatic/backend/app.ts
+++ b/src/adaos/integrations/inimatic/backend/app.ts
@@ -50,91 +50,209 @@ type OpenedStreams = {
 	[sessionId: string]: {
 		[fileName: string]: StreamInfo
 	}
 }
 
 
 const app = express()
 app.use(express.json({ limit: '32mb' }))
 
 const server = http.createServer(app)
 const io = new Server(server, {
         cors: { origin: '*' },
         pingTimeout: 10000,
         pingInterval: 10000,
 })
 
 installAdaosBridge(app, server)
 const url = `redis://${process.env['PRODUCTION'] ? 'redis' : 'localhost'}:6379`
 const redisClient = await createClient({ url })
         .on('error', (err) => console.log('Redis Client Error', err))
         .connect()
 
 const ROOT_TOKEN = process.env['ROOT_TOKEN'] ?? 'dev-root-token'
 const FORGE_ROOT = '/tmp/forge'
 const SKILL_FORGE_KEY_PREFIX = 'forge:skills'
+const SCENARIO_FORGE_KEY_PREFIX = 'forge:scenarios'
 const TTL_SUBNET_SECONDS = 60 * 60 * 24 * 7
 const TTL_NODE_SECONDS = 60 * 60 * 24
+const TEMPLATE_OPTIONS: Record<string, string[]> = {
+        skill: ['python-minimal', 'demo_skill'],
+        scenario: ['template'],
+}
 
 await mkdir(FORGE_ROOT, { recursive: true })
 
+const requireRootToken: express.RequestHandler = (req, res, next) => {
+        const token = req.header('X-AdaOS-Token') ?? ''
+        if (token && token === ROOT_TOKEN) {
+                next()
+                return
+        }
+        res.status(401).json({ error: 'unauthorized' })
+}
+
+app.get('/healthz', (_req, res) => {
+        res.json({ ok: true })
+})
+
+app.get('/adaos/healthz', (req, res) => {
+        res.json({ ok: true, adaos: req.header('X-AdaOS-Base') ?? null })
+})
+
+app.use('/v1', requireRootToken)
+
+app.get('/v1/templates', (req, res) => {
+        const type = String(req.query['type'] ?? '').toLowerCase()
+        res.json({ templates: TEMPLATE_OPTIONS[type] ?? [] })
+})
+
+app.post('/v1/subnets/register', async (req, res) => {
+        const subnetId = generateSubnetId()
+        const record = {
+                subnet_id: subnetId,
+                subnet_name: typeof req.body?.subnet_name === 'string' ? req.body.subnet_name : undefined,
+                ts: Date.now(),
+        }
+        await redisClient.setEx(`forge:subnet:${subnetId}`, TTL_SUBNET_SECONDS, JSON.stringify(record))
+        res.json({ subnet_id: subnetId })
+})
+
+app.post('/v1/nodes/register', async (req, res) => {
+        const subnetId = req.body?.subnet_id
+        if (typeof subnetId !== 'string' || !subnetId) {
+                res.status(400).json({ error: 'subnet_id is required' })
+                return
+        }
+        const nodeId = generateNodeId()
+        const record = { node_id: nodeId, subnet_id: subnetId, ts: Date.now() }
+        await redisClient.setEx(`forge:node:${nodeId}`, TTL_NODE_SECONDS, JSON.stringify(record))
+        res.json({ node_id: nodeId, subnet_id: subnetId })
+})
+
+const draftEndpoint = (
+        kind: 'skills' | 'scenarios',
+) =>
+        app.post(`/v1/${kind}/draft`, async (req, res) => {
+                const nodeId = req.body?.node_id
+                const name = req.body?.name
+                const archiveB64 = req.body?.archive_b64
+                if (typeof nodeId !== 'string' || typeof name !== 'string' || typeof archiveB64 !== 'string') {
+                        res.status(400).json({ error: 'node_id, name and archive_b64 are required' })
+                        return
+                }
+                try {
+                        const stored = await storeDraftArchive(kind, nodeId, name, archiveB64)
+                        res.json({ stored })
+                } catch (error) {
+                        const reason = error instanceof Error ? error.message : 'unknown error'
+                        res.status(400).json({ error: reason })
+                }
+        })
+
+draftEndpoint('skills')
+draftEndpoint('scenarios')
+
+app.post('/v1/skills/pr', (_req, res) => {
+        res.json({ pr_url: 'https://example.com/mock-skill-pr' })
+})
+
+app.post('/v1/scenarios/pr', (_req, res) => {
+        res.json({ pr_url: 'https://example.com/mock-scenario-pr' })
+})
+
 function isValidGuid(guid: string) {
 	return /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(
 		guid
 	)
 }
 
 const openedStreams: OpenedStreams = {}
 const FILESPATH = '/tmp/inimatic_public_files/'
 
 if (!fs.existsSync(FILESPATH)) {
         fs.mkdirSync(FILESPATH)
 }
 
 function cleanupSessionBucket(sessionId: string) {
         if (openedStreams[sessionId] && !Object.keys(openedStreams[sessionId]).length) {
                 delete openedStreams[sessionId]
         }
 }
 
 function assertSafeName(name: string) {
         if (!name || name.includes('..') || name.includes('/') || name.includes('\\')) {
                 throw new Error('invalid name')
         }
         if (path.basename(name) !== name) {
                 throw new Error('invalid name')
         }
 }
 
 const safeBasename = (value: string) => path.basename(value)
 
 const ensureForgeDir = async (nodeId: string, kind: 'skills' | 'scenarios', name: string) => {
         const dir = path.join(FORGE_ROOT, nodeId, kind, name)
         await mkdir(dir, { recursive: true })
         return dir
 }
 
+const assertSafeId = (value: string, label: string) => {
+        if (!value || value.includes('..') || value.includes('/') || value.includes('\\')) {
+                throw new Error(`${label} contains forbidden characters`)
+        }
+}
+
+const storeDraftArchive = async (
+        kind: 'skills' | 'scenarios',
+        nodeId: string,
+        name: string,
+        archiveB64: string,
+) => {
+        assertSafeId(nodeId, 'node_id')
+        assertSafeName(name)
+        if (typeof archiveB64 !== 'string' || archiveB64.trim() === '') {
+            throw new Error('archive_b64 is required')
+        }
+        let buffer: Buffer
+        try {
+                buffer = Buffer.from(archiveB64, 'base64')
+        } catch (error) {
+                throw new Error('invalid archive encoding')
+        }
+        if (!buffer.length) {
+                throw new Error('archive is empty')
+        }
+        const dir = await ensureForgeDir(nodeId, kind, name)
+        const safeName = safeBasename(name) || 'draft'
+        const filename = path.join(dir, `${Date.now()}_${safeName}.zip`)
+        await writeFile(filename, buffer)
+        const keyPrefix = kind === 'skills' ? SKILL_FORGE_KEY_PREFIX : SCENARIO_FORGE_KEY_PREFIX
+        await redisClient.lPush(`${keyPrefix}:${nodeId}:${name}`, filename)
+        return filename
+}
+
 const generateSubnetId = () => `sn_${uuidv4().replace(/-/g, '').slice(0, 8)}`
 const generateNodeId = () => `node_${uuidv4().replace(/-/g, '').slice(0, 8)}`
 
 function saveFileChunk(sessionId: string, fileName: string, content: Array<number>) {
         if (!openedStreams[sessionId]) {
                 openedStreams[sessionId] = {}
         }
 
         const safeFile = safeBasename(fileName)
 
         if (!openedStreams[sessionId][safeFile]) {
                 const timestamp = String(Date.now())
                 const stream = fs.createWriteStream(
                         FILESPATH + timestamp + '_' + safeFile
                 )
                 const destroyTimeout = setTimeout(() => {
                         openedStreams[sessionId][safeFile].stream.destroy()
                         fs.unlink(
                                 FILESPATH +
                                 openedStreams[sessionId][safeFile].timestamp +
                                 '_' +
                                 safeFile,
                                 () => {}
                         )
                         delete openedStreams[sessionId][safeFile]
@@ -152,51 +270,51 @@ function saveFileChunk(sessionId: string, fileName: string, content: Array<numbe
         clearTimeout(openedStreams[sessionId][safeFile].destroyTimeout)
         openedStreams[sessionId][safeFile].destroyTimeout = setTimeout(() => {
                 openedStreams[sessionId][safeFile].stream.destroy()
                 fs.unlink(
                         FILESPATH +
                         openedStreams[sessionId][safeFile].timestamp +
                         '_' +
                         safeFile,
                         (error) => {
                                 if (error) console.log(error)
                         }
                 )
                 delete openedStreams[sessionId][safeFile]
                 cleanupSessionBucket(sessionId)
                 console.log('destroy', openedStreams)
         }, 30000)
 
         return new Promise<void>((resolve, reject) =>
                 openedStreams[sessionId][safeFile].stream.write(
                         new Uint8Array(content),
                         (error) => (error ? reject(error) : resolve())
                 )
         )
 }
 
-io.on('connect', (socket) => {
+io.on('connection', (socket) => {
 	console.log(socket.id)
 
 	socket.on('disconnecting', async () => {
 		const rooms = Array.from(socket.rooms).filter(
 			(roomId) => roomId != socket.id
 		)
 		if (!rooms.length) return
 
 		const sessionId = rooms[0]
 		console.log('disconnect', socket.id, socket.rooms, sessionId)
 		const sessionData: UnionSessionData = JSON.parse(
 			(await redisClient.get(sessionId))!
 		)
 
 		if (sessionData == null) {
 			socket.to(sessionId).emit('initiator_disconnect')
 			return
 		}
 
 		let isInitiator = sessionData.initiatorSocketId === socket.id
 		if (isInitiator) {
 			if (sessionData.type === 'public') {
 				await Promise.all(
 					sessionData.fileNames.map((item) => {
 						const path =
diff --git a/src/adaos/locales/en.json b/src/adaos/locales/en.json
index 2f2816a023fa2d59af109a2a45fcc44d997f3abd..e5b2d3faaab09cb7ad917feae3b82e506881daa3 100644
--- a/src/adaos/locales/en.json
+++ b/src/adaos/locales/en.json
@@ -88,27 +88,33 @@
         "cli.scenario.test.none": "No scenario tests found",
         "cli.scenario.test.running": "Running scenario tests: {command}",
         "cli.skill.list.item": "- {name} (version: {version})",
         "cli.skill.fs_missing": "⚠ Missing on disk (registered): {items}",
         "cli.skill.fs_extra": "⚠ Extra on disk (not registered): {items}",
         "cli.skill.sync.done": "Skill synchronization completed.",
         "cli.skill.install.done": "Installed skill {name} (version: {version}) at {path}",
         "cli.skill.uninstall.done": "Skill '{name}' removed.",
         "cli.skill.reconcile.missing_root": "Skills directory is missing. Run: adaos skill sync",
         "cli.skill.reconcile.added": "Registry updated: {items}",
         "cli.skill.reconcile.empty": "(nothing)",
         "cli.skill.push.name_help": "Skill name (monorepo subdirectory)",
         "cli.skill.push.name_override_help": "Override skill name for Root draft",
         "cli.skill.push.nothing": "Nothing to push.",
         "cli.skill.push.done": "Pushed {name}: {revision}",
         "cli.skill.push.not_found": "Skill source '{name}' not found",
         "cli.skill.push.root.success": "Skill draft stored at {path}",
         "cli.skill.push.root.dry_run": "[dry-run] Skill draft would be stored at {path}",
         "cli.skill.create.created": "Created: {path}",
         "cli.skill.create.hint_push": "Use `adaos skill push {name}` to publish the draft to Root forge.",
         "cli.skill.run.name_help": "Skill name (directory under skills_root)",
         "cli.skill.run.topic_help": "Topic or intent",
         "cli.skill.run.payload_help": "JSON payload, e.g. '{\"city\":\"Berlin\"}'",
         "cli.skill.run.payload_type_error": "Payload must be a JSON object",
         "cli.skill.run.payload_invalid": "Invalid --payload: {error}",
-        "cli.skill.run.success": "OK: {result}"
+        "cli.skill.run.success": "OK: {result}",
+        "cli.repo.help": "Manage cached registry clones.",
+        "cli.repo.reset.help": "Repository kind (skills or scenarios)",
+        "cli.repo.reset.running": "Resetting {kind} repository at {path}",
+        "cli.repo.reset.not_initialized": "Cached {kind} repository is not initialized at {path}",
+        "cli.repo.reset.failed": "Failed to reset repository: {reason}",
+        "cli.repo.reset.done": "Repository reset at {path}"
 }
diff --git a/src/adaos/locales/ru.json b/src/adaos/locales/ru.json
index 905af770b25cb48f8f9370314c848785cd135d9c..026eb51c3a57fc7d60515039b247b7fb1e9ada8e 100644
--- a/src/adaos/locales/ru.json
+++ b/src/adaos/locales/ru.json
@@ -89,27 +89,33 @@
         "cli.scenario.test.none": "Тесты сценариев не найдены",
         "cli.scenario.test.running": "Запуск тестов сценариев: {command}",
         "cli.skill.list.item": "- {name} (версия: {version})",
         "cli.skill.fs_missing": "⚠ На диске отсутствуют (есть в реестре): {items}",
         "cli.skill.fs_extra": "⚠ На диске лишние (нет в реестре): {items}",
         "cli.skill.sync.done": "Синхронизация навыков завершена.",
         "cli.skill.install.done": "Установлен навык {name} (версия: {version}) в {path}",
         "cli.skill.uninstall.done": "Навык '{name}' удалён.",
         "cli.skill.reconcile.missing_root": "Каталог навыков ещё не создан. Выполните: adaos skill sync",
         "cli.skill.reconcile.added": "В реестре отмечено: {items}",
         "cli.skill.reconcile.empty": "(ничего)",
         "cli.skill.push.name_help": "Имя навыка (подпапка монорепо)",
         "cli.skill.push.name_override_help": "Переопределить имя навыка для черновика Root",
         "cli.skill.push.nothing": "Нет изменений для отправки.",
         "cli.skill.push.done": "Отправлен {name}: {revision}",
         "cli.skill.push.not_found": "Исходники навыка '{name}' не найдены",
         "cli.skill.push.root.success": "Черновик навыка сохранён в {path}",
         "cli.skill.push.root.dry_run": "[dry-run] Черновик навыка был бы сохранён в {path}",
         "cli.skill.create.created": "Создано: {path}",
         "cli.skill.create.hint_push": "Используйте `adaos skill push {name}` чтобы отправить черновик в Root forge.",
         "cli.skill.run.name_help": "Имя навыка (каталог в skills_root)",
         "cli.skill.run.topic_help": "Топик или интент",
         "cli.skill.run.payload_help": "JSON-пейлоад, например '{\"city\":\"Berlin\"}'",
         "cli.skill.run.payload_type_error": "Payload должен быть JSON-объектом",
         "cli.skill.run.payload_invalid": "Некорректный --payload: {error}",
-        "cli.skill.run.success": "OK: {result}"
+        "cli.skill.run.success": "OK: {result}",
+        "cli.repo.help": "Управление кэшем реестра.",
+        "cli.repo.reset.help": "Тип репозитория (skills или scenarios)",
+        "cli.repo.reset.running": "Сбрасываю репозиторий {kind} в {path}",
+        "cli.repo.reset.not_initialized": "Кэшированный репозиторий {kind} не инициализирован в {path}",
+        "cli.repo.reset.failed": "Не удалось сбросить репозиторий: {reason}",
+        "cli.repo.reset.done": "Репозиторий сброшен: {path}"
 }
diff --git a/src/adaos/ports/paths.py b/src/adaos/ports/paths.py
index 591fdf7f263bd161f68e5ca244f5b46a883de9df..d759a36e54137222394ebf9d03a4d231ec2a3118 100644
--- a/src/adaos/ports/paths.py
+++ b/src/adaos/ports/paths.py
@@ -1,29 +1,34 @@
 from __future__ import annotations
 from typing import Protocol
 from pathlib import Path
 
 
 class PathProvider(Protocol):
     """Contract published by :mod:`adaos.adapters.fs.path_provider`."""
 
     @property
     def base(self) -> Path: ...
 
     @property
     def package_dir(self) -> Path: ...
 
     def base_dir(self) -> Path: ...
     def locales_dir(self) -> Path: ...
     def locales_base_dir(self) -> Path: ...
     def skills_locales_dir(self) -> Path: ...
     def scenarios_locales_dir(self) -> Path: ...
+    def workspace_dir(self) -> Path: ...
+    def skills_workspace_dir(self) -> Path: ...
+    def scenarios_workspace_dir(self) -> Path: ...
+    def skills_cache_dir(self) -> Path: ...
+    def scenarios_cache_dir(self) -> Path: ...
     def skill_templates_dir(self) -> Path: ...
     def scenario_templates_dir(self) -> Path: ...
     def skills_dir(self) -> Path: ...
     def scenarios_dir(self) -> Path: ...
     def models_dir(self) -> Path: ...
     def state_dir(self) -> Path: ...
     def cache_dir(self) -> Path: ...
     def logs_dir(self) -> Path: ...
     def tmp_dir(self) -> Path: ...
     
diff --git a/src/adaos/sdk/manage/environment.py b/src/adaos/sdk/manage/environment.py
index 41992703e027cef4f93cf59f95e8e88d4773b7c8..c78cb7a10eebbc1cab2ac3ac6f62f2baa2746cda 100644
--- a/src/adaos/sdk/manage/environment.py
+++ b/src/adaos/sdk/manage/environment.py
@@ -1,49 +1,55 @@
 """Environment preparation utilities for the CLI and SDK."""
 
 from __future__ import annotations
 
 import os
 from pathlib import Path
 
 from adaos.services.agent_context import get_ctx
 from adaos.adapters.db import SqliteSkillRegistry
 from adaos.adapters.skills.git_repo import GitSkillRepository
 from adaos.adapters.scenarios.git_repo import GitScenarioRepository
 
 __all__ = ["prepare_environment"]
 
 
 def prepare_environment() -> None:
     """Ensure base directories and registries exist for local usage."""
 
     ctx = get_ctx()
 
     skills_root = Path(ctx.paths.skills_dir())
     scenarios_root = Path(ctx.paths.scenarios_dir())
+    skills_cache = Path(
+        (ctx.paths.skills_cache_dir() if hasattr(ctx.paths, "skills_cache_dir") else ctx.paths.skills_dir())
+    )
+    scenarios_cache = Path(
+        (ctx.paths.scenarios_cache_dir() if hasattr(ctx.paths, "scenarios_cache_dir") else ctx.paths.scenarios_dir())
+    )
     state_root = Path(ctx.paths.state_dir())
     cache_root = Path(ctx.paths.cache_dir())
     logs_root = Path(ctx.paths.logs_dir())
 
-    for directory in (skills_root, scenarios_root, state_root, cache_root, logs_root):
+    for directory in (skills_root, scenarios_root, skills_cache, scenarios_cache, state_root, cache_root, logs_root):
         directory.mkdir(parents=True, exist_ok=True)
 
     SqliteSkillRegistry(ctx.sql)
 
     if os.getenv("ADAOS_TESTING") == "1":
         return
 
-    if ctx.settings.skills_monorepo_url and not (skills_root / ".git").exists():
+    if ctx.settings.skills_monorepo_url and not (skills_cache / ".git").exists():
         GitSkillRepository(
             paths=ctx.paths,
             git=ctx.git,
             monorepo_url=ctx.settings.skills_monorepo_url,
             monorepo_branch=ctx.settings.skills_monorepo_branch,
         ).ensure()
 
-    if ctx.settings.scenarios_monorepo_url and not (scenarios_root / ".git").exists():
+    if ctx.settings.scenarios_monorepo_url and not (scenarios_cache / ".git").exists():
         GitScenarioRepository(
             paths=ctx.paths,
             git=ctx.git,
             url=ctx.settings.scenarios_monorepo_url,
             branch=ctx.settings.scenarios_monorepo_branch,
         ).ensure()
diff --git a/src/adaos/sdk/manage/scenarios.py b/src/adaos/sdk/manage/scenarios.py
index 3c471ba34c56721b262c547c9fcc8f4f9f8c670a..34be4e4c8712eaba5e242c4dea0dc90fa6078f20 100644
--- a/src/adaos/sdk/manage/scenarios.py
+++ b/src/adaos/sdk/manage/scenarios.py
@@ -43,52 +43,72 @@ __all__ = [
 def _manager(ctx: Any) -> ScenarioManager:
     repo = getattr(ctx, "scenarios_repo", None)
     if repo is None:
         repo = GitScenarioRepository(
             paths=ctx.paths,
             git=ctx.git,
             url=getattr(ctx.settings, "scenarios_monorepo_url", None),
             branch=getattr(ctx.settings, "scenarios_monorepo_branch", None),
         )
     registry = SqliteScenarioRegistry(ctx.sql)
     return ScenarioManager(repo=repo, registry=registry, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)
 
 
 def _repo(ctx: Any) -> GitScenarioRepository:
     repo = getattr(ctx, "scenarios_repo", None)
     if repo is not None:
         return repo
     return GitScenarioRepository(
         paths=ctx.paths,
         git=ctx.git,
         url=getattr(ctx.settings, "scenarios_monorepo_url", None),
         branch=getattr(ctx.settings, "scenarios_monorepo_branch", None),
     )
 
 
+def _workspace_root(ctx: Any) -> Path:
+    attr = getattr(ctx.paths, "scenarios_workspace_dir", None)
+    if attr is not None:
+        value = attr() if callable(attr) else attr
+    else:
+        base = getattr(ctx.paths, "scenarios_dir")
+        value = base() if callable(base) else base
+    return Path(value)
+
+
+def _cache_root(ctx: Any) -> Path:
+    attr = getattr(ctx.paths, "scenarios_cache_dir", None)
+    if attr is not None:
+        value = attr() if callable(attr) else attr
+    else:
+        base = getattr(ctx.paths, "scenarios_dir")
+        value = base() if callable(base) else base
+    return Path(value)
+
+
 def _scenario_dir(ctx: Any, scenario_id: str) -> Path:
-    return Path(ctx.paths.scenarios_dir()) / scenario_id
+    return _workspace_root(ctx) / scenario_id
 
 
 @tool(
     "manage.scenarios.create",
     summary="create a scenario from a template",
     stability="experimental",
     examples=["manage.scenarios.create('demo')"],
 )
 def create(scenario_id: str, template: str = "template") -> str:
     ctx = _require_cap("scenarios.manage")
     repo = _repo(ctx)
     repo.ensure()
     template_dir = Path(ctx.paths.scenario_templates_dir()) / template
     if not template_dir.exists():
         raise FileNotFoundError(f"template '{template}' not found")
     dest = _scenario_dir(ctx, scenario_id)
     if dest.exists():
         raise FileExistsError(f"scenario '{scenario_id}' already exists")
     shutil.copytree(template_dir, dest)
     return str(dest.resolve())
 
 
 @tool(
     "manage.scenarios.install",
     summary="install scenario into workspace",
@@ -102,57 +122,58 @@ def install(scenario_id: str) -> str:
     return str(getattr(meta, "path", _scenario_dir(ctx, scenario_id)))
 
 
 @tool(
     "manage.scenarios.uninstall",
     summary="remove an installed scenario",
     stability="stable",
     examples=["manage.scenarios.uninstall('greet_on_boot')"],
 )
 def uninstall(scenario_id: str) -> str:
     ctx = _require_cap("scenarios.manage")
     mgr = _manager(ctx)
     mgr.remove(scenario_id)
     return scenario_id
 
 
 @tool(
     "manage.scenarios.pull",
     summary="pull scenario sources",
     stability="experimental",
 )
 def pull(scenario_id: str) -> str:
     ctx = _require_cap("scenarios.manage")
     repo = _repo(ctx)
     repo.ensure()
+    cache_root = _cache_root(ctx)
     if hasattr(ctx.git, "sparse_add"):
         try:
-            ctx.git.sparse_add(str(ctx.paths.scenarios_dir()), scenario_id)
+            ctx.git.sparse_add(str(cache_root), f"scenarios/{scenario_id}")
         except Exception:
             pass
     if hasattr(ctx.git, "pull"):
-        ctx.git.pull(ctx.paths.scenarios_dir())
+        ctx.git.pull(str(cache_root))
     return scenario_id
 
 
 @tool(
     "manage.scenarios.push",
     summary="push local scenario changes",
     stability="experimental",
 )
 def push(scenario_id: str, message: Optional[str] = None, signoff: bool = False) -> str:
     ctx = _require_cap("scenarios.manage")
     mgr = _manager(ctx)
     msg = message or f"update scenario {scenario_id}"
     return mgr.push(scenario_id, msg, signoff=signoff)
 
 
 @tool(
     "manage.scenarios.list",
     summary="list installed scenarios",
     stability="stable",
 )
 def list_installed() -> list[str]:
     ctx = _require_cap("scenarios.manage")
     mgr = _manager(ctx)
     records = mgr.list_installed()
     result: list[str] = []
diff --git a/src/adaos/services/scenario/manager.py b/src/adaos/services/scenario/manager.py
index a18f80d0dbbafec6cc15da4c1859730cbe726103..1730a4b3726c5e6a7c86eca0b955a08e9546acb6 100644
--- a/src/adaos/services/scenario/manager.py
+++ b/src/adaos/services/scenario/manager.py
@@ -11,103 +11,125 @@ from adaos.services.eventbus import emit
 from adaos.services.agent_context import get_ctx
 from adaos.services.fs.safe_io import remove_tree
 from adaos.services.git.safe_commit import sanitize_message, check_no_denied
 from adaos.adapters.db import SqliteScenarioRegistry
 from adaos.services.agent_context import AgentContext
 
 _name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")
 
 
 class ScenarioManager:
     """Coordinate scenario lifecycle operations via repository and registry."""
 
     def __init__(
         self,
         *,
         repo: ScenarioRepository,
         registry: SqliteScenarioRegistry,
         git: GitClient,
         paths: PathProvider,
         bus: EventBus,
         caps: Capabilities,  # SqliteScenarioRegistry протоколом не ограничиваем
     ):
         self.repo, self.reg, self.git, self.paths, self.bus, self.caps = repo, registry, git, paths, bus, caps
         self.ctx: AgentContext = get_ctx()
 
+    def _cache_root(self) -> Path:
+        getter = getattr(self.paths, "scenarios_cache_dir", None)
+        if getter is not None:
+            value = getter() if callable(getter) else getter
+        else:
+            base = getattr(self.paths, "scenarios_dir")
+            value = base() if callable(base) else base
+        return Path(value)
+
     def list_installed(self) -> list[SkillRecord]:
         self.caps.require("core", "scenarios.manage")
         return self.reg.list()
 
     def list_present(self) -> list[SkillMeta]:
         self.caps.require("core", "scenarios.manage")
         self.repo.ensure()
         return self.repo.list()
 
     def sync(self) -> None:
         self.caps.require("core", "scenarios.manage", "net.git")
         self.repo.ensure()
-        root = self.paths.scenarios_dir()
+        root = self._cache_root()
         names = [r.name for r in self.reg.list()]
-        self.git.sparse_init(root, cone=False)
-        if names:
-            self.git.sparse_set(root, names, no_cone=True)
-        self.git.pull(root)
+        prefixed = [f"scenarios/{n}" for n in names]
+        self.git.sparse_init(str(root), cone=False)
+        if prefixed:
+            self.git.sparse_set(str(root), prefixed, no_cone=True)
+        self.git.pull(str(root))
         emit(self.bus, "scenario.sync", {"count": len(names)}, "scenario.mgr")
 
     def install(self, name: str, *, pin: Optional[str] = None) -> SkillMeta:
         self.caps.require("core", "scenarios.manage", "net.git")
         name = name.strip()
         if not _name_re.match(name):
             raise ValueError("invalid scenario name")
 
         self.repo.ensure()
 
         self.reg.register(name, pin=pin)
         try:
             test_mode = os.getenv("ADAOS_TESTING") == "1"
             if test_mode:
                 return f"installed: {name} (registry-only{' test-mode' if test_mode else ''})"
             # 3) mono-only установка через репозиторий (sparse-add + pull)
             meta = self.ctx.scenarios_repo.install(name, branch=None)
             if not meta:
                 raise FileNotFoundError(f"scenario '{name}' not found in monorepo")
             emit(self.bus, "scenario.installed", {"id": meta.id.value, "pin": pin}, "scenario.mgr")
             return meta
         except Exception:
             self.reg.unregister(name)
             raise
 
     def remove(self, name: str) -> None:
         self.caps.require("core", "scenarios.manage", "net.git")
         self.repo.ensure()
         self.reg.unregister(name)
-        root = self.paths.scenarios_dir()
+        root = self._cache_root()
         names = [r.name for r in self.reg.list()]
-        self.git.sparse_init(root, cone=False)
-        if names:
-            self.git.sparse_set(root, names, no_cone=True)
-        self.git.pull(root)
-        remove_tree(str(Path(root) / name), fs=self.paths.ctx.fs if hasattr(self.paths, "ctx") else get_ctx().fs)
+        prefixed = [f"scenarios/{n}" for n in names]
+        self.git.sparse_init(str(root), cone=False)
+        if prefixed:
+            self.git.sparse_set(str(root), prefixed, no_cone=True)
+        self.git.pull(str(root))
+        remove_tree(
+            str(root / "scenarios" / name),
+            fs=self.paths.ctx.fs if hasattr(self.paths, "ctx") else get_ctx().fs,
+        )
         emit(self.bus, "scenario.removed", {"id": name}, "scenario.mgr")
 
     def push(self, name: str, message: str, *, signoff: bool = False) -> str:
         self.caps.require("core", "scenarios.manage", "git.write", "net.git")
-        root = self.paths.scenarios_dir()
+        root = self._cache_root()
         if not (Path(root) / ".git").exists():
             raise RuntimeError("Scenarios repo is not initialized. Run `adaos scenario sync` once.")
         sub = name.strip()
-        changed = self.git.changed_files(root, subpath=sub)
+        subpath = f"scenarios/{sub}"
+        changed = self.git.changed_files(str(root), subpath=subpath)
         if not changed:
             return "nothing-to-push"
         bad = check_no_denied(changed)
         if bad:
             raise PermissionError(f"push denied: sensitive files matched: {', '.join(bad)}")
         msg = sanitize_message(message)
         ctx = get_ctx()
-        sha = self.git.commit_subpath(root, subpath=sub, message=msg, author_name=ctx.settings.git_author_name, author_email=ctx.settings.git_author_email, signoff=signoff)
+        sha = self.git.commit_subpath(
+            str(root),
+            subpath=subpath,
+            message=msg,
+            author_name=ctx.settings.git_author_name,
+            author_email=ctx.settings.git_author_email,
+            signoff=signoff,
+        )
         if sha != "nothing-to-commit":
-            self.git.push(root)
+            self.git.push(str(root))
         return sha
 
     def uninstall(self, name: str) -> None:
         """Alias for :meth:`remove` to keep parity with :class:`SkillManager`."""
         self.remove(name)
diff --git a/src/adaos/services/skill/manager.py b/src/adaos/services/skill/manager.py
index e8ec0459b151893c6be6944a4899fbed06bd69ae..88110cd5b863086684904ce657f00fcdecf6d942 100644
--- a/src/adaos/services/skill/manager.py
+++ b/src/adaos/services/skill/manager.py
@@ -16,132 +16,154 @@ from adaos.services.settings import Settings
 from adaos.services.agent_context import get_ctx
 from adaos.services.skill.validation import SkillValidationService
 from adaos.services.agent_context import AgentContext
 
 _name_re = re.compile(r"^[a-zA-Z0-9_\-\/]+$")
 
 
 class SkillManager:
     def __init__(
         self,
         *,
         git: GitClient,  # Deprecated. TODO Move to ctx
         paths: PathProvider,  # Deprecated. TODO Move to ctx
         caps: Capabilities,
         settings: Settings | None = None,
         registry: SkillRegistry = None,
         repo: SkillRepository | None = None,  # Deprecated. TODO Move to ctx
         bus: EventBus | None = None,
     ):
         self.reg = registry
         self.bus = bus
         self.caps = caps
         self.settings = settings
         self.ctx: AgentContext = get_ctx()
 
+    def _cache_root(self) -> Path:
+        paths = self.ctx.paths
+        getter = getattr(paths, "skills_cache_dir", None)
+        if getter is not None:
+            value = getter() if callable(getter) else getter
+        else:
+            base = getattr(paths, "skills_dir")
+            value = base() if callable(base) else base
+        return Path(value)
+
     def list_installed(self) -> list[SkillRecord]:
         self.caps.require("core", "skills.manage")
         return self.ctx.skills_repo.list()
 
     def list_present(self) -> list[SkillMeta]:
         self.caps.require("core", "skills.manage")
         self.ctx.skills_repo.ensure()
         return self.ctx.skills_repo.list()
 
     def get(self, skill_id: str) -> Optional[SkillMeta]:
         return self.ctx.skills_repo.get(skill_id)
 
     def sync(self) -> None:
         self.caps.require("core", "skills.manage", "net.git")
         self.ctx.skills_repo.ensure()
-        root = self.ctx.paths.skills_dir()
+        root = self._cache_root()
         names = [r.name for r in self.reg.list()]
-        ensure_clean(self.ctx.git, root, names)
-        self.ctx.git.sparse_init(root, cone=False)
-        if names:
-            self.ctx.git.sparse_set(root, names, no_cone=True)
-        self.ctx.git.pull(root)
+        prefixed = [f"skills/{n}" for n in names]
+        ensure_clean(self.ctx.git, str(root), prefixed)
+        self.ctx.git.sparse_init(str(root), cone=False)
+        if prefixed:
+            self.ctx.git.sparse_set(str(root), prefixed, no_cone=True)
+        self.ctx.git.pull(str(root))
         emit(self.bus, "skill.sync", {"count": len(names)}, "skill.mgr")
 
     def install(self, name: str, pin: str | None = None, validate: bool = True, strict: bool = True, probe_tools: bool = False) -> tuple[SkillMeta, Optional[object]]:
         """
         Возвращает (meta, report|None). При strict и ошибках валидации можно выбрасывать исключение.
         """
         self.caps.require("core", "skills.manage")
         name = name.strip()
         if not _name_re.match(name):
             raise ValueError("invalid skill name")
 
         # 1) регистрируем (идемпотентно)
         self.reg.register(name, pin=pin)
         # 2) в тестах/без .git — только реестр
-        root = self.ctx.paths.skills_dir()
         test_mode = os.getenv("ADAOS_TESTING") == "1"
         if test_mode:
             return f"installed: {name} (registry-only{' test-mode' if test_mode else ''})"
         # 3) mono-only установка через репозиторий (sparse-add + pull)
         meta = self.ctx.skills_repo.install(name, branch=None)
         """ if not validate:
             return meta, None """
         report = SkillValidationService(self.ctx).validate(meta.id.value, strict=strict, probe_tools=probe_tools)
         if strict and not report.ok:
             # опционально можно откатывать установку:
             # self.ctx.skills_repo.uninstall(meta.id.value)
             # и/или пробрасывать исключение
             return meta, report
 
         return meta, report  # return f"installed: {name}"
 
     def uninstall(self, name: str) -> None:
         self.caps.require("core", "skills.manage", "net.git")
         name = name.strip()
         if not _name_re.match(name):
             raise ValueError("invalid skill name")
         # если записи нет — считаем idempotent
         rec = self.reg.get(name)
         if not rec:
             return f"uninstalled: {name} (not found)"
         self.reg.unregister(name)
-        root = self.ctx.paths.skills_dir()
+        root = self._cache_root()
         test_mode = os.getenv("ADAOS_TESTING") == "1"
         # в тестах/без .git — только реестр, без git операций
         if test_mode or not (root / ".git").exists():
             return f"uninstalled: {name} (registry-only{' test-mode' if test_mode else ''})"
         names = [r.name for r in self.reg.list()]
-        ensure_clean(self.ctx.git, root, names)
-        self.ctx.git.sparse_init(root, cone=False)
-        if names:
-            self.ctx.git.sparse_set(root, names, no_cone=True)
-        self.ctx.git.pull(root)
-        remove_tree(str(Path(root) / name), fs=self.ctx.paths.ctx.fs if hasattr(self.ctx.paths, "ctx") else get_ctx().fs)
+        prefixed = [f"skills/{n}" for n in names]
+        ensure_clean(self.ctx.git, str(root), prefixed)
+        self.ctx.git.sparse_init(str(root), cone=False)
+        if prefixed:
+            self.ctx.git.sparse_set(str(root), prefixed, no_cone=True)
+        self.ctx.git.pull(str(root))
+        remove_tree(
+            str(root / "skills" / name),
+            fs=self.ctx.paths.ctx.fs if hasattr(self.ctx.paths, "ctx") else get_ctx().fs,
+        )
         emit(self.bus, "skill.uninstalled", {"id": name}, "skill.mgr")
 
     def push(self, name: str, message: str, *, signoff: bool = False) -> str:
         self.caps.require("core", "skills.manage", "git.write", "net.git")
-        root = self.ctx.paths.skills_dir()
+        root = self._cache_root()
         if not (root / ".git").exists():
             raise RuntimeError("Skills repo is not initialized. Run `adaos skill sync` once.")
 
         sub = name.strip()
-        changed = self.ctx.git.changed_files(root, subpath=sub)
+        subpath = f"skills/{sub}"
+        changed = self.ctx.git.changed_files(str(root), subpath=subpath)
         if not changed:
             return "nothing-to-push"
         bad = check_no_denied(changed)
         if bad:
             raise PermissionError(f"push denied: sensitive files matched: {', '.join(bad)}")
         # безопасно получаем автора
         if self.settings:
             author_name = self.settings.git_author_name
             author_email = self.settings.git_author_email
         else:
             # fallback, если кто-то создаст менеджер без settings
             try:
                 ctx = get_ctx()
                 author_name = ctx.settings.git_author_name
                 author_email = ctx.settings.git_author_email
             except Exception:
                 author_name, author_email = "AdaOS Bot", "bot@adaos.local"
         msg = sanitize_message(message)
-        sha = self.ctx.git.commit_subpath(root, subpath=name.strip(), message=msg, author_name=author_name, author_email=author_email, signoff=signoff)
+        sha = self.ctx.git.commit_subpath(
+            str(root),
+            subpath=subpath,
+            message=msg,
+            author_name=author_name,
+            author_email=author_email,
+            signoff=signoff,
+        )
         if sha != "nothing-to-commit":
-            self.ctx.git.push(root)
+            self.ctx.git.push(str(root))
         return sha
diff --git a/tests/conftest.py b/tests/conftest.py
index efcf32af4881f8ee46becf4593c39c1c72cab6e7..864c469f07d4578b81819d877586ba7400c01799 100644
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -5,92 +5,110 @@ import os, asyncio, inspect, types
 from pathlib import Path
 import sys, os, subprocess
 import pytest
 
 from adaos.services.agent_context import AgentContext, set_ctx, clear_ctx
 from adaos.services.settings import Settings
 from adaos.services.eventbus import LocalEventBus
 from adaos.adapters.db.sqlite_store import SQLite, SQLiteKV
 from adaos.domain.types import ProcessSpec
 from adaos.services.logging import setup_logging, attach_event_logger  # важное: создаёт файл-лог
 
 _MIN_PY = tuple(map(int, os.getenv("ADAOS_MIN_PY", "3.11").split(".")))
 
 
 def _versions_list():
     try:
         return subprocess.check_output(["py", "-0p"], text=True, stderr=subprocess.STDOUT)
     except Exception:
         return "(py launcher not available)"
 
 
 # ---- Paths provider для тестов ----
 class TestPaths:
     def __init__(self, base: Path):
         self._base = Path(base).resolve()
-        self._skills = self._base / "skills"
-        self._scenarios = self._base / "scenarios"
+        self._workspace = self._base / "workspace"
+        self._skills = self._workspace / "skills"
+        self._scenarios = self._workspace / "scenarios"
+        self._skills_cache = self._base / "skills"
+        self._scenarios_cache = self._base / "scenarios"
         self._state = self._base / "state"
         self._cache = self._base / "cache"
         self._logs = self._base / "logs"
         self._models = self._base / "models"
         self._tmp = self._base / "tmp"
         self._i18n = self._base / "i18n"
         self._package = Path(__file__).resolve().parents[1]
         self._package_locales = self._package / "locales"
         self._skill_templates = self._package / "skills_templates"
         self._scenario_templates = self._package / "scenario_templates"
 
     @property
     def base(self) -> Path:
         return self._base
 
     @property
     def package_dir(self) -> Path:
         return self._package
 
     def base_dir(self) -> Path:
         return self._base
 
     def locales_dir(self) -> Path:
         return self._package_locales
 
     def locales_base_dir(self) -> Path:
         return self._i18n
 
     def skills_locales_dir(self) -> Path:
         return self.locales_base_dir()
 
     def scenarios_locales_dir(self) -> Path:
         return self.locales_base_dir()
 
     def skill_templates_dir(self) -> Path:
         return self._skill_templates
 
     def scenario_templates_dir(self) -> Path:
         return self._scenario_templates
 
+    def workspace_dir(self) -> Path:
+        return self._workspace
+
+    def skills_workspace_dir(self) -> Path:
+        return self._skills
+
+    def scenarios_workspace_dir(self) -> Path:
+        return self._scenarios
+
+    def skills_cache_dir(self) -> Path:
+        return self._skills_cache
+
+    def scenarios_cache_dir(self) -> Path:
+        return self._scenarios_cache
+
     def skills_dir(self) -> Path:
         return self._skills
 
     def scenarios_dir(self) -> Path:
         return self._scenarios
 
     def state_dir(self) -> Path:
         return self._state
 
     def cache_dir(self) -> Path:
         return self._cache
 
     def logs_dir(self) -> Path:
         return self._logs
 
     def models_dir(self) -> Path:
         return self._models
 
     def tmp_dir(self) -> Path:
         return self._tmp
 
 
 # ---- минимальный реалистичный процесс-менеджер для тестов ----
 class TestProc:
     async def start(self, spec: ProcessSpec):
@@ -177,52 +195,56 @@ def cli_app():
 
 
 # ---------- отдельная фикстура tmp_base_dir (нужна тестам CLI CRUD) ----------
 @pytest.fixture
 def tmp_base_dir(tmp_path, monkeypatch) -> Path:
     base_dir = tmp_path / "base"
     monkeypatch.setenv("ADAOS_BASE_DIR", str(base_dir))
     monkeypatch.setenv("ADAOS_TESTING", "1")
     return base_dir
 
 
 # ---------- автofixture: поднимаем AgentContext для каждого теста ----------
 @pytest.fixture(autouse=True)
 def _autocontext(tmp_path, monkeypatch):
     base_dir = tmp_path / "base"
     monkeypatch.setenv("ADAOS_SANDBOX_DISABLED", "1")
     monkeypatch.setenv("ADAOS_BASE_DIR", str(base_dir))
     monkeypatch.setenv("ADAOS_TESTING", "1")
 
     # Settings (без make_paths; только base_dir/profile)
     settings = Settings.from_sources().with_overrides(base_dir=str(base_dir), profile="test")
 
     # Paths + каталоги
     paths = TestPaths(base_dir)
     for p in (
-        paths.skills_dir(),
-        paths.scenarios_dir(),
+        paths.base_dir(),
+        paths.workspace_dir(),
+        paths.skills_workspace_dir(),
+        paths.scenarios_workspace_dir(),
+        paths.skills_cache_dir(),
+        paths.scenarios_cache_dir(),
         paths.state_dir(),
         paths.cache_dir(),
         paths.logs_dir(),
         paths.models_dir(),
         paths.tmp_dir(),
         paths.locales_base_dir(),
     ):
         Path(p).mkdir(parents=True, exist_ok=True)
 
     # Реальные порты
     bus = LocalEventBus()
     sql = SQLite(paths)  # БД в {state_dir}/adaos.db
     kv = SQLiteKV(sql)
     proc = TestProc()  # важно для runtime-тестов
 
     # Остальные — простые no-op объекты
     class Noop: ...
 
     ctx_kwargs = dict(
         settings=settings,
         paths=paths,
         bus=bus,
         sql=sql,
         kv=kv,
         proc=proc,
diff --git a/tests/smoke/test_skill_repo_listing.py b/tests/smoke/test_skill_repo_listing.py
index 7d8872d97bbcf8d4188e38237436c1d40b469621..8353b861baaa4cd2f5d2bf51aed5ca5954bb13d8 100644
--- a/tests/smoke/test_skill_repo_listing.py
+++ b/tests/smoke/test_skill_repo_listing.py
@@ -1,24 +1,26 @@
 # tests/smoke/test_skill_repo_listing.py
 from pathlib import Path
 from adaos.services.agent_context import get_ctx
 from adaos.adapters.skills.git_repo import GitSkillRepository
 from adaos.domain import SkillId
 from adaos.services.skill.service import SkillService
 
 
 def test_list_empty(tmp_path, monkeypatch):
     monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path / "base"))
     ctx = get_ctx()
     repo = ctx.skills_repo
     assert repo.list() == []
 
 
 def test_manifest_defaults(tmp_path, monkeypatch):
     monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path / "base"))
     ctx = get_ctx()
-    sd = Path(ctx.paths.skills_dir()) / "foo"
+    skills_root_attr = getattr(ctx.paths, "skills_cache_dir", None)
+    skills_root = skills_root_attr() if callable(skills_root_attr) else ctx.paths.skills_dir()
+    sd = Path(skills_root) / "skills" / "foo"
     sd.mkdir(parents=True, exist_ok=True)
     (sd / "skill.yaml").write_text("id: demo\nversion: '1.2.3'\nname: Demo Skill\n", encoding="utf-8")
     svc = SkillService(repo=ctx.skills_repo, bus=ctx.bus)
     items = svc.list()
     assert any(m.id.value == "demo" and m.version == "1.2.3" for m in items)
diff --git a/tests/test_cli_scenarios_crud.py b/tests/test_cli_scenarios_crud.py
index b0069d62c2485ffc82f878dc6c2afad10eefe2f3..a9ff322342dab2d36248d6f5cb397a7a2d463817 100644
--- a/tests/test_cli_scenarios_crud.py
+++ b/tests/test_cli_scenarios_crud.py
@@ -1,35 +1,35 @@
 import json
 from pathlib import Path
 
 from typer.testing import CliRunner
 
 from adaos.adapters.db import SqliteScenarioRegistry
 from adaos.services.agent_context import get_ctx
 
 
 def _scenario_names(cli_app) -> set[str]:
     result = CliRunner().invoke(cli_app, ["scenario", "list", "--json"])
     assert result.exit_code == 0
     payload = json.loads(result.stdout or "{}")
     return {item["name"] for item in payload.get("scenarios", [])}
 
 
 def test_scenario_create_command(cli_app, tmp_base_dir, tmp_path):
     runner = CliRunner()
     scenario_id = "demo_scenario_cli"
 
     result = runner.invoke(cli_app, ["scenario", "create", scenario_id, "--template", "template"])
     assert result.exit_code == 0
 
-    scenario_dir = Path(tmp_base_dir) / "scenarios" / scenario_id
+    scenario_dir = Path(tmp_base_dir) / "workspace" / "scenarios" / scenario_id
     assert scenario_dir.exists()
 
     manifest = scenario_dir / "scenario.yaml"
     assert manifest.exists()
     assert f"id: {scenario_id}" in manifest.read_text(encoding="utf-8")
 
     registry = SqliteScenarioRegistry(get_ctx().sql)
     assert registry.get(scenario_id) is not None
 
     names = _scenario_names(cli_app)
     assert scenario_id in names
diff --git a/tests/test_path_provider.py b/tests/test_path_provider.py
index 296992c50e2997b0aca127c228b72dfd11503b6f..8c2a1736952db1d4da12d475ddfbdbff12740f36 100644
--- a/tests/test_path_provider.py
+++ b/tests/test_path_provider.py
@@ -1,30 +1,45 @@
 """Tests covering path resolution helpers exposed through the global context."""
 
 from __future__ import annotations
 
 from pathlib import Path
 
 from adaos.adapters.fs.path_provider import PathProvider
 from adaos.services.agent_context import get_ctx
 from adaos.services.settings import Settings
 
 
 def test_path_provider_locales_base_dir(tmp_path):
     settings = Settings.from_sources().with_overrides(base_dir=tmp_path / "adaos-test", profile="test")
     provider = PathProvider(settings)
 
     expected = (Path(settings.base_dir).expanduser().resolve() / "i18n")
     assert provider.locales_base_dir() == expected
     assert provider.skills_locales_dir() == expected
     assert provider.scenarios_locales_dir() == expected
 
 
+def test_path_provider_workspace_layout(tmp_path):
+    settings = Settings.from_sources().with_overrides(base_dir=tmp_path / "adaos-test", profile="test")
+    provider = PathProvider(settings)
+
+    base = Path(settings.base_dir).expanduser().resolve()
+    assert provider.workspace_dir() == base / "workspace"
+    assert provider.skills_workspace_dir() == base / "workspace" / "skills"
+    assert provider.scenarios_workspace_dir() == base / "workspace" / "scenarios"
+    assert provider.skills_cache_dir() == base / "skills"
+    assert provider.scenarios_cache_dir() == base / "scenarios"
+    # Compatibility aliases
+    assert provider.skills_dir() == provider.skills_workspace_dir()
+    assert provider.scenarios_dir() == provider.scenarios_workspace_dir()
+
+
 def test_agent_context_exposes_locales_from_provider():
     ctx = get_ctx()
 
     base_locales = Path(ctx.paths.locales_base_dir())
     assert base_locales == Path(ctx.paths.skills_locales_dir())
     assert base_locales == Path(ctx.paths.scenarios_locales_dir())
 
     # ensure the directory lives under the base dir from the context
     assert base_locales.parent == Path(ctx.paths.base_dir())
 
EOF
)