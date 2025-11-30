from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from .models import CodeEntry, ModelAnalysis


API_BASE_URL = os.getenv("CODE_ANALYZER_API_BASE_URL", "http://127.0.0.1:8000")

mcp = FastMCP("code-map-mcp-adapter", json_response=True)


class ApiError(RuntimeError):
    pass


def _get_client() -> httpx.Client:
    return httpx.Client(base_url=API_BASE_URL, timeout=30.0)


def api_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    with _get_client() as client:
        resp = client.get(path, params=params)
        if resp.status_code >= 400:
            raise ApiError(f"GET {path} failed: {resp.status_code} {resp.text}")
        return resp.json()


def api_post(path: str, json: Optional[Dict[str, Any]] = None) -> Any:
    with _get_client() as client:
        resp = client.post(path, json=json)
        if resp.status_code >= 400:
            raise ApiError(f"POST {path} failed: {resp.status_code} {resp.text}")
        return resp.json()


@mcp.tool()
def list_files(tag: Optional[str] = None, limit: int = 50) -> List[CodeEntry]:
    """
    Список файлов из центрального HTTP-сервера.
    """
    params: Dict[str, Any] = {"limit": limit}
    if tag:
        params["tag"] = tag
    data = api_get("/files", params=params)
    return [CodeEntry.model_validate(e) for e in data]


@mcp.tool()
def search_code(query: str, limit: int = 20) -> List[CodeEntry]:
    """
    Поиск по карте кода через центральный HTTP-сервис.
    """
    params: Dict[str, Any] = {"query": query, "limit": limit}
    data = api_get("/search", params=params)
    return [CodeEntry.model_validate(e) for e in data]


@mcp.tool()
def get_file_info(path: str) -> Optional[CodeEntry]:
    """
    Получить одну запись по точному path.
    """
    try:
        data = api_get("/file", params={"path": path})
    except ApiError as e:
        if "404" in str(e):
            return None
        raise
    return CodeEntry.model_validate(data)


@mcp.tool()
def analyze_with_nn(question: str, max_files: int = 30) -> ModelAnalysis:
    """
    Верхнеуровневый анализ с использованием нейросети на центральном сервере.
    """
    payload = {"question": question, "max_files": max_files, "with_source": True}
    data = api_post("/analyze", json=payload)
    return ModelAnalysis.model_validate(data)


if __name__ == "__main__":
    mcp.run(transport="stdio")
