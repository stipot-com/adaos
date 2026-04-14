from adaos.services.registry import subnet_directory as mod


class _FakeRepo:
    def __init__(self) -> None:
        self.nodes = {
            "member-1": {
                "node_id": "member-1",
                "subnet_id": "alpha",
                "roles": ["member"],
                "base_url": "http://member-1.local",
                "node_state": "ready",
                "last_seen": 10.0,
            }
        }
        self.io = {"member-1": [{"io_type": "webrtc_media"}]}
        self.skills = {"member-1": [{"name": "voice_chat"}]}
        self.scenarios = {"member-1": [{"name": "kitchen"}]}
        self.runtime = {
            "member-1": {
                "primary_node_name": "Kitchen Member",
                "node_names": ["Kitchen Member"],
                "ready": True,
                "node_state": "ready",
            }
        }

    def list_nodes(self):
        return [dict(item) for item in self.nodes.values()]

    def get_node(self, node_id: str):
        node = self.nodes.get(node_id)
        return dict(node) if isinstance(node, dict) else None

    def upsert_node(self, node):
        self.nodes[str(node.get("node_id"))] = dict(node)

    def io_for_node(self, node_id: str):
        return list(self.io.get(node_id, []))

    def replace_io_capacity(self, node_id: str, io_list):
        self.io[node_id] = list(io_list or [])

    def skills_for_node(self, node_id: str):
        return list(self.skills.get(node_id, []))

    def replace_skill_capacity(self, node_id: str, skills):
        self.skills[node_id] = list(skills or [])

    def scenarios_for_node(self, node_id: str):
        return list(self.scenarios.get(node_id, []))

    def replace_scenario_capacity(self, node_id: str, scenarios):
        self.scenarios[node_id] = list(scenarios or [])

    def runtime_projection_for_node(self, node_id: str):
        return dict(self.runtime.get(node_id, {}))

    def upsert_runtime_projection(self, node_id: str, payload):
        self.runtime[node_id] = dict(payload or {})


def test_subnet_directory_get_node_returns_live_overlay_capacity_and_runtime_projection(monkeypatch) -> None:
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
    assert node["runtime_projection"]["primary_node_name"] == "Kitchen Member"
    assert node["runtime_projection"]["ready"] is True


def test_subnet_directory_ingest_snapshot_persists_scenarios_and_runtime_projection(monkeypatch) -> None:
    repo = _FakeRepo()
    monkeypatch.setattr(mod, "get_ctx", lambda: type("Ctx", (), {"sql": object()})())
    monkeypatch.setattr(mod, "SubnetRepo", lambda sql: repo)

    directory = mod.SubnetDirectory()
    directory.ingest_snapshot(
        [
            {
                "node_id": "member-2",
                "subnet_id": "alpha",
                "roles": ["member"],
                "hostname": "bedroom",
                "base_url": "http://member-2.local",
                "node_state": "ready",
                "last_seen": 25.0,
                "online": True,
                "capacity": {
                    "io": [{"io_type": "stdout"}],
                    "skills": [{"name": "weather"}],
                    "scenarios": [{"name": "sleep"}],
                },
                "runtime_projection": {
                    "captured_at": 24.0,
                    "primary_node_name": "Bedroom Member",
                    "node_names": ["Bedroom Member"],
                    "ready": True,
                    "node_state": "ready",
                    "snapshot": {
                        "captured_at": 24.0,
                        "node_state": "ready",
                        "update_status": {"state": "succeeded"},
                    },
                },
            }
        ]
    )

    node = directory.get_node("member-2")

    assert node is not None
    assert node["online"] is True
    assert node["capacity"]["scenarios"] == [{"name": "sleep"}]
    assert node["runtime_projection"]["primary_node_name"] == "Bedroom Member"
    assert node["runtime_projection"]["snapshot"]["update_status"]["state"] == "succeeded"
