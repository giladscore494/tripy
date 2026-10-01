"""Minimal GLM (Zhipu / Z.ai) client: chat completions with tools, and web search.

The model id is always supplied by the caller (env var or UI) so the same code
can compare different GLM models. No other provider is supported.

Base URL, chat path, search path and search engine are all settings: the
defaults below are starting points, not verified facts about the current API.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"
DEFAULT_CHAT_PATH = "chat/completions"
DEFAULT_SEARCH_PATH = "web_search"
DEFAULT_SEARCH_ENGINE = "search-prime"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class GLMError(RuntimeError):
    pass


@dataclass
class ChatResponse:
    message: dict
    finish_reason: str | None
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class GLMSettings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = ""
    chat_path: str = DEFAULT_CHAT_PATH
    search_path: str = DEFAULT_SEARCH_PATH
    search_engine: str = DEFAULT_SEARCH_ENGINE
    timeout_s: float = 240.0

    @classmethod
    def from_env(cls) -> "GLMSettings":
        return cls(
            api_key=os.environ.get("GLM_API_KEY", ""),
            base_url=os.environ.get("GLM_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("GLM_MODEL", ""),
            chat_path=os.environ.get("GLM_CHAT_PATH", DEFAULT_CHAT_PATH),
            search_path=os.environ.get("GLM_SEARCH_PATH", DEFAULT_SEARCH_PATH),
            search_engine=os.environ.get("GLM_SEARCH_ENGINE", DEFAULT_SEARCH_ENGINE),
        )


class GLMClient:
    def __init__(self, settings: GLMSettings, session: requests.Session | None = None,
                 max_retries: int = 3, sleeper: Callable[[float], None] = time.sleep):
        if not settings.api_key:
            raise GLMError("GLM_API_KEY is not set")
        if not settings.model:
            raise GLMError("No GLM model id selected")
        self.settings = settings
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.sleeper = sleeper

    @property
    def model(self) -> str:
        return self.settings.model

    def _url(self, path: str) -> str:
        """A path may be relative to base_url or a full URL of its own."""
        if path.startswith(("http://", "https://")):
            return path
        return self.settings.base_url.rstrip("/") + "/" + path.lstrip("/")

    def _post(self, path: str, payload: dict) -> dict:
        url = self._url(path)
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.post(url, headers=headers, data=json.dumps(payload),
                                         timeout=(15, self.settings.timeout_s))
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise GLMError(f"Invalid JSON from GLM: {exc}") from exc
                last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                if resp.status_code not in RETRY_STATUSES:
                    raise GLMError(last_error)
            if attempt < self.max_retries:
                self.sleeper(min(2 ** attempt * 2, 20))
        raise GLMError(last_error)

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float | None = None, max_tokens: int | None = None,
             extra: dict | None = None) -> ChatResponse:
        payload: dict[str, Any] = {"model": self.settings.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if extra:
            payload.update(extra)
        data = self._post(self.settings.chat_path or DEFAULT_CHAT_PATH, payload)
        choices = data.get("choices") or []
        if not choices:
            raise GLMError(f"GLM returned no choices: {json.dumps(data)[:500]}")
        choice = choices[0]
        return ChatResponse(
            message=choice.get("message") or {},
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage") or {},
            raw=data,
        )

    def web_search(self, query: str, count: int = 8, domain: str | None = None,
                   recency: str = "noLimit") -> list[dict]:
        payload: dict[str, Any] = {
            "search_engine": self.settings.search_engine,
            "search_query": query,
            "count": max(1, min(int(count), 50)),
            "search_recency_filter": recency,
        }
        if domain:
            payload["search_domain_filter"] = domain
        data = self._post(self.settings.search_path or DEFAULT_SEARCH_PATH, payload)
        results = []
        for item in data.get("search_result") or []:
            results.append({
                "title": item.get("title") or "",
                "url": item.get("link") or item.get("url") or "",
                "snippet": item.get("content") or item.get("snippet") or "",
                "site": item.get("media") or "",
                "published": item.get("publish_date") or "",
            })
        return results
