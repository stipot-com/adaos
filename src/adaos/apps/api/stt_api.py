from __future__ import annotations

import io
import os
import wave
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

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


def _read_wav_mono16k(upload: UploadFile) -> bytes:
    data = upload.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            channels = wf.getnchannels()
            rate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            if channels != 1 or rate != 16000 or sampwidth != 2:
                raise HTTPException(
                    status_code=400,
                    detail=f"expected wav mono 16kHz 16-bit; got channels={channels} rate={rate} sampwidth={sampwidth}",
                )
            frames = wf.readframes(wf.getnframes())
            return frames
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid wav: {exc}")


@router.post("/transcribe", dependencies=[Depends(require_token)])
async def stt_transcribe(
    audio: UploadFile = File(...),
    lang: Optional[str] = Form(None),
):
    """
    Minimal hub STT API for MVP.

    Accepts a WAV file (mono, 16kHz, 16-bit PCM) and returns `{ ok, text }`.
    """
    try:
        import vosk  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=501, detail=f"vosk is not available: {exc}")

    target = _resolve_lang(lang)
    model_path = _ensure_model(target)
    pcm = _read_wav_mono16k(audio)

    try:
        model = vosk.Model(str(model_path))
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

