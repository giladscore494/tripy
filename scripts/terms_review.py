"""Terms review report (Part C of the identity anchors PR). Report only: nothing is fetched and nothing is changed.

For the last N runs (default 20) of the runs folder: every importer / manufacturer domain that appeared in the runs'
events (search results, fetches, evidence), classified per vehicle against its own brand by data/source_rules.json; per
domain its current production policy (data/source_policy.json + the operator overlay), and the URL of its terms page
when one of the domain's OWN cached pages links to it (link text or URL matching `terms_review` of
data/source_rules.json). A terms page is never guessed: no cached link, no URL. The operator reads the terms and, when
they permit it, allows the domain on the Data page with the clause the decision rests on.

    python scripts/terms_review.py [--runs DIR] [--cache DIR] [--last 20] [--out report.md]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import source_authority as sa  # noqa: E402

URL = re.compile(r"https?://[^\s\"'<>\\]+")
OFFICIAL = ("official_importer", "official_manufacturer", "official_media")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def terms_links(html: str, base: str, rules: dict) -> list[str]:
    texts = [t.lower() for t in rules.get("link_text_terms") or []]
    patterns = [p.lower() for p in rules.get("url_patterns") or []]
    parser = _Links()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - a malformed page yields the links read so far
        pass
    base_host = sa.host_of(base)
    out = []
    for href, text in parser.links:
        url = urljoin(base, href or "")
        if not url.startswith("http") or sa.host_of(url) != base_host:
            continue
        low_text, low_url = text.lower(), url.lower()
        if any(t == low_text or (len(t) > 4 and t in low_text) for t in texts) or any(p in low_url for p in patterns):
            if url not in out:
                out.append(url)
    return out


def _manufacturer(record_dir: Path) -> str | None:
    try:
        payload = json.loads((record_dir / "input.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return ((payload.get("identity") or {}).get("manufacturer") or payload.get("tozar")) if isinstance(payload, dict) \
        else None


def collect(runs_dir: Path, last: int) -> tuple[list[str], dict]:
    runs = sorted((p for p in runs_dir.iterdir() if p.is_dir() and not p.name.startswith("_")),
                  key=lambda p: p.name)[-last:] if runs_dir.is_dir() else []
    domains: dict[str, dict] = defaultdict(lambda: {"authority": set(), "runs": set(), "urls": 0, "brands": set()})
    for run in runs:
        for record in (p for p in run.iterdir() if (p / "events.jsonl").is_file()):
            brand = _manufacturer(record)
            try:
                text = (record / "events.jsonl").read_text("utf-8", errors="replace")
            except OSError:
                continue
            for url in set(URL.findall(text)):
                cls = sa.classify_source(url, brand)
                if cls.get("source_authority") not in OFFICIAL:
                    continue
                entry = domains[cls["source_domain"]]
                entry["authority"].add(cls["source_authority"])
                entry["runs"].add(run.name)
                entry["brands"].add(brand or "?")
                entry["urls"] += 1
    return [r.name for r in runs], domains


def cached_terms(cache_dir: Path, rules: dict) -> dict[str, list[str]]:
    from src.storage.cache import DocumentCache

    out: dict[str, list[str]] = defaultdict(list)
    if not (cache_dir / "documents").is_dir():
        return out
    cache = DocumentCache(cache_dir)
    for meta in cache.list_documents():
        url, doc = meta.get("url"), meta.get("document_id")
        if not url or not doc or meta.get("doc_type") == "pdf" or meta.get("kind") in ("pdf", "registry", "open_dataset", "vpic"):
            continue
        try:
            html = cache.read_body(doc).decode("utf-8", errors="replace")
        except OSError:
            continue
        for link in terms_links(html, url, rules):
            if link not in out[sa.host_of(url)]:
                out[sa.host_of(url)].append(link)
    return out


def report(runs: list[str], domains: dict, terms: dict) -> str:
    lines = ["# Terms review", "", f"Runs read: {len(runs)} ({runs[0] if runs else '—'} … {runs[-1] if runs else '—'})",
             f"Policy: {sa.source_policy().get('version')} (overlay: {sa.source_policy().get('overlay_path') or 'none'})",
             "", "| domain | authority | brands | runs | policy (entry) | terms clause | checked | terms page (cached link) |",
             "|---|---|---|---|---|---|---|---|"]
    table = {e["id"]: e for e in sa.source_policy().get("entries") or []}
    for domain in sorted(domains, key=lambda d: (-len(domains[d]["runs"]), d)):
        info, pol = domains[domain], sa.policy_of(domain)
        entry = table.get(pol.get("entry")) or {}
        links = terms.get(domain) or []
        lines.append(f"| {domain} | {', '.join(sorted(info['authority']))} | {', '.join(sorted(info['brands']))} | "
                     f"{len(info['runs'])} | {pol['policy']} ({pol.get('entry') or 'unlisted'}) | "
                     f"{entry.get('terms_clause') or '—'} | {entry.get('checked_at') or '—'} | "
                     f"{'<br>'.join(links[:3]) if links else 'no cached link'} |")
    if not domains:
        lines.append("| (no importer or manufacturer domain in the runs read) | | | | | | | |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from src.storage.paths import resolve_paths

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    paths = resolve_paths()
    parser.add_argument("--runs", default=str(paths.runs_dir))
    parser.add_argument("--cache", default=str(paths.cache_dir))
    parser.add_argument("--last", type=int, default=20)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    sa.set_policy_overlay_dir(paths.data_dir / "derived")
    rules = sa.rules().get("terms_review") or {}
    runs, domains = collect(Path(args.runs), args.last)
    text = report(runs, domains, cached_terms(Path(args.cache), rules))
    if args.out:
        Path(args.out).write_text(text, "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
