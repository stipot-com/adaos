from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query

from .core import (
    CodeMapError,
    get_entry,
    list_entries,
    read_source_text,
    search_entries,
)
from .model import analyze_with_model
from .models import CodeEntry, ModelAnalysis


app = FastAPI(
    title="Code Analyzer Core API",
    description="Центральный HTTP-сервис анализа кода и подготовки данных для нейросети.",
    version="0.1.0",
)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/files", response_model=List[CodeEntry])
def api_list_files(
    tag: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> List[CodeEntry]:
    try:
        return list_entries(tag=tag, limit=limit)
    except CodeMapError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search", response_model=List[CodeEntry])
def api_search(
    query: str = Query(..., min_length=1),
    limit: int = Query(default=20, ge=1, le=200),
) -> List[CodeEntry]:
    try:
        return search_entries(query=query, limit=limit)
    except CodeMapError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/file", response_model=CodeEntry)
def api_get_file(path: str = Query(...)) -> CodeEntry:
    try:
        entry = get_entry(path)
    except CodeMapError as e:
        raise HTTPException(status_code=500, detail=str(e))

    if entry is None:
        raise HTTPException(status_code=404, detail="File not found in code map")
    return entry


@app.post("/analyze", response_model=ModelAnalysis)
def api_analyze(
    question: str,
    max_files: int = 30,
    with_source: bool = True,
) -> ModelAnalysis:
    if not question.strip():
        raise HTTPException(status_code=400, detail="Empty question")

    try:
        entries = search_entries(question, limit=max_files)
    except CodeMapError as e:
        raise HTTPException(status_code=500, detail=str(e))

    source_texts: Dict[str, Optional[str]] = {}
    if with_source:
        for e in entries:
            path, text = read_source_text(e.path)
            source_texts[path] = text

    analysis = analyze_with_model(
        question=question,
        files=entries,
        source_texts=source_texts if with_source else None,
    )
    return analysis
