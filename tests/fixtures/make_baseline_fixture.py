"""Regenerate the synthetic GLM-5.3 baseline fixture (batch 20261001T185509Z-glm-5.3-one, vehicle 101122).

The real baseline run directory is not committed, so this reproduces its failure
pattern in the event format the pre-refactor code wrote:

* run_started with max_steps=30 and the old glm_config (no attempts / finalizer keys);
* 30 research steps: searches, HTML fetches, PDF fetches, document queries, evidence,
  one HTTP 403 page; step 30 is still fetching another PDF;
* then the old "budget exhausted" finalization call: repeated 240 s ReadTimeouts on
  chat/completions (old client: no request_kind / timeout / usage_unknown keys);
* the process was stopped before anything else: no run_finished, no result.json,
  no documents/ export. The documents only exist in the shared cache.

    python tests/fixtures/make_baseline_fixture.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "baseline_runs"
BATCH = "20261001T185509Z-glm-5.3-one"
RECORD = "101122"
T0 = datetime(2026, 10, 1, 18, 55, 9, tzinfo=timezone.utc)
CHAT = "chat/completions"
SEARCH = "web_search"

sys.path.insert(0, str(HERE.parent.parent))
from src.storage.cache import document_id_for  # noqa: E402


def ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


DOCS = [  # (kind, url, title, content_type, doc_type, text)
    ("fetch", "https://www.xpeng.co.il/g6", "XPENG G6 | אקספנג ישראל", "text/html; charset=utf-8", "html",
     "XPENG G6 MAX AWD\nמחיר: 229,990 ₪\nסוללה 87.5 kWh\nטווח WLTP 550 ק\"מ\nטעינה מהירה DC עד 280 kW\n"
     "0-100 קמ\"ש 4.1 שניות\nאחריות 7 שנים או 150,000 ק\"מ\nאחריות סוללה 8 שנים או 160,000 ק\"מ"),
    ("pdf", "https://www.xpeng.co.il/media/g6-pricelist-2026.pdf", "", "application/pdf", "pdf",
     "[page 1]\nG6 2026 price list\nMAX AWD NSGHA 229,990\nMAX RWD 199,990\n[page 2]\nTires 255/45 R20"),
    ("fetch", "https://www.xpeng.com/g6", "XPENG G6", "text/html", "html",
     "G6 Performance AWD\nLength 4753 mm Width 1920 mm Height 1650 mm Wheelbase 2890 mm\n"
     "Max torque 660 Nm\nTop speed 202 km/h\nCLTC range 650 km"),
    ("pdf", "https://www.xpeng.com/eu/g6/specs.pdf", "", "application/pdf", "pdf",
     "[page 1]\nXPENG G6 Technical specifications (EU)\nAWD Performance 87.5 kWh\nWLTP range 550 km\n"
     "DC 10-80% 20 min\nAC on-board charger 11 kW\nBoot 571 l\n[page 2]\nGround clearance 160 mm"),
    ("fetch", "https://www.carsales.example/xpeng-g6-review", "XPeng G6 review", "text/html", "html",
     "Review: the G6 Performance AWD does 0-100 in 4.1 s; real-world range around 470 km; "
     "the EU car uses 255/45R20 tyres; some markets list 551 litres of boot space"),
    ("fetch", "https://forbidden.example/g6", "403 Forbidden", "text/html", "html", "Access denied"),
    ("pdf", "https://www.xpeng.com/eu/g6/warranty.pdf", "", "application/pdf", "pdf",
     "[page 1]\nWarranty: 5 years / 100,000 km vehicle; battery 8 years / 160,000 km"),
    ("pdf", "https://www.xpeng.com/au/g6/brochure.pdf", "", "application/pdf", "pdf",
     "[page 1]\nG6 Brochure (Australia)\n14.96\" centre display\nWireless Apple CarPlay\nPanoramic glass roof"),
]


def build() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    run_dir = ROOT / BATCH / RECORD
    run_dir.mkdir(parents=True)
    cache_docs = ROOT / "_cache" / "documents"
    doc_ids = []
    for kind, url, title, ctype, doc_type, text in DOCS:
        doc_id = document_id_for(kind, url)
        doc_ids.append(doc_id)
        folder = cache_docs / doc_id
        folder.mkdir(parents=True)
        body = (f"<html><title>{title}</title><body>{text}</body></html>" if doc_type == "html" else
                "%PDF-1.4 synthetic").encode("utf-8")
        status = 403 if "forbidden" in url else 200
        meta = {"document_id": doc_id, "kind": kind, "url": url, "fetched_at": "2026-10-01T18:56:00+00:00",
                "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "text_chars": len(text),
                "status": status, "final_url": url, "redirects": [], "headers": {"content-type": ctype},
                "content_type": ctype, "truncated": False, "doc_type": doc_type,
                "extraction_path": "pdfplumber" if doc_type == "pdf" else "html.visible_text"}
        if doc_type == "html":
            meta["title"] = title
        else:
            meta.update(pages=text.count("[page "), pdf_metadata={})
        (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        (folder / "body.bin").write_bytes(body)
        (folder / "text.txt").write_text(text, "utf-8")

    glm_config = {"model": "glm-5.3", "base_url": "https://api.z.ai/api/paas/v4", "chat_path": CHAT,
                  "search_path": SEARCH, "search_engine": "search-prime", "timeout_s": 240.0,
                  "thinking": "provider_default", "max_tokens": "provider_default",
                  "temperature": "provider_default", "tool_choice": "auto", "extra_request_body": {},
                  "search_backend": "glm", "recorded_at": ts(0)}
    pricing = {"input_per_mtok": 1.4, "output_per_mtok": 4.4, "web_search_per_call": 0.01,
               "source": "Z.ai official pricing (as provided 2026-10-01)"}
    agent_config = {"max_steps": 30, "max_tool_output_chars": 12000, "keep_recent_tool_results": 8,
                    "compact_tool_output_chars": 1200, "temperature": None, "max_tokens": None,
                    "include_level3": True, "thinking": "", "extra_body": {}}
    batch = {"batch_id": BATCH, "created_at": ts(0), "model": "glm-5.3", "glm_config": glm_config,
             "prompt_version": "baseline-pv", "search_backend": "glm", "pricing": pricing,
             "level15_source": "snapshot", "level15_note": "Frozen snapshot", "selection": "One vehicle",
             "record_ids": [RECORD], "agent_config": agent_config,
             "tool_config": {"search_backend": "glm", "max_response_bytes": 15728640, "connect_timeout_s": 10.0,
                             "read_timeout_s": 40.0, "render_timeout_s": 45.0}}
    (ROOT / BATCH / "batch.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1), "utf-8")
    payload = {"identity": {"manufacturer": "אקספנג", "manufacturer_entity": "אקספנג סין", "commercial_name": "G6",
                            "year": 2026, "trim": "MAX", "model_code": "NSGHA", "segment": "private",
                            "government_record_id": RECORD},
               "engine_drivetrain": {"power_hp": 486, "fuel": "חשמל", "fuel_normalized": "electric",
                                     "propulsion_normalized": "battery_electric", "drivetrain_normalized": "awd"},
               "raw_row": {"upstream_record_id": RECORD, "degem_nm": "NSGHA"}}
    (run_dir / "input.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")

    events: list[dict] = []
    clock = [0.0]

    def ev(kind: str, advance: float = 0.5, **data) -> None:
        clock[0] += advance
        events.append({"seq": len(events) + 1, "ts": ts(clock[0]), "kind": kind, **data})

    ev("run_started", model="glm-5.3", record_id=RECORD, prompt_version="baseline-pv", max_steps=30,
       search_backend="glm", glm_config=glm_config, pricing=pricing)
    evidence_n = [0]

    def model_turn(step: int, calls: list[tuple[str, dict]], content: str = "") -> list[dict]:
        tool_calls = [{"id": f"call_{step}_{i}", "type": "function",
                       "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                      for i, (name, args) in enumerate(calls)]
        latency = 9000 + step * 600
        ev("api_call", advance=latency / 1000, endpoint=CHAT, attempt=1, latency_ms=latency, status=200)
        ev("model_response", finish_reason="tool_calls",
           usage={"prompt_tokens": 5000 + step * 2500, "completion_tokens": 400, "total_tokens": 5400 + step * 2500,
                  "prompt_tokens_details": {"cached_tokens": 0}},
           latency_ms=latency, response_meta={"id": f"resp-{step}", "model": "glm-5.3"}, content=content,
           reasoning_content=f"Step {step}: plan the next research action for the XPeng G6 MAX NSGHA.",
           tool_calls=tool_calls)
        return tool_calls

    def fetch(step: int, call: dict, name: str, url: str) -> str:
        index = next(i for i, d in enumerate(DOCS) if d[1] == url)
        kind, _, title, ctype, doc_type, text = DOCS[index]
        doc_id = doc_ids[index]
        status = 403 if "forbidden" in url else 200
        summary = {"document_id": doc_id, "cache_hit": False, "url": url, "final_url": url, "status": status,
                   "content_type": ctype, "bytes": 2000, "text_chars": len(text), "truncated": False,
                   "title": title, "pages": text.count("[page ") or None,
                   "extraction_path": "pdfplumber" if doc_type == "pdf" else "html.visible_text",
                   "text_preview": text[:3000],
                   "hint": "Use extract_html / extract_tables / find_in_document / get_structured_data with this document_id."}
        ev("document", document={k: v for k, v in summary.items() if k != "text_preview"})
        ev("tool_result", step=step, call_id=call["id"], name=name, result={**summary, "_elapsed_ms": 900})
        return doc_id

    def evidence(step: int, call: dict, args: dict) -> None:
        evidence_n[0] += 1
        item = {"evidence_id": f"e{evidence_n[0]}", "stored_at": ts(clock[0]),
                **{k: v for k, v in args.items() if v is not None}}
        ev("evidence", evidence=item)
        ev("tool_result", step=step, call_id=call["id"], name="store_evidence",
           result={"evidence_id": item["evidence_id"], "stored": True, "_elapsed_ms": 1})

    def search(step: int, call: dict, query: str, urls: list[str]) -> None:
        ev("api_call", endpoint=SEARCH, attempt=1, latency_ms=1800, status=200)
        ev("search", query=query, domain=None, backend="glm", result_count=len(urls), cache_hit=False)
        ev("tool_result", step=step, call_id=call["id"], name="search_web",
           result={"query": query, "domain": None, "backend": "glm", "cache_hit": False, "_elapsed_ms": 1900,
                   "results": [{"title": u.rsplit("/", 1)[-1], "url": u, "snippet": "…", "site": "", "published": "",
                                "icon": "", "refer": str(i)} for i, u in enumerate(urls)]})

    plan = []
    urls = [d[1] for d in DOCS]
    for step in range(1, 31):
        if step == 30:
            plan.append([("fetch_pdf", {"url": urls[7]})])  # still fetching another PDF at the budget
        elif step % 5 == 1:
            plan.append([("search_web", {"query": f"XPeng G6 2026 MAX NSGHA specs {step}"})])
        elif step % 5 == 2:
            plan.append([("fetch_url" if DOCS[(step // 5) % 7][4] == "html" else "fetch_pdf",
                          {"url": urls[(step // 5) % 7]})])
        elif step % 5 == 3:
            plan.append([("find_in_document", {"document_id": doc_ids[(step // 5) % 7], "query": "kWh"})])
        elif step % 5 == 4:
            plan.append([("store_evidence", {"field": ["battery_gross_kwh", "electric_range_km", "torque_nm",
                                                       "list_price", "vehicle_warranty", "cargo_volume_l"][(step // 5) % 6],
                                             "value": ["87.5", "550", "660", "229,990 ILS", "7y/150,000 km",
                                                       "571"][(step // 5) % 6],
                                             "source_url": urls[(step // 5) % 7],
                                             "quote": "verbatim fragment",
                                             "note": "EU brochure differs from the Israeli importer page"
                                             if step == 24 else None})])
        else:
            plan.append([("search_web", {"query": f"XPeng G6 NSGHA importer Israel {step}"})])

    for step, calls in enumerate(plan, start=1):
        tool_calls = model_turn(step, calls, content="Continuing research." if step == 15 else "")
        for call, (name, args) in zip(tool_calls, calls):
            ev("tool_call", step=step, call_id=call["id"], name=name, arguments=call["function"]["arguments"])
            if name == "search_web":
                search(step, call, args["query"], urls[:4])
            elif name in ("fetch_url", "fetch_pdf"):
                fetch(step, call, name, args["url"])
            elif name == "find_in_document":
                text = DOCS[doc_ids.index(args["document_id"])][5]
                pos = max(0, text.find("kWh"))
                ev("tool_result", step=step, call_id=call["id"], name=name,
                   result={"document_id": args["document_id"], "query": "kWh", "scope": "text",
                           "hits": [{"offset": pos, "match_score": 1.0, "snippet": text[max(0, pos - 60):pos + 60]}]
                           if "kWh" in text else [], "hit_count": 1 if "kWh" in text else 0, "_elapsed_ms": 3})
            elif name == "store_evidence":
                evidence(step, call, args)

    # Budget exhausted -> old FINALIZE_PROMPT call with the full history: repeated read timeouts.
    for attempt in range(1, 4):
        ev("api_error", advance=240.5, endpoint=CHAT, attempt=attempt, latency_ms=240012, status=None,
           error=("ReadTimeout: HTTPSConnectionPool(host='api.z.ai', port=443): Read timed out. "
                  "(read timeout=240.0)"), body=None, headers={})
    with (run_dir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    build()
    print(f"wrote {ROOT}")
