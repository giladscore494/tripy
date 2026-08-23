from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from web_search import (
    FreeWebSearchError,
    _is_safe_result_url,
    _validate_public_destination,
    search_web,
)


class UrlSafetyTests(unittest.TestCase):
    def test_rejects_local_private_and_credential_urls(self):
        self.assertFalse(_is_safe_result_url("http://localhost/admin"))
        self.assertFalse(_is_safe_result_url("http://127.0.0.1/"))
        self.assertFalse(_is_safe_result_url("http://10.0.0.1/"))
        self.assertFalse(_is_safe_result_url("https://user:pass@example.com/"))
        self.assertFalse(_is_safe_result_url("file:///etc/passwd"))
        self.assertTrue(_is_safe_result_url("https://example.com/news"))

    @patch(
        "web_search.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    def test_rejects_hostname_that_resolves_to_private_ip(self, _getaddrinfo):
        with self.assertRaisesRegex(ValueError, "Private network"):
            _validate_public_destination("https://example.com/")


class SearchTests(unittest.TestCase):
    @patch("web_search.fetch_page", return_value="Full current article")
    @patch("web_search._search_text")
    def test_bounds_deduplicates_and_extracts_one_page(self, mocked_search, mocked_fetch):
        mocked_search.return_value = [
            {"title": "One", "href": "https://one.example/a", "body": "First snippet"},
            {"title": "Duplicate", "href": "https://one.example/a", "body": "Duplicate"},
            {"title": "Private", "href": "http://127.0.0.1/secret", "body": "No"},
            {"title": "Two", "href": "https://two.example/b", "body": "Second snippet"},
            {"title": "Three", "href": "https://three.example/c", "body": "Third snippet"},
            {"title": "Four", "href": "https://four.example/d", "body": "Fourth snippet"},
        ]

        results = search_web("  current event  ")

        mocked_search.assert_called_once_with("current event")
        mocked_fetch.assert_called_once_with("https://one.example/a")
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["content"], "Full current article")
        self.assertEqual([result["title"] for result in results], ["One", "Two", "Three"])

    @patch("web_search._search_text", side_effect=RuntimeError("blocked"))
    def test_backend_failure_is_user_safe(self, _mocked_search):
        with self.assertRaisesRegex(FreeWebSearchError, "temporarily unavailable"):
            search_web("latest news")


if __name__ == "__main__":
    unittest.main()
