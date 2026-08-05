"""SSRF guard for the web_fetch tool — validate URLs before any fetch.

Reusable, pure-stdlib validator (urllib.parse / ipaddress / socket).
No network I/O beyond a blocking socket.getaddrinfo lookup (no connection
is ever made here). Used by tool_registry/web.py to gate both the requests
path and the Obscura subprocess fallback.

Blocked targets:
  - non-http(s) schemes (file://, ftp://, javascript:, ...)
  - URLs carrying embedded credentials (user:pass@host)
  - hostless / malformed URLs
  - localhost / *.localhost hostnames
  - literal loopback, unspecified, link-local, multicast, RFC1918 private,
    IPv6 unique-local (fc00::/7) and other non-global addresses
  - hostnames whose DNS resolution yields ANY blocked address (all results
    are checked — mixed public/private answers are rejected)
"""

import ipaddress
import socket
import urllib.parse
from typing import Optional

# Networks that stdlib ipaddress does not consistently flag as non-global
# across Python versions (verified on 3.14: CGNAT/NAT64/site-local/6to4 are
# reported is_global=True there). Explicit membership checks are required.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT (RFC 6598)
    ipaddress.ip_network("192.0.0.0/24"),       # IETF protocol assignments (RFC 6890)
    ipaddress.ip_network("192.88.99.0/24"),     # 6to4 relay anycast (RFC 7526)
    ipaddress.ip_network("198.18.0.0/15"),      # benchmarking (RFC 2544/6815)
    ipaddress.ip_network("255.255.255.255/32"), # limited broadcast
    ipaddress.ip_network("fec0::/10"),          # IPv6 site-local (RFC 3879, deprecated)
    ipaddress.ip_network("64:ff9b::/96"),       # NAT64 well-known prefix (RFC 6052) — embeds IPv4
]

_ERROR_TEXTS = {
    "format": "无效的 URL 格式",
    "scheme": "仅支持 http/https 协议",
    "host": "URL 缺少主机名",
    "port": "URL 端口无效",
    "credentials": "URL 不允许携带用户名/密码凭据",
    "localhost": "不允许访问本地主机（localhost）",
    "loopback": "不允许访问回环地址",
    "unspecified": "不允许访问未指定地址",
    "link_local": "不允许访问链路本地地址",
    "multicast": "不允许访问组播地址",
    "private": "不允许访问私有或保留地址",
    "non_global": "不允许访问非公网地址",
    "dns": "无法解析域名，请检查 URL 是否正确",
}


def _getaddrinfo(host: str, port: int):
    """Indirection over socket.getaddrinfo so tests can patch name
    resolution without touching the real socket module."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _ip_error(ip: ipaddress._BaseAddress) -> Optional[str]:
    """Return a user-facing error message if `ip` must not be fetched, else None."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        # IPv4-mapped IPv6 (::ffff:a.b.c.d) — evaluate the embedded IPv4,
        # which is what the connection would actually reach.
        return _ip_error(ip.ipv4_mapped)
    if ip.is_loopback:
        return _ERROR_TEXTS["loopback"]
    if ip.is_unspecified:
        return _ERROR_TEXTS["unspecified"]
    if ip.is_link_local:
        return _ERROR_TEXTS["link_local"]
    if ip.is_multicast:
        return _ERROR_TEXTS["multicast"]
    if ip.is_private or ip.is_reserved:
        return _ERROR_TEXTS["private"]
    for net in _BLOCKED_NETWORKS:
        if ip.version == net.version and ip in net:
            return _ERROR_TEXTS["private"]
    if not ip.is_global:
        return _ERROR_TEXTS["non_global"]
    return None


def validate_public_http_url(url: str) -> Optional[str]:
    """Validate a URL as a fetchable public HTTP(S) target (SSRF guard).

    Returns None if the URL is safe to fetch, otherwise a user-facing
    error message explaining why it was rejected.

    The check runs before any cache lookup, HTTP request, or subprocess
    spawn, so invalid targets never reach the network or the browser.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except (ValueError, AttributeError):
        return _ERROR_TEXTS["format"]

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return f"{_ERROR_TEXTS['scheme']}，当前协议：{scheme or '（无）'}"

    if parsed.username is not None or parsed.password is not None:
        return _ERROR_TEXTS["credentials"]

    try:
        hostname = parsed.hostname
    except ValueError:
        return _ERROR_TEXTS["format"]
    if not hostname:
        return _ERROR_TEXTS["host"]

    try:
        port = parsed.port
    except ValueError:
        return _ERROR_TEXTS["port"]

    # Hostname fast-path: reject localhost by name even if DNS were mocked
    # or misconfigured to return something unusual.
    host_lower = hostname.rstrip(".").lower()
    if host_lower == "localhost" or host_lower.endswith(".localhost"):
        return _ERROR_TEXTS["localhost"]

    # Literal IP address — evaluated directly, no DNS involved.
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None:
        return _ip_error(ip)

    # Hostname — resolve and reject if ANY resolved address is non-public.
    # Checking every answer defends against mixed public/private DNS results.
    try:
        infos = _getaddrinfo(hostname, port or (443 if scheme == "https" else 80))
    except socket.gaierror:
        return _ERROR_TEXTS["dns"]
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        err = _ip_error(ip)
        if err is not None:
            return err
    return None
