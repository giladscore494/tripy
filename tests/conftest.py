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
