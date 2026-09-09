"""认证依赖。审查页面对用户不鉴权。"""

from fastapi import Depends, Request


async def verify_api_token(request: Request) -> bool:
    """审查应用对前端不鉴权。

    调用 OCR 网关所需的 Token 只放在服务端 ``OCR_GATEWAY_TOKEN``，
    由后端代填，用户打开审查页面时不需要输入。
    """
    del request
    return True


def require_auth():
    return Depends(verify_api_token)
