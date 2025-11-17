# src/adaos/services/interpreter/workspace.py
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from adaos.services.agent_context import AgentContext


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _hash_payload(payload: Any) -> str:
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass(slots=True)
class IntentMapping:
    intent: str
    description: str | None = None
    skill: str | None = None
    tool: str | None = None
    scenario: str | None = None
    examples: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        return {k: v for k, v in payload.items() if v not in (None, [], "")}


class InterpreterWorkspace:
    CONFIG_FILENAME = "config.yaml"
    METADATA_FILENAME = "metadata.json"
    DATA_PACKAGE = "adaos.interpreter_data"
    CONFIG_TEMPLATE = "config.default.yml"
    DEFAULT_DATASET = "moodbot"
    SKILL_METADATA_DIR = "interpreter"
    SKILL_METADATA_FILE = "intents.yml"

    def __init__(self, ctx: AgentContext):
        self._ctx = ctx
        state_dir = Path(ctx.paths.state_dir())
        self.root = (state_dir / "interpreter").resolve()
        self.datasets_dir = self.root / "datasets"
        self.config_path = self.root / self.CONFIG_FILENAME
        self.metadata_path = self.root / self.METADATA_FILENAME
        self.project_dir = self.root / "rasa_project"

        self.root.mkdir(parents=True, exist_ok=True)
        self.datasets_dir.mkdir(parents=True, exist_ok=True)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_default_files()

    @property
    def context(self) -> AgentContext:
        return self._ctx

    # ------------------------------------------------------------------ config
    def _ensure_default_files(self) -> None:
        if not self.config_path.exists():
            self._seed_config_template()

        preset = self.get_dataset_preset()
        self.sync_dataset_from_repo(preset, overwrite=False)
        self.generate_dataset_from_skills()

        custom_file = self.datasets_dir / "custom.md"
        if not custom_file.exists():
            custom_file.write_text(
                "# custom dataset\n"
                "- сюда можно добавлять свои примеры\n",
                encoding="utf-8",
            )

    def _seed_config_template(self) -> None:
        try:
            template = resources.files(self.DATA_PACKAGE) / self.CONFIG_TEMPLATE
        except Exception:
            template = None
        if template is None:
            fallback = {
                "dataset": {"preset": self.DEFAULT_DATASET},
                "intents": [],
            }
            self.config_path.write_text(yaml.safe_dump(fallback, allow_unicode=True, sort_keys=False), encoding="utf-8")
            return
        with resources.as_file(template) as src:
            shutil.copy(src, self.config_path)

    def available_presets(self) -> List[str]:
        try:
            base = resources.files(self.DATA_PACKAGE)
        except Exception:
            return []
        return sorted(item.name for item in base.iterdir() if item.is_dir() and not item.name.startswith("__"))

    def get_dataset_preset(self) -> str:
        try:
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return self.DEFAULT_DATASET
        dataset_section = data.get("dataset") or {}
        return dataset_section.get("preset") or self.DEFAULT_DATASET

    def sync_dataset_from_repo(self, preset: str, *, overwrite: bool = False) -> Path:
        try:
            preset_root = resources.files(self.DATA_PACKAGE) / preset
        except Exception as exc:
            raise ValueError(f"dataset preset '{preset}' not found in package") from exc

        dest = self.datasets_dir / preset
        if overwrite and dest.exists():
            shutil.rmtree(dest)
        if dest.exists():
            return dest
        with resources.as_file(preset_root) as src_dir:
            shutil.copytree(src_dir, dest)
        return dest

    # ----------------------------------------------------- skill metadata
    def collect_skill_intents(self) -> List[IntentMapping]:
        """Reads interpreter metadata from installed skills."""
        result: List[IntentMapping] = []
        skills_root = Path(self._ctx.paths.skills_dir())
        try:
            skills = self._ctx.skills_repo.list()
        except Exception:
            skills = []
        for meta in skills:
            skill_path = skills_root / meta.id.value
            meta_dir = skill_path / self.SKILL_METADATA_DIR
            if not meta_dir.exists():
                continue
            for fname in (self.SKILL_METADATA_FILE, "intents.yaml"):
                fpath = meta_dir / fname
                if not fpath.exists():
                    continue
                try:
                    payload = yaml.safe_load(fpath.read_text(encoding="utf-8")) or []
                except Exception:
                    payload = []
                if isinstance(payload, dict) and "intents" in payload:
                    payload = payload["intents"]
                for entry in payload:
                    intent = (entry or {}).get("intent")
                    if not intent:
                        continue
                    examples = entry.get("examples") or []
                    result.append(
                        IntentMapping(
                            intent=intent,
                            description=entry.get("description"),
                            skill=entry.get("skill") or meta.id.value,
                            tool=entry.get("tool"),
                            scenario=entry.get("scenario"),
                            examples=examples,
                        )
                    )
        return result

    def generate_dataset_from_skills(self) -> None:
        """Creates synthetic dataset fragments describing skill metadata."""
        skill_intents = self.collect_skill_intents()
        auto_dir = self.datasets_dir / "skills_auto"
        if not skill_intents:
            if auto_dir.exists():
                shutil.rmtree(auto_dir)
            return
        auto_dir.mkdir(parents=True, exist_ok=True)

        nlu_payload = {"version": "3.1", "nlu": []}
        for mapping in skill_intents:
            if not mapping.examples:
                continue
            formatted = "\n".join(f"- {ex}" for ex in mapping.examples if ex)
            if formatted.strip():
                nlu_payload["nlu"].append({"intent": mapping.intent, "examples": formatted})
        if nlu_payload["nlu"]:
            (auto_dir / "nlu.yml").write_text(
                yaml.safe_dump(nlu_payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        meta_doc = {
            "intents": [
                {
                    "intent": m.intent,
                    "description": m.description,
                    "skill": m.skill,
                    "tool": m.tool,
                    "scenario": m.scenario,
                }
                for m in skill_intents
            ]
        }
        (auto_dir / "meta.yml").write_text(yaml.safe_dump(meta_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def load_config(self) -> Dict[str, Any]:
        try:
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            self._ensure_default_files()
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        intents = data.get("intents") or []
        data["intents"] = sorted(intents, key=lambda x: x.get("intent", ""))
        return data

    def save_config(self, config: Dict[str, Any]) -> None:
        config = dict(config)
        config["intents"] = sorted(config.get("intents", []), key=lambda x: x.get("intent", ""))
        self.config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def list_intents(self) -> List[IntentMapping]:
        config = self.load_config()
        mappings: List[IntentMapping] = []
        for entry in config.get("intents", []):
            mappings.append(
                IntentMapping(
                    intent=entry.get("intent", ""),
                    description=entry.get("description"),
                    skill=entry.get("skill"),
                    tool=entry.get("tool"),
                    scenario=entry.get("scenario"),
                    examples=entry.get("examples") or [],
                )
            )
        return mappings

    def upsert_intent(self, mapping: IntentMapping) -> None:
        config = self.load_config()
        intents = config.get("intents", [])
        intents = [item for item in intents if item.get("intent") != mapping.intent]
        intents.append(mapping.as_dict())
        config["intents"] = intents
        self.save_config(config)

    def remove_intent(self, intent: str) -> bool:
        config = self.load_config()
        intents = config.get("intents", [])
        new_intents = [item for item in intents if item.get("intent") != intent]
        if len(new_intents) == len(intents):
            return False
        config["intents"] = new_intents
        self.save_config(config)
        return True

    # -------------------------------------------------------------- metadata IO
    def load_metadata(self) -> Dict[str, Any]:
        if not self.metadata_path.exists():
            return {}
        try:
            return json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def save_metadata(self, payload: Dict[str, Any]) -> None:
        self.metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # -------------------------------------------------------------- snapshots
    def _skill_snapshot(self) -> List[Dict[str, Any]]:
        skills = []
        try:
            for meta in self._ctx.skills_repo.list():
                skills.append({"id": meta.id.value, "name": meta.name, "version": meta.version})
        except Exception:
            pass
        return sorted(skills, key=lambda x: x["id"])

    def _dataset_snapshot(self) -> List[Dict[str, Any]]:
        snapshot: List[Dict[str, Any]] = []
        if not self.datasets_dir.exists():
            return snapshot
        for path in sorted(self.datasets_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            try:
                data = path.read_bytes()
            except OSError:
                continue
            snapshot.append({"path": rel, "hash": hashlib.sha256(data).hexdigest(), "size": len(data)})
        return snapshot

    def _auto_intents_snapshot(self) -> List[Dict[str, Any]]:
        items = []
        for mapping in self.collect_skill_intents():
            items.append(
                {
                    "intent": mapping.intent,
                    "skill": mapping.skill,
                    "tool": mapping.tool,
                    "scenario": mapping.scenario,
                    "examples_hash": _hash_payload(mapping.examples),
                }
            )
        return sorted(items, key=lambda x: x["intent"])

    def _config_hash(self, config: Dict[str, Any]) -> str:
        return _hash_payload(config.get("intents", []))

    def _skill_hash(self, snapshot: Iterable[Dict[str, Any]]) -> str:
        return _hash_payload(list(snapshot))

    def _dataset_hash(self, snapshot: Iterable[Dict[str, Any]]) -> str:
        return _hash_payload(list(snapshot))

    def fingerprint(
        self,
        config: Dict[str, Any],
        skills: List[Dict[str, Any]],
        datasets: List[Dict[str, Any]],
        auto_intents: List[Dict[str, Any]],
    ) -> str:
        payload = {
            "intents": config.get("intents", []),
            "skills": skills,
            "datasets": datasets,
            "auto_intents": auto_intents,
        }
        return _hash_payload(payload)

    # -------------------------------------------------------------- status info
    def describe_status(self) -> Dict[str, Any]:
        config = self.load_config()
        skills = self._skill_snapshot()
        datasets = self._dataset_snapshot()
        auto_intents = self._auto_intents_snapshot()
        current_fp = self.fingerprint(config, skills, datasets, auto_intents)
        meta = self.load_metadata()
        needs_training = current_fp != meta.get("fingerprint")

        reasons: List[str] = []
        if not meta.get("fingerprint"):
            reasons.append("Модель ещё ни разу не обучалась.")
        else:
            if meta.get("config_hash") != self._config_hash(config):
                reasons.append("Конфигурация интентов изменилась.")
            if meta.get("skill_hash") != self._skill_hash(skills):
                reasons.append("Список скиллов изменился.")
            if meta.get("dataset_hash") != self._dataset_hash(datasets):
                reasons.append("Наборы данных изменились.")
            if meta.get("auto_intents_hash") != _hash_payload(auto_intents):
                reasons.append("Интенты из скиллов обновились.")

        return {
            "needs_training": needs_training,
            "reasons": reasons,
            "intent_count": len(config.get("intents", [])),
            "trained_at": meta.get("trained_at"),
            "dataset_summary": self.dataset_summary(),
            "fingerprint": current_fp,
            "skills": skills,
            "auto_intents": auto_intents,
        }

    def dataset_summary(self) -> Dict[str, Any]:
        summary = {"files": [], "total_size": 0}
        for entry in self._dataset_snapshot():
            summary["files"].append({"path": entry["path"], "size": entry["size"]})
            summary["total_size"] += entry["size"]
        return summary

    # ----------------------------------------------------- rasa project export
    def build_rasa_project(self) -> Path:
        self.generate_dataset_from_skills()
        config = self.load_config()
        manual = self.list_intents()
        auto = self.collect_skill_intents()
        combined: Dict[str, IntentMapping] = {m.intent: m for m in auto if m.intent}
        for mapping in manual:
            combined[mapping.intent] = mapping
        mappings = list(combined.values())
        dataset_files = self._dataset_snapshot()
        if not mappings and not dataset_files:
            raise RuntimeError("Нет доступных данных для обучения (интенты или датасеты).")

        preset = self.get_dataset_preset()
        self.sync_dataset_from_repo(preset, overwrite=False)

        project = self.project_dir
        data_dir = project / "data"
        packaged_dir = data_dir / "datasets"
        data_dir.mkdir(parents=True, exist_ok=True)
        packaged_dir.mkdir(parents=True, exist_ok=True)

        nlu_payload: Dict[str, Any] = {"version": "3.1", "nlu": []}
        for mapping in mappings:
            if not mapping.examples:
                continue
            formatted = "\n".join(f"- {ex}" for ex in mapping.examples if ex)
            if formatted.strip():
                nlu_payload["nlu"].append({"intent": mapping.intent, "examples": formatted})
        (data_dir / "intents_from_config.yml").write_text(
            yaml.safe_dump(nlu_payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        # copy datasets
        for child in self.datasets_dir.iterdir():
            dest = packaged_dir / child.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            if child.is_dir():
                shutil.copytree(child, dest)
            else:
                shutil.copy2(child, dest)

        config_target = project / "config.yml"
        if not config_target.exists():
            config_target.write_text(
                yaml.safe_dump(
                    {
                        "language": "ru",
                        "pipeline": [
                            {"name": "WhitespaceTokenizer"},
                            {"name": "CountVectorsFeaturizer"},
                            {
                                "name": "CountVectorsFeaturizer",
                                "analyzer": "char_wb",
                                "min_ngram": 3,
                                "max_ngram": 5,
                            },
                            {"name": "DIETClassifier", "epochs": 80, "constrain_similarities": True},
                        ],
                        "policies": [{"name": "RulePolicy", "core_fallback_threshold": 0.4}],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

        readme = project / "README.txt"
        readme.write_text(
            "Этот каталог формируется автоматически из данных репозитория и state/interpreter.\n",
            encoding="utf-8",
        )
        return project

    # ---------------------------------------------------------------- training
    def record_training(self, *, note: str | None = None, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        config = self.load_config()
        skills = self._skill_snapshot()
        datasets = self._dataset_snapshot()
        auto_intents = self._auto_intents_snapshot()
        meta = {
            "trained_at": _utc_now(),
            "fingerprint": self.fingerprint(config, skills, datasets, auto_intents),
            "config_hash": self._config_hash(config),
            "skill_hash": self._skill_hash(skills),
            "dataset_hash": self._dataset_hash(datasets),
            "auto_intents_hash": _hash_payload(auto_intents),
            "skills_snapshot": skills,
            "intent_count": len(config.get("intents", [])),
            "dataset_summary": self.dataset_summary(),
        }
        if note:
            meta["note"] = note
        if extra:
            meta["extra"] = extra
        self.save_metadata(meta)
        return meta
