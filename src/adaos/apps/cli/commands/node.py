from __future__ import annotations

import json
import platform
from urllib.parse import urlparse, urlunparse
from typing import Any

import requests
import typer

from adaos.services.node_config import displayable_path, load_config, save_config, set_role as cfg_set_role
from adaos.apps.cli.active_control import resolve_control_token

app = typer.Typer(help="Node operations (join/status/role).")
role_app = typer.Typer(help="Manage local node role.")
app.add_typer(role_app, name="role")


def _print(data: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        typer.echo(str(data))


def _control_get_json(*, control: str, path: str, token: str, timeout: float = 2.5) -> tuple[int | None, Any]:
    url = control.rstrip("/") + path
    headers = {"X-AdaOS-Token": token or resolve_control_token()}
    sess = requests.Session()
    try:
        sess.trust_env = False
    except Exception:
        pass
    try:
        response = sess.get(url, headers=headers, timeout=timeout)
    except Exception:
        return None, None
    try:
        payload = response.json()
    except Exception:
        payload = (response.text or "").strip()
    return response.status_code, payload


def _print_reliability_summary(payload: dict[str, Any]) -> None:
    node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    tree = runtime.get("readiness_tree") if isinstance(runtime.get("readiness_tree"), dict) else {}
    matrix = runtime.get("degraded_matrix") if isinstance(runtime.get("degraded_matrix"), dict) else {}
    channel_diagnostics = runtime.get("channel_diagnostics") if isinstance(runtime.get("channel_diagnostics"), dict) else {}
    channel_overview = runtime.get("channel_overview") if isinstance(runtime.get("channel_overview"), dict) else {}
    strategy = runtime.get("hub_root_transport_strategy") if isinstance(runtime.get("hub_root_transport_strategy"), dict) else {}
    protocol = runtime.get("hub_root_protocol") if isinstance(runtime.get("hub_root_protocol"), dict) else {}
    sidecar = runtime.get("sidecar_runtime") if isinstance(runtime.get("sidecar_runtime"), dict) else {}
    strategy_assessment = strategy.get("assessment") if isinstance(strategy.get("assessment"), dict) else {}
    integration = tree.get("integration") if isinstance(tree.get("integration"), dict) else {}

    typer.echo(
        f"node={node.get('node_id') or '?'} role={node.get('role') or '?'} "
        f"ready={bool(node.get('ready'))} state={node.get('node_state') or '?'}"
    )
    if channel_overview:
        for name in ("hub_root", "hub_root_browser", "browser_hub_sync"):
            item = channel_overview.get(name) if isinstance(channel_overview.get(name), dict) else {}
            if item:
                typer.echo(
                    f"{name}: {item.get('effective_status') or 'unknown'}/"
                    f"{item.get('effective_state') or 'unknown'}"
                )
    if strategy:
        typer.echo(
            "hub_root.transport: "
            f"requested={strategy.get('requested_transport') or '-'} "
            f"effective={strategy.get('effective_transport') or '-'} "
            f"state={strategy_assessment.get('state') or 'unknown'} "
            f"server={strategy.get('selected_server') or '-'} "
            f"last={strategy.get('last_event') or '-'} "
            f"attempts={strategy.get('attempt_seq') if strategy.get('attempt_seq') is not None else '?'}"
        )
    if sidecar:
        provenance = sidecar.get("transport_provenance") if isinstance(sidecar.get("transport_provenance"), dict) else {}
        process = sidecar.get("process") if isinstance(sidecar.get("process"), dict) else {}
        typer.echo(
            "sidecar: "
            f"phase={sidecar.get('phase') or '-'} "
            f"enabled={bool(sidecar.get('enabled'))} "
            f"status={sidecar.get('status') or 'unknown'} "
            f"transport={sidecar.get('local_listener_state') or ('ready' if sidecar.get('transport_ready') else 'down')}/"
            f"{sidecar.get('remote_session_state') or '-'} "
            f"control={sidecar.get('control_ready') or '-'} "
            f"route={sidecar.get('route_ready') or '-'} "
            f"connects={provenance.get('remote_connect_total') or 0}/"
            f"{provenance.get('remote_connect_fail_total') or 0} "
            f"superseded={provenance.get('superseded_total') or 0} "
            f"pid={process.get('listener_pid') or '-'} "
            f"local={sidecar.get('local_url') or '-'} "
            f"diag_age_s={sidecar.get('diag_age_s') if sidecar.get('diag_age_s') is not None else '-'}"
        )
    if protocol:
        assessment = protocol.get("assessment") if isinstance(protocol.get("assessment"), dict) else {}
        coverage = protocol.get("hardening_coverage") if isinstance(protocol.get("hardening_coverage"), dict) else {}
        classes = protocol.get("traffic_classes") if isinstance(protocol.get("traffic_classes"), dict) else {}
        control_cls = classes.get("control") if isinstance(classes.get("control"), dict) else {}
        route_cls = classes.get("route") if isinstance(classes.get("route"), dict) else {}
        outboxes = protocol.get("integration_outboxes") if isinstance(protocol.get("integration_outboxes"), dict) else {}
        control_authority = protocol.get("control_authority") if isinstance(protocol.get("control_authority"), dict) else {}
        tg_outbox = outboxes.get("telegram") if isinstance(outboxes.get("telegram"), dict) else {}
        llm_outbox = outboxes.get("llm") if isinstance(outboxes.get("llm"), dict) else {}
        route_runtime = protocol.get("route_runtime") if isinstance(protocol.get("route_runtime"), dict) else {}
        route_flows = route_runtime.get("flows") if isinstance(route_runtime.get("flows"), dict) else {}
        route_control_flow = route_flows.get("control") if isinstance(route_flows.get("control"), dict) else {}
        route_frame_flow = route_flows.get("frame") if isinstance(route_flows.get("frame"), dict) else {}
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
            "protocol: "
            f"state={assessment.get('state') or 'unknown'} "
            f"control_subs={control_cls.get('active_subscriptions') or 0} "
            f"route_subs={route_cls.get('active_subscriptions') or 0} "
            f"route_backlog={route_runtime.get('pending_events') or 0} "
            f"route_ctrl={route_control_flow.get('state') or '-'} "
            f"route_frame={route_frame_flow.get('state') or '-'} "
            f"tg_outbox={tg_outbox.get('size') or 0} "
            f"tg_durable={'yes' if tg_outbox.get('durable_store') else 'no'} "
            f"tg_persisted={tg_outbox.get('persisted_size') or 0} "
            f"tg_mode={tg_outbox.get('idempotency_mode') or '-'} "
            f"llm_mode={llm_outbox.get('idempotency_mode') or '-'} "
            f"llm_cache={llm_outbox.get('cache_hit_total') or 0}/{llm_outbox.get('cache_miss_total') or 0} "
            f"coverage={coverage.get('covered_flows') or 0}/{coverage.get('total_flows') or 0} "
            f"pending_acks={protocol.get('pending_ack_streams') or 0} "
            f"control_cursor={control_lifecycle_stream.get('last_acked_cursor') or 0}/{control_lifecycle_stream.get('last_issued_cursor') or 0} "
            f"control_auth={control_authority.get('state') or '-'} "
            f"control_ack_age={control_lifecycle_stream.get('last_ack_ago_s') if control_lifecycle_stream.get('last_ack_ago_s') is not None else '-'} "
            f"core_update_cursor={core_update_stream.get('last_acked_cursor') or 0}/{core_update_stream.get('last_issued_cursor') or 0}"
        )
    for name in ("hub_local_core", "root_control", "route", "sync", "media"):
        item = tree.get(name) if isinstance(tree.get(name), dict) else {}
        typer.echo(f"{name}: {item.get('status') or 'unknown'}")
    for name in ("telegram", "github", "llm"):
        item = integration.get(name) if isinstance(integration.get(name), dict) else {}
        typer.echo(f"integration.{name}: {item.get('status') or 'unknown'}")
    for name in ("root_control", "route"):
        item = channel_diagnostics.get(name) if isinstance(channel_diagnostics.get(name), dict) else {}
        stability = item.get("stability") if isinstance(item.get("stability"), dict) else {}
        if item:
            incident_classes = item.get("incident_classes_5m") if isinstance(item.get("incident_classes_5m"), dict) else {}
            top_incident = next(iter(incident_classes.keys()), None)
            typer.echo(
                f"diag.{name}: {stability.get('state') or 'unknown'} "
                f"score={stability.get('score') if stability.get('score') is not None else '?'} "
                f"recent_non_ready_5m={item.get('recent_non_ready_transitions_5m') or 0} "
                f"last_class={item.get('last_incident_class') or '-'} "
                f"top_5m={top_incident or '-'}"
            )
    for name in (
        "new_root_backed_member_admission",
        "root_routed_browser_proxy",
        "telegram_action_completion",
        "github_action_completion",
        "llm_action_completion",
        "core_update_coordination_via_root",
    ):
        item = matrix.get(name) if isinstance(matrix.get(name), dict) else {}
        typer.echo(f"{name}: {'allowed' if item.get('allowed') else 'blocked'}")


def _normalize_rendezvous_url(*, rendezvous_url: str, root_base: str) -> str:
    """
    Root/hub join endpoints can sit behind TLS-terminating proxies and occasionally return
    `http://...` rendezvous URLs even when the public entrypoint is `https://...`.

    We persist the rendezvous into node.yaml; ensure scheme matches the public Root URL when safe.
    """
    try:
        hub_u = urlparse(str(rendezvous_url or "").strip())
        root_u = urlparse(str(root_base or "").strip())
    except Exception:
        return rendezvous_url

    # Safe upgrade for typical public deployments (no explicit ports).
    if (
        hub_u.scheme == "http"
        and root_u.scheme == "https"
        and hub_u.hostname
        and root_u.hostname
        and hub_u.hostname.lower() == root_u.hostname.lower()
        and hub_u.port is None
        and root_u.port is None
    ):
        return urlunparse(hub_u._replace(scheme="https"))

    return rendezvous_url


def _ensure_absolute_key_paths(cfg) -> None:
    """
    Persist key paths in node.yaml as absolute paths under ADAOS_BASE_DIR.

    This matches hub-style config and avoids ambiguity when `node.yaml` is inspected manually.
    """
    try:
        cfg.root_settings.ca_cert = displayable_path(cfg.ca_cert_path())
    except Exception:
        pass
    try:
        cfg.subnet_settings.hub.key = displayable_path(cfg.hub_key_path())
        cfg.subnet_settings.hub.cert = displayable_path(cfg.hub_cert_path())
    except Exception:
        pass


@app.command("join")
def node_join(
    code: str = typer.Option(..., "--code", help="Short one-time join-code"),
    root: str = typer.Option(..., "--root", help="Join endpoint base URL (Hub or Root proxy)"),
    hub_url: str | None = typer.Option(None, "--hub-url", help="Optional explicit Hub URL override (offline/LAN setups)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    """
    Join a subnet as member using a short one-time join-code.

    This stores returned subnet token + hub URL into node.yaml under the active base_dir.
    """
    cfg = load_config()

    root_base = root.rstrip("/")
    candidates = [
        # Root-mediated join (preferred): Root issues/validates join-code and returns hub rendezvous.
        root_base + "/v1/subnets/join",
        # Legacy / offline: join directly against a hub node that holds the join-code locally.
        root_base + "/api/node/join",
    ]
    payload = {
        "code": code,
        "node_id": cfg.node_id,
        "hostname": platform.node(),
    }
    sess = requests.Session()
    try:
        sess.trust_env = False
    except Exception:
        pass

    def _is_missing_route(resp: requests.Response) -> bool:
        if resp.status_code not in (404, 405):
            return False
        try:
            js = resp.json()
        except Exception:
            txt = (resp.text or "").lower()
            return "cannot post" in txt or "not found" in txt
        if isinstance(js, dict):
            detail = js.get("detail")
            return detail == "Not Found"
        return False

    resp = None
    used_url = None
    last_body: Any = None
    for url in candidates:
        try:
            r = sess.post(url, json=payload, timeout=10)
        except Exception as exc:
            typer.secho(f"[AdaOS] join failed: {type(exc).__name__}: {exc}", fg=typer.colors.RED)
            typer.echo(f"url: {url}")
            raise typer.Exit(code=2) from exc
        if r.status_code == 200:
            resp = r
            used_url = url
            break
        # Route not available on this server; try next candidate.
        if _is_missing_route(r):
            continue
        resp = r
        used_url = url
        try:
            last_body = r.json()
        except Exception:
            last_body = (r.text or "").strip()
        break

    if resp is None or used_url is None:
        typer.secho("[AdaOS] join failed: no join endpoint found on root URL", fg=typer.colors.RED)
        for url in candidates:
            typer.echo(f"url: {url}")
        typer.echo("hint: pass --root http://<HUB_HOST>:8777 for direct hub join (offline/local dev), or update Root to a build that supports /v1/subnets/join.")
        raise typer.Exit(code=1)

    if resp.status_code != 200:
        if last_body is None:
            try:
                last_body = resp.json()
            except Exception:
                last_body = (resp.text or "").strip()
        typer.secho(f"[AdaOS] join failed: HTTP {resp.status_code}", fg=typer.colors.RED)
        typer.echo(f"url: {used_url}")
        if last_body:
            typer.echo(last_body)
        if resp.status_code == 404 and isinstance(last_body, dict) and last_body.get("detail") == "join-code not found":
            typer.echo("hint: if the code was created in hub local mode (--local), join against the hub URL: --root http://<HUB_HOST>:8777")
        raise typer.Exit(code=1)

    data = resp.json() or {}
    token = str(data.get("token") or "").strip()
    subnet_id = str(data.get("subnet_id") or "").strip()
    rendezvous_url = str(data.get("hub_url") or root).strip()
    if hub_url:
        rendezvous_url = str(hub_url).strip()
    rendezvous_url = _normalize_rendezvous_url(rendezvous_url=rendezvous_url, root_base=root_base)
    if not token or not subnet_id or not rendezvous_url:
        typer.secho("[AdaOS] join failed: invalid response from server (missing token/subnet_id/hub_url)", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    cfg.token = token
    cfg.subnet_id = subnet_id
    cfg.hub_url = rendezvous_url
    try:
        cfg.root_settings.base_url = root.strip()
    except Exception:
        pass
    cfg.role = "member"
    _ensure_absolute_key_paths(cfg)
    save_config(cfg)

    out = {
        "ok": True,
        "node_id": cfg.node_id,
        "subnet_id": cfg.subnet_id,
        "role": cfg.role,
        "hub_url": cfg.hub_url,
        "root_url": cfg.root_settings.base_url,
        "join_url": used_url,
    }
    _print(out, json_output=json_output)


@app.command("status")
def node_status(
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Probe local control API for readiness"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    result: dict[str, Any] = {
        "node_id": cfg.node_id,
        "subnet_id": cfg.subnet_id,
        "role": cfg.role,
        "hub_url": cfg.hub_url,
        "ready": None,
        "route_mode": None,
        "connected_to_hub": None,
    }
    if probe:
        status_code, payload = _control_get_json(
            control=control0,
            path="/api/node/status",
            token=resolve_control_token(explicit=cfg.token),
        )
        if status_code == 200 and isinstance(payload, dict):
            result["ready"] = bool(payload.get("ready"))
            result["route_mode"] = payload.get("route_mode")
            result["connected_to_hub"] = payload.get("connected_to_hub")
    _print(result, json_output=json_output)


@app.command("reliability")
def node_reliability(
    control: str | None = typer.Option(None, "--control", help="Control API base URL (default: active server)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    from adaos.apps.cli.active_control import resolve_control_base_url, resolve_control_token

    cfg = load_config()
    control0 = resolve_control_base_url(explicit=control, hub_url=cfg.hub_url if cfg.role == "member" else None)
    status_code, payload = _control_get_json(
        control=control0,
        path="/api/node/reliability",
        token=resolve_control_token(explicit=cfg.token),
    )
    if status_code is None:
        typer.secho("[AdaOS] reliability probe failed: local control API is unreachable", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    if status_code != 200 or not isinstance(payload, dict):
        typer.secho(f"[AdaOS] reliability probe failed: HTTP {status_code}", fg=typer.colors.RED)
        if payload:
            typer.echo(payload)
        raise typer.Exit(code=1)

    if json_output:
        _print(payload, json_output=True)
    else:
        _print_reliability_summary(payload)


@role_app.command("set")
def role_set(
    role: str = typer.Option(..., "--role", help="hub|member"),
    subnet_id: str | None = typer.Option(None, "--subnet-id"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    cfg = cfg_set_role(role, hub_url=None, subnet_id=subnet_id)
    out = {"ok": True, "node_id": cfg.node_id, "subnet_id": cfg.subnet_id, "role": cfg.role, "ready": None}
    _print(out, json_output=json_output)
