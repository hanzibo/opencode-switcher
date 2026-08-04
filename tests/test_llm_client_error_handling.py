import unittest
from unittest.mock import MagicMock, patch
import requests
from ai_engine.llm_client import _LLMHttpClient, LLMRequestConfig, _extract_http_error_details


class TestLLMClientErrorHandling(unittest.TestCase):
    def test_build_request_headers_and_url_normalization(self):
        client = _LLMHttpClient()
        config = LLMRequestConfig(
            base_url="https://opencode.ai/zen/go/v1/ ",
            api_key=" sk-testkey123 ",
            model_name="deepseek-v4-flash",
        )
        url, headers, body = client._build_request(config, [], stream=True)
        self.assertEqual(url, "https://opencode.ai/zen/go/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-testkey123")
        self.assertEqual(headers["User-Agent"], "OpenCodeSwitcher/1.0 (Linux; GTK3)")

    def test_build_request_url_with_existing_chat_completions(self):
        client = _LLMHttpClient()
        config = LLMRequestConfig(
            base_url="https://opencode.ai/zen/go/v1/chat/completions",
            api_key="sk-key",
            model_name="gpt-4o",
        )
        url, headers, body = client._build_request(config, [], stream=True)
        self.assertEqual(url, "https://opencode.ai/zen/go/v1/chat/completions")

    def test_extract_http_error_details_json_string(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.json.return_value = {"error": "Invalid API key provided"}
        err = requests.exceptions.HTTPError(response=mock_resp)
        
        msg = _extract_http_error_details(err)
        self.assertEqual(msg, "HTTP 403: Invalid API key provided")

    def test_extract_http_error_details_json_detail(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.json.return_value = {"detail": "Forbidden: User-Agent not allowed"}
        err = requests.exceptions.HTTPError(response=mock_resp)
        
        msg = _extract_http_error_details(err)
        self.assertEqual(msg, "HTTP 403: Forbidden: User-Agent not allowed")

    def test_extract_http_error_details_html(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.json.side_effect = ValueError("Not JSON")
        mock_resp.text = "<html><head><title>403 Access Denied</title></head><body>Access Denied</body></html>"
        err = requests.exceptions.HTTPError(response=mock_resp)
        
        msg = _extract_http_error_details(err)
        self.assertEqual(msg, "HTTP 403: 403 Access Denied")


if __name__ == "__main__":
    unittest.main()
