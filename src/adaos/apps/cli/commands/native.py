from __future__ import annotations

import os
import sys
from typing import Optional

import typer

from adaos.adapters.audio.tts.native_tts import NativeTTS

app = typer.Typer(help="Native (audio) commands")


def _is_android() -> bool:
    return sys.platform == "android" or "ANDROID_ARGUMENT" in os.environ


def _try_start_android_service() -> bool:
    # Start Foreground Service via Java intent (Android only).
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


def _load_vosk_stt_class():
    """
    Vosk is an optional native dependency. Import it lazily so the CLI can run
    on servers without audio libs (or when Vosk shared libs cannot be loaded).
    """
    try:
        from adaos.adapters.audio.stt.vosk_stt import VoskSTT  # noqa: WPS433 (runtime import)

        return VoskSTT
    except Exception as e:
        typer.echo(
            "Vosk STT is unavailable in this environment.\n"
            f"Reason: {e}\n"
            "If you don't need voice, ignore this. If you do:\n"
            "  Debian/Ubuntu: sudo apt-get install -y libatomic1\n",
            err=True,
        )
        raise typer.Exit(code=1)


@app.command("say")
def say(
    text: str = typer.Argument(..., help="Text to speak"),
    lang: Optional[str] = typer.Option(None, "--lang", "-l", help="Language"),
    voice: Optional[str] = typer.Option(None, "--voice", "-v", help="Voice name"),
    rate: Optional[int] = typer.Option(None, "--rate", help="Speech rate"),
    volume: Optional[float] = typer.Option(None, "--volume", help="Volume [0..1]"),
):
    tts = NativeTTS(voice=voice, rate=rate, volume=volume, lang_hint=(lang or "").lower() or None)
    tts.say(text)


@app.command("start")
def start(
    lang: str = typer.Option("en", "--lang", help="Language"),
    samplerate: int = typer.Option(16000, "--samplerate", help="Sample rate"),
    device: Optional[str] = typer.Option(None, "--device", help="Audio input device"),
    echo: bool = typer.Option(True, "--echo", help="Echo recognized phrases via TTS"),
    model_path: Optional[str] = typer.Option(None, "--model-path", help="Path to Vosk model"),
    use_android_service: bool = True,
):
    """Run offline STT listener (Vosk). Ctrl+C to exit."""

    external = None
    if use_android_service and _is_android():
        ok = _try_start_android_service()
        if ok:
            from adaos.platform.android.mic_udp import AndroidMicUDP  # noqa: WPS433 (runtime import)

            external = AndroidMicUDP().listen_stream()

    VoskSTT = _load_vosk_stt_class()
    stt = VoskSTT(
        model_path=model_path,
        samplerate=samplerate,
        device=device,
        lang=lang,
        external_stream=external,
    )

    tts = None
    if echo:
        try:
            from adaos.platform.android.android_tts import AndroidTTS  # noqa: WPS433 (runtime import)

            tts = AndroidTTS(lang_hint="en-US" if lang.startswith("en") else "ru-RU")
        except Exception:
            tts = NativeTTS(lang_hint=lang)

    typer.echo("[Vosk] Ready. Say something...")
    try:
        for phrase in stt.listen_stream():
            typer.echo(f"[STT] {phrase}")
            if tts:
                try:
                    tts.say(phrase)
                except Exception as e:
                    typer.echo(f"[TTS error] {e}")
    except KeyboardInterrupt:
        pass
    finally:
        stt.close()

