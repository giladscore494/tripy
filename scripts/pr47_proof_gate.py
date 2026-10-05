"""PR #47 proof gate 1: a dry run (no network) on production run 20261005T210701Z-glm-5.3-flash-one, record 38626
(Toyota Corolla 2024 BUSINESS EDI, wagon, hybrid 1.8, 98 hp, 2WD).

    1. the IL version-page resolver over the run's own site-search results (tests/fixtures/pr47_pages.SEARCH_RESULTS)
       and the pages the run cached (the cartube wagon / Sense pages, the carzone model, compare and trim pages;
       any other URL is "not cached": the dry run never fetches)
    2. admission of the run's candidates from the cartube wagon page and the carzone compare / trim pages with the
       current code (value, quote, document as the run's candidate table recorded them), against the field states the
       run recorded

    python scripts/pr47_proof_gate.py
"""

from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from fixtures import pr47_pages as F  # noqa: E402
from fixtures.corolla_harvest import put  # noqa: E402
from src.evidence_admission import AdmissionContext, admit  # noqa: E402
from src.fields import resolve_requested_fields  # noqa: E402
from src.storage.cache import DocumentCache  # noqa: E402

PAGES = {F.WAGON_URL: F.cartube_wagon_html, F.SENSE_SEARCH_URL: F.cartube_sense_html, F.MODEL_URL: F.carzone_model_html,
         F.COMPARE_URL: F.carzone_compare_html, F.TRIM_URL: F.carzone_trim_html}

# (field, value, quote, page): the run's candidates on these pages (candidates.csv origin "dom_pair · <doc> · quote")
FACTS = [
    ("acceleration_0_100_s", 9.4, "תאוצה 0-100 | 9.4 שנ׳ - - -", F.COMPARE_URL),
    ("top_speed_kmh", 180, "מהירות מרבית | 180 קמ״ש - - -", F.COMPARE_URL),
    ("curb_weight_kg", 1400, "משקל עצמי | 1,400 ק״ג - - -", F.COMPARE_URL),
    ("ground_clearance_mm", 135, "מרווח גחון | 135 מ״מ - - -", F.COMPARE_URL),
    ("cargo_volume_l", 596, "תא מטען | 596 ליטר 596 ליטר 596 ליטר 596 ליטר", F.COMPARE_URL),
    ("fuel_tank_l", 43, "מיכל דלק | 43 ליטר - - -", F.COMPARE_URL),
    ("sunroof_panoramic", False, "גג שמש | ללא ללא ללא ללא", F.COMPARE_URL),
    ("tire_size_front", "205/55R16", "צמיג קדמי | 205/55R16 - - -", F.COMPARE_URL),
    ("tire_size_rear", "205/55R16", "צמיג אחורי | 205/55R16 - - -", F.COMPARE_URL),
    ("acceleration_0_100_s", 9.4, '0-100 קמ"ש (שניות) | 9.4', F.WAGON_URL),
    ("top_speed_kmh", 180, 'מהירות מרבית (קמ"ש) | 180', F.WAGON_URL),
    ("curb_weight_kg", 1400, 'משקל עצמי (ק"ג) | 1,400', F.WAGON_URL),
    ("cargo_volume_l", 596, "נפח תא מטען אחורי (ליטר) | 596", F.WAGON_URL),
    ("wheelbase_mm", 2700, 'בסיס גלגלים (ס"מ) | 270.0', F.WAGON_URL),
    ("tire_size_front", "205/55R16", "מידות צמיגים | 205/55R16", F.WAGON_URL),
    ("sunroof_panoramic", False, "חלון גג | אין", F.WAGON_URL),
    ("acceleration_0_100_s", 9.4, "תאוצה 0-100 | 9.4 שניות", F.TRIM_URL),
]
# the field states the run recorded (binding replay summary of the run, MCP)
RECORDED = {"acceleration_0_100_s": "ok", "top_speed_kmh": "ok", "curb_weight_kg": "ok", "ground_clearance_mm": "ok",
            "cargo_volume_l": "ok", "fuel_tank_l": "ok", "sunroof_panoramic": "variant_not_exact",
            "tire_size_front": "variant_not_exact", "tire_size_rear": "variant_not_exact",
            "wheelbase_mm": "variant_not_exact"}


class _Ctx:
    def __init__(self, cache, adm):
        self.cache, self.counters, self.admission = cache, Counter(), adm


class _NoPace:
    @staticmethod
    def wait(domain):
        return 0.0


def resolver(cache, adm) -> dict:
    from src.il_version_pages import resolve

    fetched = []

    def search(query, domain):
        return {"results": [{"url": u, "title": t} for u, t in F.SEARCH_RESULTS.get(domain, [])]}

    def fetch(url):
        fetched.append(url)
        if url not in PAGES:
            return {"error": "not_cached_in_the_run", "status": None}
        return {"document_id": put(cache, url, PAGES[url](), "html"), "status": 200}
    return resolve(_Ctx(cache, adm), None, payload=F.PAYLOAD_38626, search=search, fetch=fetch,
                   robots=lambda u: True, pace=_NoPace())


def main() -> int:
    from urllib.parse import unquote

    cache = DocumentCache(Path(tempfile.mkdtemp()) / "cache")
    adm = AdmissionContext.for_run(F.PAYLOAD_38626, None, resolve_requested_fields(None, propulsion="hybrid"), "IL")
    out = resolver(cache, adm)
    print("## 1. Resolver dry run (the run's search results and cached pages)\n")
    print("queries:", " · ".join(out["queries"]))
    print("\n| candidate | role | score | decision |\n|---|---|---|---|")
    for c in out["candidates"]:
        print(f"| {unquote(c['url'])[:110]} | {c['role']} | {c.get('score')} | {c.get('decision')} |")
    print("\n| fetched page | status | reason |\n|---|---|---|")
    for p in out["pages"]:
        print(f"| {unquote(p['url'])[:110]} | {p['status']} | {p['reason']} |")
    print(f"\nacq_il_version_pages_found = {out['found']}")
    dropped = [r for r in out["results"] if r.get("reason") == "other_family"]
    print("dropped other_family:", ", ".join(unquote(r["url"])[:80] for r in dropped) or "-")

    print("\n## 2. Admission replay of the run's facts on these pages\n")
    print("| field | value | page | level now | requirement | match | basis | column |\n|---|---|---|---|---|---|---|---|")
    exact: dict[str, list] = {}
    for field, value, quote, url in FACTS:
        doc = put(cache, url, PAGES[url](), "html")
        decision = admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": doc}, [doc])
        if not decision["accepted"]:
            print(f"| {field} | {value} | {url.split('/')[2]} | rejected: {decision.get('reasons')} | | | | |")
            continue
        r = decision["record"]
        column = r.get("trim_column") or {}
        page = "carzone compare" if url == F.COMPARE_URL else "carzone trim" if url == F.TRIM_URL else "cartube wagon"
        print(f"| {field} | {value} | {page} | {r['binding_level']} | {r['binding_requirement']} | "
              f"{r['variant_match']} | {r.get('binding_basis') or '-'} | "
              f"{column.get('status', '-')}{(' #' + str(column['column'])) if 'column' in column else ''} |")
        if r["variant_match"] == "exact":
            exact.setdefault(field, []).append((value, page))
    print("\n## 3. Field states that change (recorded -> with this PR)\n")
    print("| field | recorded | now | value | source |\n|---|---|---|---|---|")
    changed = 0
    for field, state in RECORDED.items():
        values = {str(v) for v, _ in exact.get(field, [])}
        now = "ok" if len(values) == 1 else "conflicting" if len(values) > 1 else state if state != "ok" else "not ok"
        if now != state:
            changed += 1
            value, page = exact[field][0] if exact.get(field) else ("-", "-")
            print(f"| {field} | {state} | {now} | {value} | {page} |")
    if not changed:
        print("| (none) | | | | |")
    compare_target = any(page == "carzone compare" and field == "sunroof_panoramic"
                         for field, vals in exact.items() for _, page in vals)
    wagon = next((p for p in out["pages"] if p["url"] == F.WAGON_URL), {})
    ok = compare_target and wagon.get("reason") == "power_mismatch"
    print("\nPASS" if ok else "\nFAIL", "- carzone compare column binds exact_market_trim:", compare_target,
          "- cartube wagon page rejection:", wagon.get("reason"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
