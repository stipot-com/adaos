from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
import os

import requests
from adaos.services.agent_context import get_ctx


def _llm_endpoint() -> str:
    override = os.getenv("ADAOS_LLM_ENDPOINT")
    if override:
        return override
    return f"{get_ctx().settings.api_base}/v1/llm/response"


def _llm_models_endpoint() -> str:
    """
    Use the same base as _llm_endpoint (Root LLM proxy), but with /models.

    This ensures we go through the same mtls / token path as /v1/llm/response
    instead of calling upstream APIs directly from the hub.
    """
    override = os.getenv("ADAOS_LLM_MODELS_ENDPOINT")
    if override:
        return override
    base_response = _llm_endpoint()
    # Strip trailing '/response' if present.
    if base_response.endswith("/response"):
        base = base_response.rsplit("/", 1)[0]
    else:
        base = base_response.rstrip("/")
    return f"{base}/models"


def _auth_headers() -> Dict[str, str]:
    token = os.getenv("ADAOS_LLM_TOKEN") or os.getenv("ADAOS_ROOT_TOKEN") or os.getenv("ADAOS_TOKEN") or "dev-local-token"
    headers = {
        "X-AdaOS-Token": token,
        "Content-Type": "application/json",
    }
    try:
        ctx = get_ctx()
    except Exception:
        ctx = None
    settings = getattr(ctx, "settings", None) if ctx is not None else None
    config = getattr(ctx, "config", None) if ctx is not None else None
    subnet_id = str(
        os.getenv("ADAOS_SUBNET_ID")
        or getattr(settings, "subnet_id", None)
        or getattr(config, "subnet_id", None)
        or ""
    ).strip()
    node_id = str(getattr(config, "node_id", None) or "").strip()
    if subnet_id:
        headers["X-AdaOS-Subnet-Id"] = subnet_id
    if node_id:
        headers["X-AdaOS-Node-Id"] = node_id
    return headers


def list_llm_models(*, timeout: float | None = None) -> Dict[str, Any]:
    """
    Fetch available LLM models from the Root LLM proxy.
    """
    resp = requests.get(_llm_models_endpoint(), headers=_auth_headers(), timeout=timeout or 30)
    resp.raise_for_status()
    data: Dict[str, Any] = resp.json() if resp.text else {}
    return data


def _extract_output_text(payload: Mapping[str, Any]) -> Optional[str]:
    if not payload:
        return None
    meta = payload.get("metadata") or {}
    if isinstance(meta, Mapping):
        direct = meta.get("output_text")
        if isinstance(direct, str):
            return direct
    direct_root = payload.get("output_text")
    if isinstance(direct_root, str):
        return direct_root
    output = payload.get("output")
    if isinstance(output, Iterable):
        chunks: list[str] = []
        for block in output:
            if not isinstance(block, Mapping):
                continue
            for part in block.get("content") or []:
                if not isinstance(part, Mapping):
                    continue
                text_val = part.get("text") or part.get("output_text") or part.get("content")
                if isinstance(text_val, str):
                    chunks.append(text_val)
        if chunks:
            return "".join(chunks)
    return None


def send_response(
    messages: Iterable[Mapping[str, str]],
    *,
    model: Optional[str] = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    top_p: float | None = None,
    timeout: float | None = None,
) -> Dict[str, Any]:
    """
    Send a message batch to the Root LLM proxy (Responses API wrapper).

    Returns dict with raw response plus convenience "output_text" field.
    """
    payload: Dict[str, Any] = {
        "model": model or os.getenv("ADAOS_LLM_MODEL") or "gpt-4o-mini",
        "messages": [{"role": msg.get("role", "user"), "content": msg.get("content", "")} for msg in messages],
    }
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    if top_p is not None:
        payload["top_p"] = float(top_p)

    resp = requests.post(_llm_endpoint(), json=payload, headers=_auth_headers(), timeout=timeout or 45)
    resp.raise_for_status()
    data: Dict[str, Any] = resp.json() if resp.text else {}
    data["output_text"] = _extract_output_text(data)
    return data


def _load_prompt(template_path: Path, substitutions: Mapping[str, str]) -> str:
    text = Path(template_path).read_text(encoding="utf-8")
    for key, value in substitutions.items():
        text = text.replace(key, value)
    return text


def request_ts_draft(
    technical_spec: str,
    *,
    model: Optional[str] = None,
    code_map_path: str | Path = "artifacts/code_map.yaml",
    output_path: str | Path | None = None,
    timeout: float | None = None,
) -> Dict[str, Any]:
    """
    Build a TS-focused LLM request and send it via the Root LLM proxy.

    - injects the Technical Specification into the ts_detailed_request template
    - inlines artifacts/code_map.yaml content
    - when output_path is provided, writes the LLM output to that file
      (callers may omit it to keep artifacts in Yjs / memory only)
    """
    code_map_text = Path(code_map_path).read_text(encoding="utf-8") if code_map_path else ""
    prompt_path = Path(__file__).parent / "prompts" / "ts_detailed_request.md"
    prompt = _load_prompt(
        prompt_path,
        {
            "<<<USER_REQUEST>>>": technical_spec.strip(),
            "<<<artifacts\\code_map.yaml>>>": code_map_text.strip(),
        },
    )
    response = send_response(
        [{"role": "user", "content": prompt}],
        model=model,
        timeout=timeout,
    )
    output_text = response.get("output_text") or ""
    final_output_path: str | Path | None = output_path
    if final_output_path:
        out_path = Path(final_output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_text, encoding="utf-8")
    return {
        "request_prompt": prompt,
        "response": response,
        "output_text": output_text,
        "output_path": str(final_output_path) if final_output_path else None,
    }
