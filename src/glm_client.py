"""Minimal GLM (Z.ai) client: chat completions with tools, and the standalone web search API.

The model id is always supplied by the caller (env var or UI) so the same code
can compare different GLM models. No other provider is supported.

Defaults match the official Z.ai documentation (base URL, POST /chat/completions,
POST /web_search, engine "search-prime"); every one of them stays configurable.
Z.ai Web Reader is intentionally not used: page reading is done by our own tools.
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
RAW_ERROR_BODY_LIMIT = 64 * 1024
# Response headers worth keeping on errors (request ids, rate limits). Never request headers.
ERROR_HEADER_HINTS = ("request", "trace", "ratelimit", "rate-limit", "retry-after", "content-type", "date")


class GLMError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None,
                 endpoint: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.endpoint = endpoint

    def as_dict(self) -> dict:
        return {"message": str(self), "status": self.status, "endpoint": self.endpoint, "body": self.body}


@dataclass
class ChatResponse:
    message: dict
    finish_reason: str | None
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    latency_ms: int = 0


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
            base_url=os.environ.get("GLM_BASE_URL") or DEFAULT_BASE_URL,
            model=os.environ.get("GLM_MODEL", ""),
            chat_path=os.environ.get("GLM_CHAT_PATH") or DEFAULT_CHAT_PATH,
            search_path=os.environ.get("GLM_SEARCH_PATH") or DEFAULT_SEARCH_PATH,
            search_engine=os.environ.get("GLM_SEARCH_ENGINE") or DEFAULT_SEARCH_ENGINE,
        )

    def public(self) -> dict:
        """Everything except the API key."""
        return {"model": self.model, "base_url": self.base_url, "chat_path": self.chat_path,
                "search_path": self.search_path, "search_engine": self.search_engine, "timeout_s": self.timeout_s}


class GLMClient:
    """`hook(kind, **data)` receives one `api_call` or `api_error` event per HTTP attempt."""

    def __init__(self, settings: GLMSettings, session: requests.Session | None = None,
                 max_retries: int = 3, sleeper: Callable[[float], None] = time.sleep,
                 hook: Callable[..., Any] | None = None):
        if not settings.api_key:
            raise GLMError("GLM_API_KEY is not set")
        if not settings.model:
            raise GLMError("No GLM model id selected")
        self.settings = settings
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.sleeper = sleeper
        self.hook = hook

    @property
    def model(self) -> str:
        return self.settings.model

    def _emit(self, kind: str, **data: Any) -> None:
        if self.hook:
            try:
                self.hook(kind, **data)
            except Exception:
                pass

    def _url(self, path: str) -> str:
        """A path may be relative to base_url or a full URL of its own."""
        if path.startswith(("http://", "https://")):
            return path
        return self.settings.base_url.rstrip("/") + "/" + path.lstrip("/")

    def _post(self, path: str, payload: dict) -> tuple[dict, int]:
        url = self._url(path)
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        last: GLMError | None = None
        for attempt in range(1, self.max_retries + 2):
            started = time.monotonic()
            try:
                resp = self.session.post(url, headers=headers, data=json.dumps(payload),
                                         timeout=(15, self.settings.timeout_s))
            except requests.RequestException as exc:
                latency = int((time.monotonic() - started) * 1000)
                last = GLMError(f"{type(exc).__name__}: {exc}", endpoint=path)
                self._emit("api_error", endpoint=path, attempt=attempt, latency_ms=latency, status=None,
                           error=str(last), body=None, headers={})
            else:
                latency = int((time.monotonic() - started) * 1000)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        body = resp.text[:RAW_ERROR_BODY_LIMIT]
                        self._emit("api_error", endpoint=path, attempt=attempt, latency_ms=latency, status=200,
                                   error="invalid_json", body=body, headers=self._error_headers(resp))
                        raise GLMError("Invalid JSON from GLM", status=200, body=body, endpoint=path)
                    self._emit("api_call", endpoint=path, attempt=attempt, latency_ms=latency, status=200)
                    return data, latency
                body = resp.text[:RAW_ERROR_BODY_LIMIT]
                last = GLMError(f"HTTP {resp.status_code} from {path}", status=resp.status_code, body=body,
                                endpoint=path)
                self._emit("api_error", endpoint=path, attempt=attempt, latency_ms=latency,
                           status=resp.status_code, error=str(last), body=body, headers=self._error_headers(resp))
                if resp.status_code not in RETRY_STATUSES:
                    raise last
            if attempt <= self.max_retries:
                self.sleeper(min(2 ** attempt, 20))
        assert last is not None
        raise last

    @staticmethod
    def _error_headers(resp) -> dict:
        return {k: v for k, v in (resp.headers or {}).items()
                if any(hint in k.lower() for hint in ERROR_HEADER_HINTS)}

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
        path = self.settings.chat_path or DEFAULT_CHAT_PATH
        data, latency = self._post(path, payload)
        choices = data.get("choices") or []
        if not choices:
            raise GLMError("GLM returned no choices", status=200, body=json.dumps(data)[:RAW_ERROR_BODY_LIMIT],
                           endpoint=path)
        choice = choices[0]
        return ChatResponse(
            message=choice.get("message") or {},
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage") or {},
            raw=data,
            latency_ms=latency,
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
        data, _ = self._post(self.settings.search_path or DEFAULT_SEARCH_PATH, payload)
        results = []
        for item in data.get("search_result") or []:
            results.append({
                "title": item.get("title") or "",
                "url": item.get("link") or "",
                "snippet": item.get("content") or "",
                "site": item.get("media") or "",
                "published": item.get("publish_date") or "",
                "icon": item.get("icon") or "",
                "refer": item.get("refer") or "",
            })
        return results
