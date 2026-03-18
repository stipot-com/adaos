from __future__ import annotations

import json
import queue
from pathlib import Path
from typing import Generator, Iterable, Optional

from adaos.services.agent_context import get_ctx


class VoskSTT:
    def __init__(
        self,
        model_path: Optional[str] = None,
        samplerate: int = 16000,
        device: Optional[int | str] = None,
        lang: str = "en",
        external_stream: Optional[Iterable[bytes]] = None,
    ):
        # Import Vosk lazily so AdaOS can run on servers without audio deps.
        try:
            import vosk  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "Vosk is not available (failed to import native library). "
                "If you don't need voice/STT, you can ignore this. "
                "If you do, install required system libs (Debian/Ubuntu: libatomic1). "
                f"Original error: {exc}"
            ) from exc

        self._external_stream = external_stream
        self._q: "queue.Queue[bytes]" = queue.Queue()
        self.stream = None
        self.samplerate = int(samplerate or 16000)

        ADAOS_VOSK_MODEL = str(get_ctx().paths.base_dir() / "models" / "vosk" / "en-us")  # TODO: move to constants
        if model_path:
            model_dir = Path(model_path)
        else:
            from adaos.adapters.audio.stt.model_manager import ensure_vosk_model

            model_dir = ensure_vosk_model(lang or "en", base_dir=ADAOS_VOSK_MODEL)

        if not Path(model_dir).exists():
            raise RuntimeError(f"Vosk model not found: {model_dir}")

        self.model = vosk.Model(str(model_dir))
        self.rec = vosk.KaldiRecognizer(self.model, self.samplerate)
        self.rec.SetWords(True)

        if external_stream is None:
            try:
                import sounddevice as sd  # type: ignore
            except Exception as exc:
                raise RuntimeError(
                    "Audio backend (sounddevice/PortAudio) is unavailable. "
                    "Install system libportaudio (and dev headers) or run without native audio. "
                    f"Original error: {exc}"
                ) from exc

            dev = None
            if device is not None:
                try:
                    dev = int(device) if not isinstance(device, int) else device
                except Exception:
                    dev = device  # allow passing device name

            self.stream = sd.RawInputStream(
                samplerate=self.samplerate,
                blocksize=8000,
                dtype="int16",
                channels=1,
                callback=self._on_audio,
                device=dev,
            )
            self.stream.start()

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if status:
            # Optional: log status if needed.
            pass
        self._q.put(bytes(indata))

    def listen_stream(self) -> Generator[str, None, None]:
        """Infinite generator of final phrases (final STT results)."""
        if self._external_stream is not None:
            for chunk in self._external_stream:
                if self.rec.AcceptWaveform(chunk):
                    res = json.loads(self.rec.Result() or "{}")
                    text = (res.get("text") or "").strip()
                    if text:
                        yield text
            return

        while True:
            data = self._q.get()
            if self.rec.AcceptWaveform(data):
                res = json.loads(self.rec.Result() or "{}")
                text = (res.get("text") or "").strip()
                if text:
                    yield text

    def close(self) -> None:
        try:
            if self.stream is not None:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass

