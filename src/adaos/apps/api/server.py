# src/adaos/api/server.py
import logging
from fastapi import FastAPI, Depends, HTTPException, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from pydantic import BaseModel, Field
import platform, time

from adaos.apps.api.auth import require_token
from adaos.build_info import BUILD_INFO
from adaos.sdk.data.env import get_tts_backend
from adaos.adapters.audio.tts.native_tts import NativeTTS

from adaos.apps.bootstrap import bootstrap_app
from adaos.services.bootstrap import run_boot_sequence, shutdown, is_ready
from adaos.services.agent_context import get_ctx
from adaos.services.nlu.context import DialogContext
from adaos.services.nlu.arbiter import arbitrate
from adaos.services.eventbus import emit, EVENT_NLU_INTERPRETATION
from adaos.services.observe import start_observer, stop_observer
from adaos.services.nlu.registry import registry as nlu_registry
from adaos.adapters.db import SqliteSkillRegistry

bootstrap_app()


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) инициализируем AgentContext (публикуется через set_ctx внутри bootstrap_app)

    # 2) только теперь импортируем то, что может косвенно дернуть контекст
    from adaos.apps.api import tool_bridge, subnet_api, observe_api, node_api, scenarios, root_endpoints, skills

    # 3) монтируем роутеры после bootstrap
    app.include_router(tool_bridge.router, prefix="/api")
    app.include_router(subnet_api.router, prefix="/api")
    app.include_router(node_api.router, prefix="/api/node")
    app.include_router(observe_api.router, prefix="/api/observe")
    app.include_router(scenarios.router, prefix="/api/scenarios")
    app.include_router(skills.router, prefix="/api/skills")
    app.include_router(root_endpoints.router)

    # 4) поднимаем наблюдатель и выполняем boot-последовательность
    await start_observer()
    await run_boot_sequence(app)

    ctx = get_ctx()
    registry = SqliteSkillRegistry(ctx.sql)
    try:
        installed = [rec.name for rec in registry.list() if getattr(rec, "installed", True)]
    except Exception:  # pragma: no cover - defensive logging
        logger.exception("NLU: failed to list installed skills")
        installed = []
    try:
        loaded = nlu_registry.load_all_active(installed or [])
        # полезные логи на старте
        loaded_names = sorted(list(loaded.keys()))
        if loaded_names:
            logger.info("NLU: loaded for skills: %s", ", ".join(loaded_names))
        else:
            logger.warning("NLU: no skills loaded (registry empty)")
    except Exception:
        logger.exception("NLU: bootstrap failed during load_all_active()")

    try:
        yield
    finally:
        await stop_observer()
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
    # можно расширить OVOS/Rhasspy позже
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


_nlu_router = APIRouter(prefix="/nlu", tags=["nlu"])
_dialog_ctx = DialogContext()


class InterpretReq(BaseModel):
    text: str
    lang: str = "ru"


@_nlu_router.post("/interpret")
def nlu_interpret(req: InterpretReq):
    ctx = get_ctx()
    event = arbitrate(req.text, req.lang, _dialog_ctx)
    payload = event["payload"]
    # если emit ожидает сигнатуру без bus — замените на emit(EVENT_NLU_INTERPRETATION, payload, source="api.nlu")
    emit(ctx.bus, EVENT_NLU_INTERPRETATION, payload, source="api.nlu")
    chosen = payload.get("chosen", {})
    logger.info(
        "NLU: chosen intent=%s skill=%s slots=%s trace=%s",
        chosen.get("intent"),
        chosen.get("skill"),
        chosen.get("slots"),
        event.get("trace_id"),
    )
    return event


app.include_router(_nlu_router)
