from __future__ import annotations

from pathlib import Path


def test_checkout_target_version_ignores_non_sha(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    calls: list[list[str]] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(mod, "_run", lambda cmd, cwd=None: calls.append(list(cmd)))

    mod._checkout_target_version(tmp_path, target_rev="main", target_version="2026.3.1")
    assert calls == []


def test_checkout_target_version_checks_out_sha(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    calls: list[list[str]] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "git")

    def _fake_run(cmd, *, cwd=None):
        calls.append(list(cmd))

    monkeypatch.setattr(mod, "_run", _fake_run)

    mod._checkout_target_version(tmp_path, target_rev="main", target_version="a" * 12)
    assert calls == [["git", "checkout", "aaaaaaaaaaaa"]]


def test_checkout_target_version_fetches_then_retries(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    calls: list[list[str]] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "git")

    state = {"attempt": 0}

    def _fake_run(cmd, *, cwd=None):
        calls.append(list(cmd))
        if cmd[:2] == ["git", "checkout"] and state["attempt"] == 0:
            state["attempt"] += 1
            raise RuntimeError("missing commit in shallow clone")

    monkeypatch.setattr(mod, "_run", _fake_run)

    mod._checkout_target_version(tmp_path, target_rev="main", target_version="b" * 12)
    assert calls == [
        ["git", "checkout", "bbbbbbbbbbbb"],
        ["git", "fetch", "--depth", "50", "origin", "main"],
        ["git", "checkout", "bbbbbbbbbbbb"],
    ]

