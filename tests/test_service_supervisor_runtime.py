from __future__ import annotations

import asyncio

from adaos.services.skill import service_supervisor_runtime as runtime_module


class _FakeSupervisor:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None]] = []

    def ensure_discovered(self) -> None:
        return None

    def list(self) -> list[str]:
        return ["service_skill", "voice_service"]

    async def restart(self, name: str) -> None:
        self.events.append(("restart", name))

    async def stop(self, name: str) -> None:
        self.events.append(("stop", name))


def test_service_supervisor_runtime_stops_service_on_skill_deactivated(monkeypatch) -> None:
    fake = _FakeSupervisor()
    monkeypatch.setattr(runtime_module, "get_service_supervisor", lambda: fake)

    asyncio.run(runtime_module._on_skill_deactivated({"name": "service_skill"}))

    assert fake.events == [("stop", "service_skill")]


def test_service_supervisor_runtime_stops_all_services_on_subnet_stopping(monkeypatch) -> None:
    fake = _FakeSupervisor()
    monkeypatch.setattr(runtime_module, "get_service_supervisor", lambda: fake)

    asyncio.run(runtime_module._on_subnet_stopping({"reason": "admin_shutdown"}))

    assert fake.events == [("stop", "service_skill"), ("stop", "voice_service")]
