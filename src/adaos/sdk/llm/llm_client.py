from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
import os

import requests


def _base_url() -> str:
    env_base = (
        os.getenv("ADAOS_LLM_BASE")
        or os.getenv("ADAOS_ROOT_BASE")
        or "http://127.0.0.1:8777"
    )
    return env_base.rstrip("/")


def _llm_endpoint() -> str:
    override = os.getenv("ADAOS_LLM_ENDPOINT")
    if override:
        return override
    return f"{_base_url()}/v1/llm/response"


def _auth_headers() -> Dict[str, str]:
    token = os.getenv("ADAOS_LLM_TOKEN") or os.getenv("ADAOS_ROOT_TOKEN") or os.getenv("ADAOS_TOKEN") or "dev-local-token"
    return {
        "X-AdaOS-Token": token,
        "Content-Type": "application/json",
    }


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
        "messages": [
            {"role": msg.get("role", "user"), "content": msg.get("content", "")} for msg in messages
        ],
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
    - writes the LLM output to output_path when provided (default LLM Artifacts draft)
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
    if final_output_path is None:
        final_output_path = Path("artifacts") / "llm_artifacts" / "ts_draft.md"
    if final_output_path:
        out_path = Path(final_output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_text, encoding="utf-8")
    return {
        "request_prompt": prompt,
        "response": response,
        "output_path": str(final_output_path) if final_output_path else None,
    }
