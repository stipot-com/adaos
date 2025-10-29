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
from adaos.integrations.ovos.tts import OVOSTTSAdapter
from adaos.integrations.rhasspy.tts import RhasspyTTSAdapter

from adaos.apps.bootstrap import bootstrap_app
from adaos.services.bootstrap import run_boot_sequence, shutdown, is_ready
from adaos.services.observe import start_observer, stop_observer
from adaos.services.node_config import load_config
from adaos.services.router import RouterService
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.agent_context import get_ctx as _get_ctx
from adaos.services.io_console import print_text

bootstrap_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) инициализируем AgentContext (публикуется через set_ctx внутри bootstrap_app)

    # 2) только теперь импортируем то, что может косвенно дернуть контекст
    from adaos.apps.api import tool_bridge, subnet_api, observe_api, node_api, scenarios, root_endpoints, skills
    from adaos.apps.api import io_webhooks

    # 3) монтируем роутеры после bootstrap
    app.include_router(tool_bridge.router, prefix="/api")
    app.include_router(subnet_api.router, prefix="/api")
    app.include_router(node_api.router, prefix="/api/node")
    app.include_router(observe_api.router, prefix="/api/observe")
    app.include_router(scenarios.router, prefix="/api/scenarios")
    app.include_router(skills.router, prefix="/api/skills")
    app.include_router(root_endpoints.router)
    # Chat IO webhooks (mounted without /api prefix to keep exact paths)
    app.include_router(io_webhooks.router)

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
    await run_boot_sequence(app)
    try:
        await router_service.start()
    except Exception:
        pass
    # hub: seed self node into directory (base_url + capacity)
    try:
        conf = load_config()
        from adaos.services.registry.subnet_directory import get_directory
        from adaos.services.capacity import get_local_capacity
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
    # Start directory staler on hub to mark nodes offline after TTL
    try:
        conf = load_config()
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
    allow_origins=["http://localhost:4200", "http://127.0.0.1:4200", "*"],  # под dev и/или произвольный origin
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-AdaOS-Token", "Authorization"],
    allow_credentials=False,  # токен идёт в заголовке, куки не нужны
)

# --- базовые эндпоинты (для проверки, что всё живо) ---


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
    if mode == "ovos":
        return OVOSTTSAdapter()
    if mode == "rhasspy":
        return RhasspyTTSAdapter()
    return NativeTTS()


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


@app.post("/api/io/console/print", dependencies=[Depends(require_token)])
async def io_console_print(payload: SayRequestLike):
    conf = load_config()
    print_text(payload.text, node_id=conf.node_id)
    return {"ok": True}


# --- health endpoints (без авторизации; удобно для оркестраторов/проб) ---
@app.get("/health/live")
async def health_live():
    return {"ok": True, "adaos": {"version": BUILD_INFO.version, "build_date": BUILD_INFO.build_date}}


@app.get("/health/ready")
async def health_ready():
    # 200 только когда прошёл boot sequence
    if not is_ready():
        raise HTTPException(status_code=503, detail="not ready")
    return {"ok": True, "adaos": {"version": BUILD_INFO.version, "build_date": BUILD_INFO.build_date}}
