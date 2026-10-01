"""Shared concurrency limits for GLM chat (per ACTUAL model id) and Search-Prime.

    shared per process   ConcurrencyController (provider limits are per account, so every batch,
                         vehicle and client of this process draws from the same pools)
    per HTTP attempt     a slot of the pool for that request is held only while the request is
                         in flight: acquired right before session.post, released in `finally`.
                         A retry is a new attempt and acquires again. Tool execution, document
                         parsing, page fetches and local evaluation never hold a model slot.

Each pool is a bounded semaphore with observable occupancy (`active`, `waiting`) so the live
dashboard can show "44 / 48" without touching private semaphore internals. Its operational limit
can be lowered or raised at run time but never above the known provider limit.

Unknown model ids get a conservative fallback (GLM_UNKNOWN_MODEL_MAX_INFLIGHT, default 1); nothing
assumes an unknown model shares Flash's 50 slots.

Configuration (environment):

    BATCH_MAX_WORKERS=50                 vehicle workers of a batch (not a request limit)
    GLM_CHAT_MAX_INFLIGHT=               operational chat limit for every known model
                                         (capped at each model's provider limit; empty = defaults)
    GLM_CHAT_MAX_INFLIGHT_BY_MODEL=      JSON {"glm-5.3-flash": 50, ...}; wins over the line above
    GLM_SEARCH_MAX_INFLIGHT=5            Search-Prime (capped at the provider limit)
    GLM_UNKNOWN_MODEL_MAX_INFLIGHT=1     any model id not listed below
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

# Z.ai account limits relevant to this application (concurrent requests in flight).
PROVIDER_MODEL_LIMITS: dict[str, int] = {
    "glm-5.3-flash": 50,
    "glm-5.3-flashx": 20,
    "glm-5.3": 5,
}
SEARCH_PRIME_PROVIDER_LIMIT = 5
# Operational defaults: a little headroom on the large pools (other clients of the same account,
# retries racing a release); none on the 5-slot pool, where one slot is 20% of the capacity.
DEFAULT_MODEL_LIMITS: dict[str, int] = {
    "glm-5.3-flash": 48,
    "glm-5.3-flashx": 18,
    "glm-5.3": 5,
}
DEFAULT_SEARCH_MAX_INFLIGHT = 5
DEFAULT_UNKNOWN_MODEL_MAX_INFLIGHT = 1
DEFAULT_BATCH_MAX_WORKERS = 50
WAIT_POLL_S = 0.25


class BatchCancelled(BaseException):
    """Cooperative cancellation of a running batch (like KeyboardInterrupt: a control-flow exception).

    Raised inside a vehicle worker at a safe point (before a model call, before a tool call, while
    waiting for a slot). run_vehicle treats it like any other interruption: partial evidence and
    state are persisted, the finalizer is skipped, status = interrupted, and it is re-raised."""


def normalize_model_id(model: Any) -> str:
    return str(model or "").strip().lower()


def _env_int(env: Callable[[str], str | None], name: str) -> int | None:
    raw = (env(name) or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def batch_max_workers_from_env(env: Callable[[str], str | None] = os.environ.get) -> int:
    value = _env_int(env, "BATCH_MAX_WORKERS")
    return max(1, value) if value else DEFAULT_BATCH_MAX_WORKERS


class SlotPool:
    """A bounded semaphore (acquire blocks at `limit`, an unmatched release raises) whose occupancy
    is observable and whose limit can change at run time (clamped to `hard_limit`)."""

    def __init__(self, name: str, limit: int, hard_limit: int | None = None):
        self.name = name
        self.hard_limit = hard_limit
        self._cond = threading.Condition()
        self.limit = self._clamp(limit)
        self.active = 0
        self.waiting = 0
        self.peak = 0

    def _clamp(self, value: int) -> int:
        value = max(1, int(value))
        return min(value, self.hard_limit) if self.hard_limit else value

    def set_limit(self, value: int) -> int:
        with self._cond:
            self.limit = self._clamp(value)
            self._cond.notify_all()
            return self.limit

    def try_acquire(self) -> bool:
        with self._cond:
            if self.active < self.limit:
                self.active += 1
                self.peak = max(self.peak, self.active)
                return True
            return False

    def acquire(self, cancel: threading.Event | None = None, poll_s: float = WAIT_POLL_S) -> None:
        with self._cond:
            self.waiting += 1
            try:
                while self.active >= self.limit:
                    if cancel is not None and cancel.is_set():
                        raise BatchCancelled("batch cancelled while waiting for a request slot")
                    self._cond.wait(poll_s)
                self.active += 1
                self.peak = max(self.peak, self.active)
            finally:
                self.waiting -= 1

    def release(self) -> None:
        with self._cond:
            if self.active <= 0:
                raise ValueError(f"{self.name}: released more slots than acquired")
            self.active -= 1
            self._cond.notify()

    def snapshot(self) -> dict:
        with self._cond:
            return {"active": self.active, "waiting": self.waiting, "limit": self.limit,
                    "provider_limit": self.hard_limit, "peak": self.peak}


class Observation:
    """Per-batch window over the shared pools: peaks and queue waits seen while it is registered."""

    def __init__(self) -> None:
        self.peak_chat_inflight_by_model: dict[str, int] = {}
        self.peak_search_inflight = 0
        self.chat_queue_wait_count = 0
        self.chat_queue_wait_ms = 0
        self.search_queue_wait_count = 0
        self.search_queue_wait_ms = 0
        self.chat_requests = 0
        self.search_requests = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class ConcurrencyController:
    def __init__(self, model_limits: dict[str, int] | None = None, search_limit: int | None = None,
                 unknown_model_limit: int | None = None, chat_max_inflight: int | None = None,
                 provider_model_limits: dict[str, int] | None = None,
                 search_provider_limit: int = SEARCH_PRIME_PROVIDER_LIMIT):
        self.provider_model_limits = {normalize_model_id(k): int(v)
                                      for k, v in (provider_model_limits or PROVIDER_MODEL_LIMITS).items()}
        limits = {k: v for k, v in DEFAULT_MODEL_LIMITS.items()}
        if chat_max_inflight:
            limits = {k: int(chat_max_inflight) for k in self.provider_model_limits}
        for key, value in (model_limits or {}).items():
            limits[normalize_model_id(key)] = int(value)
        self._configured = limits
        self.unknown_model_limit = max(1, int(unknown_model_limit or DEFAULT_UNKNOWN_MODEL_MAX_INFLIGHT))
        self.search_provider_limit = search_provider_limit
        self._lock = threading.Lock()
        self._chat: dict[str, SlotPool] = {}
        self.search = SlotPool("search-prime", search_limit or DEFAULT_SEARCH_MAX_INFLIGHT, search_provider_limit)
        self._observers: list[Observation] = []

    @classmethod
    def from_env(cls, env: Callable[[str], str | None] = os.environ.get) -> "ConcurrencyController":
        by_model: dict[str, int] = {}
        raw = (env("GLM_CHAT_MAX_INFLIGHT_BY_MODEL") or "").strip()
        if raw:
            try:
                by_model = {str(k): int(v) for k, v in json.loads(raw).items()}
            except (ValueError, AttributeError, TypeError):
                by_model = {}
        return cls(model_limits=by_model, search_limit=_env_int(env, "GLM_SEARCH_MAX_INFLIGHT"),
                   unknown_model_limit=_env_int(env, "GLM_UNKNOWN_MODEL_MAX_INFLIGHT"),
                   chat_max_inflight=_env_int(env, "GLM_CHAT_MAX_INFLIGHT"))

    # -- limits -----------------------------------------------------------------------------

    def provider_limit_for(self, model: str) -> int | None:
        return self.provider_model_limits.get(normalize_model_id(model))

    def is_known_model(self, model: str) -> bool:
        return normalize_model_id(model) in self.provider_model_limits

    def limit_for(self, model: str) -> int:
        return self.chat_pool(model).limit

    def chat_pool(self, model: str) -> SlotPool:
        key = normalize_model_id(model)
        with self._lock:
            pool = self._chat.get(key)
            if pool is None:
                hard = self.provider_model_limits.get(key)
                # explicit setting > default operational limit > provider limit; unknown model: fallback
                configured = self._configured.get(key) or hard or self.unknown_model_limit
                pool = SlotPool(key, configured, hard)
                self._chat[key] = pool
            return pool

    def set_model_limit(self, model: str, limit: int) -> int:
        """Operational limit for one model id, clamped to its provider limit (unknown: no clamp)."""
        self._configured[normalize_model_id(model)] = int(limit)
        return self.chat_pool(model).set_limit(limit)

    def set_search_limit(self, limit: int) -> int:
        return self.search.set_limit(limit)

    def config(self, models: list[str] | None = None) -> dict:
        """The operational and provider limits (for batch.json and the UI). No secrets."""
        names = [normalize_model_id(m) for m in (models or []) if m]
        with self._lock:
            names += [n for n in self._chat if n not in names]
        return {
            "model_limits": {n: self.limit_for(n) for n in names},
            "provider_model_limits": {n: self.provider_limit_for(n) for n in names},
            "known_provider_model_limits": dict(self.provider_model_limits),
            "unknown_model_max_inflight": self.unknown_model_limit,
            "search_max_inflight": self.search.limit,
            "search_provider_limit": self.search_provider_limit,
            "slot_scope": "per HTTP attempt (acquired before the request, released when it returns or fails)",
        }

    # -- observation ---------------------------------------------------------------------------

    @contextmanager
    def observe(self) -> Iterator[Observation]:
        obs = Observation()
        with self._lock:
            self._observers.append(obs)
        try:
            yield obs
        finally:
            with self._lock:
                if obs in self._observers:
                    self._observers.remove(obs)

    def _note(self, kind: str, model: str | None, active: int, wait_ms: int, waited: bool) -> None:
        with self._lock:
            for obs in self._observers:
                if kind == "chat":
                    obs.chat_requests += 1
                    key = normalize_model_id(model)
                    obs.peak_chat_inflight_by_model[key] = max(obs.peak_chat_inflight_by_model.get(key, 0), active)
                    if waited:
                        obs.chat_queue_wait_count += 1
                        obs.chat_queue_wait_ms += wait_ms
                else:
                    obs.search_requests += 1
                    obs.peak_search_inflight = max(obs.peak_search_inflight, active)
                    if waited:
                        obs.search_queue_wait_count += 1
                        obs.search_queue_wait_ms += wait_ms

    def snapshot(self) -> dict:
        with self._lock:
            chat = dict(self._chat)
        return {"chat": {name: pool.snapshot() for name, pool in chat.items()}, "search": self.search.snapshot()}

    # -- slots -----------------------------------------------------------------------------------

    @contextmanager
    def slot(self, kind: str, model: str | None = None, *, emit: Callable[..., Any] | None = None,
             cancel: threading.Event | None = None) -> Iterator[dict]:
        """Hold one in-flight slot for ONE HTTP attempt. `kind` is "chat" or "search".

        Emits `<prefix>_queue_wait_started` (only when the pool is full) and `<prefix>_slot_acquired`
        (only after a wait) through `emit(kind, **data)`. Yields {active, limit, wait_ms}."""
        pool = self.chat_pool(model or "") if kind == "chat" else self.search
        prefix = "model" if kind == "chat" else "search"
        base = {"model": normalize_model_id(model)} if kind == "chat" else {}
        if cancel is not None and cancel.is_set():
            raise BatchCancelled("batch cancelled before a request slot was acquired")
        started = time.monotonic()
        waited = not pool.try_acquire()
        if waited:
            snap = pool.snapshot()
            _safe_emit(emit, f"{prefix}_queue_wait_started", **base, active=snap["active"], limit=snap["limit"],
                       waiting=snap["waiting"] + 1)
            pool.acquire(cancel)
        wait_ms = int((time.monotonic() - started) * 1000)
        snap = pool.snapshot()
        info = {"active": snap["active"], "limit": snap["limit"], "wait_ms": wait_ms}
        self._note(kind, model, snap["active"], wait_ms, waited)
        try:
            if waited:
                _safe_emit(emit, f"{prefix}_slot_acquired", **base, **info)
            yield info
        finally:
            pool.release()


def _safe_emit(emit: Callable[..., Any] | None, kind: str, **data: Any) -> None:
    if emit is None:
        return
    try:
        emit(kind, **data)
    except Exception:  # observability must never break a request (control-flow exceptions propagate)
        pass


_DEFAULT: ConcurrencyController | None = None
_DEFAULT_LOCK = threading.Lock()


def default_controller() -> ConcurrencyController:
    """The process-wide controller (built from the environment on first use)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = ConcurrencyController.from_env()
        return _DEFAULT
