"""The API's error envelope: {"error": {"code", "message", ...}}. Messages are redacted; no traceback ever leaves the
server (unexpected errors are logged server-side and answered with a generic 500)."""

from __future__ import annotations

from typing import Any

from ..app_config import redact


class ApiError(Exception):
    def __init__(self, http_status: int, code: str, message: str, *, headers: dict | None = None, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = http_status, code, redact(message), extra
        self.headers = headers

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


def not_found(what: str, value: str) -> ApiError:
    return ApiError(404, f"unknown_{what}", f"Unknown {what.replace('_', ' ')}: {redact(str(value))[:80]!r}.")
