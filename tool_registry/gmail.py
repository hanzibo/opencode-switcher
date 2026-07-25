"""Gmail Mail Reader — read emails from Gmail via REST API + OAuth 2.0.

Uses the official Google Gmail API with OAuth 2.0 authentication.
Requires credentials.json from Google Cloud Console (Desktop App type).
Token is auto-refreshed and cached in token.json.
"""

import base64
import html as html_mod
import json
import os
import re
from email.utils import parsedate_to_datetime
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from clipboard_store import GMAIL_CREDENTIALS_DIR

# ── Constants ────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_CREDENTIALS_JSON = os.path.join(GMAIL_CREDENTIALS_DIR, "credentials.json")
_TOKEN_JSON = os.path.join(GMAIL_CREDENTIALS_DIR, "token.json")

_MAX_RESULTS = 20
_MAX_BODY_CHARS = 10000


# ── OAuth 2.0 helpers ────────────────────────────────────────────────

def _get_credentials() -> Credentials:
    """Load or refresh OAuth 2.0 credentials.

    Priority:
    1. token.json on disk (auto-refreshed if expired)
    2. credentials.json → OAuth browser flow → save token.json

    Returns:
        google.oauth2.credentials.Credentials
    """
    creds: Optional[Credentials] = None

    # 1. Try loading cached token
    if os.path.isfile(_TOKEN_JSON):
        try:
            with open(_TOKEN_JSON) as f:
                token_data = json.load(f)
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        except Exception as e:
            print(f"Gmail: failed to load token.json: {e}", flush=True)

    # 2. Refresh or full auth
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"Gmail: token refresh failed, re-authorizing: {e}", flush=True)
            creds = None

    if not creds or not creds.valid:
        # 3. Full OAuth flow
        if not os.path.isfile(_CREDENTIALS_JSON):
            raise FileNotFoundError(
                "Gmail OAuth credentials not found.\n\n"
                "请先配置 Gmail API 凭据：\n"
                "1. 访问 https://console.cloud.google.com/ 创建项目\n"
                "2. 启用 Gmail API\n"
                "3. 配置 OAuth 同意屏幕（External → 添加测试用户）\n"
                "4. 创建 OAuth 2.0 客户端 ID（桌面应用类型）\n"
                "5. 下载 credentials.json 并放入：\n"
                f"   {_CREDENTIALS_JSON}\n\n"
                "配置完成后，打开 Settings → Gmail → 点击「登录 Google 账号」授权。"
            )

        flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS_JSON, SCOPES)
        creds = flow.run_local_server(port=0)

        # 4. Save token
        _save_token(creds)

    return creds


def _save_token(creds: Credentials):
    """Persist OAuth token to token.json with secure permissions."""
    os.makedirs(GMAIL_CREDENTIALS_DIR, exist_ok=True)
    token_dict = json.loads(creds.to_json())

    # Try to extract email from the token info
    try:
        if hasattr(creds, 'token_info') and creds.token_info:
            email = creds.token_info.get('email', '')
            if email:
                token_dict['email'] = email
    except Exception:
        pass

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    try:
        fd = os.open(_TOKEN_JSON, flags, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(token_dict, f, indent=2)
    except Exception as e:
        print(f"Gmail: failed to save token.json: {e}", flush=True)

    # Also sync store for settings UI
    try:
        from clipboard_store import GmailOAuthStore
        store = GmailOAuthStore()
        store.save_token(token_dict, email=token_dict.get("email", ""))
    except Exception:
        pass


# ── Gmail API helpers ────────────────────────────────────────────────

def _get_service() -> "Resource":
    """Get authenticated Gmail API service instance."""
    creds = _get_credentials()
    return build("gmail", "v1", credentials=creds)


def _get_header_value(headers: list, name: str) -> str:
    """Get a header value by name, or fallback for missing Subject."""
    value = _get_header(headers, name)
    if name.lower() == "subject" and not value:
        return "(无主题)"
    return value


def _format_date(date_str: str) -> str:
    """Parse and format email date string."""
    if not date_str:
        return ""
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return date_str


def _extract_body(payload: dict, max_chars: int = _MAX_BODY_CHARS) -> str:
    """Recursively extract plain text body from a Gmail message payload.

    Gmail API returns MIME structure as nested dicts:
    - payload['mimeType'] == 'text/plain' → direct body
    - payload['parts'] → multipart, recurse
    - payload['body']['data'] → base64url encoded content
    """
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        try:
            text = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n...（全文共 {len(text)} 字符，已截断）"
            return text.strip()
        except Exception:
            return ""

    if mime_type == "text/html" and body_data:
        # Only used as fallback when no text/plain found
        try:
            text = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
            text = re.sub(r"<[^>]+>", "", text)
            text = html_mod.unescape(text)
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n...（全文共 {len(text)} 字符，已截断）"
            return text.strip()
        except Exception:
            return ""

    # Multipart: recurse into parts
    parts = payload.get("parts", [])
    plain_texts = []
    html_texts = []

    for part in parts:
        body = _extract_body(part, max_chars)
        if not body:
            continue
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            plain_texts.append(body)
        elif mime == "text/html":
            html_texts.append(body)
        elif mime.startswith("multipart/"):
            # Nested multipart — content already extracted by recursion
            plain_texts.append(body)

    if plain_texts:
        return "\n".join(plain_texts).strip()
    if html_texts:
        return "\n".join(html_texts).strip()

    return ""


def _get_header(headers: list, name: str) -> str:
    """Extract a header value by name from Gmail API headers list."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# ── Main tool entrypoint ─────────────────────────────────────────────

def execute_read_gmail_mail(
    max_results: int = 5,
    query: str = "",
    include_body: bool = True,
) -> str:
    """Read emails from Gmail via the Gmail REST API with OAuth 2.0.

    Supports Gmail search syntax in the ``query`` parameter:
        ``from:someone@gmail.com``
        ``subject:hello``
        ``after:2024/1/1``
        ``is:unread``
        ``has:attachment``
        Combinations: ``from:boss after:2024/6/1 is:unread``

    Args:
        max_results: Number of emails to return (1-20).
        query: Gmail search query string. Empty = inbox latest.
        include_body: Whether to include email body text.

    Returns:
        Formatted string with email list.
    """
    max_results = max(1, min(_MAX_RESULTS, max_results))

    try:
        service = _get_service()
    except FileNotFoundError as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Gmail OAuth 授权失败：{e}\n\n请打开 Settings → Gmail → 重新授权。"

    try:
        # Build search query
        q = query.strip()
        if not q:
            q = "in:inbox"

        # List messages
        result = (
            service.users()
            .messages()
            .list(userId="me", q=q, maxResults=max_results)
            .execute()
        )

        messages = result.get("messages", [])
        if not messages:
            return f"📭 无匹配邮件（查询：{q}）"

        result_parts = [f"📧 找到 {len(messages)} 封邮件（查询：{q}）\n"]

        for msg_meta in messages:
            try:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_meta["id"], format="full")
                    .execute()
                )

                headers = msg.get("payload", {}).get("headers", [])
                subject = _get_header_value(headers, "Subject")
                from_ = _get_header_value(headers, "From")
                date_str = _format_date(_get_header(headers, "Date"))
                to_ = _get_header_value(headers, "To")
                snippet = msg.get("snippet", "")
                labels = msg.get("labelIds", [])

                result_parts.append(f"📩 发件人: {from_}")
                result_parts.append(f"📎 主题: {subject}")
                result_parts.append(f"🕐 时间: {date_str}")
                if to_:
                    result_parts.append(f"👤 收件人: {to_}")

                # Label badges
                label_str = "、".join(labels) if labels else ""
                if label_str:
                    result_parts.append(f"🏷️ 标签: {label_str}")

                # Snippet (always available from list)
                if snippet:
                    result_parts.append(f"📋 摘要: {snippet}")

                # Full body (optional, more expensive)
                if include_body:
                    body_text = _extract_body(msg.get("payload", {}))
                    if body_text:
                        result_parts.append(f"📄 正文:\n{body_text}")

                result_parts.append("─" * 40)

            except Exception as e:
                result_parts.append(f"⚠️ 读取邮件时出错：{e}")
                result_parts.append("─" * 40)

        return "\n".join(result_parts).strip()

    except Exception as e:
        return f"❌ Gmail API 请求失败：{e}"


# ── Tool schema registration ─────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_gmail_mail",
            "description": (
                "读取 Gmail 邮箱中的邮件（Google Gmail REST API + OAuth 2.0）。"
                "支持 Gmail 强大的搜索语法，可通过 query 参数灵活过滤邮件：\n"
                "  - from:someone@gmail.com  按发件人\n"
                "  - subject:hello           按主题\n"
                "  - after:2024/1/1          按日期\n"
                "  - before:2024/12/31       截止日期\n"
                "  - is:unread               未读邮件\n"
                "  - is:read                 已读邮件\n"
                "  - has:attachment          含附件\n"
                "  - in:sent / in:drafts     指定文件夹\n"
                "  - label:xxx               按标签\n"
                "可组合使用，例如：from:boss after:2024/6/1 is:unread\n"
                "首次使用需在 Settings → Gmail 中完成 OAuth 授权。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "返回的最大邮件数量（1-20，默认 5）",
                        "default": 5,
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Gmail 搜索查询字符串。使用 Gmail 搜索语法：\n"
                            "  from:xxx — 按发件人过滤\n"
                            "  subject:xxx — 按主题过滤\n"
                            "  after:YYYY/M/D — 起始日期\n"
                            "  before:YYYY/M/D — 截止日期\n"
                            "  is:unread — 未读邮件\n"
                            "  has:attachment — 含附件\n"
                            "留空则返回收件箱最新邮件。"
                        ),
                        "default": "",
                    },
                    "include_body": {
                        "type": "boolean",
                        "description": "是否包含邮件正文。True 会完整下载邮件内容，仅在用户要求查看邮件详细内容时使用。",
                        "default": True,
                    },
                },
            },
        },
    },
]
