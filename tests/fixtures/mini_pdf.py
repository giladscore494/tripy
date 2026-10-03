"""A minimal PDF writer for tests (no reportlab): one Helvetica text run per (x, y, text) item, no ruling lines.
Enough for pdfplumber to read words and their positions; ASCII text only."""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[tuple[float, float, str]]], size: int = 10) -> bytes:
    objects: list[bytes] = []
    kids = []
    font_ref = 3 + 2 * len(pages)
    for index, items in enumerate(pages):
        page_no, content_no = 3 + 2 * index, 4 + 2 * index
        stream = "".join(f"BT /F1 {size} Tf {x:.1f} {y:.1f} Td ({_escape(t)}) Tj ET\n" for x, y, t in items).encode()
        objects.append(f"{page_no} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 {font_ref} 0 R >> >> /Contents {content_no} 0 R >> endobj\n"
                       .encode())
        objects.append(f"{content_no} 0 obj << /Length {len(stream)} >> stream\n".encode() + stream
                       + b"endstream endobj\n")
        kids.append(f"{page_no} 0 R")
    head = [b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
            f"2 0 obj << /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >> endobj\n".encode()]
    tail = [f"{font_ref} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n".encode()]
    body = b"%PDF-1.4\n"
    offsets = []
    for obj in head + objects + tail:
        offsets.append(len(body))
        body += obj
    xref = len(body)
    body += f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode()
    body += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    body += f"trailer << /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return body


def spec_matrix(rows: list[tuple[str, ...]], top: float = 700, left: float = 72, column: float = 200,
                step: float = 18) -> list[tuple[float, float, str]]:
    """Text items of a borderless table: one row per line, cells at fixed column positions."""
    return [(left + j * column, top - i * step, cell) for i, row in enumerate(rows) for j, cell in enumerate(row)]
