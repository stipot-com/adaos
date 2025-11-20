#!/usr/bin/env python3
# python repo_skim.py  --mode lite
# Compact repo skimmer → artifacts/code_map.yaml
# Focus: ONLY src/adaos/**; YAML output; TS endpoints/types/interfaces/exports
# Omits empty keys, ignores empty __init__.py, sorted by path.
# - YAML output (no external deps required; uses PyYAML if available)
# - No "lang" key
# - No "decorators"
# - Class methods collapsed into single string under "methds"

from __future__ import annotations
import ast, os, re, sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(os.getcwd())
ART = ROOT / "artifacts"
ART.mkdir(parents=True, exist_ok=True)

SCAN_ROOT = ROOT / "src" / "adaos"  # (1) only this subtree

# Folders/files to *skip entirely*
EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "node_modules",
    "dist",
    "build",
    ".next",
    "coverage",
}
# Extra ignores for inimatic (8)
INIMATIC_EXTRA_IGNORES = {
    "src/types.d.ts",
    "src/zone-flags.ts",
    "src/polyfills.ts",
    "src/test.ts",
    "src/environments/environment.ts",
    "src/environments/environment.prod.ts",
    "src/main.ts",
}

# File extensions we care about
PY_EXT = ".py"
TS_EXTS = {".ts", ".tsx", ".js", ".jsx"}  # TS/JS family (we still parse JS with same regexes)


# ---------- helpers ----------
def under_scan_root(p: Path) -> bool:
    try:
        p.relative_to(SCAN_ROOT)
        return True
    except ValueError:
        return False


def is_excluded_dir(p: Path) -> bool:
    return any(part in EXCLUDE_DIRS for part in p.parts)


def is_empty_init_py(p: Path) -> bool:
    # (7) ignore empty __init__.py (only whitespace/comments or pass)
    if p.name != "__init__.py":
        return False
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return False
    if not txt:
        return True
    # trivial content?
    # only comments / pass / __all__ definition without names
    body = "\n".join(line for line in txt.splitlines() if not line.strip().startswith("#")).strip()
    if body in ("", "pass"):
        return True
    # very small __all__ without symbols
    if re.fullmatch(r"__all__\s*=\s*\[\s*\]", body):
        return True
    return False


def dump_yaml(data: Any) -> str:
    try:
        import yaml  # type: ignore

        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    except Exception:
        # minimal emitter for list[dict]
        def emit(v, indent=0):
            sp = "  " * indent
            if isinstance(v, list):
                return "\n".join([f"{sp}- {emit(x, indent+1).strip()}" if not isinstance(x, (list, dict)) else f"{sp}-\n{emit(x, indent+1)}" for x in v])
            if isinstance(v, dict):
                lines = []
                for k, val in v.items():
                    if val in (None, "", [], {}):
                        continue
                    if isinstance(val, (list, dict)):
                        lines.append(f"{sp}{k}:")
                        lines.append(emit(val, indent + 1))
                    else:
                        lines.append(f"{sp}{k}: {val}")
                return "\n".join(lines)
            return str(v)

        return emit(data)


def csv_or_none(items: List[str]) -> str | None:
    items = [s for s in (i.strip() for i in items) if s]
    return ", ".join(items) if items else None


# ---------- Python skim ----------
def skim_python(p: Path) -> Dict[str, Any]:
    if is_empty_init_py(p):
        return {"__skip__": True}
    out: Dict[str, Any] = {}
    try:
        src = p.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src, filename=str(p))
    except Exception as e:
        return {"parse_error": str(e)}

    funcs: List[str] = []
    classes: List[Dict[str, Any]] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                funcs.append(node.name)
        elif isinstance(node, ast.ClassDef):
            cls_name = node.name
            # collect methods, keep dunders like __init__, skip _private
            meths = []
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if b.name.startswith("_") and not b.name.startswith("__"):
                        continue
                    meths.append(b.name)
            entry = {"Name": cls_name}
            m = csv_or_none(meths)
            if m:
                entry["methds"] = m
            classes.append(entry)

    f = csv_or_none(funcs)
    if f:
        out["funcs"] = f
    if classes:
        out["classes"] = classes
    return out


# ---------- TS/JS skim ----------
# (6,9) endpoints: support both `router.get('/x')` and `router.route('/x').get(...)`
RE_ENDPOINT_DIRECT = re.compile(
    r"""(?P<obj>\b(?:[A-Za-z_$][A-Za-z0-9_$]*Router|router|rootRouter|app)\b)\s*
        \.\s*(?P<method>get|post|put|delete|patch|options|head)\s*
        \(\s*['"](?P<path>/[^'"]*)['"]""",
    re.IGNORECASE | re.VERBOSE,
)

RE_ENDPOINT_CHAINED = re.compile(
    r"""(?P<obj>\b(?:[A-Za-z_$][A-Za-z0-9_$]*Router|router|rootRouter|app)\b)\s*
        \.\s*route\s*\(\s*['"](?P<path>/[^'"]*)['"]\s*\)\s*
        \.\s*(?P<method>get|post|put|delete|patch|options|head)\s*\(""",
    re.IGNORECASE | re.VERBOSE,
)

# (10) export type/interface
RE_EXPORT_TYPE = re.compile(r"(?m)^\s*export\s+type\s+([A-Za-z0-9_]+)\b")
RE_EXPORT_INTERFACE = re.compile(r"(?m)^\s*export\s+interface\s+([A-Za-z0-9_]+)\b")
# (11) export const ... = { ... }  (collect name)
RE_EXPORT_CONST_OBJ = re.compile(r"(?m)^\s*export\s+const\s+([A-Za-z0-9_]+)\s*:\s*[A-Za-z0-9_<>,\s\[\]\|&?]*=\s*\{")
RE_EXPORT_CONST_SIMPLE = re.compile(r"(?m)^\s*export\s+const\s+([A-Za-z0-9_]+)\s*=")


def skim_ts_js(p: Path) -> Dict[str, Any]:
    rel = str(p.relative_to(ROOT)).replace("\\", "/")
    if is_inimatic_ignored(rel):
        return {"__skip__": True}

    out: Dict[str, Any] = {}
    try:
        src = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"parse_error": str(e)}

    # endpoints
    endpoints = []
    for m in RE_ENDPOINT_DIRECT.finditer(src):
        endpoints.append(f"{m.group('method').upper()} {m.group('path')}")
    for m in RE_ENDPOINT_CHAINED.finditer(src):
        endpoints.append(f"{m.group('method').upper()} {m.group('path')}")
    if endpoints:
        out["endpoints"] = ", ".join(sorted(set(endpoints)))

    # types / interfaces
    types = set(RE_EXPORT_TYPE.findall(src))
    interfaces = set(RE_EXPORT_INTERFACE.findall(src))
    if types:
        out["types"] = ", ".join(sorted(types))
    if interfaces:
        out["interfaces"] = ", ".join(sorted(interfaces))

    # exports (const)
    consts = set(RE_EXPORT_CONST_OBJ.findall(src)) | set(RE_EXPORT_CONST_SIMPLE.findall(src))
    if consts:
        out["exports"] = ", ".join(sorted(consts))

    return out


def is_inimatic_ignored(rel: str) -> bool:
    if "src/adaos/integrations/inimatic/" not in rel:
        return False
    for ig in INIMATIC_EXTRA_IGNORES:
        if rel.endswith(ig):
            return True
    return False


# ---------- tags (optional; YAML rules) ----------
def load_tag_rules() -> List[Dict[str, Any]]:
    y = ART / "tags.yml"
    if not y.exists():
        return []
    try:
        import yaml  # type: ignore

        obj = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
        return obj.get("rules", [])
    except Exception:
        return []


def apply_tags(rel: str, rules: List[Dict[str, Any]]) -> List[str]:
    import fnmatch

    tags = []
    for r in rules:
        pat = r.get("match")
        if pat and fnmatch.fnmatch(rel, pat):
            tags += r.get("add", [])
    # inline @tags near file head
    try:
        head = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")[:400]
        m = re.search(r"@tags\s+([^\n]+)", head)
        if m:
            tags += m.group(1).split()
    except Exception:
        pass
    tags = sorted(set(tags))
    return tags


# ---------- MD summary ----------
def write_md(entries: List[Dict[str, Any]]):
    base = ROOT / "src" / "adaos"
    lines = ["# Code Map"]
    for rec in entries:
        full = ROOT / rec["path"]
        try:
            rel_from_base = full.relative_to(base).as_posix()
        except Exception:
            # если файл вне src/adaos — пропускаем (но мы и так не сканируем вне)
            continue

        # summary: берем самое информативное
        summary_parts = []
        if "classes" in rec:
            summary_parts.append("classes: " + ", ".join(c["Name"] for c in rec["classes"]))
        elif "funcs" in rec:
            summary_parts.append("funcs: " + rec["funcs"])
        elif "endpoints" in rec:
            summary_parts.append("endpoints: " + rec["endpoints"])
        elif "exports" in rec:
            summary_parts.append("exports: " + rec["exports"])
        elif "types" in rec or "interfaces" in rec:
            t = rec.get("types")
            i = rec.get("interfaces")
            if t and i:
                summary_parts.append(f"types: {t}; interfaces: {i}")
            elif t:
                summary_parts.append(f"types: {t}")
            else:
                summary_parts.append(f"interfaces: {i}")

        summary = summary_parts[0] if summary_parts else rel_from_base
        lines.append(f"[{summary}]({rel_from_base})")

    # кладем в src/adaos
    (base / "code_map.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    tag_rules = load_tag_rules()
    entries: List[Dict[str, Any]] = []

    # Walk only src/adaos/**
    for p in SCAN_ROOT.rglob("*"):
        if not p.is_file():
            continue
        if is_excluded_dir(p):
            continue
        # respect only python + ts/js family
        ext = p.suffix.lower()
        if ext not in (PY_EXT, *TS_EXTS):
            continue

        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        # per-file skim
        if ext == PY_EXT:
            info = skim_python(p)
        else:
            info = skim_ts_js(p)

        if info.get("__skip__"):
            continue

        rec: Dict[str, Any] = {"path": rel}
        # attach tags if any
        tags = apply_tags(rel, tag_rules)
        if tags:
            rec["tags"] = tags
        # merge info
        for k, v in info.items():
            if k == "__skip__":
                continue
            if v in (None, "", [], {}):
                continue
            rec[k] = v

        entries.append(rec)

    # sort by path (12)
    entries.sort(key=lambda r: r["path"])

    # YAML out
    (ART / "code_map.yaml").write_text(dump_yaml(entries) + "\n", encoding="utf-8")
    write_md(entries)
    print(f"Wrote {ART/'code_map.yaml'} and {ART/'code_map.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
