from __future__ import annotations

import json
import os
import ssl
from pathlib import Path
from typing import Any

import typer

from adaos.services.agent_context import get_ctx
from adaos.apps.cli.active_control import probe_control_api, resolve_control_base_url, resolve_control_token
from adaos.services.join_codes import create as create_join_code
from adaos.services.root.client import RootHttpClient, RootHttpError
from adaos.services.root.service import RootAuthError, RootAuthService

app = typer.Typer(help="Hub operations.")
join_code_app = typer.Typer(help="Join-code management.")
app.add_typer(join_code_app, name="join-code")
root_link_app = typer.Typer(help="Hub-root link diagnostics and control.")
app.add_typer(root_link_app, name="root")


def _print(data: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, dict) and "code" in data:
            typer.echo(str(data["code"]))
        else:
            typer.echo(str(data))


def _local_api_base() -> str:
    # Backward-compatible helper used by legacy code; prefer env-configured active server.
    return resolve_control_base_url()


@root_link_app.command("status")
def hub_root_status(json_output: bool = typer.Option(False, "--json", help="JSON output")) -> None:
    """
    Print current hub-root health snapshot (derived from /api/node/reliability).
    """
    import requests

    base = resolve_control_base_url()
    token = resolve_control_token()
    url = base + "/api/node/reliability"
    headers = {"X-AdaOS-Token": token}
    r = requests.get(url, headers=headers, timeout=5.0)
    r.raise_for_status()
    data = r.json()
    if json_output:
        _print(data, json_output=True)
        return
    runtime = (data or {}).get("runtime") if isinstance((data or {}).get("runtime"), dict) else {}
    overview = runtime.get("channel_overview") if isinstance(runtime.get("channel_overview"), dict) else {}
    strategy = runtime.get("hub_root_transport_strategy") if isinstance(runtime.get("hub_root_transport_strategy"), dict) else {}
    strategy_assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}
    root = overview.get("hub_root") if isinstance(overview.get("hub_root"), dict) else {}
    route = overview.get("hub_root_browser") if isinstance(overview.get("hub_root_browser"), dict) else {}
    typer.echo(
        f"hub_root={root.get('effective_status') or 'unknown'}/{root.get('effective_state') or 'unknown'} | "
        f"hub_root_browser={route.get('effective_status') or 'unknown'}/{route.get('effective_state') or 'unknown'} | "
        f"transport={strategy.get('effective_transport') or '-'} "
        f"state={strategy_assessment.get('state') or 'unknown'} "
        f"server={strategy.get('selected_server') or '-'} "
        f"last={strategy.get('last_event') or '-'}"
    )


@root_link_app.command("watch")
def hub_root_watch(
    interval_s: float = typer.Option(1.0, "--interval", min=0.2),
) -> None:
    """
    Continuously poll /api/node/reliability and print hub-root link state.
    """
    import requests, time as _time

    base = resolve_control_base_url()
    token = resolve_control_token()
    url = base + "/api/node/reliability"
    headers = {"X-AdaOS-Token": token}
    while True:
        try:
            r = requests.get(url, headers=headers, timeout=5.0)
            r.raise_for_status()
            data = r.json()
            runtime = (data or {}).get("runtime") if isinstance((data or {}).get("runtime"), dict) else {}
            overview = runtime.get("channel_overview") if isinstance(runtime.get("channel_overview"), dict) else {}
            strategy = runtime.get("hub_root_transport_strategy") if isinstance(runtime.get("hub_root_transport_strategy"), dict) else {}
            strategy_assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}
            root = overview.get("hub_root") if isinstance(overview.get("hub_root"), dict) else {}
            route = overview.get("hub_root_browser") if isinstance(overview.get("hub_root_browser"), dict) else {}
            ts = _time.strftime("%H:%M:%S")
            typer.echo(
                f"{ts} hub_root={root.get('effective_status') or 'unknown'}/{root.get('effective_state') or 'unknown'} "
                f"hub_root_browser={route.get('effective_status') or 'unknown'}/{route.get('effective_state') or 'unknown'} "
                f"transport={strategy.get('effective_transport') or '-'} "
                f"state={strategy_assessment.get('state') or 'unknown'} "
                f"last={strategy.get('last_event') or '-'}"
            )
        except KeyboardInterrupt:
            raise
        except Exception as e:
            ts = _time.strftime("%H:%M:%S")
            typer.echo(f"{ts} error: {type(e).__name__}: {e}")
        _time.sleep(float(interval_s))


@root_link_app.command("reconnect")
def hub_root_reconnect(
    transport: str | None = typer.Option(None, "--transport", help="ws|tcp"),
    url_override: str | None = typer.Option(None, "--url-override", help="Override NATS server URL"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """
    Request hub-root reconnect and optionally update transport overrides on-the-fly.
    """
    import requests

    base = resolve_control_base_url()
    token = resolve_control_token()
    url = base + "/api/node/hub-root/reconnect"
    headers = {"X-AdaOS-Token": token}
    payload = {"transport": transport, "url_override": url_override}
    r = requests.post(url, headers=headers, json=payload, timeout=8.0)
    r.raise_for_status()
    _print(r.json(), json_output=json_output)


@join_code_app.command("create")
def join_code_create(
    ttl_minutes: int = typer.Option(15, "--ttl-min", min=1, max=60),
    length: int = typer.Option(8, "--length", min=8, max=12),
    root: str | None = typer.Option(None, "--root", help="Root server base URL (default: node.yaml root.base_url)"),
    local: bool = typer.Option(False, "--local", help="Create join-code locally on the hub (offline mode; member must join via hub URL)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    """
    Create a short one-time join-code for adding member nodes to this hub's subnet.

    Default mode: create a Root session so members can join via Root using the short code.
    Offline mode: use `--local` to create the join-code on the hub directly.
    """
    ctx = get_ctx()
    conf = ctx.config
    if conf.role != "hub":
        raise typer.BadParameter("join-code creation is available only on a hub node (role=hub)")

    if local:
        info = create_join_code(subnet_id=conf.subnet_id, ttl_seconds=int(ttl_minutes) * 60, length=int(length), ctx=ctx)
        out = {
            "ok": True,
            "mode": "local",
            "code": info.code,
            "subnet_id": conf.subnet_id,
            "expires_at": info.expires_at,
        }
        _print(out, json_output=json_output)
        return

    root_base = (root or getattr(getattr(conf, "root_settings", None), "base_url", None) or "https://api.inimatic.com").rstrip("/")

    path = "/v1/subnets/join-code"
    url = root_base + path
    payload = {
        "subnet_id": conf.subnet_id,
        "ttl_minutes": int(ttl_minutes),
        "length": int(length),
    }

    verify: str | bool | ssl.SSLContext = True
    cert_tuple: tuple[str, str] | None = None
    try:
        ca_path = conf.ca_cert_path()
        cert_path = conf.hub_cert_path()
        key_path = conf.hub_key_path()
        if isinstance(ca_path, Path) and ca_path.exists():
            try:
                import certifi  # type: ignore

                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ctx = ssl.create_default_context()
            try:
                ctx.load_verify_locations(cafile=str(ca_path))
            except Exception:
                pass
            verify = ctx
        if isinstance(cert_path, Path) and isinstance(key_path, Path) and cert_path.exists() and key_path.exists():
            cert_tuple = (str(cert_path), str(key_path))
    except Exception:
        verify = True
        cert_tuple = None

    data: Any | None = None
    mtls_unauth: RootHttpError | None = None

    # Preferred auth: hub mTLS (available right after bootstrap).
    if cert_tuple is not None:
        try:
            client = RootHttpClient(base_url=root_base, verify=verify, cert=cert_tuple)
            data = client.request("POST", path, json=payload, timeout=10.0)
        except RootHttpError as exc:
            # Unauthorized: fall back to owner bearer session (dev/browser workflow).
            if exc.status_code in (401, 403):
                mtls_unauth = exc
            else:
                if exc.status_code in (404, 405):
                    raise typer.BadParameter(
                        f"Root does not support join-code create endpoint yet: {path}. Deploy a newer Root build or use `--local` on the hub."
                    ) from exc
                raise typer.BadParameter(f"Root join-code create failed: HTTP {exc.status_code}; url={url}; body={exc.payload}") from exc
        except Exception as exc:  # pragma: no cover
            raise typer.BadParameter(f"Root join-code create failed: {type(exc).__name__}: {exc}") from exc

    # Fallback auth: owner bearer session (developer workflow).
    if data is None:
        # Do not force owner session setup for hub join-code. If hub mTLS was rejected and
        # there is no configured owner profile, surface a clear error instead of asking for root-login.
        state = getattr(conf, "root_state", None)
        profile = state.get("profile") if isinstance(state, dict) else None

        # If Root is behind a TLS terminator/reverse proxy, hub mTLS cannot reach the backend.
        # Use ROOT_TOKEN (same as `adaos dev root init`) as a non-interactive auth fallback when available.
        if mtls_unauth is not None and not profile:
            root_token = (
                os.getenv("HUB_ROOT_TOKEN")
                or os.getenv("ADAOS_ROOT_TOKEN")
                or os.getenv("ROOT_TOKEN")
                or os.getenv("ADAOS_ROOT_OWNER_TOKEN")
                or ""
            ).strip()
            if root_token:
                try:
                    client = RootHttpClient(base_url=root_base, verify=verify)
                    data = client.request(
                        "POST",
                        path,
                        json=payload,
                        headers={"X-Root-Token": root_token},
                        timeout=10.0,
                    )
                except RootHttpError as exc:
                    if exc.status_code in (401, 403):
                        raise typer.BadParameter(
                            "Root rejected ROOT_TOKEN for join-code create "
                            f"(HTTP {exc.status_code}). "
                            "This Root build likely hasn't been updated to accept X-Root-Token on "
                            f"{path} yet. Deploy the Root backend changes for rev2026, or use `--local` "
                            "on the hub for offline/LAN-only mode. "
                            f"url={url}; body={exc.payload}"
                        ) from exc
                    raise typer.BadParameter(
                        f"Root join-code create failed via ROOT_TOKEN: HTTP {exc.status_code}; url={url}; body={exc.payload}"
                    ) from exc
                except Exception as exc:  # pragma: no cover
                    raise typer.BadParameter(f"Root join-code create failed: {type(exc).__name__}: {exc}") from exc
            else:
                raise typer.BadParameter(
                    f"Root rejected hub mTLS join-code create (HTTP {mtls_unauth.status_code}). "
                    "This Root build likely still requires token-based auth (mTLS is not reaching the backend). "
                    f"Set ROOT_TOKEN in the environment (or .env) and re-run, or use `--local` on the hub. "
                    f"body={mtls_unauth.payload}"
                ) from mtls_unauth

        if data is None:
            try:
                auth = RootAuthService(http=RootHttpClient(base_url=root_base, verify=verify))
                access_token = auth.get_access_token(conf)
            except RootAuthError as exc:
                if cert_tuple is None:
                    raise typer.BadParameter(
                        f"Root session is not configured: {exc}. Run either: adaos dev root init (hub bootstrap) or adaos dev root login (owner session)."
                    ) from exc
                raise typer.BadParameter(
                    f"Root session is not configured: {exc}. Hub mTLS request was unauthorized; Root may require an owner session. Run: adaos dev root login"
                ) from exc

            try:
                client = RootHttpClient(base_url=root_base, verify=verify)
                data = client.request(
                    "POST",
                    path,
                    json=payload,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10.0,
                )
            except RootHttpError as exc:
                raise typer.BadParameter(f"Root join-code create failed: HTTP {exc.status_code}; url={url}; body={exc.payload}") from exc
            except Exception as exc:  # pragma: no cover
                raise typer.BadParameter(f"Root join-code create failed: {type(exc).__name__}: {exc}") from exc

    if not isinstance(data, dict):
        raise typer.BadParameter("Root join-code create returned invalid response (expected JSON object)")
    code = str(data.get("code") or "").strip()
    expires_at_utc = data.get("expires_at_utc")
    if not code:
        raise typer.BadParameter("Root join-code create returned invalid response (missing code)")

    out = {
        "ok": True,
        "mode": "root",
        "code": code,
        "subnet_id": conf.subnet_id,
        "root_url": root_base,
        "expires_at_utc": expires_at_utc,
    }
    _print(out, json_output=json_output)
