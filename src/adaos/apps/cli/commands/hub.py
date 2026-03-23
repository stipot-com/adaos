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


def _root_verify_from_conf(conf: Any) -> str | bool | ssl.SSLContext:
    verify: str | bool | ssl.SSLContext = True
    try:
        ca_path = conf.ca_cert_path()
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
    except Exception:
        verify = True
    return verify


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
    diagnostics = runtime.get("channel_diagnostics") if isinstance(runtime.get("channel_diagnostics"), dict) else {}
    strategy = runtime.get("hub_root_transport_strategy") if isinstance(runtime.get("hub_root_transport_strategy"), dict) else {}
    protocol = runtime.get("hub_root_protocol") if isinstance(runtime.get("hub_root_protocol"), dict) else {}
    sidecar = runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {}
    strategy_assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}
    protocol_assessment = protocol.get("assessment") if isinstance(protocol.get("assessment"), dict) else {}
    root = overview.get("hub_root") if isinstance(overview.get("hub_root"), dict) else {}
    route = overview.get("hub_root_browser") if isinstance(overview.get("hub_root_browser"), dict) else {}
    root_diag = diagnostics.get("root_control") if isinstance(diagnostics.get("root_control"), dict) else {}
    route_diag = diagnostics.get("route") if isinstance(diagnostics.get("route"), dict) else {}
    route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
    outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
    tg_outbox = outboxes.get("telegram") if isinstance(outboxes.get("telegram"), dict) else {}
    llm_outbox = outboxes.get("llm") if isinstance(outboxes.get("llm"), dict) else {}
    streams = protocol.get("streams") if isinstance(protocol.get("streams"), dict) else {}
    control_lifecycle_stream = next(
        (
            entry
            for entry in streams.values()
            if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.control.lifecycle"
        ),
        {},
    )
    core_update_stream = next(
        (
            entry
            for entry in streams.values()
            if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.integration.github_core_update"
        ),
        {},
    )
    typer.echo(
        f"hub_root={root.get('effective_status') or 'unknown'}/{root.get('effective_state') or 'unknown'} | "
        f"hub_root_browser={route.get('effective_status') or 'unknown'}/{route.get('effective_state') or 'unknown'} | "
        f"transport={strategy.get('effective_transport') or '-'} "
        f"state={strategy_assessment.get('state') or 'unknown'} "
        f"server={strategy.get('selected_server') or '-'} | "
        f"protocol={protocol_assessment.get('state') or 'unknown'} "
        f"route_backlog={route_runtime.get('pending_events') or 0} "
        f"tg_outbox={tg_outbox.get('size') or 0} "
        f"tg_mode={tg_outbox.get('idempotency_mode') or '-'} "
        f"llm_mode={llm_outbox.get('idempotency_mode') or '-'} "
        f"llm_cache={llm_outbox.get('cache_hit_total') or 0}/{llm_outbox.get('cache_miss_total') or 0} "
        f"pending_acks={protocol.get('pending_ack_streams') or 0} "
        f"control_cursor={control_lifecycle_stream.get('last_acked_cursor') or 0}/{control_lifecycle_stream.get('last_issued_cursor') or 0} "
        f"control_ack_age={control_lifecycle_stream.get('last_ack_ago_s') if control_lifecycle_stream.get('last_ack_ago_s') is not None else '-'} "
        f"core_update_cursor={core_update_stream.get('last_acked_cursor') or 0}/{core_update_stream.get('last_issued_cursor') or 0} | "
        f"sidecar={sidecar.get('status') or ('disabled' if not sidecar.get('enabled') else 'unknown')} | "
        f"root_incident={root_diag.get('last_incident_class') or '-'} "
        f"route_incident={route_diag.get('last_incident_class') or '-'} "
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
            diagnostics = runtime.get("channel_diagnostics") if isinstance(runtime.get("channel_diagnostics"), dict) else {}
            strategy = runtime.get("hub_root_transport_strategy") if isinstance(runtime.get("hub_root_transport_strategy"), dict) else {}
            protocol = runtime.get("hub_root_protocol") if isinstance(runtime.get("hub_root_protocol"), dict) else {}
            sidecar = runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {}
            strategy_assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}
            protocol_assessment = protocol.get("assessment") if isinstance(protocol.get("assessment"), dict) else {}
            root = overview.get("hub_root") if isinstance(overview.get("hub_root"), dict) else {}
            route = overview.get("hub_root_browser") if isinstance(overview.get("hub_root_browser"), dict) else {}
            root_diag = diagnostics.get("root_control") if isinstance(diagnostics.get("root_control"), dict) else {}
            route_diag = diagnostics.get("route") if isinstance(diagnostics.get("route"), dict) else {}
            route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
            outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
            tg_outbox = outboxes.get("telegram") if isinstance(outboxes.get("telegram"), dict) else {}
            llm_outbox = outboxes.get("llm") if isinstance(outboxes.get("llm"), dict) else {}
            streams = protocol.get("streams") if isinstance(protocol.get("streams"), dict) else {}
            control_lifecycle_stream = next(
                (
                    entry
                    for entry in streams.values()
                    if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.control.lifecycle"
                ),
                {},
            )
            core_update_stream = next(
                (
                    entry
                    for entry in streams.values()
                    if isinstance(entry, dict) and str(entry.get("flow_id") or "") == "hub_root.integration.github_core_update"
                ),
                {},
            )
            ts = _time.strftime("%H:%M:%S")
            typer.echo(
                f"{ts} hub_root={root.get('effective_status') or 'unknown'}/{root.get('effective_state') or 'unknown'} "
                f"hub_root_browser={route.get('effective_status') or 'unknown'}/{route.get('effective_state') or 'unknown'} "
                f"transport={strategy.get('effective_transport') or '-'} "
                f"protocol={protocol_assessment.get('state') or 'unknown'} "
                f"route_backlog={route_runtime.get('pending_events') or 0} "
                f"tg_outbox={tg_outbox.get('size') or 0} "
                f"tg_mode={tg_outbox.get('idempotency_mode') or '-'} "
                f"llm_mode={llm_outbox.get('idempotency_mode') or '-'} "
                f"llm_cache={llm_outbox.get('cache_hit_total') or 0}/{llm_outbox.get('cache_miss_total') or 0} "
                f"pending_acks={protocol.get('pending_ack_streams') or 0} "
                f"control_cursor={control_lifecycle_stream.get('last_acked_cursor') or 0}/{control_lifecycle_stream.get('last_issued_cursor') or 0} "
                f"control_ack_age={control_lifecycle_stream.get('last_ack_ago_s') if control_lifecycle_stream.get('last_ack_ago_s') is not None else '-'} "
                f"core_update_cursor={core_update_stream.get('last_acked_cursor') or 0}/{core_update_stream.get('last_issued_cursor') or 0} "
                f"sidecar={sidecar.get('status') or ('disabled' if not sidecar.get('enabled') else 'unknown')} "
                f"root_incident={root_diag.get('last_incident_class') or '-'} "
                f"route_incident={route_diag.get('last_incident_class') or '-'} "
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


@root_link_app.command("reports")
def hub_root_reports(
    kind: str = typer.Option("all", "--kind", help="all|control|core-update"),
    hub_id: str | None = typer.Option(None, "--hub-id", help="Hub/subnet id to inspect (default: current hub)"),
    root: str | None = typer.Option(None, "--root", help="Root server base URL"),
    token: str | None = typer.Option(
        None,
        "--token",
        help="ROOT_TOKEN used for root reports. Falls back to ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.",
    ),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """
    Fetch explicit root-side hub reports for control and core-update streams.
    """
    ctx = get_ctx()
    conf = ctx.config
    root_base = (root or getattr(getattr(conf, "root_settings", None), "base_url", None) or "https://api.inimatic.com").rstrip("/")
    root_token = str(
        token
        or os.getenv("HUB_ROOT_TOKEN")
        or os.getenv("ADAOS_ROOT_TOKEN")
        or os.getenv("ROOT_TOKEN")
        or ""
    ).strip()
    if not root_token:
        raise typer.BadParameter("Missing ROOT_TOKEN. Pass --token or set ROOT_TOKEN/ADAOS_ROOT_TOKEN/HUB_ROOT_TOKEN.")
    target_hub_id = hub_id
    if target_hub_id is None:
        try:
            target_hub_id = str(conf.subnet_id or "").strip() or None
        except Exception:
            target_hub_id = None

    kind_key = str(kind or "all").strip().lower()
    if kind_key not in {"all", "control", "core-update", "core_update"}:
        raise typer.BadParameter("kind must be one of: all, control, core-update")

    client = RootHttpClient(base_url=root_base, verify=_root_verify_from_conf(conf))
    payload: dict[str, Any] = {"ok": True, "root_url": root_base}
    if kind_key in {"all", "control"}:
        payload["control"] = client.root_control_reports(root_token=root_token, hub_id=target_hub_id)
    if kind_key in {"all", "core-update", "core_update"}:
        payload["core_update"] = client.root_core_update_reports(root_token=root_token, hub_id=target_hub_id)
    if json_output:
        _print(payload, json_output=True)
        return

    def _protocol(report: dict[str, Any]) -> dict[str, Any]:
        value = report.get("_protocol")
        return value if isinstance(value, dict) else {}

    control_items = payload.get("control", {}).get("items") if isinstance(payload.get("control"), dict) else None
    if isinstance(control_items, list):
        typer.echo("control reports:")
        if not control_items:
            typer.echo("  (empty)")
        for item in control_items:
            if not isinstance(item, dict):
                continue
            report = item.get("report") if isinstance(item.get("report"), dict) else {}
            proto = _protocol(report)
            lifecycle = report.get("lifecycle") if isinstance(report.get("lifecycle"), dict) else {}
            root_control = report.get("root_control") if isinstance(report.get("root_control"), dict) else {}
            route = report.get("route") if isinstance(report.get("route"), dict) else {}
            typer.echo(
                "  "
                f"{item.get('hub_id') or report.get('subnet_id') or '-'} "
                f"cursor={proto.get('cursor') or 0} "
                f"message={proto.get('message_id') or '-'} "
                f"root_received={report.get('root_received_at') or '-'} "
                f"ack={report.get('root_ack_result') or '-'} "
                f"node_state={lifecycle.get('node_state') or '-'} "
                f"root={root_control.get('status') or root_control.get('state') or '-'} "
                f"route={route.get('status') or route.get('state') or '-'}"
            )

    core_items = payload.get("core_update", {}).get("items") if isinstance(payload.get("core_update"), dict) else None
    if isinstance(core_items, list):
        typer.echo("core_update reports:")
        if not core_items:
            typer.echo("  (empty)")
        for item in core_items:
            if not isinstance(item, dict):
                continue
            report = item.get("report") if isinstance(item.get("report"), dict) else {}
            proto = _protocol(report)
            status = report.get("status") if isinstance(report.get("status"), dict) else {}
            slot = report.get("slot_status") if isinstance(report.get("slot_status"), dict) else {}
            typer.echo(
                "  "
                f"{item.get('hub_id') or report.get('subnet_id') or '-'} "
                f"cursor={proto.get('cursor') or 0} "
                f"message={proto.get('message_id') or '-'} "
                f"root_received={report.get('root_received_at') or '-'} "
                f"ack={report.get('root_ack_result') or '-'} "
                f"state={status.get('state') or '-'} "
                f"phase={status.get('phase') or '-'} "
                f"slot={slot.get('active_slot') or '-'}"
            )


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
