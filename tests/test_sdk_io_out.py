from types import SimpleNamespace

from adaos.sdk.io import out
from adaos.services.eventbus import LocalEventBus
from adaos.services.webspace_id import coerce_webspace_id


def test_coerce_webspace_id_unwraps_nested_and_stringified_values() -> None:
    assert coerce_webspace_id({"webspace_id": "default"}, fallback="fallback") == "default"
    assert coerce_webspace_id("{'webspace_id': 'default'}", fallback="fallback") == "default"
    assert coerce_webspace_id([{"workspace_id": "desktop"}], fallback="fallback") == "desktop"
    assert coerce_webspace_id("", fallback="fallback") == "fallback"


def test_stream_publish_normalizes_webspace_meta(monkeypatch) -> None:
    bus = LocalEventBus()
    seen = []
    bus.subscribe("io.out.stream.publish", lambda ev: seen.append(ev))
    monkeypatch.setattr(out, "get_ctx", lambda: SimpleNamespace(bus=bus))

    result = out.stream_publish(
        "infrastate.realtime",
        {"state": "ok"},
        _meta={
            "webspace_id": "{'webspace_id': 'default'}",
            "webspace_ids": [{"webspace_id": "default"}, {"workspace_id": "desktop"}],
        },
    )

    assert result == {"ok": True}
    assert len(seen) == 1
    meta = seen[0].payload["_meta"]
    assert meta["webspace_id"] == "default"
    assert meta["webspace_ids"] == ["default", "desktop"]
