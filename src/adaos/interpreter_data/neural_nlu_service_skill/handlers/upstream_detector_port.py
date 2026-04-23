"""
Upstream detector integration module.

Source reference:
https://github.com/Fla1lx/neural-network-module-for-determining-user-intent
(DetectionIntents.ipynb)

The notebook code is adapted into importable runtime code for AdaOS service-skill.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except Exception:  # pragma: no cover - optional runtime dependency
    torch = None
    nn = None
    F = None

_CITY_RU = re.compile(r"\b(?:в|во)\s+(?P<city>[^?.!,;:]+)", re.IGNORECASE | re.UNICODE)
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_NUMBER_RE = re.compile(r"\b\d+\b")
_DATE_RE = re.compile(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b")


@dataclass
class Config:
    PAD_IDX: int = 0
    BOS_IDX: int = 1
    EOS_IDX: int = 2
    UNK_IDX: int = 3
    SPECIALS: tuple[str, ...] = ("<pad>", "<bos>", "<eos>", "<unk>")
    EMB_DIM: int = 96
    CNN_CHANNELS: int = 128
    LSTM_HIDDEN: int = 128
    PROJ_DIM: int = 128
    MAX_LEN: int = 128


class NLUEncoder(nn.Module):  # type: ignore[misc]
    """Char-CNN + BiLSTM encoder from upstream notebook, adapted for service inference."""

    def __init__(self, vocab_size: int, num_labels: int, cfg: Config):
        super().__init__()
        self.emb = nn.Embedding(vocab_size, cfg.EMB_DIM, padding_idx=cfg.PAD_IDX)
        self.conv = nn.Conv1d(cfg.EMB_DIM, cfg.CNN_CHANNELS, kernel_size=5, padding=2)
        self.pool = nn.AdaptiveMaxPool1d(64)
        self.bi_lstm = nn.LSTM(cfg.CNN_CHANNELS, cfg.LSTM_HIDDEN, batch_first=True, bidirectional=True)
        self.proj = nn.Linear(2 * cfg.LSTM_HIDDEN, cfg.PROJ_DIM)
        self.cls = nn.Linear(2 * cfg.LSTM_HIDDEN, num_labels)
        self.dropout = nn.Dropout(0.1)

    def forward(self, x):
        e = self.emb(x).transpose(1, 2)
        c = F.relu(self.conv(e))
        c = self.pool(c).transpose(1, 2)
        out, _ = self.bi_lstm(c)
        feat = self.dropout(out.mean(dim=1))
        logits = self.cls(feat)
        z = F.normalize(self.proj(feat), dim=-1)
        return logits, z


def mask_entities(text: str) -> str:
    masked = _TIME_RE.sub("{time}", text)
    masked = _DATE_RE.sub("{date}", masked)
    masked = _NUMBER_RE.sub("{number}", masked)
    return masked


class Detector:
    def __init__(self) -> None:
        self._adapter = self._load_adapter()
        self._cfg = Config()
        self._engine = self._load_neural_engine()

    def _load_adapter(self):
        token = os.getenv("ADAOS_NEURAL_ADAPTER", "").strip()
        if not token or ":" not in token:
            return None
        module_name, func_name = token.split(":", 1)
        module_name = module_name.strip()
        func_name = func_name.strip()
        if not module_name or not func_name:
            return None
        try:
            mod = __import__(module_name, fromlist=[func_name])
            fn = getattr(mod, func_name, None)
            if callable(fn):
                return fn
        except Exception:
            return None
        return None

    @staticmethod
    def _artifacts_root() -> Path:
        base_dir = os.getenv("ADAOS_BASE_DIR", "").strip()
        if base_dir:
            return Path(base_dir).expanduser().resolve() / "state" / "nlu" / "neural"
        return Path.home() / ".adaos" / "state" / "nlu" / "neural"

    def _load_neural_engine(self):
        if torch is None or nn is None or F is None:
            return None
        default_root = self._artifacts_root()
        model_path = os.getenv("ADAOS_NEURAL_MODEL_PATH", "").strip() or str(default_root / "model.pt")
        labels_path = os.getenv("ADAOS_NEURAL_LABELS_PATH", "").strip() or str(default_root / "labels.json")
        vocab_path = os.getenv("ADAOS_NEURAL_VOCAB_PATH", "").strip() or str(default_root / "vocab.json")
        if not (model_path and labels_path and vocab_path):
            return None
        if not (os.path.exists(model_path) and os.path.exists(labels_path) and os.path.exists(vocab_path)):
            return None
        try:
            with open(labels_path, "r", encoding="utf-8") as fp:
                labels = json.load(fp)
            with open(vocab_path, "r", encoding="utf-8") as fp:
                vocab = json.load(fp)
            if not isinstance(labels, list) or not labels:
                return None
            if not isinstance(vocab, list) or not vocab:
                return None
            stoi = {str(ch): idx for idx, ch in enumerate(vocab)}
            model = NLUEncoder(len(vocab), len(labels), self._cfg)
            state = torch.load(model_path, map_location="cpu")
            model.load_state_dict(state)
            model.eval()
            return {"model": model, "labels": labels, "stoi": stoi}
        except Exception:
            return None

    def _encode_text(self, text: str, stoi: dict[str, int]) -> list[int]:
        masked_text = mask_entities(text)
        ids = [self._cfg.BOS_IDX]
        ids.extend(stoi.get(ch, self._cfg.UNK_IDX) for ch in masked_text[: self._cfg.MAX_LEN - 2])
        ids.append(self._cfg.EOS_IDX)
        if len(ids) < self._cfg.MAX_LEN:
            ids.extend([self._cfg.PAD_IDX] * (self._cfg.MAX_LEN - len(ids)))
        return ids[: self._cfg.MAX_LEN]

    def _score_with_skill_weights(self, probs: list[float], labels: list[str]) -> list[float]:
        raw = os.getenv("ADAOS_NEURAL_SKILL_WEIGHTS", "").strip()
        if not raw:
            return probs
        try:
            weights_obj = json.loads(raw)
        except Exception:
            return probs
        if not isinstance(weights_obj, dict):
            return probs
        weights = [1.0] * len(probs)
        for i, label in enumerate(labels):
            w = weights_obj.get(label)
            if isinstance(w, (int, float)) and float(w) > 0:
                weights[i] = float(w)
        scaled = [p * w for p, w in zip(probs, weights)]
        total = float(sum(scaled))
        if total <= 0:
            return probs
        return [v / total for v in scaled]

    def _neural_detect(self, text: str) -> dict[str, Any] | None:
        engine = self._engine
        if not isinstance(engine, dict) or torch is None:
            return None
        model = engine["model"]
        labels = engine["labels"]
        stoi = engine["stoi"]
        try:
            ids = self._encode_text(text, stoi)
            x = torch.tensor([ids], dtype=torch.long)
            with torch.no_grad():
                logits, _ = model(x)
                probs = [float(v) for v in torch.softmax(logits[0], dim=-1).cpu().tolist()]
            probs = self._score_with_skill_weights(probs, labels)
            order = sorted(range(len(probs)), key=lambda i: probs[i], reverse=True)
            top_idx = order[0]
            alternatives = [{"intent": str(labels[i]), "confidence": float(probs[i])} for i in order[1:4]]
            return {
                "top_intent": str(labels[top_idx]),
                "confidence": float(probs[top_idx]),
                "alternatives": alternatives,
                "slots": {},
                "evidence": {"backend": "charcnn_bilstm", "masked": mask_entities(text)},
            }
        except Exception:
            return None

    def detect(self, text: str, *, webspace_id: str | None = None, locale: str | None = None) -> dict[str, Any]:
        if self._adapter is not None:
            try:
                out = self._adapter(text=text, webspace_id=webspace_id, locale=locale)
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
        neural = self._neural_detect(text)
        if isinstance(neural, dict):
            return neural
        return self._fallback_detect(text)

    @staticmethod
    def _fallback_detect(text: str) -> dict[str, Any]:
        lowered = text.lower()
        if any(token in lowered for token in ("weather", "погод", "температур", "градус")):
            city = None
            m = _CITY_RU.search(text)
            if m:
                city = m.group("city").strip()
            return {
                "top_intent": "desktop.open_weather",
                "confidence": 0.83,
                "slots": {"city": city} if city else {},
                "evidence": {"backend": "fallback", "rule": "weather_keywords"},
            }
        if any(token in lowered for token in ("таймер", "timer")):
            return {
                "top_intent": "timer.start",
                "confidence": 0.81,
                "slots": {},
                "evidence": {"backend": "fallback", "rule": "timer_keywords"},
            }
        return {
            "top_intent": "",
            "confidence": 0.0,
            "slots": {},
            "evidence": {"backend": "fallback", "rule": "none"},
        }
