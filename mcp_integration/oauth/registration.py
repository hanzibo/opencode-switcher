"""OAuth 客户端注册 — 动态客户端注册（RFC 7591）与预注册直通。

优先级（MCP 2025-11-25 规范）：
1. 预注册 client_id（配置了就用）
2. DCR（AS 元数据提供 registration_endpoint 时）
3. 兜底：由调用方提示用户输入 client 信息
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from mcp_integration.oauth.models import OAuthClientInformationFull

logger = logging.getLogger(__name__)


class OAuthRegistrationError(Exception):
    """客户端注册失败。"""


async def dynamic_register(
    registration_endpoint: str,
    redirect_uris: List[str],
    client_name: str = "opencode-switcher",
    timeout: float = 30.0,
) -> OAuthClientInformationFull:
    """向 AS 的 registration_endpoint 发起动态客户端注册（RFC 7591 §3）。

    Parameters
    ----------
    registration_endpoint : str
        AS 元数据中的 registration_endpoint。
    redirect_uris : list of str
        注册的回调 URI 列表。
    client_name : str
        客户端名称。
    timeout : float
        请求超时秒数。

    Returns
    -------
    OAuthClientInformationFull
        注册成功返回客户端信息（client_id 等）。

    Raises
    ------
    OAuthRegistrationError
    """
    import aiohttp

    payload: Dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                registration_endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                body = await resp.text()
                if resp.status not in (200, 201):
                    raise OAuthRegistrationError(
                        f"注册端点 HTTP {resp.status}: {body[:200]}"
                    )
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    raise OAuthRegistrationError(f"注册端点返回非 JSON: {body[:200]}")
    except OAuthRegistrationError:
        raise
    except Exception as e:
        raise OAuthRegistrationError(f"注册请求失败: {e}")

    client = OAuthClientInformationFull.from_registration_response(data)
    if not client.client_id:
        raise OAuthRegistrationError("注册响应缺少 client_id")
    logger.info("动态客户端注册成功: client_id=%s", client.client_id)
    return client
