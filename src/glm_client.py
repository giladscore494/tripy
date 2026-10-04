"""Minimal GLM (Z.ai) client: chat completions with tools, and the standalone web search API.

The model id is always supplied by the caller (env var or UI) so the same code
can compare different GLM models. No other provider is supported. An optional
finalizer model id (GLM_FINALIZER_MODEL) is used only for the compact
finalization call; research turns always use the main model id.

Attempt semantics are explicit: GLM_CHAT_MAX_ATTEMPTS / GLM_SEARCH_MAX_ATTEMPTS
are the TOTAL number of HTTP attempts per request (1 = no retry). Every attempt
is reported separately. A timed-out or dropped chat request has an unknown
billing outcome (the provider may have processed it), so such attempts are
flagged `usage_unknown`.

Defaults match the official Z.ai documentation (base URL, POST /chat/completions,
POST /web_search, engine "search-prime"); every one of them stays configurable.
Z.ai Web Reader is intentionally not used: page reading is done by our own tools.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .concurrency import (RATE_LIMIT_BACKOFF_BASE_S, RATE_LIMIT_BACKOFF_MAX_S, RATE_LIMIT_BUDGET_S,
                          RATE_LIMIT_RETRY_AFTER_MAX_S, ConcurrencyController, default_controller)

DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"
DEFAULT_CHAT_PATH = "chat/completions"
DEFAULT_SEARCH_PATH = "web_search"
DEFAULT_SEARCH_ENGINE = "search-prime"
DEFAULT_CHAT_MAX_ATTEMPTS = 2       # expensive: at most two full chat/completions attempts by default
DEFAULT_SEARCH_MAX_ATTEMPTS = 3
DEFAULT_CHAT_TIMEOUT_S = 240.0
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
        self.timeout = False
        self.usage_unknown = False
        self.rate_limited = False
        self.attempts: int | None = None

    def as_dict(self) -> dict:
        return {"message": str(self), "status": self.status, "endpoint": self.endpoint, "body": self.body,
                "timeout": self.timeout, "usage_unknown": self.usage_unknown, "attempts": self.attempts,
                "rate_limited": self.rate_limited}


@dataclass
class ChatResponse:
    message: dict
    finish_reason: str | None
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    latency_ms: int = 0


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return max(minimum, int(raw)) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass
class GLMSettings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = ""
    chat_path: str = DEFAULT_CHAT_PATH
    search_path: str = DEFAULT_SEARCH_PATH
    search_engine: str = DEFAULT_SEARCH_ENGINE
    timeout_s: float = DEFAULT_CHAT_TIMEOUT_S        # read timeout per HTTP attempt
    finalizer_model: str = ""                         # "" = use `model` for finalization too
    chat_max_attempts: int = DEFAULT_CHAT_MAX_ATTEMPTS
    search_max_attempts: int = DEFAULT_SEARCH_MAX_ATTEMPTS

    @classmethod
    def from_env(cls) -> "GLMSettings":
        return cls(
            api_key=os.environ.get("GLM_API_KEY", ""),
            base_url=os.environ.get("GLM_BASE_URL") or DEFAULT_BASE_URL,
            model=os.environ.get("GLM_MODEL", ""),
            chat_path=os.environ.get("GLM_CHAT_PATH") or DEFAULT_CHAT_PATH,
            search_path=os.environ.get("GLM_SEARCH_PATH") or DEFAULT_SEARCH_PATH,
            search_engine=os.environ.get("GLM_SEARCH_ENGINE") or DEFAULT_SEARCH_ENGINE,
            timeout_s=_env_float("GLM_CHAT_TIMEOUT_S", DEFAULT_CHAT_TIMEOUT_S),
            finalizer_model=(os.environ.get("GLM_FINALIZER_MODEL") or "").strip(),
            chat_max_attempts=_env_int("GLM_CHAT_MAX_ATTEMPTS", DEFAULT_CHAT_MAX_ATTEMPTS),
            search_max_attempts=_env_int("GLM_SEARCH_MAX_ATTEMPTS", DEFAULT_SEARCH_MAX_ATTEMPTS),
        )

    @property
    def effective_finalizer_model(self) -> str:
        return self.finalizer_model or self.model

    def public(self) -> dict:
        """Everything except the API key."""
        return {"model": self.model, "research_model": self.model,
                "finalizer_model": self.effective_finalizer_model,
                "finalizer_model_source": "GLM_FINALIZER_MODEL" if self.finalizer_model else "GLM_MODEL",
                "base_url": self.base_url, "chat_path": self.chat_path,
                "search_path": self.search_path, "search_engine": self.search_engine, "timeout_s": self.timeout_s,
                "chat_max_attempts": self.chat_max_attempts, "search_max_attempts": self.search_max_attempts,
                "attempt_semantics": "max_attempts = total HTTP attempts per request (1 = no retry)"}


class GLMClient:
    """`hook(kind, **data)` receives one `api_call` or `api_error` event per HTTP attempt.

    Every event carries `request_kind` ("chat" | "search"), `attempt` and
    `max_attempts`. `api_error` events also carry `timeout` and `usage_unknown`.

    Concurrency: every HTTP attempt holds one slot of the shared ConcurrencyController
    (src/concurrency.py) while it is in flight: the pool of the ACTUAL model id of that request
    for chat, the Search-Prime pool for web_search. `activity_hook(kind, **data)` receives the
    request lifecycle (`model_queue_wait_started`, `model_slot_acquired`, `model_request_started`,
    `model_request_finished` and the `search_*` equivalents); it is separate from `hook` so the
    per-attempt api_call / api_error accounting stays exactly as it was.

    A client is cheap and holds per-run mutable state (hooks, session): create one per vehicle
    worker from the same immutable GLMSettings and share only the controller.
    """

    def __init__(self, settings: GLMSettings, session: requests.Session | None = None,
                 sleeper: Callable[[float], None] = time.sleep, hook: Callable[..., Any] | None = None,
                 concurrency: ConcurrencyController | None = None,
                 activity_hook: Callable[..., Any] | None = None, cancel_event=None):
        if not settings.api_key:
            raise GLMError("GLM_API_KEY is not set")
        if not settings.model:
            raise GLMError("No GLM model id selected")
        self.settings = settings
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.hook = hook
        self.activity_hook = activity_hook
        self.concurrency = concurrency or default_controller()
        self.cancel_event = cancel_event
        self.rate_limit_budget_s = RATE_LIMIT_BUDGET_S
        self.jitter: Callable[[], float] = random.random

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def finalizer_model(self) -> str:
        return self.settings.effective_finalizer_model

    def _emit(self, kind: str, **data: Any) -> None:
        if self.hook:
            try:
                self.hook(kind, **data)
            except Exception:
                pass

    def _activity(self, kind: str, **data: Any) -> None:
        if self.activity_hook:
            try:
                self.activity_hook(kind, **data)
            except Exception:
                pass

    def _url(self, path: str) -> str:
        """A path may be relative to base_url or a full URL of its own."""
        if path.startswith(("http://", "https://")):
            return path
        return self.settings.base_url.rstrip("/") + "/" + path.lstrip("/")

    def _post(self, path: str, payload: dict, request_kind: str,
              timeout_s: float | None = None, max_attempts: int | None = None) -> tuple[dict, int]:
        url = self._url(path)
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        if not max_attempts:
            max_attempts = self.settings.chat_max_attempts if request_kind == "chat" \
                else self.settings.search_max_attempts
        max_attempts = max(1, int(max_attempts))
        common = {"endpoint": path, "request_kind": request_kind, "max_attempts": max_attempts}
        if request_kind == "chat":
            common["model"] = payload.get("model")
        last: GLMError | None = None
        rate_waited = 0.0          # H8: time this request spent waiting out 429s (never counted as an attempt)
        rate_limited = 0
        attempt, spent = 0, 0
        while spent < max_attempts:
            attempt += 1
            spent += 1
            will_retry = spent < max_attempts
            resp, failure, latency = self._attempt(url, headers, payload, request_kind, attempt, max_attempts,
                                                   timeout_s)
            if failure is None and resp.status_code == 429:
                # H8: a 429 lowers the shared pool's limit and is retried after Retry-After / a backoff without
                # spending an attempt, while RATE_LIMIT_BUDGET_S of rate-limit waiting remains
                rate_limited += 1
                wait = self._rate_limit_wait(resp, rate_limited)
                retry = rate_waited + wait <= self.rate_limit_budget_s
                limit = self.concurrency.note_rate_limited(request_kind, payload.get("model"), retried=retry)
                body = resp.text[:RAW_ERROR_BODY_LIMIT]
                last = GLMError(f"HTTP 429 from {path}", status=429, body=body, endpoint=path)
                last.rate_limited = True
                self._emit("api_error", **common, attempt=attempt, latency_ms=latency, status=429,
                           error=str(last), error_type="http_429", timeout=False, usage_unknown=False,
                           will_retry=retry, body=body, headers=self._error_headers(resp), rate_limited=True,
                           rate_limit_wait_s=round(wait, 2) if retry else None, concurrency_limit=limit)
                if not retry:
                    last.attempts = attempt
                    raise last
                spent -= 1
                rate_waited += wait
                self.sleeper(wait)
                continue
            if failure is None and resp.status_code == 200:
                self.concurrency.note_success(request_kind, payload.get("model"))
            if failure is not None:
                exc = failure
                timeout = isinstance(exc, requests.Timeout)
                # A connect timeout never reached the provider; anything else may have been processed.
                usage_unknown = not isinstance(exc, requests.ConnectTimeout)
                last = GLMError(f"{type(exc).__name__}: {exc}", endpoint=path)
                last.timeout, last.usage_unknown = timeout, usage_unknown
                self._emit("api_error", **common, attempt=attempt, latency_ms=latency, status=None,
                           error=str(last), error_type=type(exc).__name__, timeout=timeout,
                           usage_unknown=usage_unknown, will_retry=will_retry, body=None, headers={})
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        body = resp.text[:RAW_ERROR_BODY_LIMIT]
                        self._emit("api_error", **common, attempt=attempt, latency_ms=latency, status=200,
                                   error="invalid_json", error_type="invalid_json", timeout=False,
                                   usage_unknown=True, will_retry=False, body=body,
                                   headers=self._error_headers(resp))
                        raise GLMError("Invalid JSON from GLM", status=200, body=body, endpoint=path)
                    self._emit("api_call", **common, attempt=attempt, latency_ms=latency, status=200)
                    return data, latency
                body = resp.text[:RAW_ERROR_BODY_LIMIT]
                retryable = resp.status_code in RETRY_STATUSES
                last = GLMError(f"HTTP {resp.status_code} from {path}", status=resp.status_code, body=body,
                                endpoint=path)
                self._emit("api_error", **common, attempt=attempt, latency_ms=latency, status=resp.status_code,
                           error=str(last), error_type=f"http_{resp.status_code}", timeout=False,
                           usage_unknown=False, will_retry=will_retry and retryable, body=body,
                           headers=self._error_headers(resp))
                if not retryable:
                    raise last
            if will_retry:
                self.sleeper(min(2 ** spent, 20))
        assert last is not None
        last.attempts = attempt
        raise last

    def _rate_limit_wait(self, resp, count: int) -> float:
        """Seconds to wait after the `count`-th 429 of a request: Retry-After when the provider sends one (seconds or
        an HTTP date, clamped), else an exponential backoff with up to 25 % jitter."""
        raw = str((resp.headers or {}).get("Retry-After") or "").strip()
        if raw:
            try:
                return max(0.0, min(float(raw), RATE_LIMIT_RETRY_AFTER_MAX_S))
            except ValueError:
                try:
                    from email.utils import parsedate_to_datetime
                    from datetime import datetime, timezone

                    delta = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
                    return max(0.0, min(delta, RATE_LIMIT_RETRY_AFTER_MAX_S))
                except (TypeError, ValueError, OverflowError):
                    pass
        base = min(RATE_LIMIT_BACKOFF_BASE_S * (2 ** (count - 1)), RATE_LIMIT_BACKOFF_MAX_S)
        return base * (1 + 0.25 * self.jitter())

    def _attempt(self, url: str, headers: dict, payload: dict, request_kind: str, attempt: int,
                 max_attempts: int,
                 timeout_s: float | None = None) -> tuple[Any, requests.RequestException | None, int]:
        """ONE HTTP attempt inside one concurrency slot. Returns (response, request exception, latency_ms).

        The slot is released as soon as the response (or failure) is back: never during the retry
        sleep, response parsing by the caller, tool execution or anything else."""
        prefix = "model" if request_kind == "chat" else "search"
        info = {"request_kind": request_kind, "attempt": attempt, "max_attempts": max_attempts}
        model = payload.get("model") if request_kind == "chat" else None
        if model:
            info["model"] = model
        resp, failure, latency = None, None, 0
        with self.concurrency.slot(request_kind, model, emit=self._activity, cancel=self.cancel_event) as slot:
            self._activity(f"{prefix}_request_started", **info, active=slot["active"], limit=slot["limit"],
                           wait_ms=slot["wait_ms"])
            started = time.monotonic()
            try:
                resp = self.session.post(url, headers=headers, data=json.dumps(payload),
                                         timeout=(15, timeout_s or self.settings.timeout_s))
            except requests.RequestException as exc:
                failure = exc
            finally:
                latency = int((time.monotonic() - started) * 1000)
        self._activity(f"{prefix}_request_finished", **info, latency_ms=latency,
                       status=getattr(resp, "status_code", None), ok=failure is None
                       and getattr(resp, "status_code", None) == 200,
                       error_type=type(failure).__name__ if failure is not None else None)
        return resp, failure, latency

    @staticmethod
    def _error_headers(resp) -> dict:
        return {k: v for k, v in (resp.headers or {}).items()
                if any(hint in k.lower() for hint in ERROR_HEADER_HINTS)}

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float | None = None, max_tokens: int | None = None,
             extra: dict | None = None, model: str | None = None, timeout_s: float | None = None,
             max_attempts: int | None = None) -> ChatResponse:
        """`model` overrides the configured model id for this one call (the finalizer, a phase override);
        `timeout_s` the read timeout per attempt of this one call (a phase override); `max_attempts` the total HTTP
        attempts of this one call (a phase override; None = GLM_CHAT_MAX_ATTEMPTS). Retry semantics are unchanged."""
        payload: dict[str, Any] = {"model": model or self.settings.model, "messages": messages}
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
        data, latency = self._post(path, payload, "chat", timeout_s=timeout_s, max_attempts=max_attempts)
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
        data, _ = self._post(self.settings.search_path or DEFAULT_SEARCH_PATH, payload, "search")
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
