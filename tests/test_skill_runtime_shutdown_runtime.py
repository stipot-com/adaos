from __future__ import annotations

from adaos.services.skill import runtime_shutdown_runtime as runtime_module


class _FakeManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def shutdown_active_runtimes(self, *, reason: str, event_type: str, hooks):
        payload = {
            "reason": reason,
            "event_type": event_type,
            "hooks": list(hooks),
        }
        self.calls.append(payload)
        return {
            "ok": True,
            "failed_total": 0,
            "active_total": 1,
            "skills": [],
            **payload,
        }


def test_runtime_shutdown_runtime_maps_draining_to_drain_hook(monkeypatch) -> None:
    fake = _FakeManager()
    monkeypatch.setattr(runtime_module, "_manager", lambda: fake)

    runtime_module._on_subnet_draining({"reason": "core_update_prepare"})

    assert fake.calls == [
        {
            "reason": "core_update_prepare",
            "event_type": "subnet.draining",
            "hooks": ["drain"],
        }
    ]


def test_runtime_shutdown_runtime_maps_stopping_to_dispose_chain(monkeypatch) -> None:
    fake = _FakeManager()
    monkeypatch.setattr(runtime_module, "_manager", lambda: fake)

    runtime_module._on_subnet_stopping({"reason": "admin_shutdown"})

    assert fake.calls == [
        {
            "reason": "admin_shutdown",
            "event_type": "subnet.stopping",
            "hooks": ["dispose", "before_deactivate"],
        }
    ]
