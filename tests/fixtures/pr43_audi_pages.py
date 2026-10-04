"""SYNTHETIC Audi documents for PR #43 (hybrid / combustion binding precision). NOT production artifacts.

Written from the excerpts of production run 20261004T171912Z (records 15376, 15400, 94995) quoted in the PR #43 brief
and read through the MCP:

    q8_2020   the 2020 cartube news article on the first-generation Q8 55 / 60 TFSI e (d_07e2368751441913): the quote of
              the wrong acceleration 5.8 s ("381 כ"ס ... 5.8 שניות ... בדגם Q8 60 TFSI e הבכיר ... 462 כ"ס")
    q8_2025   a single-version 2025 page of the Q8 55 TFSI e (engine 340 hp, an electric motor, 5.7 s)
    q7_2024   the 2024 cartube Q7 TFSIe article (d_b145de40994211aa): "Q7 S-line 55 TFSIe quattro ... חישוקי "21" and
              "Q7 S-line 60 TFSIe quattro / הבכיר יותר מוסיף לרשימה הזו חישוקי "22"; `with_table` adds a spec table whose
              columns state each version's engine power (a hypothetical page, for the designation link)
    q3_40tfsi the cartube per-version page "2.0 40TFSI 4X4 2024" (d_a1aa908f89b78577) of the Q3 record 94995
"""

from __future__ import annotations

# --- targets (Level 1.5 payload parts binding reads) ------------------------------------------------------------------

Q8_PAYLOAD = {"identity": {"manufacturer": "אאודי", "commercial_name": "Q8", "year": 2025, "trim": "SLINE SUPER",
                           "model_code": "4MT023", "government_record_id": "15376",
                           "government_codes": {"tozeret_cd": 21, "degem_cd": 260, "sug_degem": "P"}},
              "engine_drivetrain": {"engine_cc": 2995, "power_hp": 340, "propulsion_normalized": "plug_in",
                                    "drivetrain_normalized": "awd", "automatic": 1},
              "structure": {"body_normalized": "suv"}}
Q7_PAYLOAD = {"identity": {"manufacturer": "אאודי", "commercial_name": "Q7", "year": 2025, "trim": "S LINE",
                           "government_record_id": "15400"},
              "engine_drivetrain": {"engine_cc": 2995, "power_hp": 340, "propulsion_normalized": "plug_in",
                                    "drivetrain_normalized": "awd", "automatic": 1},
              "structure": {"body_normalized": "suv"}}
Q3_PAYLOAD = {"identity": {"manufacturer": "אאודי", "commercial_name": "Q3", "year": 2024, "trim": "SLINE",
                           "government_record_id": "94995"},
              "engine_drivetrain": {"engine_cc": 1984, "power_hp": 190, "propulsion_normalized": "conventional",
                                    "drivetrain_normalized": "awd", "automatic": 1},
              "structure": {"body_normalized": "suv"}}

NAV = "<header><nav><ul><li>ראשי</li><li>מחירון רכב חדש</li><li>חדשות רכב</li></ul></nav></header>"

# --- Q8: the 2020 article (H1 + H3) -----------------------------------------------------------------------------------

Q8_2020_URL = "https://www.cartube.co.il/חדשות-רכב/פלאג-אין-אודי-חושפת-q8-tfsi-e-היברידי"
Q8_2020_QUOTE = ('בדגם Q8 55 TFSI e יחידת ההנעה הזו מייצרת הספק מרבי של 381 כ"ס ומונט מרבי של 61.2 קג"מ, מספיק לנתון '
                 'תאוצה 0-100 קמ"ש של 5.8 שניות ולמהירות מרבית של 240 קמ"ש מוגבלת אלקטרונית. בדגם Q8 60 TFSI e הבכיר '
                 'ההספק המרבי עומד על 462 כ"ס')


def q8_2020_html() -> str:
    return f"""<html><head><title>פלאג-אין: אודי חושפת Q8 TFSI e היברידי - cartube</title></head><body>{NAV}<main>
<h1>פלאג-אין: אודי חושפת Q8 TFSI e היברידי</h1>
<p>אודי משיקה שתי גרסאות פלאג-אין לקרוסאובר Q8 בהספקים של עד 462 כ"ס וטווח נסיעה חשמלי של עד 47 ק"מ.</p>
<p>חדשות רכב</p><p>cartube</p><p>אוקטובר 13, 2020</p>
<p>שתי יחידות ההנעה ההיברידיות פלאג-אין של אודי Q8 מבוססות על מנוע 3.0 ליטר V6 טורבו בהספק מרבי של 340 כ"ס ומומנט מרבי
של 45.9 קג"מ ומנוע חשמלי בהספק של 135 כ"ס המשולב בתוך תיבת ה-8 הילוכים האוטומטית של הדגם המעבירה את הכוח לכל ארבעת
הגלגלים דרך מערכת הנעה כפולה קוואטרו.</p>
<p>{Q8_2020_QUOTE} והמומנט המרבי על 71.3 קג"מ, כאן זה כבר מספיק לנתון תאוצה 0-100 קמ"ש של 5.4 שניות ולמהירות מרבית זהה.</p>
<p>2021 אודי Q8 פלאג-אין</p>
<h2>לקריאה נוספת</h2><p>אודי Q8 החדש 2024 נחת בישראל - מחיר החל מ-720 אלף שקל</p>
<p>רביעי, 2026.09.16</p>
</main><footer><p>זכויות יוצרים © 2026 cartube. כל הזכויות שמורות.</p></footer></body></html>"""


# --- Q8: a single-version 2025 page (H1: the designation region) ------------------------------------------------------

Q8_2025_URL = "https://www.audi.co.il/il/web/he/models/q8/q8-55-tfsi-e.html"


def q8_2025_html() -> str:
    return f"""<html><head><title>אאודי Q8 55 TFSI e quattro פלאג-אין 2025 | Audi Israel</title></head><body>{NAV}<main>
<h1>Audi Q8 TFSI e 2025</h1>
<p>קרוסאובר פנאי-שטח פלאג-אין עם הנעה כפולה quattro.</p>
<h2>Q8 55 TFSI e quattro 340 כ"ס</h2>
<p>מנוע בנזין 3.0 ליטר V6 בהספק מרבי של 340 כ"ס</p>
<p>מנוע חשמלי בהספק מרבי של 136 כ"ס</p>
<p>תאוצה 0-100 קמ"ש: 5.7 שניות</p>
<p>מהירות מרבית: 240 קמ"ש</p>
</main></body></html>"""


# --- Q7: the 2024 article (H2) -----------------------------------------------------------------------------------------

Q7_URL = "https://www.cartube.co.il/חדשות-רכב/אודי-q7-tfsie-פלאג-אין-החדש-2024-בישראל-מחיר-682000-שקל"
Q7_QUOTE_21 = ('מצויד ברשימת אבזור ארוכה הכוללת, בין היתר: לוח מחוונים דיגיטלי "12.3, חלון גג פנורמי, ריפודי עור, '
               'דלת תא מטען חשמלית, חישוקי "21 ועוד ועוד.')
Q7_QUOTE_22 = 'הבכיר יותר מוסיף לרשימה הזו חישוקי "22, תצוגה עילית, מערכת היגוי אחורית, חימום להגה.'
Q7_TABLE = """<h2>מפרט טכני</h2>
<table><tr><td>גרסה</td><td>Q7 55 TFSIe quattro</td><td>Q7 60 TFSIe quattro</td></tr>
<tr><td>הספק מנוע בנזין (כ"ס)</td><td>340</td><td>490</td></tr>
<tr><td>הנעה</td><td>quattro</td><td>quattro</td></tr></table>"""


def q7_html(with_table: bool = False) -> str:
    return f"""<html><head><title>אודי Q7 TFSIe פלאג-אין החדש 2024 בישראל - מחיר החל מ-682,000 שקל - cartube</title>
</head><body>{NAV}<main>
<h1>אודי Q7 TFSIe פלאג-אין החדש 2024 בישראל - מחיר החל מ-682,000 שקל</h1>
<p>מאת: רן אלקובי</p><p>יולי 22, 2024</p>
<p>אודי ישראל מודיעה על תחילת השיווק של אודי Q7 החדש לשנת 2024 בשני דגמי פלאג-אין TFSIe.</p>
<p>דגם Q7 55 TFSIe עם הספק כולל של 394 כ"ס</p>
<p>דגם Q7 60 TFSIe עם הספק כולל של 489 כ"ס</p>
<p>מערכת פלאג-אין על בסיס מנוע 3.0 ליטר V6 ומנוע חשמלי 175 כ"ס</p>
<h2>אודי Q7 החדש - אבזור נוחות ובטיחות:</h2>
<p>אודי<br>Q7 S-line 55 TFSIe quattro<br>{Q7_QUOTE_21}</p>
<p>דגם<br>Q7 S-line 60 TFSIe quattro<br>{Q7_QUOTE_22}</p>
{Q7_TABLE if with_table else ""}
</main></body></html>"""


# --- Q3: the cartube per-version page (H4) ----------------------------------------------------------------------------

Q3_URL = "https://www.cartube.co.il/מחירון-רכב-חדש/10-אודי/49-אודי-q3/327-2-0-40tfsi-4x4-2024"
Q3_ROWS = (("מחיר", "263,990 ₪"), ("הספק", '190 כ"ס'), ("נפח מנוע", '1,984 סמ"ק'), ("תאוצה 0-100", "7.3 שנ'"),
           ("מהירות מרבית", 'קמ"ש 222'), ("תיבת הילוכים", "S tronic אוטומטית"), ("אורך", 'מ"מ 4,484'),
           ("רוחב", 'מ"מ 1,849'), ("גובה", 'מ"מ 1,616'))


def q3_html() -> str:
    rows = "".join(f'<div class="spec-row"><div class="k">{k}</div><div class="v">{v}</div></div>' for k, v in Q3_ROWS)
    return f"""<html><head><title>אודי Q3 2.0 40TFSI 4X4 2024 - מחירון רכב חדש - cartube</title></head><body>{NAV}
<main><h1>אודי Q3</h1>
<h2>אודי Q3 2.0 40TFSI 4X4</h2><div class="spec-list">{rows}</div>
</main></body></html>"""


# --- the synthetic runs of the proof gate (scripts/pr43_proof_gate.py) ----------------------------------------------------

G6_DIMENSIONS = "אורך x רוחב x גובה | 4,758 / 1,920 / 1,650 מ״מ"
Q8_GEARBOX = 'מנוע חשמלי בהספק של 135 כ"ס המשולב בתוך תיבת ה-8 הילוכים האוטומטית של הדגם'
# record -> (payload, propulsion, documents {url: html}, requests (field, value, quote, url))
RUNS = {
    "15376": (Q8_PAYLOAD, {Q8_2020_URL: q8_2020_html, Q8_2025_URL: q8_2025_html}, (
        ("acceleration_0_100_s", 5.8, Q8_2020_QUOTE, Q8_2020_URL),          # production e1 (wrong: the 2020 55 TFSI e)
        ("gearbox_type", "automatic", Q8_GEARBOX, Q8_2020_URL),              # production e17
        ("gear_count", 8, Q8_GEARBOX, Q8_2020_URL),                         # production e19
        ("top_speed_kmh", 240, 'מהירות מרבית: 240 קמ"ש', Q8_2025_URL))),
    "15400": (Q7_PAYLOAD, {Q7_URL: q7_html}, (
        ("rim_diameter_in", 22, Q7_QUOTE_22, Q7_URL),),),                   # production (wrong: the senior 60 TFSIe)
    "94995": (Q3_PAYLOAD, {Q3_URL: q3_html}, (
        ("acceleration_0_100_s", 7.3, "תאוצה 0-100 | 7.3 שנ'", Q3_URL),),),
}


def build(dest, g6: bool = True):
    """Write the synthetic runs and their document cache under <dest>/runs (run this with the code the recorded
    bindings should describe: main before PR #43). The G6 run holds the height evidence the production run recorded
    from the full dimension quote (admission on main refuses that quote; its record is the one admitted from the
    number-only quote, with the production quote and binding: body_powertrain / unclear, as e16 of
    20261003T221935Z)."""
    import json as _json
    from pathlib import Path

    from fixtures import pr42_g6_pages as G
    from fixtures.corolla_harvest import put
    from src.candidate_harvest import harvest_document
    from src.evidence_admission import AdmissionContext, admit
    from src.fields import resolve_requested_fields
    from src.storage.cache import DocumentCache

    dest = Path(dest)
    cache = DocumentCache(dest / "runs" / "_cache")
    runs = dict(RUNS)
    if g6:
        runs["101122"] = (G.PAYLOAD, {G.PAGE_URL: G.page_html}, (
            ("height_mm", 1650, "4,758 / 1,920 / 1,650 מ״מ", G.PAGE_URL),))
    out = []
    for record, (payload, documents, requests) in runs.items():
        propulsion = payload["engine_drivetrain"]["propulsion_normalized"]
        fields = sorted({r[0] for r in requests} | ({"length_mm", "width_mm", "height_mm"} if record == "101122"
                                                   else set()))
        specs = resolve_requested_fields(fields, propulsion=propulsion)
        adm = AdmissionContext.for_run(payload, None, specs, "IL")
        docs = {url: put(cache, url, html(), "html") for url, html in documents.items()}
        events = [{"kind": "run_started", "seq": 1, "target_market": "IL", "record_id": record, "vehicle_label": {},
                   "requested_field_specs": [{"name": s["name"], "applicable": True} for s in specs]}]
        for url, doc in docs.items():
            cands, _ = harvest_document(cache, doc, specs)
            events.append({"kind": "candidates_harvested", "seq": len(events) + 1, "document_id": doc, "url": url,
                           "candidates": cands})
        for i, (field, value, quote, url) in enumerate(requests, start=1):
            decision = admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": docs[url]},
                             list(docs.values()))
            if not decision["accepted"]:
                raise AssertionError((record, field, value, decision))
            record_ = {**decision["record"], "evidence_id": f"e{i}"}
            if record == "101122":
                record_.update(quote=G6_DIMENSIONS, binding_level="body_powertrain", variant_match="unclear")
                for key in ("binding_basis", "binding_rules", "variant_map_region", "market_trim"):
                    record_.pop(key, None)
            events.append({"kind": "evidence", "seq": len(events) + 1, "evidence": record_})
        run_dir = dest / "runs" / "SYNTH-pr43" / record
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "input.json").write_text(_json.dumps(payload, ensure_ascii=False), "utf-8")
        (run_dir / "events.jsonl").write_text("".join(_json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                                              "utf-8")
        out.append(run_dir)
    for path in (dest / "runs" / "_cache" / "documents").glob("*/derived_*"):
        path.unlink()                                   # the replay recomputes every derived extraction
    return out


if __name__ == "__main__":
    import sys

    print(build(sys.argv[1]))
