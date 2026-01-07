from __future__ import annotations

import io
import os
import base64
import json
import wave
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from adaos.apps.api.auth import require_token
from adaos.services.agent_context import get_ctx

router = APIRouter(prefix="/stt", tags=["stt"])


def _resolve_lang(lang: Optional[str]) -> str:
    raw = (lang or "").strip().lower()
    if raw.startswith("ru"):
        return "ru-ru"
    if raw.startswith("en"):
        return "en-us"
    return "ru-ru"


def _model_dir(target: str) -> Path:
    ctx = get_ctx()
    base = Path(ctx.paths.base_dir()) / "models" / "vosk" / target
    return base


def _ensure_model(target: str) -> Path:
    path = _model_dir(target)
    if path.exists() and any(path.iterdir()):
        return path

    if os.getenv("ADAOS_VOSK_AUTO_DOWNLOAD", "").strip() == "1":
        from adaos.adapters.audio.stt.model_manager import ensure_vosk_model

        return ensure_vosk_model(target, base_dir=Path(get_ctx().paths.base_dir()) / "models" / "vosk")

    raise HTTPException(
        status_code=503,
        detail=f"vosk model not found for '{target}'. Install it under {path} or set ADAOS_VOSK_AUTO_DOWNLOAD=1",
    )


def _read_wav_mono16k(data: bytes) -> bytes:
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            channels = wf.getnchannels()
            rate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            if sampwidth != 2:
                raise HTTPException(
                    status_code=400,
                    detail=f"expected wav PCM16; got channels={channels} rate={rate} sampwidth={sampwidth}",
                )
            frames = wf.readframes(wf.getnframes())
            if channels == 1:
                return frames
            # Best-effort: downmix multi-channel WAV by taking channel 0.
            try:
                import array

                samples = array.array("h")
                samples.frombytes(frames)
                mono = samples[0::channels]
                return mono.tobytes()
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail=f"failed to downmix wav channels={channels}",
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid wav: {exc}")

def _try_parse_json(body: bytes) -> dict | None:
    if not body:
        return None
    if body[:1] not in (b"{", b"["):
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if isinstance(obj, dict):
        return obj
    return None


def _decode_audio_from_request(body: bytes, content_type: str | None) -> bytes:
    """
    Support both:
      - raw WAV body (Content-Type: audio/wav)
      - JSON wrapper (Content-Type: application/json) with fields:
          { "audio_b64": "...", "lang": "ru-RU" }
    This avoids binary-body issues through some proxies.
    """
    ct = (content_type or "").lower().strip()
    obj: dict | None = None
    if "application/json" in ct or ct.endswith("+json"):
        obj = _try_parse_json(body)
        if obj is None:
            raise HTTPException(status_code=400, detail="invalid json")
    else:
        # Some proxies drop/override the Content-Type header. If the body looks
        # like JSON, still try to decode it as a wrapper.
        obj = _try_parse_json(body)

    if obj is not None:
        b64 = obj.get("audio_b64") or obj.get("wav_b64") or obj.get("data")
        if not isinstance(b64, str) or not b64.strip():
            raise HTTPException(status_code=400, detail="invalid json: missing audio_b64")
        token = b64.strip()
        if token.startswith("data:"):
            # data:audio/wav;base64,...
            try:
                token = token.split(",", 1)[1]
            except Exception:
                raise HTTPException(status_code=400, detail="invalid data url")
        try:
            return base64.b64decode(token, validate=False)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid base64: {exc}")
    # default: treat as raw wav bytes
    return body


@router.post("/transcribe", dependencies=[Depends(require_token)])
async def stt_transcribe(
    request: Request,
    lang: Optional[str] = None,
):
    """
    Minimal hub STT API for MVP.

    Accepts a raw WAV body (mono, 16kHz, 16-bit PCM) and returns `{ ok, text }`.
    """
    try:
        import vosk  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=501, detail=f"vosk is not available: {exc}")

    target = _resolve_lang(lang)
    model_path = _ensure_model(target)
    try:
        body = await request.body()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"failed to read body: {exc}")
    ct = request.headers.get("content-type")
    # If JSON wrapper includes its own lang override, apply it (also when
    # Content-Type is missing but body looks like JSON).
    obj = _try_parse_json(body)
    if isinstance(obj, dict) and isinstance(obj.get("lang"), str) and obj.get("lang").strip():
        target = _resolve_lang(obj.get("lang"))
        model_path = _ensure_model(target)
    body = _decode_audio_from_request(body, ct)
    pcm = _read_wav_mono16k(body)

    try:
        model = vosk.Model(str(model_path))
        # Use the model-native rate (16k) regardless of input; the frontend
        # encodes 16kHz WAV, and we also accept other rates for debugging.
        rec = vosk.KaldiRecognizer(model, 16000)
        rec.SetWords(False)
        rec.AcceptWaveform(pcm)
        import json

        res = json.loads(rec.FinalResult() or "{}")
        text = (res.get("text") or "").strip()
        return {"ok": True, "text": text}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
