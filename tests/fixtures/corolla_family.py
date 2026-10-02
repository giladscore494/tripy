"""Offline cold vs warm benchmark for verified fact reuse (PR 3): three Corolla Touring Sports 2024 variants run one
after another on ONE shared cache, as a batch of related variants would:

    A   1.8 Hybrid Business   cold: nothing in memory
    A'  1.8 Hybrid Business   the same variant again (a re-run): its facts are reused, and the routes that found
                              nothing for ground clearance are not paid for again (negative route memory)
    A2  1.8 Hybrid Premium    warm, SAME technical variant, other trim: technical facts of A may be reused (each one
                              re-admitted against A2); trim / commercial facts may not
    B   2.0 Hybrid Business   warm cache, DIFFERENT powertrain: inherits nothing from A (scope keys differ, and a 1.8
                              source is vetoed by re-admission anyway). Its boot is 581 L, never A's 596 L.

The same deterministic policy model (tests/fixtures/corolla_tail.PolicyGLM) plays every run; only the memory differs.
Run directly for a report:  python tests/fixtures/corolla_family.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixtures import corolla_tail as tail  # noqa: E402
from fixtures.corolla_touring import PAYLOAD, VEHICLE  # noqa: E402

from src.benchmark import compute_metrics  # noqa: E402
from src.storage.cache import DocumentCache  # noqa: E402

FIELDS = ["length_mm", "width_mm", "wheelbase_mm", "height_mm", "curb_weight_kg", "ground_clearance_mm",
          "cargo_volume_l", "fuel_tank_l", "battery_gross_kwh", "torque_nm", "acceleration_0_100_s", "top_speed_kmh",
          "list_price", "warranty_years"]

CARTUBE_PREMIUM = "https://www.cartube.co.il/toyota/corolla-touring-sports-2024-1-8-hybrid-premium"
CARTUBE_PREMIUM_TEXT = "\n".join([
    "טויוטה קורולה טורינג ספורט 2024 1.8 היברידי Premium",
    'רוחב: 1,790 מ"מ',
    'גובה 146.0 ס"מ',
    "אחריות: שלוש שנים",
    'מחיר: 189,990 ש"ח',
])
CARTUBE_20 = "https://www.cartube.co.il/toyota/corolla-touring-sports-2024-2-0-hybrid-business"
CARTUBE_20_TEXT = "\n".join([
    "טויוטה קורולה טורינג ספורט 2024 2.0 היברידי Business",
    'רוחב: 1,790 מ"מ',
    'גובה 146.0 ס"מ',
    "אחריות: שלוש שנים",
    'מחיר: 199,990 ש"ח',
])
EU_SPEC_20 = "https://www.toyota-europe.com/new-cars/corolla-touring-sports/2-0-hybrid-specifications"
EU_SPEC_20_HTML = """<html><head><title>Toyota Corolla Touring Sports 2024 2.0 Hybrid 196 - technical specifications</title>
</head><body><h1>Toyota Corolla Touring Sports 2024 2.0 Hybrid 196</h1>
<p>Fuel tank capacity: 43 l</p>
<p>Battery capacity: 0.91 kWh</p>
<p>Kerb weight: 1,455 kg</p>
<p>Luggage capacity: 581 l</p>
<p>Top speed: 180 km/h</p>
<p>0-100 km/h: 8.1 s</p>
<p>Wheelbase: 2,700 mm</p>
<p>Length: 4,650 mm</p>
</body></html>"""

ROUTES = {**tail.ROUTES, CARTUBE_PREMIUM: (CARTUBE_PREMIUM_TEXT, "text/plain"),
          CARTUBE_20: (CARTUBE_20_TEXT, "text/plain"), EU_SPEC_20: (EU_SPEC_20_HTML, "text/html; charset=utf-8")}


def _primary(page: str, stores: list[tuple]) -> list[dict]:
    calls = [tail._store(f"p{i}", field, value, page, quote, unit) for i, (field, value, quote, unit)
             in enumerate(stores)]
    return [tail._turn(tail._call("p0", "fetch_url", {"url": page})), tail._turn(*calls),
            tail._say({"summary": "primary research", "fields": {}})]


def _index(engine: str, spec_page: str) -> list[tuple]:
    return [("ground clearance", [{"url": tail.FORUM, "title": "ground clearance?", "snippet": "owners"}]),
            ("", [{"url": spec_page, "title": f"Corolla Touring Sports {engine} Hybrid technical specifications",
                   "snippet": "fuel tank, battery capacity, kerb weight, luggage capacity, top speed"}])]


def variant(name: str) -> dict:
    """payload, vehicle metadata, the primary research script and the search index of one variant."""
    if name == "A":
        stores = [("torque_nm", 142, "מומנט מנוע בנזין: 142 ניוטון-מטר", "Nm"),
                  ("warranty_years", 3, "אחריות: שלוש שנים", "years"),
                  ("list_price", "179990-183990", 'מחיר: 179,990-183,990 ש"ח', "ILS")]
        return {"payload": PAYLOAD, "vehicle": VEHICLE, "record_id": "38626", "engine": "1.8",
                "primary": _primary(tail.CARTUBE, stores), "index": _index("1.8", tail.EU_SPEC)}
    if name == "A2":
        payload = copy.deepcopy(PAYLOAD)
        payload["identity"].update({"trim": "PREMIUM", "government_record_id": "38627"})
        stores = [("warranty_years", 3, "אחריות: שלוש שנים", "years"),
                  ("list_price", 189990, 'מחיר: 189,990 ש"ח', "ILS")]
        return {"payload": payload, "vehicle": {**VEHICLE, "trim": "PREMIUM"}, "record_id": "38627", "engine": "1.8",
                "primary": _primary(CARTUBE_PREMIUM, stores), "index": _index("1.8", tail.EU_SPEC)}
    payload = copy.deepcopy(PAYLOAD)
    payload["identity"].update({"government_record_id": "38700", "model_code": "MZEA12L DWXNBW"})
    payload["engine_drivetrain"].update({"engine_cc": 1987, "power_hp": 152})
    stores = [("warranty_years", 3, "אחריות: שלוש שנים", "years"),
              ("list_price", 199990, 'מחיר: 199,990 ש"ח', "ILS")]
    return {"payload": payload, "vehicle": {**VEHICLE, "model_code": "MZEA12L DWXNBW"}, "record_id": "38700",
            "engine": "2.0", "primary": _primary(CARTUBE_20, stores), "index": _index("2.0", EU_SPEC_20)}


def run_variant(name: str, workdir: Path, cache: DocumentCache, memory: bool = True) -> dict:
    """One variant's run (each call starts a fresh run directory under `workdir`)."""
    v = variant(name)
    client = tail.PolicyGLM(primary=v["primary"], search_index=v["index"], engine=v["engine"])
    run = tail.run_mode("cluster", workdir, client=client, payload=v["payload"], vehicle=v["vehicle"], routes=ROUTES,
                        record_id=v["record_id"], cache=cache, batch="family", requested_fields=FIELDS,
                        research_memory_enabled=memory)
    run["metrics"] = compute_metrics(run["result"])
    return run


def summarize(run: dict) -> dict:
    result, m = run["result"], run["metrics"]
    states = {f: s["state"] for f, s in result["research_bundle"]["field_states"].items()}
    values = {}
    for item in result["evidence"]:
        values.setdefault(item["field"], []).append(item["value"])
    return {"model_calls": m["model_calls"], "total_tokens": m["total_tokens"], "tail_model_calls": m["tail_model_calls"],
            "search_calls": m["search_api_calls"], "documents_fetched": m["tool_calls_by_name"].get("fetch_url", 0),
            "verified_fact_cache_hits": m["verified_fact_cache_hits"], "search_cache_hits": m["search_cache_hits"],
            "document_cache_hits": m["document_cache_hits"], "candidate_cache_hits": m["candidate_cache_hits"],
            "negative_route_cache_hits": m["negative_route_cache_hits"],
            "negative_route_fields_shown": m["negative_route_fields_shown"], "fields_ok": sum(
                1 for s in states.values() if s == "ok"), "states": states, "values": values,
            "reused_fields": sorted({i["field"] for i in (result.get("fact_reuse") or {}).get("items") or []})}


def benchmark(workdir: Path | None = None) -> dict:
    root = Path(workdir or tempfile.mkdtemp(prefix="corolla-family-"))
    cache = DocumentCache(root / "cache")
    out = {}
    for name, key in (("A", "A"), ("A", "A_rerun"), ("A2", "A2"), ("B", "B")):
        out[key] = summarize(run_variant(name, root / key, cache))
    # the same A2 without memory (cold, but with the warm document / search caches): what the memory itself saves
    out["A2_no_memory"] = summarize(run_variant("A2", root / "A2_cold", DocumentCache(root / "cache_cold"),
                                                memory=False))
    return out


if __name__ == "__main__":
    report = benchmark()
    keys = [k for k in report["A"] if k not in ("states", "values", "reused_fields")]
    print(f"{'metric':28}" + "".join(f"{n:>14}" for n in report))
    for key in keys:
        print(f"{key:28}" + "".join(f"{str(report[n][key]):>14}" for n in report))
    for name in report:
        print(f"\n{name}: reused {report[name]['reused_fields']}")
        print("  cargo_volume_l values:", report[name]["values"].get("cargo_volume_l"),
              "state:", report[name]["states"].get("cargo_volume_l"))
    print(json.dumps({n: r["states"] for n, r in report.items()}, indent=1)[:2000])
