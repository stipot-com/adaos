from adaos.services.registry import subnet_directory as mod


class _FakeRepo:
    def __init__(self) -> None:
        self._node = {
            "node_id": "member-1",
            "subnet_id": "alpha",
            "roles": ["member"],
            "base_url": "http://member-1.local",
            "node_state": "ready",
            "last_seen": 10.0,
        }

    def list_nodes(self):
        return [dict(self._node)]

    def get_node(self, node_id: str):
        if node_id == self._node["node_id"]:
            return dict(self._node)
        return None

    def io_for_node(self, node_id: str):
        return [{"io_type": "webrtc_media"}] if node_id == self._node["node_id"] else []

    def skills_for_node(self, node_id: str):
        return [{"name": "voice_chat"}] if node_id == self._node["node_id"] else []

    def scenarios_for_node(self, node_id: str):
        return [{"name": "kitchen"}] if node_id == self._node["node_id"] else []


def test_subnet_directory_get_node_returns_live_overlay_and_capacity(monkeypatch) -> None:
    monkeypatch.setattr(mod, "get_ctx", lambda: type("Ctx", (), {"sql": object()})())
    monkeypatch.setattr(mod, "SubnetRepo", lambda sql: _FakeRepo())

    directory = mod.SubnetDirectory()
    directory.live["member-1"] = {"online": True, "last_seen": 11.0}

    node = directory.get_node("member-1")

    assert node is not None
    assert node["node_id"] == "member-1"
    assert node["online"] is True
    assert node["capacity"]["io"] == [{"io_type": "webrtc_media"}]
    assert node["capacity"]["skills"] == [{"name": "voice_chat"}]
    assert node["capacity"]["scenarios"] == [{"name": "kitchen"}]
