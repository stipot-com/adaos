# -*- coding: utf-8 -*-
import typer, sys, os, json
from typing import Optional
from pathlib import Path
from adaos.apps.cli.i18n import _

from adaos.adapters.audio.tts.native_tts import NativeTTS

VoskSTT = None  # ленивый импорт
from adaos.adapters.audio.stt.vosk_stt import VoskSTT

app = typer.Typer(help="Native (audio) commands")


def _is_android() -> bool:
    return sys.platform == "android" or "ANDROID_ARGUMENT" in os.environ


def _try_start_android_service():
    # Стартуем Foreground Service через Java-интент
    try:
        from jnius import autoclass

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Intent = autoclass("android.content.Intent")
        context = PythonActivity.mActivity
        service_cls = autoclass("ai.adaos.platform.android.AdaOSAudioService")
        intent = Intent(context, service_cls)
        context.startForegroundService(intent)
        return True
    except Exception as e:
        typer.echo(f"[AndroidService] failed: {e}")
        return False


def _run_nlu_demo(endpoint: str = "http://127.0.0.1:8777") -> None:
    try:
        import httpx
    except Exception as exc:  # pragma: no cover - optional dependency path
        typer.echo(f"[NLU] httpx is required for demo mode: {exc}")
        return

    typer.echo("[NLU] Demo mode. Type phrases and press Enter (Ctrl+D to exit).")
    with httpx.Client(base_url=endpoint, timeout=5.0) as client:
        for line in sys.stdin:
            text = line.strip()
            if not text:
                continue
            try:
                resp = client.post("/nlu/interpret", json={"text": text, "lang": "ru"})
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                typer.echo(f"[NLU] HTTP error {exc.response.status_code}: {exc.response.text}")
                continue
            except httpx.RequestError as exc:
                typer.echo(f"[NLU] request error: {exc}")
                continue
            try:
                payload = resp.json()
            except ValueError:
                typer.echo(f"[NLU] invalid JSON: {resp.text}")
                continue
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("say")
def say(
    text: str = typer.Argument(..., help="Текст для озвучивания"),
    lang: str = typer.Option(None, "--lang", "-l", help="Язык"),
    voice: str = typer.Option(None, "--voice", "-v", help="Голос"),
    rate: int = typer.Option(None, "--rate", help="Скорость речи"),
    volume: float = typer.Option(None, "--volume", help="Громкость [0..1]"),
):
    tts = NativeTTS(voice=voice, rate=rate, volume=volume, lang_hint=(lang or "").lower() or None)
    tts.say(text)


import typer

from adaos.adapters.audio.stt.vosk_stt import VoskSTT


@app.command("start")
def start(
    lang: str = typer.Option("en", "--lang", help="Язык"),
    samplerate: int = typer.Option(16000, "--samplerate", help="Частота дискретизации"),
    device: str = typer.Option(None, "--device", help="Устройство"),
    echo: bool = typer.Option(True, "--echo", help="Эхо-тест"),
    model_path: str = typer.Option(None, "--model-path", help="Путь к модели"),
    nlu_demo: bool = typer.Option(False, "--nlu-demo", help="Запустить NLU демонстрацию", show_default=False),
    use_android_service: bool = True,
):
    """
    Запускает офлайн-слушатель (Vosk). Ctrl+C для выхода.
    """
    if nlu_demo:
        _run_nlu_demo()
        return

    external = None
    if use_android_service and _is_android():
        ok = _try_start_android_service()
        if ok:
            from adaos.platform.android.mic_udp import AndroidMicUDP

            external = AndroidMicUDP().listen_stream()
    global VoskSTT
    if VoskSTT is None:
        from adaos.adapters.audio.stt.vosk_stt import VoskSTT
    stt = VoskSTT(model_path=model_path, samplerate=samplerate, device=device, lang=lang)

    tts = None
    if echo:
        try:
            # Android TTS (если запускаемся внутри APK)
            from adaos.platform.android.android_tts import AndroidTTS

            tts = AndroidTTS(lang_hint="en-US" if lang.startswith("en") else "ru-RU")
        except Exception:
            # Десктоп/фоллбек
            from adaos.adapters.audio.tts.native_tts import NativeTTS

            tts = NativeTTS(lang_hint=lang)

    typer.echo("[Vosk] Ready. Say something...")

    try:
        for phrase in stt.listen_stream():
            typer.echo(f"[STT] {phrase}")
            if tts:
                try:
                    tts.say(phrase)  # Говорим каждую итоговую фразу
                except Exception as e:
                    typer.echo(f"[TTS error] {e}")
    except KeyboardInterrupt:
        pass
    finally:
        stt.close()
