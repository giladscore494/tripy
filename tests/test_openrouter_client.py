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


def _mock_response(data: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(data).encode("utf-8")
    response.__enter__.return_value = response
    return response


class BuildPayloadTests(unittest.TestCase):
    def test_web_search_is_off_by_default(self):
        payload = build_payload([{"role": "user", "content": "hello"}])

        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_web_search_uses_free_client_side_function(self):
        payload = build_payload(
            [{"role": "user", "content": "latest news"}],
            web_search=True,
        )

        self.assertEqual(payload["tool_choice"], "auto")
        self.assertFalse(payload["parallel_tool_calls"])
        tool = payload["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["function"]["name"], "search_web")
        self.assertNotIn("openrouter:web_search", json.dumps(payload))

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
    def test_parses_model_and_unique_citations(self):
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
        }

        result = parse_chat_response(data, fallback_model=DEFAULT_MODEL)

        self.assertEqual(result.content, "Current answer")
        self.assertEqual(result.web_search_requests, 0)
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(result.citations[0]["title"], "Example")

    def test_rejects_empty_choices(self):
        with self.assertRaises(OpenRouterError):
            parse_chat_response({"choices": []}, fallback_model=DEFAULT_MODEL)


class RequestTests(unittest.TestCase):
    @patch("openrouter_client.urlopen")
    def test_sends_key_without_exposing_it_in_payload(self, mocked_urlopen: Mock):
        mocked_urlopen.return_value = _mock_response(
            {
                "choices": [{"message": {"content": "hello"}}],
                "model": DEFAULT_MODEL,
            }
        )

        chat_completion(
            api_key="secret-key",
            messages=[{"role": "user", "content": "hello"}],
        )

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        self.assertNotIn("secret-key", request.data.decode("utf-8"))

    @patch("openrouter_client.urlopen")
    def test_executes_one_free_search_and_returns_real_citations(self, mocked_urlopen: Mock):
        first_response = {
            "model": DEFAULT_MODEL,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_search_1",
                                "type": "function",
                                "function": {
                                    "name": "search_web",
                                    "arguments": json.dumps({"query": "latest North Korea test"}),
                                },
                            }
                        ],
                    }
                }
            ],
        }
        final_response = {
            "model": DEFAULT_MODEL,
            "choices": [
                {
                    "message": {
                        "content": "A current answer with a [source](https://example.com/news)."
                    }
                }
            ],
        }
        mocked_urlopen.side_effect = [_mock_response(first_response), _mock_response(final_response)]
        search_runner = Mock(
            return_value=[
                {
                    "title": "Example News",
                    "url": "https://example.com/news",
                    "snippet": "A current report",
                    "content": "Bounded page text",
                }
            ]
        )

        result = chat_completion(
            api_key="secret-key",
            messages=[{"role": "user", "content": "What happened today?"}],
            web_search=True,
            search_runner=search_runner,
        )

        search_runner.assert_called_once_with("latest North Korea test")
        self.assertEqual(mocked_urlopen.call_count, 2)
        self.assertEqual(result.web_search_requests, 1)
        self.assertEqual(result.citations, ({"title": "Example News", "url": "https://example.com/news"},))

        first_payload = json.loads(mocked_urlopen.call_args_list[0].args[0].data)
        final_payload = json.loads(mocked_urlopen.call_args_list[1].args[0].data)
        self.assertNotIn("openrouter:web_search", json.dumps(first_payload))
        self.assertEqual(final_payload["tool_choice"], "none")
        tool_message = next(message for message in final_payload["messages"] if message["role"] == "tool")
        self.assertEqual(tool_message["tool_call_id"], "call_search_1")
        self.assertIn("A current report", tool_message["content"])

    @patch("openrouter_client.urlopen", side_effect=TimeoutError)
    def test_timeout_is_user_safe(self, _urlopen: Mock):
        with self.assertRaisesRegex(OpenRouterError, "timed out"):
            chat_completion(
                api_key="secret-key",
                messages=[{"role": "user", "content": "hello"}],
            )


if __name__ == "__main__":
    unittest.main()
