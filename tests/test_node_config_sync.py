from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import adaos.services.node_config as node_config_mod
from adaos.adapters.db.sqlite import durable_state_get
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import NodeConfig, load_config, save_config
from adaos.services.node_runtime_state import load_nats_runtime_config, load_node_runtime_state
from adaos.services.subnet_alias import load_subnet_alias


def _detached_config() -> NodeConfig:
    current = load_config()
    return NodeConfig(
        node_id=current.node_id,
        subnet_id=current.subnet_id,
        role=current.role,
        hub_url=current.hub_url,
        token=current.token,
        root_state=copy.deepcopy(current.root_state),
        root_settings=copy.deepcopy(current.root_settings),
        subnet_settings=copy.deepcopy(current.subnet_settings),
        node_settings=copy.deepcopy(current.node_settings),
        dev_settings=copy.deepcopy(current.dev_settings),
    )


def _write_hub_cert(path: Path, *, common_name: str, organization_name: str | None = None) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject_attrs = [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    if organization_name:
        subject_attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization_name))
    subject = issuer = x509.Name(subject_attrs)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_save_config_syncs_agent_context() -> None:
    ctx = get_ctx()
    detached = _detached_config()
    detached.subnet_id = "sn_saved_sync"
    detached.subnet_settings.id = detached.subnet_id

    save_config(detached)

    assert ctx.config.subnet_id == "sn_saved_sync"
    assert load_config().subnet_id == "sn_saved_sync"


def test_load_config_refreshes_agent_context() -> None:
    ctx = get_ctx()
    load_config()
    path = Path(ctx.paths.base_dir()) / "node.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["subnet_id"] = "sn_loaded_sync"
    data["role"] = "member"
    subnet = data.get("subnet") or {}
    subnet["id"] = "sn_loaded_sync"
    data["subnet"] = subnet
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    fresh = load_config()

    assert fresh.subnet_id == "sn_loaded_sync"
    assert ctx.config.subnet_id == "sn_loaded_sync"
    assert ctx.config.role == "member"


def test_load_config_reuses_cached_node_yaml_without_reparsing(monkeypatch) -> None:
    import adaos.services.node_config as mod

    ctx = get_ctx()
    baseline = mod.load_config()
    path = Path(ctx.paths.base_dir()) / "node.yaml"
    original = path.read_text(encoding="utf-8")
    mod._NODE_CONFIG_CACHE.clear()

    calls = {"count": 0}
    original_safe_load = mod.yaml.safe_load

    def _counting_safe_load(*args, **kwargs):
        calls["count"] += 1
        return original_safe_load(*args, **kwargs)

    monkeypatch.setattr(mod.yaml, "safe_load", _counting_safe_load)

    first = mod.load_config()
    second = mod.load_config()

    assert first.subnet_id == baseline.subnet_id
    assert first.subnet_id == second.subnet_id
    assert calls["count"] == 1
    path.write_text(original, encoding="utf-8")


def test_save_config_stores_managed_key_paths_relative_to_base() -> None:
    ctx = get_ctx()
    detached = _detached_config()
    base_dir = Path(ctx.paths.base_dir())
    keys_dir = base_dir / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)

    detached.root_settings.ca_cert = str(keys_dir / "ca.cert")
    detached.subnet_settings.hub.key = str(keys_dir / "hub_private.pem")
    detached.subnet_settings.hub.cert = str(keys_dir / "hub_cert.pem")

    save_config(detached)

    data = yaml.safe_load((base_dir / "node.yaml").read_text(encoding="utf-8")) or {}
    root = data.get("root") or {}
    subnet = data.get("subnet") or {}
    hub = subnet.get("hub") or {}

    assert root.get("ca_cert") == "keys/ca.cert"
    assert hub.get("key") == "keys/hub_private.pem"
    assert hub.get("cert") == "keys/hub_cert.pem"


def test_save_config_persists_bootstrap_subnet_id_for_canonical_identity() -> None:
    ctx = get_ctx()
    detached = _detached_config()
    detached.subnet_id = "sn_bootstrap01"
    detached.subnet_settings.id = detached.subnet_id
    detached.subnet_settings.bootstrap_id = None

    save_config(detached)

    data = yaml.safe_load((Path(ctx.paths.base_dir()) / "node.yaml").read_text(encoding="utf-8")) or {}
    subnet = data.get("subnet") or {}

    assert subnet.get("id") == "sn_bootstrap01"
    assert subnet.get("bootstrap_id") == "sn_bootstrap01"


def test_load_config_migrates_legacy_key_paths_into_active_base_dir(tmp_path: Path) -> None:
    ctx = get_ctx()
    base_dir = Path(ctx.paths.base_dir())
    legacy_root = tmp_path / "legacy-checkout" / "keys"
    legacy_root.mkdir(parents=True, exist_ok=True)
    legacy_key = legacy_root / "hub_private.pem"
    legacy_cert = legacy_root / "hub_cert.pem"
    legacy_ca = legacy_root / "ca.cert"
    legacy_key.write_text("legacy-key", encoding="utf-8")
    legacy_cert.write_text("legacy-cert", encoding="utf-8")
    legacy_ca.write_text("legacy-ca", encoding="utf-8")

    node_path = base_dir / "node.yaml"
    node_path.write_text(
        yaml.safe_dump(
            {
                "node_id": "node_legacy",
                "subnet_id": "sn_legacy",
                "role": "hub",
                "root": {"ca_cert": str(legacy_ca)},
                "subnet": {
                    "id": "sn_legacy",
                    "hub": {
                        "key": str(legacy_key),
                        "cert": str(legacy_cert),
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    fresh = load_config()
    keys_dir = base_dir / "keys"

    assert fresh.ca_cert_path() == (keys_dir / "ca.cert")
    assert fresh.hub_key_path() == (keys_dir / "hub_private.pem")
    assert fresh.hub_cert_path() == (keys_dir / "hub_cert.pem")
    assert (keys_dir / "ca.cert").read_text(encoding="utf-8") == "legacy-ca"
    assert (keys_dir / "hub_private.pem").read_text(encoding="utf-8") == "legacy-key"
    assert (keys_dir / "hub_cert.pem").read_text(encoding="utf-8") == "legacy-cert"

    migrated = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    assert ((migrated.get("root") or {}).get("ca_cert")) == "keys/ca.cert"
    assert (((migrated.get("subnet") or {}).get("hub") or {}).get("key")) == "keys/hub_private.pem"
    assert (((migrated.get("subnet") or {}).get("hub") or {}).get("cert")) == "keys/hub_cert.pem"


def test_load_config_recovers_uuid_subnet_from_hub_certificate() -> None:
    ctx = get_ctx()
    base_dir = Path(ctx.paths.base_dir())
    node_path = base_dir / "node.yaml"
    cert_path = base_dir / "keys" / "hub_cert.pem"
    uuid_subnet = "9d91f466-0349-475d-9887-2d2bb3c783ee"
    recovered_subnet = "sn_cert1234"

    _write_hub_cert(cert_path, common_name=f"subnet:{recovered_subnet}", organization_name=f"subnet:{recovered_subnet}")
    node_path.write_text(
        yaml.safe_dump(
            {
                "node_id": "node_cert",
                "subnet_id": uuid_subnet,
                "role": "hub",
                "subnet": {
                    "id": uuid_subnet,
                    "hub": {
                        "cert": "keys/hub_cert.pem",
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    node_config_mod._NODE_CONFIG_CACHE.clear()

    fresh = load_config()

    assert fresh.subnet_id == recovered_subnet
    assert fresh.subnet_settings.id == recovered_subnet
    saved = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    assert saved.get("subnet_id") == recovered_subnet
    assert ((saved.get("subnet") or {}).get("id")) == recovered_subnet


def test_load_config_recovers_uuid_subnet_from_nats_user_when_certificate_missing() -> None:
    ctx = get_ctx()
    base_dir = Path(ctx.paths.base_dir())
    node_path = base_dir / "node.yaml"
    cert_path = base_dir / "keys" / "hub_cert.pem"
    if cert_path.exists():
        cert_path.unlink()
    uuid_subnet = "9d91f466-0349-475d-9887-2d2bb3c783ee"
    recovered_subnet = "sn_nats1234"

    node_path.write_text(
        yaml.safe_dump(
            {
                "node_id": "node_nats",
                "subnet_id": uuid_subnet,
                "role": "hub",
                "subnet": {"id": uuid_subnet},
                "nats": {"user": f"hub_{recovered_subnet}"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    node_config_mod._NODE_CONFIG_CACHE.clear()

    fresh = load_config()

    assert fresh.subnet_id == recovered_subnet
    assert fresh.subnet_settings.id == recovered_subnet
    saved = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    assert saved.get("subnet_id") == recovered_subnet


def test_load_config_prefers_bootstrap_subnet_id_over_nats_user_when_explicit_values_drift() -> None:
    ctx = get_ctx()
    base_dir = Path(ctx.paths.base_dir())
    node_path = base_dir / "node.yaml"
    cert_path = base_dir / "keys" / "hub_cert.pem"
    if cert_path.exists():
        cert_path.unlink()
    uuid_subnet = "9d91f466-0349-475d-9887-2d2bb3c783ee"
    bootstrap_subnet = "sn_boot1234"
    nats_subnet = "sn_nats5678"

    node_path.write_text(
        yaml.safe_dump(
            {
                "node_id": "node_bootstrap",
                "subnet_id": uuid_subnet,
                "role": "hub",
                "subnet": {
                    "id": uuid_subnet,
                    "bootstrap_id": bootstrap_subnet,
                },
                "nats": {"user": f"hub_{nats_subnet}"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    node_config_mod._NODE_CONFIG_CACHE.clear()

    fresh = load_config()

    assert fresh.subnet_id == bootstrap_subnet
    assert fresh.subnet_settings.id == bootstrap_subnet
    assert fresh.subnet_settings.bootstrap_id == bootstrap_subnet
    saved = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    assert saved.get("subnet_id") == bootstrap_subnet
    assert ((saved.get("subnet") or {}).get("id")) == bootstrap_subnet
    assert ((saved.get("subnet") or {}).get("bootstrap_id")) == bootstrap_subnet


def test_load_config_generates_sn_prefixed_subnet_id_for_empty_config() -> None:
    ctx = get_ctx()
    base_dir = Path(ctx.paths.base_dir())
    node_path = base_dir / "node.yaml"
    cert_path = base_dir / "keys" / "hub_cert.pem"
    if cert_path.exists():
        cert_path.unlink()

    node_path.write_text("{}", encoding="utf-8")
    node_config_mod._NODE_CONFIG_CACHE.clear()

    fresh = load_config()

    assert fresh.subnet_id.startswith("sn_")
    assert not node_config_mod._looks_like_uuid_token(fresh.subnet_id)


def test_save_config_moves_dynamic_runtime_state_out_of_node_yaml() -> None:
    ctx = get_ctx()
    node_path = Path(ctx.paths.base_dir()) / "node.yaml"
    detached = _detached_config()
    detached.hub_url = "http://127.0.0.1:8778"
    detached.token = "runtime-token"
    detached.root_state = {
        "profile": {
            "owner_id": "owner-1",
            "subject": "owner@example.test",
            "scopes": ["hub.manage"],
            "access_expires_at": datetime.now(timezone.utc).isoformat(),
            "hub_ids": [detached.subnet_id],
        },
        "access_token_cached": "access-1",
        "refresh_token_fallback": "refresh-1",
    }

    save_config(detached)

    saved = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    root = saved.get("root") or {}
    runtime_state = load_node_runtime_state()
    persisted_root = durable_state_get("node_config", "root_state") or {}

    assert "hub_url" not in saved
    assert "token" not in saved
    assert "root_state" not in saved
    assert "profile" not in root
    assert "access_token_cached" not in root
    assert "refresh_token_fallback" not in root
    assert runtime_state.get("hub_url") == "http://127.0.0.1:8778"
    assert runtime_state.get("token") == "runtime-token"
    assert persisted_root["profile"]["owner_id"] == "owner-1"
    assert persisted_root["access_token_cached"] == "access-1"
    assert persisted_root["refresh_token_fallback"] == "refresh-1"


def test_load_config_migrates_legacy_nats_runtime_state_and_alias() -> None:
    ctx = get_ctx()
    node_path = Path(ctx.paths.base_dir()) / "node.yaml"
    node_path.write_text(
        yaml.safe_dump(
            {
                "zone_id": "ru",
                "node_id": "node-runtime",
                "subnet_id": "sn_runtime01",
                "role": "hub",
                "subnet": {"id": "sn_runtime01"},
                "nats": {
                    "ws_url": "wss://ru.api.inimatic.com/nats",
                    "user": "hub_sn_runtime01",
                    "pass": "secret-1",
                    "alias": "office",
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    node_config_mod._NODE_CONFIG_CACHE.clear()

    fresh = load_config()
    saved = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}
    runtime_nats = load_nats_runtime_config()

    assert fresh.subnet_id == "sn_runtime01"
    assert runtime_nats["ws_url"] == "wss://ru.api.inimatic.com/nats"
    assert runtime_nats["user"] == "hub_sn_runtime01"
    assert runtime_nats["pass"] == "secret-1"
    assert load_subnet_alias(subnet_id="sn_runtime01") == "office"
    assert "nats" not in saved


def test_load_config_normalizes_root_base_url_to_zone_effective_host() -> None:
    ctx = get_ctx()
    node_path = Path(ctx.paths.base_dir()) / "node.yaml"
    node_path.write_text(
        yaml.safe_dump(
            {
                "zone_id": "ru",
                "node_id": "node-zone",
                "subnet_id": "sn_zone01",
                "role": "hub",
                "root": {
                    "base_url": "https://api.inimatic.com",
                    "ca_cert": "keys/ca.cert",
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    node_config_mod._NODE_CONFIG_CACHE.clear()

    fresh = load_config()
    saved = yaml.safe_load(node_path.read_text(encoding="utf-8")) or {}

    assert fresh.root_settings.base_url == "https://ru.api.inimatic.com"
    assert ((saved.get("root") or {}).get("base_url")) == "https://ru.api.inimatic.com"
