from __future__ import annotations

import os
from typing import Dict, List, Optional

import httpx

from .models import CodeEntry, ModelAnalysis


MODEL_API_URL = os.getenv(
    "MODEL_API_URL",
    "https://model.example.com/analyze-code",  
)
MODEL_API_TOKEN = os.getenv("MODEL_API_TOKEN", "")


def _build_file_payload(
    file: CodeEntry,
    source_text: Optional[str],
    max_code_chars: int = 4000,
) -> Dict:
    code = (source_text or "")[:max_code_chars]
    return {
        "path": file.path,
        "tags": file.tags or [],
        "funcs": file.funcs or "",
        "endpoints": file.endpoints or "",
        "classes": file.classes or [],
        "code": code,
    }


def analyze_with_model(
    question: str,
    files: List[CodeEntry],
    source_texts: Optional[Dict[str, Optional[str]]] = None,
) -> ModelAnalysis:
    """
    Вызывает удалённое API модели.

    Локальный сервер:
    - подготавливает структуру (files + code),
    - отправляет JSON на удалённый endpoint,
    - парсит ответ и возвращает ModelAnalysis.
    """
    source_texts = source_texts or {}

    payload_files: List[Dict] = []
    for f in files:
        src = source_texts.get(f.path)
        payload_files.append(_build_file_payload(f, src))

    payload = {
        "question": question,
        "files": payload_files,
    }

    headers = {"Content-Type": "application/json"}
    if MODEL_API_TOKEN:
        headers["Authorization"] = f"Bearer {MODEL_API_TOKEN}"

    try:
        resp = httpx.post(MODEL_API_URL, json=payload, headers=headers, timeout=60.0)
    except httpx.RequestError as e:
        # сеть/коннект
        raise RuntimeError(f"Ошибка запроса к модели: {e}") from e

    if resp.status_code >= 400:
        raise RuntimeError(
            f"Модель вернула ошибку {resp.status_code}: {resp.text}"
        )

    data = resp.json()
    answer_text = data.get("answer", "").strip()
    if not answer_text:
        # запасной вариант, если модель не вернула поле
        answer_text = f"(Модель не вернула поле 'answer'. Сырой ответ: {data})"

    return ModelAnalysis(
        question=question,
        used_files=files,
        answer=answer_text,
    )
