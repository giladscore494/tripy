from __future__ import annotations

import ipaddress
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESULTS = 3
MAX_QUERY_CHARS = 300
MAX_TITLE_CHARS = 300
MAX_SNIPPET_CHARS = 1_000
MAX_PAGE_CHARS = 5_000
MAX_DOWNLOAD_BYTES = 1_000_000
SEARCH_TIMEOUT_SECONDS = 8
FETCH_TIMEOUT_SECONDS = 8
ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
USER_AGENT = "TripyChat/1.0 (keyless educational web search)"


class FreeWebSearchError(RuntimeError):
    """A safe error raised when the keyless search backend is unavailable."""


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _parsed_public_url(url: str):
    try:
        parts = urlsplit(url.strip())
        _ = parts.port
    except ValueError as exc:
        raise ValueError("Invalid URL") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Only public HTTP URLs are allowed")
    if parts.username or parts.password:
        raise ValueError("URLs with credentials are not allowed")

    hostname = parts.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Local URLs are not allowed")
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise ValueError("Private URLs are not allowed")
    return parts


def _is_safe_result_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    try:
        _parsed_public_url(url)
    except ValueError:
        return False
    return True


def _validate_public_destination(url: str) -> None:
    parts = _parsed_public_url(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Could not resolve URL") from exc
    if not addresses:
        raise ValueError("Could not resolve URL")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("Private network destinations are not allowed")


class _SafeRedirectHandler(HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _validate_public_destination(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _search_text(query: str) -> list[dict[str, Any]]:
    from ddgs import DDGS

    return DDGS(timeout=SEARCH_TIMEOUT_SECONDS).text(
        query,
        max_results=MAX_RESULTS,
        safesearch="moderate",
        backend="auto",
    )


def _extract_text(html: str, url: str) -> str:
    from trafilatura import extract

    return extract(html, url=url, favor_precision=True) or ""


def fetch_page(url: str) -> str:
    """Fetch a small amount of public HTML and extract its main text."""
    try:
        _validate_public_destination(url)
        opener = build_opener(_SafeRedirectHandler())
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            _validate_public_destination(final_url)
            content_type = response.headers.get_content_type().lower()
            if content_type not in ALLOWED_CONTENT_TYPES:
                return ""
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_DOWNLOAD_BYTES:
                        return ""
                except ValueError:
                    pass
            raw = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(raw) > MAX_DOWNLOAD_BYTES:
                raw = raw[:MAX_DOWNLOAD_BYTES]
            charset = response.headers.get_content_charset() or "utf-8"
            html = raw.decode(charset, errors="replace")
            return _clean(_extract_text(html, final_url), MAX_PAGE_CHARS)
    except Exception:
        return ""


def search_web(query: str) -> list[dict[str, str]]:
    """Return bounded keyless web results and one extracted page when available."""
    query = _clean(query, MAX_QUERY_CHARS)
    if not query:
        raise FreeWebSearchError("The search query is empty.")

    try:
        raw_results = _search_text(query)
    except Exception as exc:
        raise FreeWebSearchError("Free web search is temporarily unavailable.") from exc

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            continue
        url = raw.get("href") or raw.get("url")
        if not _is_safe_result_url(url) or url in seen_urls:
            continue
        title = _clean(raw.get("title"), MAX_TITLE_CHARS) or str(url)
        snippet = _clean(raw.get("body") or raw.get("snippet"), MAX_SNIPPET_CHARS)
        result = {"title": title, "url": str(url)}
        if snippet:
            result["snippet"] = snippet
        results.append(result)
        seen_urls.add(str(url))
        if len(results) >= MAX_RESULTS:
            break

    for result in results[:2]:
        content = fetch_page(result["url"])
        if content:
            result["content"] = content
            break
    return results
