"""The streamed download of a resource file and its projected CSV reader.

The body is read once, as a stream: hashed (sha256) and counted while it is parsed, optionally teed to a file on the
runner's disk (`to_disk`; the work folder, never git). An HTML page or a CKAN / JSON error body instead of the data
is refused before any row is read (`html_body`); a body that is not a delimited table is `format_changed`. Rows come
out projected (schemas.projection): a column outside the projection is never copied out of the parser's row, so
licence-plate, chassis and engine numbers are never stored, logged or sampled.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import sys
from pathlib import Path
from typing import Any, BinaryIO, Iterator

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
HEAD_BYTES = 64 * 1024
DELIMITERS = (",", "|", ";", "\t")


class DownloadRefused(RuntimeError):
    """The body is not the data: `reason` is html_body or format_changed."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


class HashingStream(io.RawIOBase):
    """A raw stream that hashes and counts every byte read (and copies it to `tee` when given)."""

    def __init__(self, source: BinaryIO, tee: BinaryIO | None = None):
        self.source, self.tee = source, tee
        self.digest = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self.source.read(len(buffer))
        if not data:
            return 0
        n = len(data)
        buffer[:n] = data
        self.digest.update(data)
        self.size += n
        if self.tee is not None:
            self.tee.write(data)
        return n


def is_error_body(head: bytes) -> str | None:
    """A reason when the first bytes are not a data file: an HTML / XML page, a JSON body, or nothing at all."""
    text = head[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if not text:
        return "empty body"
    if text.startswith((b"<!doctype", b"<html", b"<?xml", b"<head", b"<body")) or b"<html" in text[:1024]:
        return "an HTML page instead of the data"
    if text.startswith((b"{", b"[")):
        return "a JSON body instead of the data"
    return None


def _encoding(head: bytes) -> str:
    """utf-8-sig when the head decodes as UTF-8 (a character cut at the end of the head is allowed), else cp1255."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(head, final=False)
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "cp1255"


def _delimiter(first_line: str) -> str:
    counts = {d: first_line.count(d) for d in DELIMITERS}
    best = max(DELIMITERS, key=lambda d: (counts[d], -DELIMITERS.index(d)))
    if counts[best] == 0:
        raise DownloadRefused("format_changed", "the header line has no delimiter")
    return best


class CsvDownload:
    """One streamed CSV body: `header`, `rows()` (the parser's rows; the caller projects them), then `sha256` /
    `file_size` once the rows are exhausted."""

    def __init__(self, source: BinaryIO, tee_path: Path | None = None):
        self._tee = open(tee_path, "wb") if tee_path else None
        self._raw = HashingStream(source, self._tee)
        self._buffered = io.BufferedReader(self._raw, 1 << 20)
        head = self._buffered.peek(HEAD_BYTES)[:HEAD_BYTES]
        reason = is_error_body(head)
        if reason:
            self.close()
            raise DownloadRefused("html_body", reason)
        self.encoding = _encoding(head)
        first = head.decode(self.encoding, errors="ignore").splitlines()[0] if head else ""
        self.delimiter = _delimiter(first)
        self._text = io.TextIOWrapper(self._buffered, encoding=self.encoding, errors="replace", newline="")
        self._reader = csv.reader(self._text, delimiter=self.delimiter)
        try:
            self.header = next(self._reader)
        except StopIteration:
            self.close()
            raise DownloadRefused("format_changed", "no header line") from None
        if len(self.header) < 2:
            self.close()
            raise DownloadRefused("format_changed", "fewer than two columns")
        self.malformed = 0

    def rows(self) -> Iterator[list[str]]:
        width = len(self.header)
        for row in self._reader:
            if not row:
                continue
            if len(row) != width:
                self.malformed += 1
            yield row
        while self._buffered.read(1 << 20):       # drain: the sha256 covers the whole body
            pass

    @property
    def sha256(self) -> str:
        return self._raw.digest.hexdigest()

    @property
    def file_size(self) -> int:
        return self._raw.size

    def close(self) -> None:
        if self._tee is not None and not self._tee.closed:
            self._tee.close()


def project(row: list[str], columns: dict[str, int]) -> dict[str, Any]:
    """The projected row: only the columns of the projection, blank cells as None."""
    out: dict[str, Any] = {}
    for name, index in columns.items():
        value = row[index].strip() if index < len(row) else ""
        out[name] = value or None
    return out
