# src/adaos/api/server.py
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from pydantic import BaseModel, Field
import platform, time, os

from adaos.apps.api.auth import require_token
from adaos.build_info import BUILD_INFO
from adaos.sdk.data.env import get_tts_backend
from adaos.adapters.audio.tts.native_tts import NativeTTS
from adaos.integrations.rhasspy.tts import RhasspyTTSAdapter

from adaos.apps.bootstrap import init_ctx
from adaos.services.bootstrap import run_boot_sequence, shutdown, is_ready
from adaos.services.observe import start_observer, stop_observer
from adaos.services.agent_context import get_ctx
from adaos.services.router import RouterService
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.agent_context import get_ctx as _get_ctx
from adaos.services.io_console import print_text
from adaos.services.capacity import install_io_in_capacity, get_local_capacity, _load_node_yaml as _load_node, _save_node_yaml as _save_node
from adaos.domain import Event as DomainEvent

init_ctx()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) инициализируем AgentContext (публикуется через set_ctx внутри bootstrap_app)

    # 2) только теперь импортируем то, что может косвенно дернуть контекст
    from adaos.apps.api import tool_bridge, subnet_api, observe_api, node_api, scenarios, root_endpoints, skills, stt_api
    from adaos.apps.api import io_webhooks
    from adaos.services.yjs.gateway import router as y_router, start_y_server, stop_y_server

    # 3) монтируем роутеры после bootstrap
    app.include_router(tool_bridge.router, prefix="/api")
    app.include_router(subnet_api.router, prefix="/api")
    app.include_router(node_api.router, prefix="/api/node")
    app.include_router(observe_api.router, prefix="/api/observe")
    app.include_router(scenarios.router, prefix="/api/scenarios")
    app.include_router(skills.router, prefix="/api/skills")
    app.include_router(stt_api.router, prefix="/api")
    app.include_router(root_endpoints.router)
    # Chat IO webhooks (mounted without /api prefix to keep exact paths)
    app.include_router(io_webhooks.router)
    # Yjs / events gateways (Stage A1)
    app.include_router(y_router)

    # 3.5) сохранить ссылки на контекст/шину в state для внешних компонентов
    try:
        app.state.ctx = _get_ctx()
        app.state.bus = app.state.ctx.bus
    except Exception:
        pass

    # 3.6) стартуем RouterService с локальной шиной
    router_service = RouterService(eventbus=app.state.bus, base_dir=app.state.ctx.paths.base_dir())
    app.state.router_service = router_service
    # Periodic liveness staler (hub only)
    staler_task = None

    # 4) поднимаем наблюдатель и выполняем boot-последовательность
    await start_observer()
    # Start Yjs websocket server background task
    try:
        await start_y_server()
    except Exception:
        pass
    # Start router early so ui.notify/ui.say from boot sequence are routed.
    try:
        await router_service.start()
    except Exception:
        pass
    await run_boot_sequence(app)
    # hub: seed self node into directory (base_url + capacity)
    try:
        conf = get_ctx().config
        from adaos.services.registry.subnet_directory import get_directory

        directory = get_directory()
        base_url = os.environ.get("ADAOS_SELF_BASE_URL")
        node_item = {
            "node_id": conf.node_id,
            "subnet_id": conf.subnet_id,
            "hostname": platform.node(),
            "roles": [conf.role],
            "base_url": base_url,
            "capacity": get_local_capacity(),
        }
        directory.on_register(node_item)
    except Exception:
        pass

    # 4.5) Hub-only: detect Telegram binding on Root for this subnet and expose IO telegram in capacity.
    tg_enabled = False
    try:
        conf = get_ctx().config
        if conf.role == "hub" and conf.subnet_id:
            ctx = _get_ctx()
            api_base = getattr(ctx.settings, "api_base", "https://api.inimatic.com")
            import requests as _requests

            link_url = f"{api_base.rstrip('/')}/io/tg/pair/link"
            r = _requests.get(link_url, params={"hub_id": conf.subnet_id}, timeout=3.0)
            if r.status_code == 200 and (r.json() or {}).get("ok"):
                # install telegram IO into capacity and refresh directory snapshot for this node
                install_io_in_capacity("telegram", ["text", "lang:ru", "lang:en"], priority=60)
                try:
                    from adaos.services.registry.subnet_directory import get_directory as _get_dir

                    cap = get_local_capacity()
                    _get_dir().repo.replace_io_capacity(conf.node_id, cap.get("io") or [])
                except Exception:
                    pass
                # Send greeting via Root
                try:
                    from adaos.sdk.data.i18n import _ as _t

                    text = _t("subnet.started")
                except Exception:
                    text = "subnet.started"
                try:
                    node_yaml = _load_node()
                except Exception:
                    node_yaml = {}
                alias = ((node_yaml.get("nats") or {}).get("alias")) or getattr(get_ctx().settings, "default_hub", None) or conf.subnet_id
                try:
                    prefixed_text = f"[{alias}]: {text}" if alias else text
                    _requests.post(
                        f"{api_base.rstrip('/')}/io/tg/send",
                        json={"hub_id": conf.subnet_id, "text": prefixed_text},
                        timeout=3.0,
                    )
                except Exception:
                    pass
                tg_enabled = True
    except Exception:
        pass
    # Start directory staler on hub to mark nodes offline after TTL
    try:
        conf = get_ctx().config
        if conf.role == "hub":
            import asyncio as _asyncio

            async def _staler():
                directory = get_directory()
                while True:
                    directory.mark_stale_if_expired(45.0)
                    await _asyncio.sleep(5.0)

            staler_task = _asyncio.create_task(_staler(), name="subnet-directory-staler")
        else:
            # member: periodically fetch snapshot from hub and ingest locally
            import asyncio as _asyncio
            import requests as _requests

            async def _pull_snapshot():
                directory = get_directory()
                while True:
                    try:
                        if conf.hub_url:
                            url = f"{conf.hub_url.rstrip('/')}/api/subnet/nodes"
                            r = await _asyncio.to_thread(
                                _requests.get,
                                url,
                                headers={"X-AdaOS-Token": conf.token or "dev-local-token"},
                                timeout=3.0,
                            )
                            if r.status_code == 200:
                                payload = r.json() or {}
                                directory.ingest_snapshot(payload.get("nodes") or [])
                    except Exception:
                        pass
                    await _asyncio.sleep(10.0)

            staler_task = _asyncio.create_task(_pull_snapshot(), name="subnet-directory-snapshot-puller")
    except Exception:
        pass

    try:
        yield
    finally:
        await stop_observer()
        # Stop ypy-websocket background server so it does not keep the process alive.
        try:
            await stop_y_server()
        except Exception:
            pass
        # On graceful shutdown, notify Telegram and UI if enabled
        try:
            if tg_enabled:
                conf = get_ctx().config
                ctx = _get_ctx()
                api_base = getattr(ctx.settings, "api_base", "https://api.inimatic.com")
                try:
                    from adaos.sdk.data.i18n import _ as _t

                    text = _t("subnet.stopped")
                except Exception:
                    text = "subnet.stopped"
                import requests as _requests

                try:
                    node_yaml = _load_node()
                except Exception:
                    node_yaml = {}
                alias = ((node_yaml.get("nats") or {}).get("alias")) or getattr(get_ctx().settings, "default_hub", None) or conf.subnet_id
                prefixed_text = f"[{alias}]: {text}" if alias else text
                # Try routed notify first if router is running.
                routed = False
                try:
                    if getattr(router_service, "_started", False):
                        ctx.bus.publish(
                            DomainEvent(
                                type="ui.notify",
                                payload={"text": prefixed_text},
                                source="api",
                                ts=time.time(),
                            )
                        )
                        routed = True
                except Exception:
                    routed = False
                if not routed:
                    _requests.post(
                        f"{api_base.rstrip('/')}/io/tg/send",
                        json={"hub_id": conf.subnet_id, "text": prefixed_text},
                        timeout=2.5,
                    )
                # Also emit a subnet.stopped event on the local bus so that
                # skills (e.g. greet_on_boot_skill) can update infra status.
                try:
                    ev = DomainEvent(
                        type="subnet.stopped",
                        payload={"subnet_id": conf.subnet_id},
                        source="api",
                        ts=time.time(),
                    )
                    ctx.bus.publish(ev)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            await router_service.stop()
        except Exception:
            pass
        try:
            if staler_task:
                staler_task.cancel()
        except Exception:
            pass
        await shutdown()


# пересоздаём приложение с lifespan
app = FastAPI(title="AdaOS API", lifespan=lifespan, version=BUILD_INFO.version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200", "http://127.0.0.1:4200", "*"],  # from local web app
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-AdaOS-Token", "Authorization"],
    allow_credentials=False,  # токен идёт в заголовке, куки не нужны
)


@app.get("/api/ping")
async def ping():
    return {"ok": True, "ts": time.time()}


class SayRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    voice: str | None = Field(default=None, description="Опционально: имя/идентификатор голоса")


class SayResponse(BaseModel):
    ok: bool
    duration_ms: int


def _make_tts():
    mode = get_tts_backend()
    if mode == "rhasspy":
        return RhasspyTTSAdapter()
    return NativeTTS()


class SetAliasRequest(BaseModel):
    alias: str = Field(..., min_length=1, max_length=64)
    hub_id: str | None = Field(default=None, description="Optional hub/subnet id; ignored on hub, for logging only.")


@app.post("/api/subnet/alias")
async def set_alias(body: SetAliasRequest, token=Depends(require_token)):
    try:
        conf = get_ctx().config
        # persist into node.yaml: nats.alias
        data = _load_node()
        nats = data.get("nats") or {}
        nats["alias"] = body.alias
        data["nats"] = nats
        _save_node_yaml = _save_node  # alias import name
        _save_node_yaml(data)
        # broadcast over local event bus
        try:
            from adaos.domain import Event as _Ev

            get_ctx().bus.publish(_Ev(type="subnet.alias.changed", payload={"alias": body.alias, "subnet_id": conf.subnet_id}, source="api"))
        except Exception:
            pass
        return {"ok": True, "alias": body.alias}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/status", dependencies=[Depends(require_token)])
async def status():
    return {
        "ok": True,
        "time": time.time(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "adaos": {
            "version": BUILD_INFO.version,
            "build_date": BUILD_INFO.build_date,
        },
    }


class YjsReloadRequest(BaseModel):
    webspace_id: str | None = Field(default=None, description="Target webspace id; defaults to 'default'")


@app.post("/api/yjs/reload", dependencies=[Depends(require_token)])
async def yjs_reload(body: YjsReloadRequest) -> dict:
    """
    Soft reload of Yjs state for a given webspace.

    For now this is implemented by recomputing the effective UI model via
    WebspaceScenarioRuntime for the target webspace. It does not drop the
    underlying YStore data; web clients can choose to clear their local
    cache if needed.
    """
    webspace_id = body.webspace_id or "default"
    try:
        # Ensure webspace exists and has YDoc/YStore backing files.
        # Detailed rebuild of ui/application and catalog is handled by
        # WebspaceScenarioRuntime on events (scenarios.synced, skills.activated,
        # desktop.webspace.reload, desktop.scenario.set). Here we only trigger
        # low-level Yjs bootstrap so that clients can reconnect safely.
        await ensure_webspace_ready(webspace_id)
    except Exception as exc:  # pragma: no cover - defensive guard
        raise HTTPException(status_code=500, detail=f"yjs_reload failed: {exc}") from exc
    return {
        "ok": True,
        "webspace_id": webspace_id,
    }


# TODO deprecated use bus instead. No external interface
@app.post("/api/say", response_model=SayResponse, dependencies=[Depends(require_token)])
async def say(payload: SayRequest):
    t0 = time.perf_counter()
    _make_tts().say(payload.text)
    dt = int((time.perf_counter() - t0) * 1000)
    return SayResponse(ok=True, duration_ms=dt)


# --- IO console endpoint for cross-node routing ---
class SayRequestLike(BaseModel):
    text: str
    origin: dict | None = None


# TODO deprecated use bus instead. No external interface
@app.post("/api/io/console/print", dependencies=[Depends(require_token)])
async def io_console_print(payload: SayRequestLike):
    conf = get_ctx().config
    print_text(payload.text, node_id=conf.node_id)
    return {"ok": True}


# --- health endpoints (без авторизации; удобно для оркестраторов/проб) ---
@app.get("/health/live")
async def health_live():
    return {"ok": True, "adaos": {"version": BUILD_INFO.version, "build_date": BUILD_INFO.build_date}}


@app.get("/health/ready")
async def health_ready():
    if not is_ready():
        raise HTTPException(status_code=503, detail="not ready")
    return {"ok": True, "adaos": {"version": BUILD_INFO.version, "build_date": BUILD_INFO.build_date}}
