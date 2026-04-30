from __future__ import annotations

from pathlib import Path

from adaos.services.yjs import load_mark_history as history_mod


def test_load_mark_history_appends_and_filters(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "yjs_load_mark.jsonl"
    monkeypatch.setattr(history_mod, "_history_path", lambda: path)

    history_mod.append_history_snapshot(
        webspace_id="desktop",
        ts=100.0,
        rows=[
            {
                "kind": "owner",
                "id": "_by_owner/skill_infrastate_skill",
                "display": "_by_owner/skill_infrastate_skill",
                "status": "critical",
                "avg_bps": 123.0,
                "peak_bps": 456.0,
                "avg_wps": 1.5,
                "peak_wps": 3.0,
                "recent_bytes": 1000,
                "recent_writes": 5,
                "lifetime_bytes": 2000,
                "sample_total": 7,
                "write_total": 7,
                "current_size_bytes": 2222,
                "last_source": "async_get_ydoc",
                "last_changed_at": 100.0,
                "last_changed_ago_s": 0.0,
            },
            {
                "kind": "root",
                "id": "data",
                "display": "data",
                "status": "nominal",
                "avg_bps": 50.0,
                "peak_bps": 70.0,
                "avg_wps": 0.5,
                "peak_wps": 1.0,
                "recent_bytes": 300,
                "recent_writes": 2,
                "lifetime_bytes": 500,
                "sample_total": 3,
                "write_total": 3,
                "current_size_bytes": 600,
                "last_source": "ystore_write",
                "last_changed_at": 100.0,
                "last_changed_ago_s": 0.0,
            },
        ],
    )

    all_rows = history_mod.list_history_rows(limit=10)
    owner_rows = history_mod.list_history_rows(limit=10, kind="owner")
    filtered_rows = history_mod.list_history_rows(
        limit=10,
        webspace_id="desktop",
        bucket_id="_by_owner/skill_infrastate_skill",
        status="critical",
        last_source="async_get_ydoc",
        since_ts=99.0,
        until_ts=101.0,
    )

    assert all_rows["path"] == str(path)
    assert all_rows["count"] == 2
    assert owner_rows["count"] == 1
    assert owner_rows["items"][0]["bucket_id"] == "_by_owner/skill_infrastate_skill"
    assert filtered_rows["count"] == 1
    assert filtered_rows["items"][0]["avg_bps"] == 123.0


def test_load_mark_history_skips_when_file_exceeds_cap(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "yjs_load_mark.jsonl"
    path.write_text("x" * 64, encoding="utf-8")
    monkeypatch.setattr(history_mod, "_history_path", lambda: path)
    monkeypatch.setattr(history_mod, "_MAX_HISTORY_BYTES", 32)

    history_mod.append_history_snapshot(
        webspace_id="desktop",
        ts=100.0,
        rows=[
            {
                "kind": "owner",
                "id": "_by_owner/skill_infrastate_skill",
                "status": "critical",
            }
        ],
    )

    assert path.read_text(encoding="utf-8") == "x" * 64
