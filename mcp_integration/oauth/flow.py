"""OAuth 2.1 授权码流实现 — PKCE S256 + loopback 回调 + token 交换/刷新。

纯 asyncio + aiohttp，不依赖 anyio（Python 3.14 兼容）。

流程：
1. 校验 AS 支持 PKCE S256
2. 生成 PKCE 参数 + state
3. 启动 loopback 回调服务器（127.0.0.1:<随机端口>）
4. 构造授权 URL（含 resource / state / code_challenge）并打开系统浏览器
5. 等待回调 code + state 校验
6. 交换授权码 → OAuthToken
7. refresh_token 刷新（供 provider 复用）
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import shutil
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode

from mcp_integration.oauth.models import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    PKCEParameters,
)

logger = logging.getLogger(__name__)

_DEFAULT_FLOW_TIMEOUT = 300.0  # 授权流总超时（秒）
_CALLBACK_PATH = "/callback"

BrowserOpener = Callable[[str], Any]


class OAuthFlowError(Exception):
    """OAuth 授权流错误。"""


class OAuthFlowTimeoutError(OAuthFlowError):
    """用户未在超时时间内完成授权。"""


class OAuthTokenError(OAuthFlowError):
    """token 端点返回错误。"""


# ── URL 构造 ───────────────────────────────────────────────────────


def generate_state() -> str:
    """生成防 CSRF 的 state 值。"""
    return secrets.token_urlsafe(32)


def build_authorization_url(
    meta: OAuthMetadata,
    client_id: str,
    redirect_uri: str,
    pkce: PKCEParameters,
    state: str,
    resource: str,
    scope: str = "",
) -> str:
    """构造授权 URL（RFC 6749 §4.1.1 + RFC 7636 PKCE + RFC 8707 resource）。"""
    params: Dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": pkce.code_challenge_method,
        "state": state,
        "resource": resource,
    }
    if scope:
        params["scope"] = scope
    sep = "&" if "?" in meta.authorization_endpoint else "?"
    return f"{meta.authorization_endpoint}{sep}{urlencode(params)}"


# ── 浏览器 ─────────────────────────────────────────────────────────


async def open_browser(url: str, opener: Optional[BrowserOpener] = None) -> None:
    """用系统默认浏览器打开授权 URL。

    opener 可注入（测试用）；默认使用 xdg-open。
    """
    if opener is not None:
        result = opener(url)
        if asyncio.iscoroutine(result):
            await result
        return
    xdg = shutil.which("xdg-open")
    if not xdg:
        raise OAuthFlowError("未找到 xdg-open，无法打开浏览器")
    proc = await asyncio.create_subprocess_exec(
        xdg, url,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()


# ── Loopback 回调服务器 ────────────────────────────────────────────


class LoopbackRedirectServer:
    """监听 127.0.0.1 的 asyncio 回调服务器。

    收到 GET /callback?code=...&state=... 后完成 future，
    向浏览器返回「授权成功，可关闭此窗口」页面。

    Parameters
    ----------
    port : int, optional
        固定监听端口（DCR 预注册 redirect_uri 场景）；默认 0（随机）。
    timeout : float
        等待回调超时秒数。
    """

    def __init__(self, timeout: float = _DEFAULT_FLOW_TIMEOUT, port: int = 0) -> None:
        self._timeout = timeout
        self._port_requested = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._port = 0
        self._result_fut: Optional[asyncio.Future] = None

    @property
    def redirect_uri(self) -> str:
        if not self._port:
            raise OAuthFlowError("回调服务器未启动")
        return f"http://127.0.0.1:{self._port}{_CALLBACK_PATH}"

    async def start(self) -> str:
        """启动服务器并返回 redirect_uri。"""
        loop = asyncio.get_running_loop()
        self._result_fut = loop.create_future()
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self._port_requested
        )
        sock = self._server.sockets[0]
        self._port = sock.getsockname()[1]
        logger.debug("Loopback 回调服务器已启动: %s", self.redirect_uri)
        return self.redirect_uri

    async def wait_for_callback(self) -> Tuple[str, str]:
        """等待回调，返回 (code, state)。超时抛 OAuthFlowTimeoutError。"""
        if self._result_fut is None:
            raise OAuthFlowError("回调服务器未启动")
        try:
            return await asyncio.wait_for(self._result_fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            raise OAuthFlowTimeoutError(
                f"授权超时（{int(self._timeout)}s 内未完成浏览器授权）"
            )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """处理单个 HTTP 请求（仅解析请求行 + query）。"""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                return
            line = request_line.decode("utf-8", errors="replace").strip()
            parts = line.split()
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]

            # 排空剩余请求头（body 无需读取）
            while True:
                header_line = await asyncio.wait_for(reader.readline(), timeout=10)
                if not header_line or header_line in (b"\r\n", b"\n"):
                    break

            if method == "GET" and target.startswith(_CALLBACK_PATH):
                self._handle_callback(target, writer)
            else:
                self._write_simple(writer, 404, "Not Found")
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _handle_callback(self, target: str, writer: asyncio.StreamWriter) -> None:
        """解析回调 query，完成 future。"""
        query = target.partition("?")[2]
        params = parse_qs(query)
        code = params.get("code", [""])[0]
        state = params.get("state", [""])[0]
        error = params.get("error", [""])[0]

        if error:
            err_desc = params.get("error_description", [""])[0]
            if self._result_fut is not None and not self._result_fut.done():
                self._result_fut.set_exception(
                    OAuthFlowError(f"授权服务器返回错误: {error} {err_desc}".strip())
                )
            self._write_simple(writer, 400, f"授权失败: {error}")
            return

        if not code:
            self._write_simple(writer, 400, "缺少 code 参数")
            return

        if self._result_fut is not None and not self._result_fut.done():
            self._result_fut.set_result((code, state))
        self._write_simple(
            writer, 200,
            "<html><body style='font-family:sans-serif;text-align:center;"
            "padding-top:80px'><h2>✅ 授权成功</h2>"
            "<p>现在可以关闭此窗口，返回 opencode-switcher。</p></body></html>",
            content_type="text/html; charset=utf-8",
        )

    @staticmethod
    def _write_simple(
        writer: asyncio.StreamWriter,
        status: int,
        body: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
        data = body.encode("utf-8")
        resp = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8") + data
        writer.write(resp)


# ── Token 端点交互 ─────────────────────────────────────────────────


def _apply_client_auth(
    form: Dict[str, str],
    headers: Dict[str, str],
    client: OAuthClientInformationFull,
) -> None:
    """按 token_endpoint_auth_method 应用客户端认证（none/basic/post）。"""
    method = client.token_endpoint_auth_method or "none"
    if method == "client_secret_basic" and client.client_secret:
        encoded_id = quote(client.client_id, safe="")
        encoded_secret = quote(client.client_secret, safe="")
        credentials = f"{encoded_id}:{encoded_secret}"
        headers["Authorization"] = "Basic " + base64.b64encode(
            credentials.encode("utf-8")
        ).decode("utf-8")
        form.pop("client_secret", None)
    elif method == "client_secret_post" and client.client_secret:
        form["client_secret"] = client.client_secret
    # "none"：不添加任何客户端凭据（public client）


def _parse_token_response(data: Dict[str, Any]) -> OAuthToken:
    """解析 token 端点响应，计算 expires_at。"""
    token = OAuthToken.from_dict(data)
    if not token.access_token:
        raise OAuthTokenError("token 响应缺少 access_token")
    if token.expires_in is not None:
        token.expires_at = time.time() + token.expires_in
    return token


async def _post_token_form(
    token_endpoint: str,
    form: Dict[str, str],
    client: OAuthClientInformationFull,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """POST form 到 token 端点并解析 JSON 响应。"""
    import aiohttp

    headers = {"Accept": "application/json"}
    _apply_client_auth(form, headers, client)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                token_endpoint,
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise OAuthTokenError(
                        f"token 端点 HTTP {resp.status}: {body[:200]}"
                    )
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    raise OAuthTokenError(f"token 端点返回非 JSON: {body[:200]}")
    except OAuthTokenError:
        raise
    except Exception as e:
        raise OAuthTokenError(f"token 端点请求失败: {e}")


async def exchange_code(
    meta: OAuthMetadata,
    client: OAuthClientInformationFull,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    resource: str,
    timeout: float = 30.0,
) -> OAuthToken:
    """授权码 → access token（RFC 6749 §4.1.3 + RFC 7636 + RFC 8707）。"""
    form: Dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client.client_id,
        "code_verifier": code_verifier,
        "resource": resource,
    }
    data = await _post_token_form(meta.token_endpoint, form, client, timeout=timeout)
    return _parse_token_response(data)


async def refresh_token(
    meta: OAuthMetadata,
    client: OAuthClientInformationFull,
    refresh_token_value: str,
    resource: str,
    scope: str = "",
    timeout: float = 30.0,
) -> Optional[OAuthToken]:
    """刷新 access token（RFC 6749 §6 + RFC 8707）。

    Returns
    -------
    OAuthToken or None
        AS 不支持 refresh grant 时返回 None。
    """
    if not meta.supports_refresh_token():
        return None
    form: Dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token_value,
        "client_id": client.client_id,
        "resource": resource,
    }
    if scope:
        form["scope"] = scope
    data = await _post_token_form(meta.token_endpoint, form, client, timeout=timeout)
    return _parse_token_response(data)


# ── 高层封装 ───────────────────────────────────────────────────────


async def run_authorization_flow(
    meta: OAuthMetadata,
    client: OAuthClientInformationFull,
    resource: str,
    scope: str = "",
    *,
    redirect_uri: Optional[str] = None,
    callback_port: int = 0,
    timeout: float = _DEFAULT_FLOW_TIMEOUT,
    browser_opener: Optional[BrowserOpener] = None,
) -> OAuthToken:
    """执行完整 PKCE 授权码流，返回 OAuthToken。

    Parameters
    ----------
    meta : OAuthMetadata
        已发现的 AS 元数据。
    client : OAuthClientInformationFull
        注册/预注册的客户端信息。
    resource : str
        MCP Server 规范 URI（RFC 8707 resource 参数）。
    scope : str, optional
        请求的 scope（空格分隔）；为空则省略 scope 参数。
    redirect_uri : str, optional
        固定回调 URI（预注册场景）；默认启动动态 loopback。
    callback_port : int, optional
        回调服务器固定端口（DCR 注册 redirect_uri 场景）；默认 0（随机）。
    timeout : float
        授权流总超时。
    browser_opener : callable, optional
        浏览器打开器注入（测试用）。

    Raises
    ------
    OAuthFlowError / OAuthFlowTimeoutError / OAuthTokenError
    """
    if not meta.supports_pkce_s256():
        raise OAuthFlowError(
            "授权服务器不支持 PKCE S256，拒绝执行授权码流"
        )

    pkce = PKCEParameters.generate()
    state = generate_state()

    server: Optional[LoopbackRedirectServer] = None
    if redirect_uri is None:
        server = LoopbackRedirectServer(timeout=timeout, port=callback_port)
        actual_redirect_uri = await server.start()
    else:
        actual_redirect_uri = redirect_uri

    try:
        auth_url = build_authorization_url(
            meta, client.client_id, actual_redirect_uri,
            pkce, state, resource, scope,
        )
        await open_browser(auth_url, opener=browser_opener)
        logger.info("等待用户在浏览器中完成授权: %s", meta.authorization_endpoint)

        if server is not None:
            code, returned_state = await server.wait_for_callback()
        else:
            # 固定 redirect_uri：调用方必须自行注入回调处理（暂不支持自动）
            raise OAuthFlowError("固定 redirect_uri 模式需要自定义回调处理器")

        if returned_state != state:
            raise OAuthFlowError("回调 state 不匹配（可能的 CSRF 攻击）")

        return await exchange_code(
            meta, client, actual_redirect_uri, code,
            pkce.code_verifier, resource,
        )
    finally:
        if server is not None:
            await server.close()
