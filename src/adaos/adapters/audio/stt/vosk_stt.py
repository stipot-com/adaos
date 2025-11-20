from __future__ import annotations
import json
import queue
from pathlib import Path
from typing import Generator, Optional
import typer
from typing import Generator, Optional, Iterable
import vosk
from adaos.services.agent_context import get_ctx

sd = None
_sd_error = None
try:
    import sounddevice as sd  # требует PortAudio
except OSError as e:
    _sd_error = e
    sd = None
except Exception as e:
    _sd_error = e
    sd = None


class VoskSTT:
    def __init__(
        self, model_path: Optional[str] = None, samplerate: int = 16000, device: Optional[int | str] = None, lang: str = "en", external_stream: Optional[Iterable[bytes]] = None
    ):
        sd = None
        _sd_error = None
        try:
            import sounddevice as sd  # требует PortAudio
        except OSError as e:
            _sd_error = e
            sd = None
        except Exception as e:
            _sd_error = e
            sd = None

        ADAOS_VOSK_MODEL = str(get_ctx().paths.base_dir() / "models" / "vosk" / "en-us")  # TODO move to constants
        # Инициализация модели
        if model_path:
            model_dir = Path(model_path)
        else:
            # ваша ensure_vosk_model уже готова – используем
            from adaos.adapters.audio.stt.model_manager import ensure_vosk_model

            model_dir = ensure_vosk_model(lang or "en", base_dir=ADAOS_VOSK_MODEL)

        if not Path(model_dir).exists():
            raise RuntimeError(f"Vosk model not found: {model_dir}")
        if sd is None:
            raise RuntimeError(
                "Audio backend (sounddevice/PortAudio) is unavailable. "
                f"Original error: {_sd_error}. "
                "Install system libportaudio (and dev headers) or run without native audio."
            )
        self.model = vosk.Model(str(model_dir))
        self.samplerate = samplerate
        self.rec = vosk.KaldiRecognizer(self.model, self.samplerate)
        self.rec.SetWords(True)
        self._external_stream = external_stream
        self._q = None
        self.stream = None
        if external_stream is None:
            import queue  # этот import уместен

            self._q = queue.Queue()
            # авто-подбор устройства/частоты, если дефолта нет
            try:
                import sounddevice as sd

                if device is None or int(device) < 0:
                    default_in, _ = sd.default.device
                    if default_in is None or int(default_in) < 0:
                        devices = sd.query_devices()
                        candidates = [i for i, d in enumerate(devices) if d["max_input_channels"] > 0]
                        if not candidates:
                            raise RuntimeError("Не найдено ни одного входного аудио-устройства.")
                        device = candidates[0]
                    else:
                        device = int(default_in)
                # если частота не поддерживается — возьмём дефолтную для выбранного устройства
                info = sd.query_devices(int(device), "input")
                if getattr(self, "samplerate", None) is None or self.samplerate <= 0:
                    self.samplerate = int(info["default_samplerate"])
            except Exception as e:
                raise RuntimeError(f"Проблема с аудио-устройством: {e}")
            self.stream = sd.RawInputStream(
                samplerate=self.samplerate,
                blocksize=8000,
                dtype="int16",
                channels=1,
                callback=self._on_audio,
                device=int(device) if device is not None else None,
            )
            self.stream.start()
        else:
            self._q: queue.Queue[bytes] = queue.Queue()
        # авто-подбор устройства/частоты, если дефолта нет
        try:
            import sounddevice as sd

            if device is None or int(device) < 0:
                default_in, _ = sd.default.device
                if default_in is None or int(default_in) < 0:
                    devices = sd.query_devices()
                    candidates = [i for i, d in enumerate(devices) if d["max_input_channels"] > 0]
                    if not candidates:
                        raise RuntimeError("Не найдено ни одного входного аудио-устройства.")
                    device = candidates[0]
                else:
                    device = int(default_in)
            # если частота не поддерживается — возьмём дефолтную для выбранного устройства
            info = sd.query_devices(int(device), "input")
            if getattr(self, "samplerate", None) is None or self.samplerate <= 0:
                self.samplerate = int(info["default_samplerate"])
        except Exception as e:
            raise RuntimeError(f"Проблема с аудио-устройством: {e}")
        self.stream = sd.RawInputStream(
            samplerate=self.samplerate,
            blocksize=8000,
            dtype="int16",
            channels=1,
            callback=self._on_audio,
            device=int(device) if device is not None else None,
        )
        self.stream.start()

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            # можно логировать при желании
            pass
        self._q.put(bytes(indata))

    def listen_stream(self) -> Generator[str, None, None]:
        """Бесконечный генератор итоговых фраз (final results)."""
        if self._external_stream is not None:
            for chunk in self._external_stream:
                if self.rec.AcceptWaveform(chunk):
                    res = json.loads(self.rec.Result())
                    text = (res.get("text") or "").strip()
                    if text:
                        yield text
            return

        # штатный режим через sounddevice очередь
        while True:
            data = self._q.get()
            if self.rec.AcceptWaveform(data):
                res = json.loads(self.rec.Result())
                text = (res.get("text") or "").strip()
                if text:
                    yield text

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
