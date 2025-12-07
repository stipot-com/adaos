from __future__ import annotations

import re
import yaml
from pathlib import Path
from typing import Any, Dict, List, Mapping

from adaos.services.agent_context import get_ctx


def _write_skill_bundle(manifest_text: str, handler_text: str, skill_name_hint: str) -> Path:
    manifest = yaml.safe_load(manifest_text) or {}
    skill_name = manifest.get("name") or skill_name_hint

    ctx = get_ctx()
    skills_dir_attr = getattr(ctx.paths, "skills_dir", None)
    skills_root = skills_dir_attr() if callable(skills_dir_attr) else skills_dir_attr
    if not skills_root:
        raise RuntimeError("skills directory path is not configured in agent context")
    skill_dir = Path(skills_root) / str(skill_name).lower()
    skill_dir.mkdir(parents=True, exist_ok=True)

    (skill_dir / "manifest.yaml").write_text(manifest_text, encoding="utf-8")
    (skill_dir / "handler.py").write_text(handler_text, encoding="utf-8")
    return skill_dir


def _write_multifile_output(llm_output: str, target_root: Path) -> List[Path]:
    pattern = re.compile(r"<<<FILE:\s*(.+?)>>>(.*?)<<<END>>>", re.S | re.I)
    written: List[Path] = []
    for match in pattern.finditer(llm_output):
        rel_path = match.group(1).strip()
        content = match.group(2)
        if not rel_path:
            continue
        normalized = Path(rel_path)
        safe_path = target_root.joinpath(*normalized.parts).resolve()
        if target_root not in safe_path.parents and safe_path != target_root:
            raise ValueError(f"Refusing to write outside target root: {rel_path}")
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(content.strip("\n"), encoding="utf-8")
        written.append(safe_path)
    return written


def process_llm_output(
    llm_output: str,
    skill_name_hint: str = "Skill",
    *,
    target_root: str | Path | None = None,
) -> Mapping[str, Any]:
    """
    Unified LLM output handler for prompt_engineer_skill and legacy skill generation.

    Supports two formats:
    - multi-file markers: <<<FILE: path>>>\n...<<<END>>>
    - legacy skill bundle with --- manifest.yaml --- / --- handler.py ---
    """
    if "<<<FILE:" in llm_output:
        ctx = get_ctx()
        base_dir_attr = getattr(ctx.paths, "base_dir", None)
        default_root = base_dir_attr() if callable(base_dir_attr) else base_dir_attr or Path.cwd()
        root = Path(target_root) if target_root else Path(default_root) / "artifacts" / "llm_drafts"
        root.mkdir(parents=True, exist_ok=True)
        written = _write_multifile_output(llm_output, root.resolve())
        return {"mode": "multifile", "root": str(root), "files": [str(p) for p in written]}

    manifest_match = re.search(r"--- manifest.yaml ---\n(.*?)--- handler.py ---", llm_output, re.S)
    handler_match = re.search(r"--- handler.py ---\n(.*)", llm_output, re.S)
    if not manifest_match or not handler_match:
        raise ValueError("Не удалось распарсить ответ LLM. Проверь формат.")

    manifest_text = manifest_match.group(1).strip()
    handler_text = handler_match.group(1).strip()
    skill_dir = _write_skill_bundle(manifest_text, handler_text, skill_name_hint)
    return {"mode": "skill_bundle", "skill_dir": str(skill_dir)}

