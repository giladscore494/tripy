import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.cache import DocumentCache  # noqa: E402
from src.tools import ToolConfig, ToolContext  # noqa: E402
from src.tools.evidence import EvidenceStore  # noqa: E402


class FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200, content_type: str = "text/html; charset=utf-8",
                 url: str = "https://example.com/", text: str | None = None, json_data=None):
        self.content = body
        self.status_code = status
        self.headers = {"Content-Type": content_type, "Set-Cookie": "secret=1"}
        self.url = url
        self.history = []
        self._text = text
        self._json = json_data

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i : i + size]

    def close(self):
        pass

    @property
    def text(self):
        return self._text if self._text is not None else self.content.decode("utf-8", "replace")

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    """Maps URL -> FakeResponse and records calls. No network."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url not in self.routes:
            return FakeResponse(b"not found", status=404, url=url)
        response = self.routes[url]
        response.url = response.url if response.url != "https://example.com/" else url
        return response


def cache_source(cache, url: str, text: str, *, kind: str = "fetch", doc_type: str = "text", title: str = "") -> str:
    """Store a fetched document exactly as the fetch tools would (no HTTP); return its document_id.
    Evidence is admitted only from documents the run retrieved, with a quote that occurs in them."""
    meta = {"status": 200, "final_url": url, "doc_type": doc_type, "title": title,
            "content_type": "text/html" if doc_type == "html" else "text/plain"}
    if doc_type == "html":
        from src.tools.extract import visible_text

        return cache.put(kind, url, text.encode("utf-8"), meta, visible_text(text))["document_id"]
    return cache.put(kind, url, text.encode("utf-8"), meta, text)["document_id"]


def labelled_quote(field: str, value, unit: str = "") -> str:
    """A quote that states a value FOR its field (the field's first English alias next to the value), as admission
    requires; unknown fields (no dictionary entry) keep their own name."""
    from src.fields import load_schema

    spec = next((s for s in load_schema() if s["name"] == field), {})
    label = (spec.get("aliases_en") or [field.replace("_", " ")])[0]
    return f"{label}: {value}" + (f" {unit}" if unit else "")


def seed_evidence_sources(cache, script, headers: dict | None = None, default_header: str = "") -> dict[str, str]:
    """For scripted conversations: write one retrieved document per source_url that store_evidence calls cite,
    holding a header (identity text: model, propulsion, power...) and every quote cited for that URL. Returns
    {url: document_id}. Evidence still goes through the real admission gate (quote in source, value stated,
    server-side binding); the header decides which variant the page describes."""
    by_url: dict[str, list[str]] = {}
    for message in script or []:
        for call in (message.get("tool_calls") or []) if isinstance(message, dict) else []:
            fn = call.get("function") or {}
            if fn.get("name") != "store_evidence":
                continue
            args = fn.get("arguments")
            args = json.loads(args) if isinstance(args, str) else (args or {})
            if args.get("source_url"):
                by_url.setdefault(args["source_url"].split("#")[0], [])
                if args.get("quote"):
                    by_url[args["source_url"].split("#")[0]].append(args["quote"])
    out = {}
    for url, quotes in by_url.items():
        header = (headers or {}).get(url)
        if header is None:
            header = next((h for prefix, h in sorted((headers or {}).items(), key=lambda kv: -len(kv[0]))
                           if url.startswith(prefix)), default_header)
        out[url] = cache_source(cache, url, header + "\n" + "\n".join(dict.fromkeys(quotes)))
    return out


def pytest_configure(config):
    config.addinivalue_line("markers", "acquisition_mode(mode): pin the AgentConfig default acquisition mode "
                                       "(legacy | contract) for this test")
    config.addinivalue_line("markers", "sweep_mode(mode): pin the AgentConfig default document sweep mode "
                                       "(adjudication | legacy) for this test")


@pytest.fixture(autouse=True)
def _acquisition_mode_default(request, monkeypatch):
    """The AgentConfig default acquisition mode of a test: its `acquisition_mode` marker (tests encoding one mode's
    behaviour), else ACQUISITION_MODE from the environment (`ACQUISITION_MODE=legacy pytest` runs every unpinned test
    in legacy), else the code default. An explicit acquisition_mode=... argument always wins."""
    marker = request.node.get_closest_marker("acquisition_mode")
    mode = marker.args[0] if marker else (os.environ.get("ACQUISITION_MODE") or "").strip().lower()
    if mode not in ("legacy", "contract"):
        return
    from src import agent

    original = agent.AgentConfig.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("acquisition_mode", mode)
        original(self, *args, **kwargs)

    monkeypatch.setattr(agent.AgentConfig, "__init__", init)


@pytest.fixture(autouse=True)
def _sweep_mode_default(request, monkeypatch):
    """The AgentConfig default sweep mode of a test: its `sweep_mode` marker (tests encoding the legacy tool-loop
    sweep), else SWEEP_MODE from the environment, else the code default (adjudication). An explicit sweep_mode=...
    argument always wins."""
    marker = request.node.get_closest_marker("sweep_mode")
    mode = marker.args[0] if marker else (os.environ.get("SWEEP_MODE") or "").strip().lower()
    if mode not in ("legacy", "adjudication"):
        return
    from src import agent

    original = agent.AgentConfig.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("sweep_mode", mode)
        original(self, *args, **kwargs)

    monkeypatch.setattr(agent.AgentConfig, "__init__", init)


@pytest.fixture
def make_ctx(tmp_path):
    def factory(routes=None, **config):
        return ToolContext(cache=DocumentCache(tmp_path / "cache"), evidence=EvidenceStore(),
                           config=ToolConfig(**config), session=FakeSession(routes),
                           vehicle={"manufacturer": "טויוטה"})

    return factory


def minimal_pdf(text: str) -> bytes:
    """Build a tiny valid one-page PDF containing `text` (ASCII)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)
