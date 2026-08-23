from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from kimi_client import (
    DEFAULT_MODEL,
    KIMI_CHAT_COMPLETIONS_URL,
    KIMI_ESTIMATE_TOKENS_URL,
    MAX_ESTIMATED_INPUT_TOKENS,
    KimiError,
    build_payload,
    chat_completion,
)


def _mock_response(data: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(data).encode("utf-8")
    response.__enter__.return_value = response
    return response


class BuildPayloadTests(unittest.TestCase):
    def test_builds_bounded_k3_payload(self):
        payload = build_payload(
            [
                {"role": "user", "content": " hello ", "model": "ignored"},
                {"role": "tool", "content": "ignored"},
            ]
        )

        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["max_completion_tokens"], 8_192)
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "hello"})
        self.assertNotIn("tools", payload)

    def test_includes_formula_tools_when_enabled(self):
        tool = {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web",
                "parameters": {"type": "object"},
            },
        }

        payload = build_payload([{"role": "user", "content": "news"}], tools=[tool])

        self.assertEqual(payload["tools"], [tool])
        self.assertEqual(payload["tool_choice"], "auto")

    def test_preserves_complete_kimi_assistant_message_for_multi_turn_chat(self):
        provider_message = {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "hidden reasoning state",
        }

        payload = build_payload(
            [
                {"role": "user", "content": "question"},
                {
                    "role": "assistant",
                    "content": "answer",
                    "provider_message": provider_message,
                },
            ]
        )

        self.assertEqual(payload["messages"][-1], provider_message)

    def test_builds_multi_image_content_in_original_order(self):
        first = b"first-image"
        second = b"second-image"

        payload = build_payload(
            [
                {
                    "role": "user",
                    "content": "compare",
                    "images": [
                        {"data": first, "mime_type": "image/jpeg"},
                        {"data": second, "mime_type": "image/webp"},
                    ],
                }
            ]
        )

        content = payload["messages"][1]["content"]
        self.assertEqual([part["type"] for part in content], ["image_url", "image_url", "text"])
        self.assertEqual(content[-1], {"type": "text", "text": "compare"})
        encoded_first = content[0]["image_url"]["url"].split(",", 1)[1]
        encoded_second = content[1]["image_url"]["url"].split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded_first), first)
        self.assertEqual(base64.b64decode(encoded_second), second)


class RequestTests(unittest.TestCase):
    @patch("kimi_client.urlopen")
    def test_estimates_visual_tokens_before_chat_request(self, mocked_urlopen: Mock):
        mocked_urlopen.side_effect = [
            _mock_response({"data": {"total_tokens": 12_345}}),
            _mock_response(
                {
                    "model": DEFAULT_MODEL,
                    "choices": [{"message": {"role": "assistant", "content": "נראה לעין"}}],
                }
            ),
        ]

        result = chat_completion(
            api_key="moonshot-secret",
            messages=[
                {
                    "role": "user",
                    "content": "מה רואים?",
                    "images": [{"data": b"jpeg-data", "mime_type": "image/jpeg"}],
                }
            ],
        )

        self.assertEqual(mocked_urlopen.call_count, 2)
        requests = [call.args[0] for call in mocked_urlopen.call_args_list]
        self.assertEqual(requests[0].full_url, KIMI_ESTIMATE_TOKENS_URL)
        self.assertEqual(requests[1].full_url, KIMI_CHAT_COMPLETIONS_URL)
        self.assertEqual(result.estimated_input_tokens, 12_345)

    @patch("kimi_client.urlopen")
    def test_rejects_visual_input_over_safe_context_budget(self, mocked_urlopen: Mock):
        mocked_urlopen.return_value = _mock_response(
            {"data": {"total_tokens": MAX_ESTIMATED_INPUT_TOKENS + 1}}
        )

        with self.assertRaisesRegex(KimiError, "safe context budget"):
            chat_completion(
                api_key="moonshot-secret",
                messages=[
                    {
                        "role": "user",
                        "content": "analyze",
                        "images": [{"data": b"jpeg-data", "mime_type": "image/jpeg"}],
                    }
                ],
            )

        self.assertEqual(mocked_urlopen.call_count, 1)

    @patch("kimi_client.urlopen")
    def test_sends_moonshot_key_only_in_authorization_header(self, mocked_urlopen: Mock):
        mocked_urlopen.return_value = _mock_response(
            {
                "model": DEFAULT_MODEL,
                "choices": [{"message": {"role": "assistant", "content": "שלום"}}],
            }
        )

        result = chat_completion(
            api_key="moonshot-secret",
            messages=[{"role": "user", "content": "היי"}],
        )

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, KIMI_CHAT_COMPLETIONS_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer moonshot-secret")
        self.assertNotIn("moonshot-secret", request.data.decode("utf-8"))
        self.assertEqual(result.content, "שלום")
        self.assertEqual(result.provider_message["content"], "שלום")

    @patch("kimi_client.urlopen")
    def test_executes_one_official_formula_search(self, mocked_urlopen: Mock):
        tool = {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for information",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
        arguments = json.dumps({"query": "latest Moonshot AI news"})
        first_response = {
            "model": DEFAULT_MODEL,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "I should search.",
                        "tool_calls": [
                            {
                                "id": "web_search:0",
                                "type": "function",
                                "function": {"name": "web_search", "arguments": arguments},
                            }
                        ],
                    }
                }
            ],
        }
        fiber_response = {
            "status": "succeeded",
            "context": {"encrypted_output": "----MOONSHOT ENCRYPTED BEGIN----result----END----"},
        }
        final_response = {
            "model": DEFAULT_MODEL,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "עדכון נוכחי עם [מקור](https://example.com/news).",
                    }
                }
            ],
        }
        mocked_urlopen.side_effect = [
            _mock_response({"tools": [tool]}),
            _mock_response(first_response),
            _mock_response(fiber_response),
            _mock_response(final_response),
        ]

        result = chat_completion(
            api_key="moonshot-secret",
            messages=[{"role": "user", "content": "מה חדש היום?"}],
            web_search=True,
        )

        self.assertEqual(mocked_urlopen.call_count, 4)
        requests = [call.args[0] for call in mocked_urlopen.call_args_list]
        self.assertTrue(requests[0].full_url.endswith("/formulas/moonshot/web-search:latest/tools"))
        self.assertEqual(requests[0].get_method(), "GET")
        self.assertEqual(requests[1].full_url, KIMI_CHAT_COMPLETIONS_URL)
        self.assertTrue(requests[2].full_url.endswith("/formulas/moonshot/web-search:latest/fibers"))
        self.assertEqual(requests[2].get_method(), "POST")
        self.assertEqual(requests[3].full_url, KIMI_CHAT_COMPLETIONS_URL)

        first_payload = json.loads(requests[1].data)
        fiber_payload = json.loads(requests[2].data)
        final_payload = json.loads(requests[3].data)
        self.assertEqual(first_payload["tools"], [tool])
        self.assertEqual(fiber_payload, {"name": "web_search", "arguments": arguments})
        assistant_message = next(
            message for message in final_payload["messages"] if message.get("tool_calls")
        )
        self.assertEqual(assistant_message["reasoning_content"], "I should search.")
        tool_message = next(message for message in final_payload["messages"] if message["role"] == "tool")
        self.assertEqual(tool_message["tool_call_id"], "web_search:0")
        self.assertIn("MOONSHOT ENCRYPTED", tool_message["content"])

        self.assertEqual(result.web_search_requests, 1)
        self.assertEqual(result.citations, ({"title": "מקור", "url": "https://example.com/news"},))

    def test_requires_moonshot_key(self):
        with self.assertRaisesRegex(KimiError, "MOONSHOT_API_KEY"):
            chat_completion(api_key="", messages=[{"role": "user", "content": "hello"}])

    @patch("kimi_client.urlopen", side_effect=TimeoutError)
    def test_timeout_is_user_safe(self, _urlopen: Mock):
        with self.assertRaisesRegex(KimiError, "timed out"):
            chat_completion(
                api_key="moonshot-secret",
                messages=[{"role": "user", "content": "hello"}],
            )


if __name__ == "__main__":
    unittest.main()
