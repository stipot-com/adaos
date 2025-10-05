from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence
import json
import logging
import os
import time
import uuid

import typer

from adaos.services.crypto.pki import generate_rsa_key, make_csr, write_pem, write_private_key
from adaos.services.node_config import ensure_hub, load_node, node_base_dir, save_node
from adaos.services.root import (
    ClientContext,
    ConsentStatus,
    ConsentType,
    DeviceRole,
    RootAuthorityBackend,
    RootBackendError,
    Scope,
)
from adaos.services.browser.flows import sign_assertion, thumbprint_for_key
from adaos.services.root.cli_state import RootCliState, load_cli_state, save_cli_state, state_path
from adaos.services.root.client import RootHttpClient, RootHttpError
from adaos.services.root.keyring import KeyringUnavailableError, load_refresh
from adaos.services.root.service import OwnerHubsService, PkiService, RootAuthError, RootAuthService
from adaos.services.hub.agent import HubAgent
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

app = typer.Typer(help="Developer utilities for Root integration")
root_app = typer.Typer(help="Root owner operations")
hub_app = typer.Typer(help="Subnet bootstrap commands")
node_app = typer.Typer(help="Node registration commands")
root_consents_app = typer.Typer(help="Manage pending Root consents")
root_devices_app = typer.Typer(help="Manage devices within the owner subnet")
root_devices_alias_app = typer.Typer(help="Alias helpers")
app.add_typer(root_app, name="root")
app.add_typer(hub_app, name="hub")
app.add_typer(node_app, name="node")
root_app.add_typer(root_consents_app, name="consents")
root_app.add_typer(root_devices_app, name="devices")
root_devices_app.add_typer(root_devices_alias_app, name="alias")


def _http_client() -> RootHttpClient:
    return RootHttpClient()


def _debug_enabled() -> bool:
    for name in ("DEBUG", "ADAOS_CLI_DEBUG"):
        value = os.getenv(name, "").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
    return False


def _pki_paths() -> dict[str, Path]:
    base = node_base_dir()
    pki_dir = base / "pki"
    return {
        "dir": pki_dir,
        "hub_key": pki_dir / "hub.key",
        "hub_cert": pki_dir / "hub.crt",
        "chain": pki_dir / "chain.crt",
        "node_key": pki_dir / "node.key",
        "node_cert": pki_dir / "node.crt",
    }


def _print_error(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED)


def _handle_root_error(exc: Exception) -> None:
    logger = logging.getLogger(__name__)
    if isinstance(exc, RootHttpError):
        status_info = []
        if exc.status_code:
            status_info.append(f"status={exc.status_code}")
        if exc.error_code:
            status_info.append(f"code={exc.error_code}")
        status_display = f" ({', '.join(status_info)})" if status_info else ""
        _print_error(f"Root API error{status_display}: {exc}")
        logger.error(
            "root_api_error",
            extra={
                "extra": {
                    "status": exc.status_code,
                    "code": exc.error_code,
                    "message": str(exc),
                }
            },
        )
        debug_enabled = _debug_enabled()
        if exc.payload:
            if debug_enabled:
                pretty = json.dumps(exc.payload, ensure_ascii=False, indent=2)
                typer.echo("Server response payload:")
                typer.echo(pretty)
            else:
                typer.echo("Set DEBUG=1 or ADAOS_CLI_DEBUG=1 to print server payload details.")
        log_path = Path.home() / ".adaos" / "logs" / "adaos.log"
        typer.echo(f"Detailed logs: {log_path}")
        message_lower = str(exc).lower()
        if "certificate" in message_lower:
            typer.echo(
                "Verify backend TLS configuration. Run 'src/adaos/integrations/inimatic/backend/ssl/check_certs.sh' "
                "to inspect required certificate links."
            )
    elif isinstance(exc, RootAuthError):
        _print_error(str(exc))
    else:
        _print_error(str(exc))


def _handle_backend_failure(exc: RootBackendError) -> None:
    envelope = exc.envelope
    hint = f" ({envelope.hint})" if envelope.hint else ""
    _print_error(f"{envelope.code}: {envelope.message}{hint}")


def _cli_home() -> Path:
    override = os.getenv("ADAOS_BASE_DIR")
    if override:
        return Path(override).expanduser()
    return node_base_dir()


def _cli_base_dir() -> Path:
    return _cli_home() / "root-cli"


def _load_cli_state() -> RootCliState | None:
    return load_cli_state(_cli_home())


def _save_cli_state(state: RootCliState) -> Path:
    base = _cli_home()
    return save_cli_state(base, state)


def _require_cli_state() -> RootCliState:
    state = _load_cli_state()
    if state is None:
        raise typer.Exit("Root CLI is not installed; run 'adaos dev root install'.")
    return state


def _scopes_from_strings(values: Iterable[str]) -> list[Scope]:
    scopes: list[Scope] = []
    for raw in values:
        try:
            scopes.append(Scope(raw))
        except ValueError as exc:
            raise typer.BadParameter(f"unknown scope: {raw}") from exc
    return scopes or [Scope.MANAGE_IO_DEVICES]


def _parse_consent_type(value: str | None) -> ConsentType | None:
    if value is None:
        return None
    try:
        return ConsentType(value)
    except ValueError as exc:
        raise typer.BadParameter(f"unknown consent type: {value}") from exc


def _parse_device_role(value: str | None) -> DeviceRole | None:
    if value is None:
        return None
    cleaned = value.strip().upper()
    try:
        return DeviceRole[cleaned]
    except KeyError:
        try:
            return DeviceRole(cleaned)
        except ValueError as exc:
            raise typer.BadParameter(f"unknown role: {value}") from exc


def _client_context() -> ClientContext:
    origin = os.getenv("ADAOS_ROOT_ORIGIN", "https://app.inimatic.com")
    ip = os.getenv("ADAOS_ROOT_CLIENT_IP", "127.0.0.1")
    ua = os.getenv("ADAOS_ROOT_UA", "adaos-cli/1.0")
    return ClientContext(origin=origin, client_ip=ip, user_agent=ua)


def _idem(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


@contextmanager
def _root_backend() -> Iterable[RootAuthorityBackend]:
    base_dir = node_base_dir()
    db_path = Path(os.getenv("ADAOS_ROOT_DB_PATH", base_dir / "root.sqlite"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config_env = os.getenv("ADAOS_ROOT_CONFIG_PATH")
    config_path = (
        Path(config_env)
        if config_env
        else Path(__file__).resolve().parents[3] / "services" / "root" / "config.yaml"
    )
    backend = RootAuthorityBackend(db_path=db_path, config_path=config_path)
    try:
        yield backend
    finally:
        backend.close()


def _sign_channel_assertion(private_key: ec.EllipticCurvePrivateKey, thumbprint: str, *, nonce: str, audience: str) -> str:
    payload = {
        "nonce": nonce,
        "aud": audience,
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=1)).timestamp()),
    }
    header = {"alg": "ES256", "kid": thumbprint}
    return sign_assertion(private_key, header, payload)


@root_app.command("login")
def root_login() -> None:
    cfg = load_node()
    try:
        ensure_hub(cfg)
    except ValueError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)

    client = _http_client()
    auth = RootAuthService(client)

    def _on_start(info: dict) -> None:
        verify_uri = info.get("verify_uri", "https://api.inimatic.com/device")
        user_code = info.get("user_code", "<unknown>")
        typer.echo(f"Open {verify_uri} and enter code: {user_code}")

    try:
        profile = auth.login_owner(cfg, on_start=_on_start)
    except Exception as exc:  # noqa: BLE001
        _handle_root_error(exc)
        raise typer.Exit(1)
    finally:
        client.close()

    typer.secho("Owner authorization complete.", fg=typer.colors.GREEN)
    typer.echo(f"Owner ID: {profile['owner_id']}")
    typer.echo(f"Subject: {profile['subject'] or 'n/a'}")
    typer.echo(f"Scopes: {', '.join(profile['scopes']) or 'none'}")
    typer.echo(f"Access expires at: {profile['access_expires_at']}")


@root_app.command("status")
def root_status() -> None:
    cfg = load_node()
    try:
        ensure_hub(cfg)
    except ValueError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)

    state = cfg.root or {}
    profile = state.get("profile")
    if not profile:
        typer.echo("Not logged in to root. Run 'adaos dev root login'.")
        return

    typer.echo(f"Owner ID: {profile['owner_id']}")
    typer.echo(f"Subject: {profile['subject'] or 'n/a'}")
    typer.echo(f"Scopes: {', '.join(profile['scopes']) or 'none'}")
    typer.echo(f"Access expires at: {profile['access_expires_at']}")
    hubs = profile.get("hub_ids") or []
    if hubs:
        typer.echo(f"Registered hubs: {', '.join(sorted(set(hubs)))}")
    else:
        typer.echo("Registered hubs: none")

    refresh_in_keyring = False
    try:
        refresh_in_keyring = load_refresh(profile["owner_id"]) is not None
    except KeyringUnavailableError:
        typer.secho("Keyring unavailable; using fallback storage in node.yaml", fg=typer.colors.YELLOW)
    else:
        typer.echo("Refresh token stored in keyring" if refresh_in_keyring else "Refresh token missing from keyring")

    if state.get("refresh_token_fallback"):
        typer.echo("Fallback refresh token stored in node.yaml (consider securing keyring)")


@root_app.command("whoami")
def root_whoami() -> None:
    cfg = load_node()
    try:
        ensure_hub(cfg)
    except ValueError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)

    client = _http_client()
    auth = RootAuthService(client)
    try:
        info = auth.whoami(cfg)
    except Exception as exc:  # noqa: BLE001
        _handle_root_error(exc)
        raise typer.Exit(1)
    finally:
        client.close()

    typer.echo(typer.style("Current identity:", fg=typer.colors.GREEN))
    for key, value in info.items():
        typer.echo(f"- {key}: {value}")


@root_app.command("install")
def root_install(
    scope: list[str] = typer.Option([Scope.MANAGE_IO_DEVICES.value], "--scope", help="Scopes to request for the CLI device."),
    wait_seconds: int = typer.Option(300, "--wait", help="Maximum seconds to wait for owner approval."),
) -> None:
    ctx = _client_context()
    cfg = load_node()
    ensure_hub(cfg)
    requested_scopes = _scopes_from_strings(scope)
    state_path_hint = state_path(_cli_home())
    if _load_cli_state():
        typer.echo(f"Root CLI already installed (state: {state_path_hint}).")
        return

    private_key = ec.generate_private_key(ec.SECP256R1())
    thumbprint, public_pem = thumbprint_for_key(private_key)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")

    with _root_backend() as backend:
        start = backend.device_start(
            subnet_id=cfg.subnet_id,
            role=DeviceRole.OWNER,
            scopes=requested_scopes,
            context=ctx,
            jwk_thumb=thumbprint,
            public_key_pem=public_pem,
            idempotency_key=_idem("cli-start"),
        )

        user_code = start["user_code"]
        device_code = start["device_code"]
        typer.echo(f"Device pairing started. User code: {user_code}")
        typer.echo("Approve the request in the owner console to continue.")

        deadline = time.monotonic() + max(wait_seconds, 30)
        token_key = _idem("cli-token")
        tokens: dict[str, str] | None = None
        while time.monotonic() < deadline:
            payload = {
                "nonce": device_code,
                "aud": f"subnet:{cfg.subnet_id}",
                "exp": int(datetime.now(timezone.utc).timestamp()) + 90,
                "cnf": {"jkt": thumbprint},
            }
            assertion = sign_assertion(private_key, {"alg": "ES256", "kid": thumbprint}, payload)
            try:
                tokens = backend.device_token(
                    device_code=device_code,
                    assertion=assertion,
                    idempotency_key=token_key,
                )
                break
            except RootBackendError as exc:  # noqa: PERF203 - explicit loop handling
                code = exc.envelope.code
                if code in {"authorization_pending", "slow_down"}:
                    time.sleep(3)
                    continue
                if code in {"expired_device_code", "access_denied"}:
                    _print_error(exc.envelope.message)
                    raise typer.Exit(1) from exc
                raise

        if tokens is None:
            raise typer.Exit("Timed out waiting for owner approval.")

    state = RootCliState(
        device_id=tokens["device_id"],
        subnet_id=tokens["subnet_id"],
        thumbprint=thumbprint,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        created_at=tokens.get("server_time_utc", datetime.now(timezone.utc).isoformat()),
    )
    saved = _save_cli_state(state)
    typer.secho("Root CLI device registered.", fg=typer.colors.GREEN)
    typer.echo(f"Device ID: {state.device_id}")
    typer.echo(f"State stored at: {saved}")


@root_consents_app.command("list")
def root_consents_list(
    status: ConsentStatus = typer.Option(ConsentStatus.PENDING, "--status", case_sensitive=False),
    consent_type: str | None = typer.Option(None, "--type", help="Filter by consent type (member|device)"),
) -> None:
    state = _require_cli_state()
    with _root_backend() as backend:
        try:
            response = backend.list_consents(
                owner_device_id=state.device_id,
                status=status,
                consent_type=_parse_consent_type(consent_type),
            )
        except RootBackendError as exc:
            _handle_backend_failure(exc)
            raise typer.Exit(1) from exc
    consents = response.get("consents", [])
    if not consents:
        typer.echo("No matching consents.")
        return
    typer.echo("Pending consents:")
    for item in consents:
        scopes = ",".join(item.get("scopes", []))
        typer.echo(
            f"- {item.get('consent_id')} [{item.get('type')}] requester={item.get('requester_id')} scopes={scopes}"
        )


@root_consents_app.command("approve")
def root_consents_approve(consent_id: str) -> None:
    state = _require_cli_state()
    with _root_backend() as backend:
        try:
            backend.resolve_consent(
                owner_device_id=state.device_id,
                consent_id=consent_id,
                approve=True,
                idempotency_key=_idem(f"consent-approve-{consent_id}"),
            )
        except RootBackendError as exc:
            _handle_backend_failure(exc)
            raise typer.Exit(1) from exc
    typer.secho(f"Consent {consent_id} approved.", fg=typer.colors.GREEN)


@root_consents_app.command("deny")
def root_consents_deny(consent_id: str) -> None:
    state = _require_cli_state()
    with _root_backend() as backend:
        try:
            backend.resolve_consent(
                owner_device_id=state.device_id,
                consent_id=consent_id,
                approve=False,
                idempotency_key=_idem(f"consent-deny-{consent_id}"),
            )
        except RootBackendError as exc:
            _handle_backend_failure(exc)
            raise typer.Exit(1) from exc
    typer.secho(f"Consent {consent_id} denied.", fg=typer.colors.YELLOW)


@root_devices_app.command("list")
def root_devices_list(role: str | None = typer.Option(None, "--role", help="Optional role filter")) -> None:
    state = _require_cli_state()
    device_role = _parse_device_role(role)
    with _root_backend() as backend:
        try:
            response = backend.list_devices(actor_device_id=state.device_id, role=device_role)
        except RootBackendError as exc:
            _handle_backend_failure(exc)
            raise typer.Exit(1) from exc
    devices = response.get("devices", [])
    if not devices:
        typer.echo("No devices found for subnet.")
        return
    for item in devices:
        aliases = ",".join(item.get("aliases", [])) or "<none>"
        typer.echo(
            f"- {item.get('device_id')} [{item.get('role')}] aliases={aliases} scopes={','.join(item.get('scopes', []))}"
        )


@root_devices_app.command("revoke")
def root_devices_revoke(
    device_id: str,
    reason: str | None = typer.Option(None, "--reason", help="Optional justification shown in audit logs"),
) -> None:
    state = _require_cli_state()
    with _root_backend() as backend:
        try:
            backend.revoke_device(
                actor_device_id=state.device_id,
                device_id=device_id,
                reason=reason,
                idempotency_key=_idem(f"device-revoke-{device_id}"),
            )
        except RootBackendError as exc:
            _handle_backend_failure(exc)
            raise typer.Exit(1) from exc
    typer.secho(f"Device {device_id} revoked.", fg=typer.colors.YELLOW)


@root_devices_alias_app.command("set")
def root_devices_alias_set(device_id: str, alias: str) -> None:
    state = _require_cli_state()
    with _root_backend() as backend:
        try:
            listing = backend.list_devices(actor_device_id=state.device_id)
            devices = listing.get("devices", [])
            current_aliases: Sequence[str] = []
            for item in devices:
                if item.get("device_id") == device_id:
                    current_aliases = item.get("aliases", []) or []
                    break
            aliases = sorted({*current_aliases, alias})
            response = backend.update_device(
                actor_device_id=state.device_id,
                device_id=device_id,
                aliases=aliases,
                capabilities=None,
                idempotency_key=_idem(f"alias-set-{device_id}"),
            )
        except RootBackendError as exc:
            _handle_backend_failure(exc)
            raise typer.Exit(1) from exc
    typer.secho(
        f"Aliases for {device_id}: {', '.join(response.get('aliases', [])) or '<none>'}",
        fg=typer.colors.GREEN,
    )


@root_app.command("logout")
def root_logout() -> None:
    cfg = load_node()
    client = _http_client()
    auth = RootAuthService(client)
    try:
        auth.logout(cfg)
    except Exception as exc:  # noqa: BLE001
        _handle_root_error(exc)
        raise typer.Exit(1)
    finally:
        client.close()

    typer.secho("Root session removed.", fg=typer.colors.GREEN)


@root_app.command("sync-hubs")
def root_sync_hubs() -> None:
    cfg = load_node()
    client = _http_client()
    auth = RootAuthService(client)
    hubs_service = OwnerHubsService(client, auth)
    try:
        hub_ids = hubs_service.sync(cfg)
    except Exception as exc:  # noqa: BLE001
        _handle_root_error(exc)
        raise typer.Exit(1)
    finally:
        client.close()

    typer.echo("Registered hubs:")
    for hid in hub_ids:
        typer.echo(f"- {hid}")


@root_app.command("add-hub")
def root_add_hub(hub_id: Optional[str] = typer.Option(None, help="Override hub identifier")) -> None:
    cfg = load_node()
    client = _http_client()
    auth = RootAuthService(client)
    hubs_service = OwnerHubsService(client, auth)
    try:
        hubs_service.add_current_hub(cfg, hub_id=hub_id)
    except Exception as exc:  # noqa: BLE001
        _handle_root_error(exc)
        raise typer.Exit(1)
    finally:
        client.close()

    typer.secho("Hub registered with root.", fg=typer.colors.GREEN)


@hub_app.command("bootstrap")
def hub_bootstrap(
    display_name: str = typer.Option("AdaOS Hub", "--display-name", help="Hub display name for the CSR"),
    scope: list[str] = typer.Option([Scope.MANAGE_IO_DEVICES.value], "--scope", help="Scopes to grant"),
) -> None:
    cfg = load_node()
    try:
        ensure_hub(cfg)
    except ValueError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)

    owner_state = _require_cli_state()
    requested_scopes = _scopes_from_strings(scope)
    storage_dir = _cli_base_dir() / "hub"

    with _root_backend() as backend:
        agent = HubAgent(backend=backend, storage_dir=storage_dir, subnet_id=owner_state.subnet_id)

        def approve(consent_id: str) -> None:
            try:
                backend.resolve_consent(
                    owner_device_id=owner_state.device_id,
                    consent_id=consent_id,
                    approve=True,
                    idempotency_key=_idem(f"hub-consent-{consent_id}"),
                )
            except RootBackendError as exc:
                _handle_backend_failure(exc)
                raise typer.Exit(1) from exc

        try:
            bundle = agent.bootstrap(
                scopes=requested_scopes,
                display_name=display_name,
                approve=approve,
                idempotency_key=_idem("hub-bootstrap"),
            )
        except RootBackendError as exc:
            _handle_backend_failure(exc)
            raise typer.Exit(1) from exc

        if isinstance(bundle, Mapping):
            node_id = bundle.get("node_id")
            if isinstance(node_id, str) and node_id:
                cfg.node_id = node_id
                save_node(cfg)

        try:
            channel = agent.connect()
            agent.verify_channel()
        except RootBackendError as exc:
            _handle_backend_failure(exc)
            raise typer.Exit(1) from exc

    typer.secho("Hub bootstrap complete.", fg=typer.colors.GREEN)
    typer.echo(f"Hub node ID: {cfg.node_id}")
    typer.echo(f"Channel token valid until: {channel.expires_at.isoformat()}")
    typer.echo(f"Hub keystore directory: {storage_dir}")


@hub_app.command("enroll")
def hub_enroll(ttl: Optional[str] = typer.Option(None, help="Optional certificate TTL (e.g. 24h)")) -> None:
    cfg = load_node()
    try:
        ensure_hub(cfg)
    except ValueError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)

    paths = _pki_paths()
    hub_id = cfg.node_id
    key_path = paths["hub_key"]
    cert_path = paths["hub_cert"]
    chain_path = paths["chain"]
    paths["dir"].mkdir(parents=True, exist_ok=True)

    key = generate_rsa_key()
    csr = make_csr(f"hub:{hub_id}", f"owner:{cfg.subnet_id}", key)

    client = _http_client()
    auth = RootAuthService(client)
    pki = PkiService(client, auth)
    try:
        result = pki.enroll(cfg, hub_id, csr, ttl)
    except Exception as exc:  # noqa: BLE001
        _handle_root_error(exc)
        raise typer.Exit(1)
    finally:
        client.close()

    cert_pem = result.get("cert_pem")
    chain_pem = result.get("chain_pem")
    if not isinstance(cert_pem, str) or not isinstance(chain_pem, str):
        _print_error("Root did not return certificate bundle")
        raise typer.Exit(1)

    write_private_key(key_path, key)
    write_pem(cert_path, cert_pem)
    write_pem(chain_path, chain_pem)

    typer.secho("Hub certificate enrolled.", fg=typer.colors.GREEN)
    typer.echo(f"Private key: {key_path}")
    typer.echo(f"Certificate: {cert_path}")
    typer.echo(f"CA chain: {chain_path}")


@hub_app.command("init")
def hub_init(
    token: str = typer.Option(..., "--token", help="One-time bootstrap token"),
    name: Optional[str] = typer.Option(None, "--name", help="Optional subnet display name"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing hub credentials"),
) -> None:
    cfg = load_node()
    paths = _pki_paths()
    key_path = paths["hub_key"]
    cert_path = paths["hub_cert"]
    chain_path = paths["chain"]

    if not force and (key_path.exists() or cert_path.exists()):
        _print_error("Hub credentials already exist; use --force to overwrite.")
        raise typer.Exit(1)

    key = generate_rsa_key()
    csr = make_csr("subnet:pending", None, key)

    client = _http_client()
    try:
        response = client.subnets_register(csr, token, name)
    except Exception as exc:  # noqa: BLE001
        _handle_root_error(exc)
        raise typer.Exit(1)
    finally:
        client.close()

    subnet_id = response.get("subnet_id")
    cert_pem = response.get("cert_pem")
    ca_pem = response.get("ca_pem")
    if not isinstance(subnet_id, str) or not isinstance(cert_pem, str) or not isinstance(ca_pem, str):
        _print_error("Root response is missing certificate bundle")
        raise typer.Exit(1)

    write_private_key(key_path, key)
    write_pem(cert_path, cert_pem)
    write_pem(chain_path, ca_pem)

    cfg.subnet_id = subnet_id
    save_node(cfg)

    typer.secho(f"Registered subnet: {subnet_id}", fg=typer.colors.GREEN)
    typer.echo(f"Hub key saved to {key_path}")
    typer.echo(f"Hub certificate saved to {cert_path}")
    typer.echo(f"CA certificate saved to {chain_path}")


@node_app.command("register")
def node_register(
    token: Optional[str] = typer.Option(None, "--token", help="Bootstrap token for node provisioning"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing node credentials"),
) -> None:
    cfg = load_node()
    paths = _pki_paths()
    node_key_path = paths["node_key"]
    node_cert_path = paths["node_cert"]
    chain_path = paths["chain"]
    hub_key_path = paths["hub_key"]
    hub_cert_path = paths["hub_cert"]

    if not force and (node_key_path.exists() or node_cert_path.exists()):
        _print_error("Node credentials already exist; use --force to overwrite.")
        raise typer.Exit(1)

    key = generate_rsa_key()
    csr = make_csr("node:pending", f"subnet:{cfg.subnet_id}", key)

    client = _http_client()
    try:
        if token:
            response = client.nodes_register(csr, bootstrap_token=token, mtls=None, subnet_id=cfg.subnet_id)
        else:
            if not hub_cert_path.exists() or not hub_key_path.exists() or not chain_path.exists():
                _print_error("Hub credentials are missing; provide --token or run 'adaos dev hub init'.")
                raise typer.Exit(1)
            response = client.nodes_register(
                csr,
                bootstrap_token=None,
                mtls=(str(hub_cert_path), str(hub_key_path), str(chain_path)),
                subnet_id=cfg.subnet_id,
            )
    except Exception as exc:  # noqa: BLE001
        _handle_root_error(exc)
        raise typer.Exit(1)
    finally:
        client.close()

    node_id = response.get("node_id")
    cert_pem = response.get("cert_pem")
    ca_pem = response.get("ca_pem")
    if not isinstance(node_id, str) or not isinstance(cert_pem, str):
        _print_error("Root response is missing node certificate")
        raise typer.Exit(1)

    write_private_key(node_key_path, key)
    write_pem(node_cert_path, cert_pem)
    if isinstance(ca_pem, str) and ca_pem.strip():
        write_pem(chain_path, ca_pem)

    cfg.node_id = node_id
    save_node(cfg)

    typer.secho(f"Registered node: {node_id}", fg=typer.colors.GREEN)
    typer.echo(f"Node key saved to {node_key_path}")
    typer.echo(f"Node certificate saved to {node_cert_path}")


__all__ = ["app"]
