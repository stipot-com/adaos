import os

from fastapi import Header, HTTPException, Request, status

from adaos.services.agent_context import get_ctx


def _expected_token() -> str:
    env_token = str(os.getenv("ADAOS_TOKEN") or "").strip()
    if env_token:
        return env_token
    try:
        return str(get_ctx().config.token or "dev-local-token").strip() or "dev-local-token"
    except Exception:
        return "dev-local-token"


def resolve_presented_token(
    *,
    x_adaos_token: str | None = None,
    authorization: str | None = None,
    query_token: str | None = None,
) -> str | None:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_adaos_token:
        token = x_adaos_token
    elif query_token:
        token = query_token
    return token


def ensure_token(token: str | None) -> None:
    if token != _expected_token():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-AdaOS-Token",
        )


async def require_token(
    request: Request,
    x_adaos_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    """
    Принимаем либо X-AdaOS-Token, либо Authorization: Bearer <token>.
    """
    header_token = str(request.headers.get("X-AdaOS-Token") or request.headers.get("x-adaos-token") or "").strip() or None
    auth_header = str(request.headers.get("Authorization") or request.headers.get("authorization") or "").strip() or None
    query_token = str(request.query_params.get("token") or "").strip() or None
    ensure_token(
        resolve_presented_token(
            x_adaos_token=header_token or x_adaos_token,
            authorization=auth_header or authorization,
            query_token=query_token,
        )
    )


def require_owner_token(token: str) -> None:
    expected = os.getenv("ADAOS_ROOT_OWNER_TOKEN") or os.getenv("ROOT_TOKEN") or ""
    if not expected or token != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid owner token")
