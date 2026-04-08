from __future__ import annotations

from types import SimpleNamespace

from adaos.services.capacity import install_io_in_capacity
from adaos.services.user import profile as profile_module


class _FakeKV:
    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value: object) -> None:
        self._data[key] = value


def test_user_profile_update_emits_changed_event(monkeypatch) -> None:
    events: list[tuple[str, dict[str, object], str]] = []
    fake_ctx = SimpleNamespace(
        kv=_FakeKV(),
        bus=object(),
        settings=SimpleNamespace(owner_id="owner-1"),
    )
    monkeypatch.setattr(
        profile_module,
        "emit",
        lambda bus, topic, payload, source: events.append((topic, dict(payload), source)),
    )

    service = profile_module.UserProfileService(fake_ctx)
    service.update_profile({"preferred_name": "Ada"})

    assert events == [
        (
            "user.profile.changed",
            {"user_id": "owner-1", "settings": {"preferred_name": "Ada"}},
            "user.profile",
        )
    ]


def test_capacity_mutation_emits_changed_event(monkeypatch, tmp_path) -> None:
    from adaos.services import capacity as capacity_module

    events: list[tuple[str, dict[str, object], str]] = []
    monkeypatch.setattr(
        capacity_module,
        "emit",
        lambda bus, topic, payload, source: events.append((topic, dict(payload), source)),
    )
    monkeypatch.setattr(
        capacity_module,
        "_get_ctx_for_events",
        lambda: SimpleNamespace(bus=object()),
    )

    install_io_in_capacity("camera", ["video"], base_dir=tmp_path)

    assert len(events) == 1
    topic, payload, source = events[0]
    assert topic == "capacity.changed"
    assert source == "capacity"
    assert payload["base_dir"] == str(tmp_path)
    io_types = [item["io_type"] for item in payload["capacity"]["io"]]
    assert "camera" in io_types
