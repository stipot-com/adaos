#!/usr/bin/env python3
# Filter code_map.yaml by tags / globs; writes artifacts/code_map.filtered.yaml
# python artifacts/filter_code_map.py --tag uc:device_login
import sys, fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
SRC_MAP = ART / "code_map.yaml"


def load_yaml(path: Path):
    try:
        import yaml  # type: ignore

        return yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except Exception:
        # ultra-simple parser for a list of flat dicts (best-effort)
        raise RuntimeError("PyYAML not installed; please `pip install pyyaml`.")


def save_yaml(path: Path, data):
    try:
        import yaml  # type: ignore

        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    except Exception as e:
        raise


def match_any(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs or [])


def main():
    if not SRC_MAP.exists():
        print("code_map.yaml not found. Run repo_skim.py first.")
        sys.exit(2)

    data = load_yaml(SRC_MAP) or []
    tags = set()
    globs = []
    force_include = False

    it = iter(sys.argv[1:])
    for a in it:
        if a == "--tag":
            tags.add(next(it))
        elif a == "--include":
            globs.append(next(it))
            force_include = True

    sel = []
    for rec in data:
        p = rec.get("path", "")
        # базовый игнор OVOS, если нет принудительного include/тегов
        # OVOS integration removed; no special-case filtering needed

        if globs and not match_any(p, globs):
            continue
        if tags:
            rtags = set(rec.get("tags", []))
            if not (rtags & tags):
                continue
        sel.append(rec)

    out = ART / "code_map.filtered.yaml"
    save_yaml(out, sel)
    print(f"Selected {len(sel)} files → {out}")


if __name__ == "__main__":
    main()
