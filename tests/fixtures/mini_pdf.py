"""A minimal PDF writer for tests (no reportlab): one Helvetica text run per (x, y, text) item, no ruling lines.
Enough for pdfplumber to read words and their positions. ASCII text, plus Hebrew letters: a document with any Hebrew
letter gets a font whose encoding maps bytes 128.. to the Hebrew glyph names (uni05D0 ..), with explicit widths, so
pdfplumber reads the letters in the order they are drawn (left to right, i.e. the VISUAL order of a Hebrew PDF)."""

from __future__ import annotations

HEBREW_FIRST, HEBREW_LAST, HEBREW_BASE = 0x05D0, 0x05EA, 128


def _hebrew(pages: list[list[tuple[float, float, str]]]) -> bool:
    return any(HEBREW_FIRST <= ord(ch) <= HEBREW_LAST for items in pages for _, _, t in items for ch in t)


def _escape(text: str) -> str:
    out = []
    for ch in text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)"):
        code = ord(ch)
        if HEBREW_FIRST <= code <= HEBREW_LAST:
            out.append(f"\\{HEBREW_BASE + code - HEBREW_FIRST:03o}")
        elif code < 128:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def _hebrew_font(font_ref: int) -> bytes:
    from pdfminer.fontmetrics import FONT_METRICS

    widths = FONT_METRICS["Helvetica"][1]
    last = HEBREW_BASE + HEBREW_LAST - HEBREW_FIRST
    row = [widths.get(chr(c), 556) for c in range(32, 128)] + [556] * (last - 127)
    names = " ".join(f"/uni{c:04X}" for c in range(HEBREW_FIRST, HEBREW_LAST + 1))
    return (f"{font_ref} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica /FirstChar 32 /LastChar {last} "
            f"/Widths [{' '.join(map(str, row))}] /Encoding << /Type /Encoding /BaseEncoding /WinAnsiEncoding "
            f"/Differences [{HEBREW_BASE} {names}] >> >> endobj\n").encode()


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
    tail = [_hebrew_font(font_ref) if _hebrew(pages) else
            f"{font_ref} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n".encode()]
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
