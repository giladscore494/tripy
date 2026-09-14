# -*- coding: utf-8 -*-
"""MILO R5 Source Capture - Streamlit entrypoint (Hebrew RTL interface).

All networking lives in capture.py, which is importable without Streamlit.
This module renders the interface only.

There is deliberately no URL input, no database, no persistent server storage,
no authentication, no user account, no analytics and no model provider.
"""

import streamlit as st

import capture

STATE_KEY = "milo_r5_capture_result"

RTL_STYLE = """
<style>
  .stApp, .main, section.main { direction: rtl; }
  .stApp h1, .stApp h2, .stApp h3, .stApp p, .stApp li, .stApp label,
  .stApp .stMarkdown, .stApp .stAlert { direction: rtl; text-align: right; }
  .stApp code, .stApp pre, .ltr { direction: ltr; text-align: left;
      unicode-bidi: embed; display: inline-block; }
  .stApp [data-testid="stDataFrame"] { direction: ltr; text-align: left; }
  .stApp .stButton > button { direction: rtl; }
</style>
"""

STATUS_PRESENTATION = {
    capture.STATUS_READY: (
        "success",
        "מוכן לבדיקת חבילת R5",
        "כל מקורות הממשלה "
        "עברו אימות, נמצאו "
        "רשומות מועמדות, "
        "ושני דפי טויוטה "
        "הרשמיים עברו אימות.",
    ),
    capture.STATUS_NO_RECORDS: (
        "warning",
        "לא נמצאו רשומות "
        "ממשלתיות תואמות",
        "הקריאות לממשלה "
        "היו תקפות, אך החיפוש "
        "הממוקד לא החזיר "
        "מועמדי RAV4.",
    ),
    capture.STATUS_INCOMPLETE_WEB: (
        "warning",
        "מקור אינטרנט "
        "רשמי חסר",
        "לכידת נתוני הממשלה "
        "הצליחה, אך אחד מדפי "
        "טויוטה אינו זמין "
        "או נכשל באימות.",
    ),
    capture.STATUS_FAILED: (
        "error",
        "הלכידה נכשלה",
        "כשל רשת, HTTP, הפניה, "
        "גודל, JSON, דפדוף או "
        "שלמות מנע לכידה "
        "תקפה.",
    ),
}

H = {
    "title": "MILO R5 – לכידת מקורות",
    "run": "התחל לכידה",
    "reset": "איפוס לכידה",
    "purpose_h": "מטרה",
    "sources_h": "מקורות קבועים",
    "results_h": "תוצאות לפי מקור",
    "download_h": "הורדה",
    "trouble_h": "איתור תקלות",
    "no_run": "עדיין לא בוצעה "
              "לכידה במושב זה.",
    "running": "מבצע לכידה…",
    "done": "הלכידה הושלמה",
    "requesting": "שולח בקשה",
}

COLUMNS_HE = {
    "source_id": "מזהה מקור",
    "http": "HTTP",
    "validation": "אימות",
    "final_url": "כתובת סופית",
    "records": "רשומות",
    "bytes": "באייטים",
    "attempts": "ניסיונות",
    "sha256": "SHA-256",
    "error": "סיבת כשל",
}


def _results_rows(entries):
    rows = []
    for entry in entries:
        rows.append(
            {
                COLUMNS_HE["source_id"]: entry["source_id"],
                COLUMNS_HE["http"]: entry["http_status"],
                COLUMNS_HE["validation"]: entry["validation_result"],
                COLUMNS_HE["final_url"]: entry["final_url"],
                COLUMNS_HE["records"]: entry.get("returned_record_count"),
                COLUMNS_HE["bytes"]: entry["byte_count"],
                COLUMNS_HE["attempts"]: entry["attempts"],
                COLUMNS_HE["sha256"]: entry["sha256"],
                COLUMNS_HE["error"]: entry["error"] or "",
            }
        )
    return rows


def _render_purpose():
    st.subheader(H["purpose_h"])
    st.write(
        "הכלי לוכד מערכת "
        "קבועה ומוגבלת של "
        "מקורות ציבוריים "
        "– נתוני ממשלה (CKAN) "
        "ושני דפים רשמיים "
        "של טויוטה ישראל "
        "– שומר את גוף "
        "התגובה בדיוק "
        "כפי שהתקבל, מאמת "
        "אותו ומפיק קובץ ZIP "
        "מאומת עבור MILO R5."
    )
    st.info(
        "**לא נדרש מפתח API "
        "ולא נעשה שימוש "
        "במפתח.** כל המקורות "
        "ציבוריים ולקריאה "
        "בלבד. האפליקציה "
        "שולחת בקשות `GET` "
        "בלבד, ללא כותרת "
        "`Authorization`, ללא עוגיות "
        "וללא סודות. אין "
        "שדה להזנת כתובות "
        "– רשימת המקורות "
        "קבועה בקוד."
    )


def _render_sources():
    st.subheader(H["sources_h"])
    for source_id, source_type, url in capture.build_plan_preview():
        st.markdown(
            "- `{0}` – {1}<br/><code class='ltr'>{2}</code>".format(
                source_id, source_type, url
            ),
            unsafe_allow_html=True,
        )
    st.caption(
        "שאילתות הממשלה "
        "מדפדפות באמצעות "
        "`offset`/`limit` עד לקליטת כל "
        "התוצאות שדווחו "
        "עבור אותה שאילתה. "
        "הכתובת המיושנת "
        "{0} אינה נשלחת.".format(
            capture.TOYOTA_PREVIOUS_OBSOLETE_URL
        )
    )


def _render_status(result):
    kind, headline, explanation = STATUS_PRESENTATION[result["overall_status"]]
    banner = {"success": st.success, "warning": st.warning, "error": st.error}[kind]
    banner("**{0}**\n\n`{1}`\n\n{2}".format(headline, result["overall_status"], explanation))

    if result["overall_status"] != capture.STATUS_READY:
        st.warning(
            "**הארכיון חלקי "
            "(INCOMPLETE) ואינו מוכן "
            "ל-R5.** זוהי חבילת "
            "אבחון בלבד. אין "
            "להתייחס אליה "
            "כאל עדות מאומתת "
            "ואין לשנות את שם "
            "הקובץ."
        )

    for failure in result["government_failures"]:
        st.error("ממשלה: `{0}`".format(failure))
    for failure in result["web_failures"]:
        st.warning("טויוטה: `{0}`".format(failure))

    for entry in result["entries"]:
        if entry["validation_result"] == capture.RESULT_ARCHIVED_MODEL:
            st.info(
                "דף הדגם הרשמי "
                "מאשר **זהות דגם "
                "וסטטוס ארכיון "
                "בלבד** – לא מפרט "
                "טכני. לא הוסק "
                "שום נתון מהכתובת "
                "או משם הקובץ. "
                "נתוני CKAN הם המקור "
                "הטכני המובנה. "
                "נדרשת בדיקת "
                "אנוש של ה-HTML השמור."
            )


def _render_integrity(result):
    if result["integrity_verified"]:
        st.caption(
            "שלמות אומתה: גדלי "
            "הקבצים וטביעות "
            "SHA-256 חושבו מחדש והושווו "
            "מול manifest.json, ו-SHA256SUMS.txt אומת "
            "לפני בניית ה-ZIP."
        )
    else:
        st.error(
            "אימות שלמות נכשל:\n\n"
            + "\n".join("- `{0}`".format(p) for p in result["integrity_problems"])
        )


def _render_troubleshooting():
    with st.expander(H["trouble_h"]):
        st.markdown(
            "- `transport_error` – השרת לא "
            "הצליח לצאת "
            "החוצה. יש לוודא "
            "שהסביבה מאפשרת "
            "גישה ל-`data.gov.il` ו-`toyota.co.il`.\n"
            "- `failed_http_status` – המקור "
            "החזיר קוד שאינו "
            "200. אם הכתובת הרשמית "
            "שונתה – יש לעדכן "
            "את הקבועים ב-`capture.py`.\n"
            "- `failed_blocking_page` – התקבל דף "
            "חסימה/CAPTCHA/WAF.\n"
            "- `failed_missing_archive_indication` – הדף "
            "נקלט אך לא נמצא "
            "בו סימן לסיום "
            "שיווק.\n"
            "- `pagination_*` – דפדוף לא "
            "התקדם או החזיר "
            "סכומים לא עקביים; "
            "הלכידה נכשלת "
            "במכוון ולא "
            "מקצצת תוצאות."
        )


def main():
    st.set_page_config(page_title="MILO R5", page_icon="\U0001f4e6")
    st.markdown(RTL_STYLE, unsafe_allow_html=True)
    st.title(H["title"])

    _render_purpose()
    _render_sources()

    run_clicked = st.button(H["run"], type="primary")
    reset_clicked = st.button(H["reset"])

    if reset_clicked:
        st.session_state.pop(STATE_KEY, None)
        st.rerun()

    if run_clicked:
        progress_bar = st.progress(0.0, text=H["running"])
        planned = len(capture.build_plan_preview())

        def on_progress(source_id, completed):
            fraction = min(completed / float(max(planned, 1)), 0.99)
            progress_bar.progress(
                fraction, text="{0}: {1}".format(H["requesting"], source_id)
            )

        # Deliberately not cached: every run performs its own live requests.
        result = capture.run_and_package(progress=on_progress)
        progress_bar.progress(1.0, text=H["done"])
        st.session_state[STATE_KEY] = result

    result = st.session_state.get(STATE_KEY)
    if not result:
        st.caption(H["no_run"])
        _render_troubleshooting()
        return

    _render_status(result)

    st.subheader(H["results_h"])
    st.dataframe(_results_rows(result["entries"]))
    _render_integrity(result)

    st.subheader(H["download_h"])
    st.caption(
        "`{0}` – {1} bytes – sha256 `{2}`".format(
            result["archive_filename"], result["archive_byte_count"], result["archive_sha256"]
        )
    )
    st.download_button(
        label="{0} – {1}".format(H["download_h"], result["archive_filename"]),
        data=result["archive_bytes"],
        file_name=result["archive_filename"],
        mime="application/zip",
    )

    _render_troubleshooting()


if __name__ == "__main__":
    main()
