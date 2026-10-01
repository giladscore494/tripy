"""get_cached_document: reuse an already-downloaded document without paying again."""

from __future__ import annotations

from .extract import _chunk


def get_cached_document(ctx, key: str, offset: int = 0, max_chars: int | None = None) -> dict:
    key = (key or "").strip()
    meta = ctx.cache.get(key) if key.startswith("d_") else ctx.cache.find_by_url(key)
    if not meta:
        return {"found": False, "key": key, "hint": "Not cached yet; use fetch_url, fetch_pdf or render_page."}
    ctx.note_document(meta["document_id"], cache_hit=True)
    text = ctx.cache.read_text(meta["document_id"])
    info = {k: meta.get(k) for k in ("document_id", "kind", "url", "final_url", "status", "content_type",
                                      "title", "bytes", "text_chars", "extraction_path", "fetched_at")}
    cap = ctx.config.max_text_chars
    return {"found": True, **info, **_chunk(text, offset, max(200, min(int(max_chars or cap), cap)))}
