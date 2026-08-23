from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b:free"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_HISTORY_MESSAGES = 40

SYSTEM_PROMPT = (
    "Answer in the same language as the user. Be concise, accurate, and explicit about uncertainty. "
    "When a web-search tool is available and the question depends on current information, use it. "
    "Cite web sources with Markdown links and never invent citations."
)


class OpenRouterError(RuntimeError):
    """A safe, user-facing OpenRouter request or response error."""


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    citations: tuple[dict[str, str], ...]
    web_search_requests: int


def build_payload(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    web_search: bool = False,
) -> dict[str, Any]:
    """Build a bounded Chat Completions payload for OpenRouter."""
    clean_messages: list[dict[str, str]] = []
    for message in messages[-MAX_HISTORY_MESSAGES:]:
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            clean_messages.append({"role": role, "content": content})

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *clean_messages],
        "max_tokens": 2_048,
    }

    if web_search:
        payload["tools"] = [
            {
                "type": "openrouter:web_search",
                "parameters": {
                    "engine": "parallel",
                    "mode": "basic",
                    "max_results": 3,
                    "max_total_results": 3,
                    "max_uses": 1,
                    "search_context_size": "low",
                },
            }
        ]
        payload["max_tool_calls"] = 1

    return payload


def _message_content(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def _citations(message: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    citations: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    annotations = message.get("annotations")
    if not isinstance(annotations, list):
        return ()

    for annotation in annotations:
        if not isinstance(annotation, Mapping) or annotation.get("type") != "url_citation":
            continue
        raw_citation = annotation.get("url_citation")
        if not isinstance(raw_citation, Mapping):
            continue
        url = raw_citation.get("url")
        if not isinstance(url, str) or not url or url in seen_urls:
            continue
        title = raw_citation.get("title")
        citations.append(
            {
                "url": url,
                "title": title.strip() if isinstance(title, str) and title.strip() else url,
            }
        )
        seen_urls.add(url)
    return tuple(citations)


def parse_chat_response(data: Mapping[str, Any], *, fallback_model: str) -> ChatResult:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise OpenRouterError("OpenRouter returned no completion choices.")

    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise OpenRouterError("OpenRouter returned an invalid assistant message.")

    content = _message_content(message)
    if not content:
        raise OpenRouterError("OpenRouter returned an empty assistant response.")

    model = data.get("model")
    if not isinstance(model, str) or not model:
        model = fallback_model

    web_search_requests = 0
    usage = data.get("usage")
    if isinstance(usage, Mapping):
        server_tool_use = usage.get("server_tool_use")
        if isinstance(server_tool_use, Mapping):
            raw_count = server_tool_use.get("web_search_requests", 0)
            if isinstance(raw_count, int) and raw_count >= 0:
                web_search_requests = raw_count

    return ChatResult(
        content=content,
        model=model,
        citations=_citations(message),
        web_search_requests=web_search_requests,
    )


def _error_message(raw_body: bytes, reason: str = "") -> str:
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return reason or "Unknown API error"

    if isinstance(data, Mapping):
        error = data.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
    return reason or "Unknown API error"


def chat_completion(
    *,
    api_key: str,
    messages: Sequence[Mapping[str, Any]],
    model: str = DEFAULT_MODEL,
    web_search: bool = False,
    app_url: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ChatResult:
    if not api_key.strip():
        raise OpenRouterError("OPENROUTER_API_KEY is missing.")

    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        "X-OpenRouter-Title": "Tripy Chat",
    }
    if app_url and app_url.strip():
        headers["HTTP-Referer"] = app_url.strip()

    request = Request(
        OPENROUTER_API_URL,
        data=json.dumps(build_payload(messages, model=model, web_search=web_search)).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read()
    except HTTPError as exc:
        raw_body = exc.read()
        detail = _error_message(raw_body, str(exc.reason))
        raise OpenRouterError(f"OpenRouter error {exc.code}: {detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise OpenRouterError("The OpenRouter request timed out. Please try again.") from exc
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise OpenRouterError("The OpenRouter request timed out. Please try again.") from exc
        raise OpenRouterError("Could not reach OpenRouter. Please try again.") from exc

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OpenRouterError("OpenRouter returned invalid JSON.") from exc
    if not isinstance(data, Mapping):
        raise OpenRouterError("OpenRouter returned an invalid response body.")
    return parse_chat_response(data, fallback_model=model)
