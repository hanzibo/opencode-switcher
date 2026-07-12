"""QQ Mail Reader — read emails from QQ mailbox via IMAP over SSL."""

import email
import imaplib
import os
import re
from email.header import decode_header
from email.utils import parsedate_to_datetime, parsedate_tz, mktime_tz
from typing import List


_QQMAIL_IMAP_SERVER = "imap.qq.com"
_QQMAIL_IMAP_PORT = 993


def _sort_ids_by_internaldate(mail, total, max_results):
    """Fetch INTERNALDATE for all messages and return the newest N IDs.

    QQ Mail's IMAP sequence numbers are NOT in chronological order, so
    sorting by sequence number is unreliable.  INTERNALDATE is the server-
    side arrival timestamp — a lightweight metadata fetch without body data.
    """
    status, idata = mail.fetch(f'1:{total}', '(INTERNALDATE)')
    if status != 'OK':
        return None

    date_pool = []
    for token in idata:
        if not isinstance(token, bytes):
            continue
        m = re.search(rb'(\d+)\s+\(INTERNALDATE\s+"([^"]+)"\)', token)
        if not m:
            continue
        eid = int(m.group(1))
        date_str = m.group(2).decode()
        try:
            dt = parsedate_tz(date_str)
        except Exception:
            continue
        if dt:
            ts = mktime_tz(dt)
            date_pool.append((ts, eid))

    if not date_pool:
        return None

    date_pool.sort(key=lambda x: x[0], reverse=True)
    n = min(max_results, len(date_pool))
    return [str(eid).encode() for _, eid in date_pool[:n]]


def execute_read_qq_mail(max_results: int = 5, folder: str = "INBOX",
                         search_criteria: str = "ALL",
                         include_body: bool = True) -> str:
    """Read emails from QQ mailbox via IMAP over SSL.

    Requires QQ mail IMAP authorization code configured in
    qq_mail_credentials.json or QQ_MAIL_AUTH_CODE env var.
    Uses stdlib imaplib + email — zero external dependencies."""
    from clipboard_store import QQMailCredentialsStore

    max_results = max(1, min(20, max_results))

    store = QQMailCredentialsStore()
    email_addr = store.email
    auth_code = store.auth_code
    if not email_addr:
        email_addr = os.environ.get("QQ_MAIL_EMAIL", "").strip()
    if not auth_code:
        auth_code = os.environ.get("QQ_MAIL_AUTH_CODE", "").strip()

    if not email_addr or not auth_code:
        return (
            "❌ QQ邮箱未配置。请先配置邮箱地址和授权码。\n\n"
            "配置步骤：\n"
            "1. 登录 QQ邮箱网页版 → 设置 → 账号与安全\n"
            "2. 开启「POP3/SMTP/IMAP 服务」\n"
            "3. 短信验证后获取 16 位授权码\n"
            "4. 编辑 ~/.config/opencode-switcher/qq_mail_credentials.json：\n"
            '   {\n'
            '       "version": 1,\n'
            '       "email": "yourname@qq.com",\n'
            '       "auth_code": "16位授权码"\n'
            '   }\n\n'
            "也可通过环境变量配置：\n"
            "  export QQ_MAIL_EMAIL=yourname@qq.com\n"
            "  export QQ_MAIL_AUTH_CODE=你的16位授权码"
        )

    try:
        mail = imaplib.IMAP4_SSL(_QQMAIL_IMAP_SERVER, _QQMAIL_IMAP_PORT)
        mail.login(email_addr, auth_code)
    except imaplib.IMAP4.error as e:
        return f"❌ QQ邮箱登录失败：{e}\n请检查邮箱地址和授权码是否正确。"
    except Exception as e:
        return f"❌ 连接 QQ邮箱失败：{e}\n请检查网络连接。"

    try:
        try:
            status, folder_data = mail.select(folder)
            if status != "OK":
                return f"❌ 无法打开文件夹「{folder}」"
        except imaplib.IMAP4.error:
            return f"❌ 文件夹「{folder}」不存在。"

        try:
            result, data = mail.search(None, search_criteria)
            if result != "OK" or not data[0]:
                return f"📭 收件箱无匹配邮件（条件：{search_criteria}）"
        except imaplib.IMAP4.error:
            return f"❌ 搜索条件无效：{search_criteria}"

        all_ids = data[0].split()
        total = len(all_ids)
        fetch_count = min(max_results, total)

        sorted_ids = _sort_ids_by_internaldate(mail, total, fetch_count)
        if sorted_ids is None:
            return f"❌ 无法获取邮件时间信息，共 {total} 封"

        result_parts = [f"📧 共 {total} 封匹配邮件，显示最新 {fetch_count} 封\n"]

        for eid in sorted_ids:
            try:
                _, fetch_data = mail.fetch(eid, "(RFC822)")
                raw_email = fetch_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = _decode_email_header(msg["Subject"])
                from_ = str(msg.get("From", "(未知发件人)"))
                date_str = _format_email_date(msg.get("Date", ""))

                result_parts.append(f"📩 发件人: {from_}")
                result_parts.append(f"📎 主题: {subject}")
                result_parts.append(f"🕐 时间: {date_str}")

                if include_body:
                    body_text = _extract_email_body(msg)
                    if body_text:
                        if len(body_text) > 500:
                            body_text = body_text[:500] + (
                                f"\n...（全文共 {len(body_text)} 字符，已截断）")
                        result_parts.append(f"📋 内容:\n{body_text}")

                result_parts.append("─" * 40)

            except Exception as e:
                result_parts.append(f"⚠️ 读取邮件时出错：{e}")
                result_parts.append("─" * 40)

        return "\n".join(result_parts).strip()

    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _decode_email_header(header_value: str) -> str:
    """Decode an email header that may be RFC 2047 encoded."""
    if not header_value:
        return "(无主题)"
    try:
        decoded_parts = decode_header(header_value)
        result = ""
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result += part.decode(charset or "utf-8", errors="replace")
            else:
                result += part
        return result
    except Exception:
        return str(header_value)


def _format_email_date(date_str: str) -> str:
    """Parse and format an email date string."""
    if not date_str:
        return ""
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return date_str


def _extract_email_body(msg: email.message.Message) -> str:
    """Extract plain text body from an email, preferring text/plain over text/html."""
    import html as html_mod

    if msg.is_multipart():
        plain_parts = []
        html_parts_hack = []
        for part in msg.walk():
            ct = part.get_content_type()
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if ct == "text/plain":
                    plain_parts.append(text)
                elif ct == "text/html":
                    html_parts_hack.append(text)
            except Exception:
                pass
        if plain_parts:
            return "\n".join(plain_parts).strip()
        elif html_parts_hack:
            text = re.sub(r"<[^>]+>", "", "\n".join(html_parts_hack))
            return html_mod.unescape(text).strip()
        return ""
    else:
        try:
            payload = msg.get_payload(decode=True)
            if not payload:
                return ""
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                text = re.sub(r"<[^>]+>", "", text)
                text = html_mod.unescape(text)
            return text.strip()
        except Exception:
            return ""


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_qq_mail",
            "description": "读取 QQ 邮箱中的邮件。通过 IMAP over SSL 连接 QQ 邮箱，支持指定文件夹和搜索条件，返回邮件的发件人、主题、时间和正文。仅用于读取 QQ 邮箱。不适用于发送邮件、管理邮箱设置或其他邮件服务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "返回的最大邮件数量（1-20，默认 5）",
                        "default": 5
                    },
                    "folder": {
                        "type": "string",
                        "description": "邮箱文件夹，如 INBOX、Sent Messages、Drafts 等",
                        "default": "INBOX"
                    },
                    "search_criteria": {
                        "type": "string",
                        "description": "IMAP 搜索条件，如 ALL、FROM someone、SUBJECT hello、SINCE 1-Jan-2024 等",
                        "default": "ALL"
                    },
                    "include_body": {
                        "type": "boolean",
                        "description": "是否包含邮件正文",
                        "default": True
                    }
                }
            }
        }
    },
]
