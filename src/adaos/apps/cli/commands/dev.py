from __future__ import annotations

from pathlib import Path
from typing import Optional
import json
import logging
import os

import typer

from adaos.apps.cli import root_ops
from adaos.services.crypto.pki import generate_rsa_key, make_csr, write_pem, write_private_key
from adaos.services.node_config import ensure_hub, load_node, node_base_dir, save_node
from adaos.services.root.client import RootHttpClient, RootHttpError
from adaos.services.root.keyring import KeyringUnavailableError, load_refresh
from adaos.services.root.service import OwnerHubsService, PkiService, RootAuthError, RootAuthService
from adaos.services.id_gen import new_id

app = typer.Typer(help="Developer utilities for Root integration")
root_app = typer.Typer(help="Root owner operations")
hub_app = typer.Typer(help="Subnet bootstrap commands")
node_app = typer.Typer(help="Node registration commands")
app.add_typer(root_app, name="root")
app.add_typer(hub_app, name="hub")
app.add_typer(node_app, name="node")


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


def _hub_enroll(
    owner_token: Optional[str] = typer.Option(
        None,
        "--owner-token",
        envvar="ADAOS_ROOT_OWNER_TOKEN",
        help="Owner token issued by root for subnet enrollment",
    ),
    idempotency_key: Optional[str] = typer.Option(
        None,
        "--idem-key",
        help="Override Idempotency-Key header",
    ),
) -> None:
    token = owner_token or os.getenv("ADAOS_ROOT_OWNER_TOKEN")
    if not token:
        _print_error("Owner token is required. Set ADAOS_ROOT_OWNER_TOKEN or use --owner-token.")
        raise typer.Exit(1)

    try:
        config = root_ops.load_root_cli_config()
    except root_ops.RootCliError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)

    try:
        key_path, key = root_ops.ensure_hub_keypair()
    except root_ops.RootCliError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)

    fingerprint = root_ops.fingerprint_for_key(key)
    csr_pem = root_ops.build_csr("adaos-hub", key)
    idem_key = idempotency_key or new_id()

    try:
        response = root_ops.submit_subnet_registration(
            config,
            csr_pem=csr_pem,
            fingerprint=fingerprint,
            owner_token=token,
            idempotency_key=idem_key,
        )
    except root_ops.RootCliError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)

    data = response.get("data") or {}
    subnet_id = data.get("subnet_id")
    hub_device_id = data.get("hub_device_id")
    cert_pem = data.get("cert_pem")
    if not isinstance(subnet_id, str) or not isinstance(hub_device_id, str) or not isinstance(cert_pem, str):
        _print_error("Root response is missing subnet registration data.")
        raise typer.Exit(1)

    cert_path = root_ops.key_path("hub_cert")
    try:
        write_pem(cert_path, cert_pem)
    except Exception as exc:  # noqa: BLE001
        _print_error(f"Failed to save hub certificate: {exc}")
        raise typer.Exit(1)

    config.subnet_id = subnet_id
    config.keys.hub.key = str(key_path)
    config.keys.hub.cert = str(cert_path)
    try:
        root_ops.save_root_cli_config(config)
    except root_ops.RootCliError as exc:
        _print_error(str(exc))
        raise typer.Exit(1)

    typer.secho("Hub registration complete.", fg=typer.colors.GREEN)
    typer.echo(f"Subnet ID: {subnet_id}")
    typer.echo(f"Hub device ID: {hub_device_id}")
    typer.echo(f"Hub private key: {key_path}")
    typer.echo(f"Hub certificate: {cert_path}")


_hub_enroll_help = "Register the local hub with the root service using a silent owner token flow."
app.command("hub-enroll", help=_hub_enroll_help)(_hub_enroll)
app.command("hub_enroll", help=_hub_enroll_help)(_hub_enroll)
app.command("hub-register", help=_hub_enroll_help)(_hub_enroll)


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
