from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from openrouter_client import (
    DEFAULT_MODEL,
    OpenRouterError,
    build_payload,
    chat_completion,
    parse_chat_response,
)


class BuildPayloadTests(unittest.TestCase):
    def test_web_search_is_off_by_default(self):
        payload = build_payload([{"role": "user", "content": "hello"}])

        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertNotIn("tools", payload)
        self.assertNotIn("max_tool_calls", payload)

    def test_web_search_is_bounded(self):
        payload = build_payload(
            [{"role": "user", "content": "latest news"}],
            web_search=True,
        )

        self.assertEqual(payload["max_tool_calls"], 1)
        tool = payload["tools"][0]
        self.assertEqual(tool["type"], "openrouter:web_search")
        self.assertEqual(tool["parameters"]["max_uses"], 1)
        self.assertEqual(tool["parameters"]["max_total_results"], 3)

    def test_payload_filters_metadata_and_invalid_roles(self):
        payload = build_payload(
            [
                {"role": "user", "content": " question ", "model": "ignored"},
                {"role": "tool", "content": "ignored"},
                {"role": "assistant", "content": " answer ", "citations": []},
            ]
        )

        self.assertEqual(
            payload["messages"][1:],
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ],
        )


class ParseResponseTests(unittest.TestCase):
    def test_parses_model_search_usage_and_unique_citations(self):
        data = {
            "model": "stealth/ox-alpha",
            "choices": [
                {
                    "message": {
                        "content": "Current answer",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url_citation": {"title": "Example", "url": "https://example.com"},
                            },
                            {
                                "type": "url_citation",
                                "url_citation": {"title": "Duplicate", "url": "https://example.com"},
                            },
                        ],
                    }
                }
            ],
            "usage": {"server_tool_use": {"web_search_requests": 1}},
        }

        result = parse_chat_response(data, fallback_model=DEFAULT_MODEL)

        self.assertEqual(result.content, "Current answer")
        self.assertEqual(result.web_search_requests, 1)
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(result.citations[0]["title"], "Example")

    def test_rejects_empty_choices(self):
        with self.assertRaises(OpenRouterError):
            parse_chat_response({"choices": []}, fallback_model=DEFAULT_MODEL)


class RequestTests(unittest.TestCase):
    @patch("openrouter_client.urlopen")
    def test_sends_key_without_exposing_it_in_payload(self, mocked_urlopen: Mock):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "choices": [{"message": {"content": "hello"}}],
                "model": DEFAULT_MODEL,
            }
        ).encode("utf-8")
        mocked_urlopen.return_value.__enter__.return_value = response

        chat_completion(
            api_key="secret-key",
            messages=[{"role": "user", "content": "hello"}],
        )

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        self.assertNotIn("secret-key", request.data.decode("utf-8"))

    @patch("openrouter_client.urlopen", side_effect=TimeoutError)
    def test_timeout_is_user_safe(self, _urlopen: Mock):
        with self.assertRaisesRegex(OpenRouterError, "timed out"):
            chat_completion(
                api_key="secret-key",
                messages=[{"role": "user", "content": "hello"}],
            )


if __name__ == "__main__":
    unittest.main()
