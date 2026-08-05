"""Unit tests for the web_fetch SSRF guard (tool_registry/url_safety.py and web.py).

Deterministic — no real network I/O. DNS resolution is mocked at the
url_safety._getaddrinfo seam; fetch paths are mocked at the web module seam.
"""

import socket
import time
import unittest
from unittest import mock

import requests

from tool_registry import web as web_module
from tool_registry.url_safety import validate_public_http_url


def _addrinfo(*ips, port=80):
    """Build a socket.getaddrinfo-style return value for the given IPs."""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
        for ip in ips
    ]


class TestSchemeAndStructure(unittest.TestCase):
    """Non-http(s) schemes, hostless and malformed URLs."""

    def test_file_scheme_rejected(self):
        for url in ("file:///etc/passwd", "file:///tmp/x", "file://localhost/etc/passwd"):
            err = validate_public_http_url(url)
            self.assertIsNotNone(err)
            self.assertIn("协议", err)

    def test_other_schemes_rejected(self):
        for url in ("ftp://example.com/x", "javascript:alert(1)", "gopher://example.com/",
                    "data:text/plain,hi", "ws://example.com/"):
            err = validate_public_http_url(url)
            self.assertIsNotNone(err)
            self.assertIn("协议", err)

    def test_missing_scheme_rejected(self):
        for url in ("example.com/path", "//example.com/path", ""):
            err = validate_public_http_url(url)
            self.assertIsNotNone(err)
            self.assertIn("协议", err)

    def test_hostless_rejected(self):
        for url in ("http://", "https:///path", "http://?q=1"):
            self.assertIsNotNone(validate_public_http_url(url))

    def test_malformed_url_rejected(self):
        self.assertIsNotNone(validate_public_http_url("http://[::1"))
        self.assertIsNotNone(validate_public_http_url(123))
        self.assertIsNotNone(validate_public_http_url(None))

    def test_invalid_port_rejected(self):
        err = validate_public_http_url("http://example.com:99999/")
        self.assertIsNotNone(err)
        self.assertIn("端口", err)

    def test_credentials_rejected(self):
        for url in ("https://user:pass@example.com/", "https://user@example.com/",
                    "https://:pass@example.com/", "http://user%40x:pw@example.com/"):
            err = validate_public_http_url(url)
            self.assertIsNotNone(err)
            self.assertIn("凭据", err)


class TestLiteralAddresses(unittest.TestCase):
    """Literal IP addresses — no DNS involved."""

    def assert_blocked(self, url, expect=None):
        err = validate_public_http_url(url)
        self.assertIsNotNone(err, url)
        if expect:
            self.assertIn(expect, err, url)

    def test_loopback_blocked(self):
        for url in ("http://127.0.0.1/", "http://127.0.0.2/", "http://127.1.2.3/",
                    "http://127.0.0.1:8080/admin", "http://[::1]/"):
            self.assert_blocked(url, "回环")

    def test_unspecified_blocked(self):
        self.assert_blocked("http://0.0.0.0/", "未指定")
        self.assert_blocked("http://[::]/", "未指定")

    def test_rfc1918_private_blocked(self):
        for url in ("http://10.0.0.1/", "http://10.255.255.255/",
                    "http://172.16.0.1/", "http://172.31.255.255/",
                    "http://192.168.1.1/", "http://192.168.0.1/admin"):
            self.assert_blocked(url)

    def test_link_local_blocked(self):
        # 169.254.169.254 is the classic cloud metadata endpoint.
        self.assert_blocked("http://169.254.169.254/latest/meta-data/", "链路本地")
        self.assert_blocked("http://[fe80::1]/", "链路本地")

    def test_unique_local_ipv6_blocked(self):
        self.assert_blocked("http://[fc00::1]/")
        self.assert_blocked("http://[fd12:3456:789a::1]/")

    def test_cgnat_and_other_reserved_ranges_blocked(self):
        self.assert_blocked("http://100.64.0.1/")       # CGNAT
        self.assert_blocked("http://192.0.0.1/")        # IETF protocol assignments
        self.assert_blocked("http://192.88.99.1/")      # 6to4 relay anycast
        self.assert_blocked("http://198.18.0.1/")       # benchmarking
        self.assert_blocked("http://224.0.0.1/")        # multicast
        self.assert_blocked("http://240.0.0.1/")        # reserved
        self.assert_blocked("http://255.255.255.255/")  # broadcast

    def test_ipv4_mapped_ipv6_blocked(self):
        # ::ffff:a.b.c.d must be evaluated as the embedded IPv4.
        self.assert_blocked("http://[::ffff:127.0.0.1]/", "回环")
        self.assert_blocked("http://[::ffff:10.0.0.1]/", "私有")
        self.assert_blocked("http://[::ffff:169.254.169.254]/", "链路本地")

    def test_nat64_prefix_blocked(self):
        # 64:ff9b::/96 embeds an IPv4 in the low 32 bits (7f00:1 = 127.0.0.1, a00:1 = 10.0.0.1).
        self.assert_blocked("http://[64:ff9b::7f00:1]/")
        self.assert_blocked("http://[64:ff9b::a00:1]/")

    def test_site_local_ipv6_blocked(self):
        self.assert_blocked("http://[fec0::1]/")

    def test_public_literal_addresses_allowed(self):
        for url in ("http://93.184.216.34/", "http://8.8.8.8/",
                    "http://172.32.0.1/",  # just outside 172.16/12
                    "https://[2606:2800:220:1:248:1893:25c8:1946]/"):
            self.assertIsNone(validate_public_http_url(url), url)


class TestHostnameResolution(unittest.TestCase):
    """Hostnames resolved via the mocked url_safety._getaddrinfo seam."""

    def test_public_hostname_allowed(self):
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        return_value=_addrinfo("93.184.216.34")):
            self.assertIsNone(validate_public_http_url("https://example.com/"))

    def test_hostname_resolving_to_private_blocked(self):
        for ips in (("10.0.0.5",), ("127.0.0.1",), ("169.254.169.254",),
                    ("192.168.1.10",), ("::1",), ("fc00::1",)):
            with mock.patch("tool_registry.url_safety._getaddrinfo",
                            return_value=_addrinfo(*ips)):
                self.assertIsNotNone(validate_public_http_url("https://evil.example/"))

    def test_mixed_public_and_private_answers_blocked(self):
        # Even one private answer in the set must reject the URL.
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        return_value=_addrinfo("93.184.216.34", "10.0.0.5")):
            self.assertIsNotNone(validate_public_http_url("https://evil.example/"))

    def test_multiple_public_answers_allowed(self):
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        return_value=_addrinfo("93.184.216.34", "8.8.8.8")):
            self.assertIsNone(validate_public_http_url("https://example.com/"))

    def test_dns_failure_rejected(self):
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        side_effect=socket.gaierror("name or service not known")):
            err = validate_public_http_url("https://nxdomain.example/")
        self.assertIsNotNone(err)
        self.assertIn("解析", err)

    def test_localhost_name_rejected_without_dns(self):
        # The name fast-path must not trigger any resolution.
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        side_effect=AssertionError("DNS must not be called")):
            self.assertIsNotNone(validate_public_http_url("http://localhost/"))
            self.assertIsNotNone(validate_public_http_url("http://localhost:8080/x"))
            self.assertIsNotNone(validate_public_http_url("http://sub.localhost/"))
            self.assertIsNotNone(validate_public_http_url("http://localhost./"))

    def test_trailing_dot_fqdn_allowed(self):
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        return_value=_addrinfo("93.184.216.34")):
            self.assertIsNone(validate_public_http_url("https://example.com./"))

    def test_port_passed_to_resolution(self):
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        return_value=_addrinfo("93.184.216.34", port=8080)) as ga:
            self.assertIsNone(validate_public_http_url("http://example.com:8080/x"))
        self.assertEqual(ga.call_args[0][1], 8080)
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        return_value=_addrinfo("93.184.216.34", port=443)) as ga2:
            self.assertIsNone(validate_public_http_url("https://example.com/x"))
        self.assertEqual(ga2.call_args[0][1], 443)

    def test_idn_hostname_allowed(self):
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        return_value=_addrinfo("93.184.216.34")):
            self.assertIsNone(validate_public_http_url("https://bücher.example/"))


class _FakeResponse:
    def __init__(self, status_code, location=None):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}


class TestRedirectValidation(unittest.TestCase):
    """Manual redirect following with per-hop SSRF validation."""

    def test_public_redirect_followed(self):
        with mock.patch.object(web_module, "_run_request_cancellable", side_effect=[
                (_FakeResponse(302, "/new"), False),
                (_FakeResponse(200), False),
            ]), mock.patch("tool_registry.url_safety._getaddrinfo",
                           return_value=_addrinfo("93.184.216.34")):
            resp, cancelled = web_module._requests_get_safe(
                "https://example.com/a", {}, 10, None)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(cancelled)

    def test_redirect_to_private_blocked(self):
        with mock.patch.object(web_module, "_run_request_cancellable", side_effect=[
                (_FakeResponse(302, "http://127.0.0.1/admin"), False),
            ]), mock.patch("tool_registry.url_safety._getaddrinfo",
                           return_value=_addrinfo("93.184.216.34")):
            with self.assertRaises(web_module.WebFetchValidationError) as ctx:
                web_module._requests_get_safe("https://example.com/a", {}, 10, None)
        self.assertIn("回环", str(ctx.exception))

    def test_redirect_to_file_scheme_blocked(self):
        with mock.patch.object(web_module, "_run_request_cancellable", side_effect=[
                (_FakeResponse(302, "file:///etc/passwd"), False),
            ]), mock.patch("tool_registry.url_safety._getaddrinfo",
                           return_value=_addrinfo("93.184.216.34")):
            with self.assertRaises(web_module.WebFetchValidationError):
                web_module._requests_get_safe("https://example.com/a", {}, 10, None)

    def test_redirect_to_dns_private_hostname_blocked(self):
        with mock.patch.object(web_module, "_run_request_cancellable", side_effect=[
                (_FakeResponse(302, "https://internal.example/secret"), False),
            ]), mock.patch("tool_registry.url_safety._getaddrinfo",
                           return_value=_addrinfo("10.0.0.5")):
            with self.assertRaises(web_module.WebFetchValidationError):
                web_module._requests_get_safe("https://example.com/a", {}, 10, None)

    def test_redirect_chain_limit(self):
        hops = [(_FakeResponse(302, "/x"), False)] * (web_module._MAX_REDIRECTS + 1)
        with mock.patch.object(web_module, "_run_request_cancellable",
                               side_effect=hops), mock.patch(
            "tool_registry.url_safety._getaddrinfo",
            return_value=_addrinfo("93.184.216.34")):
            with self.assertRaises(requests.TooManyRedirects):
                web_module._requests_get_safe("https://example.com/a", {}, 10, None)


class TestExecuteWebFetchGuard(unittest.TestCase):
    """execute_web_fetch: validation happens before cache and fetch paths."""

    def setUp(self):
        web_module._FETCH_CACHE.clear()

    def test_blocked_url_rejected_before_cache_and_fetch(self):
        with mock.patch.object(web_module, "_get_cached_fetch",
                               side_effect=AssertionError("cache must not be queried")), \
             mock.patch.object(web_module, "_try_requests_fetch",
                               side_effect=AssertionError("must not fetch")), \
             mock.patch.object(web_module, "_try_obscura_fetch",
                               side_effect=AssertionError("must not fetch")):
            res = web_module.execute_web_fetch("http://127.0.0.1/admin")
        self.assertTrue(res.startswith("获取页面失败"))
        self.assertNotIn("http://127.0.0.1/admin", web_module._FETCH_CACHE)

    def test_blocked_scheme_url_rejected(self):
        with mock.patch.object(web_module, "_get_cached_fetch",
                               side_effect=AssertionError("cache must not be queried")):
            res = web_module.execute_web_fetch("file:///etc/passwd")
        self.assertTrue(res.startswith("获取页面失败"))
        self.assertIn("协议", res)

    def test_public_url_fetched_and_cached(self):
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        return_value=_addrinfo("93.184.216.34")), \
             mock.patch.object(web_module, "_try_requests_fetch", return_value="page text"), \
             mock.patch.object(web_module, "_try_obscura_fetch") as obs:
            res = web_module.execute_web_fetch("https://example.com/page")
        self.assertEqual(res, "page text")
        obs.assert_not_called()
        self.assertIn("https://example.com/page", web_module._FETCH_CACHE)

    def test_cached_public_url_returned(self):
        web_module._FETCH_CACHE["https://example.com/"] = (time.monotonic(), "cached text")
        with mock.patch("tool_registry.url_safety._getaddrinfo",
                        return_value=_addrinfo("93.184.216.34")), \
             mock.patch.object(web_module, "_try_requests_fetch",
                               side_effect=AssertionError("must not fetch")):
            res = web_module.execute_web_fetch("https://example.com/")
        self.assertEqual(res, "cached text")

    def test_private_redirect_does_not_fall_through_to_obscura(self):
        with mock.patch.object(web_module, "_run_request_cancellable", side_effect=[
                (_FakeResponse(302, "http://192.168.1.1/"), False),
            ]), mock.patch("tool_registry.url_safety._getaddrinfo",
                           return_value=_addrinfo("93.184.216.34")), \
             mock.patch.object(web_module, "_try_obscura_fetch") as obs:
            res = web_module.execute_web_fetch("https://example.com/a")
        self.assertTrue(res.startswith("获取页面失败"))
        self.assertIn("重定向", res)
        obs.assert_not_called()
        self.assertNotIn("https://example.com/a", web_module._FETCH_CACHE)


if __name__ == "__main__":
    unittest.main()
