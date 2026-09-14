# -*- coding: utf-8 -*-
"""Permanent source-code audit guarding the application's hard constraints."""

import io
import os
import re
import tokenize

import capture
from conftest import REPO_ROOT

MODULES = ("capture.py", "app.py")

# Patterns indicating a real credential VALUE, as opposed to prose naming a
# header the app deliberately never sends.
SECRET_VALUE_PATTERNS = (
    rb"bearer\s+[a-z0-9._\-]{8,}",
    rb"authorization\s*[:=]\s*[\"']?[a-z0-9._\-]{8,}",
    rb"api[_-]?key\s*[:=]\s*[\"']?[a-z0-9._\-]{8,}",
    rb"access[_-]?token\s*[:=]\s*[\"']?[a-z0-9._\-]{8,}",
    # Requires an assigned literal: `parsed.password:` in a rejection check is
    # not a credential, and must not be flagged.
    rb"password[\"']?\s*[:=]\s*[\"'][^\"']",
)


def sources():
    for name in MODULES:
        with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            yield name, handle.read()


def code_only():
    """Source with comments and string literals removed.

    The modules document what they deliberately do NOT do ("no requests.Session",
    "no Authorization header"). Auditing raw text would flag that prose, so
    structural checks run against executable code only.
    """
    for name, source in sources():
        pieces = []
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            pieces.append(token.string)
        yield name, " ".join(pieces)


def test_no_mutating_http_verbs():
    for name, code in code_only():
        for verb in ("requests.post", "requests.put", "requests.patch", "requests.delete"):
            assert verb not in code, name


def test_only_one_outbound_call_and_it_is_get():
    source = open(os.path.join(REPO_ROOT, "capture.py"), encoding="utf-8").read()
    calls = re.findall(r"requests\s*\.\s*(\w+)\s*\(", source)
    assert calls == ["get"], calls


def test_no_requests_session():
    for name, code in code_only():
        assert "requests.Session" not in code, name
        assert "Session (" not in code, name


def test_no_secret_lookup_in_code():
    for name, code in code_only():
        for pattern in ("st.secrets", "os.environ", "getenv", "dotenv", "keyring", "netrc"):
            assert pattern not in code, "{0} contains {1!r}".format(name, pattern)


def test_no_credential_values_in_source():
    for name, source in sources():
        lowered = source.lower().encode("utf-8")
        for pattern in SECRET_VALUE_PATTERNS:
            assert re.search(pattern, lowered) is None, \
                "{0} matches {1!r}".format(name, pattern)


def test_no_auth_header_is_ever_constructed():
    for name, source in sources():
        for pattern in (
            r"[\"']Authorization[\"']\s*:",
            r"[\"']Cookie[\"']\s*:",
            r"[\"']Proxy-Authorization[\"']\s*:",
            r"\bauth\s*=",
        ):
            assert re.search(pattern, source) is None, \
                "{0} matches {1!r}".format(name, pattern)


def test_no_generic_url_input_in_the_ui():
    for name, code in code_only():
        for widget in ("st.text_input", "st.text_area", "st.file_uploader", "st.chat_input"):
            assert widget not in code, name


def test_no_browser_automation_or_model_providers():
    for name, code in code_only():
        lowered = code.lower()
        for pattern in ("playwright", "selenium", "webdriver", "openai", "anthropic",
                        "openrouter", "kimi"):
            assert pattern not in lowered, "{0} contains {1!r}".format(name, pattern)


def test_no_writes_to_milo_supabase_or_gcp():
    for name, code in code_only():
        lowered = code.lower()
        for pattern in ("supabase", "bigquery", "google.cloud", "milo-agent-workspace",
                        "firestore"):
            assert pattern not in lowered, "{0} contains {1!r}".format(name, pattern)


def test_no_database_or_persistent_server_storage():
    for name, code in code_only():
        lowered = code.lower()
        for pattern in ("sqlite3", "psycopg", "sqlalchemy", "redis", "pymongo"):
            assert pattern not in lowered, name
        # The archive is built entirely in memory.
        assert "open (" not in lowered, name
        assert "shutil" not in lowered, name


def test_no_global_network_caching():
    for name, code in code_only():
        for pattern in ("cache_data", "cache_resource", "lru_cache"):
            assert pattern not in code, name


def test_no_analytics_or_authentication():
    for name, code in code_only():
        lowered = code.lower()
        for pattern in ("st.login", "st.user", "analytics", "mixpanel", "segment.io"):
            assert pattern not in lowered, name


def test_capture_module_imports_without_streamlit():
    """capture.py must be testable headlessly; it must not import streamlit."""
    for name, code in code_only():
        if name == "capture.py":
            assert "streamlit" not in code
    assert "streamlit" not in [m for m in dir(capture)]
