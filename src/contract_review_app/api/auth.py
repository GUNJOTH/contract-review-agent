"""API Token 认证依赖。"""

from __future__ import annotations

from secrets import compare_digest

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from contract_review_app.config import settings

_api_token_header = APIKeyHeader(name=settings.AUTH_HEADER_NAME, auto_error=False)


async def verify_api_token(
    _request: Request,
    token: str | None = Depends(_api_token_header),
) -> bool:
    """Validate the configured service token and fail closed when misconfigured.

    The configured token is never returned or logged. ``compare_digest`` keeps
    comparisons from leaking the token through an avoidable timing difference.
    """
    expected_token = settings.API_TOKEN.strip()
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_TOKEN 未配置，服务拒绝处理受保护请求",
        )

    provided_token = (token or "").strip()
    if not compare_digest(provided_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或缺少 API Token",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return True


def require_auth():
    return Depends(verify_api_token)
