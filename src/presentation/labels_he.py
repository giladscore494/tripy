"""Hebrew presentation labels for the live operations dashboard and the normal (non-debug) views.

Presentation only: canonical identifiers (field names, states, phases, tool names) stay unchanged in
events, results and raw/debug views. Field and group labels come from the schema
(data/enrichment_fields.json via src/fields.py), never from this module.
"""

from __future__ import annotations

import re

from ..fields import field_display_name, group_display_name  # noqa: F401  (re-exported for the UI)

PHASE_LABELS_HE = {
    "research": "איסוף מקורות ומחקר ראשוני",
    "deterministic_harvest": "סריקה אוטומטית של כל השדות",
    "document_sweep": "בדיקת המועמדים מול המודל",
    "field_detection": "הערכת מצב השדות",
    "field_recovery": "השלמת שדות שעדיין חסרים",
    "finalization": "בניית התוצאה הסופית",
    "finalization_repair": "בניית התוצאה הסופית",
    "recovery_finalization": "בניית התוצאה הסופית (ללא מחקר חוזר)",
}

OP_STATE_LABELS_HE = {
    "queued": "ממתין להתחלה",
    "waiting_model": "ממתין לתור למודל",
    "model_inflight": "המודל חושב…",
    "waiting_search": "ממתין לתור לחיפוש",
    "searching": "מחפש ברשת",
    "fetching": "קורא מקור",
    "extracting": "מחלץ מידע מהמקור",
    "storing_evidence": "שומר ראיה",
    "evaluating": "בודק אילו שדות הושלמו",
    "finalizing": "מרכיב תוצאה סופית",
    "completed": "הושלם",
    "failed": "נכשל",
    "interrupted": "הופסק",
}

FIELD_STATE_LABELS_HE = {
    "ok": "הושלם",
    "not_applicable": "לא רלוונטי",
    "missing": "לא נמצא",
    "unresolved": "עדיין לא הוכרע",
    "conflicting": "מידע סותר",
    "foreign_market_only": "נמצא רק לשוק זר",
    "variant_not_exact": "לא אומת לגרסה המדויקת",
    "weak_provenance": "המקור עדיין לא מספק",
}

TOOL_ACTIONS_HE = {
    "search_web": "מחפש מקורות ברשת",
    "search_official_domains": "מחפש באתרי יצרן/יבואן רשמיים",
    "fetch_url": "קורא דף אינטרנט",
    "fetch_pdf": "מוריד וקורא PDF",
    "render_page": "טוען דף דינמי",
    "find_in_document": "מחפש בתוך מסמך שכבר נאסף",
    "extract_tables": "מחלץ טבלאות",
    "extract_html": "מחלץ תוכן מהעמוד",
    "get_structured_data": "קורא נתונים מובנים",
    "get_cached_document": "קורא מסמך שכבר נאסף",
    "store_evidence": "שומר ראיה לשדה",
    "report_field_status": "מדווח על מצב שדה",
}

TOOL_OP_STATE = {
    "search_web": "searching", "search_official_domains": "searching",
    "fetch_url": "fetching", "fetch_pdf": "fetching", "render_page": "fetching",
    "find_in_document": "extracting", "extract_tables": "extracting", "extract_html": "extracting",
    "get_structured_data": "extracting", "get_cached_document": "extracting",
    "store_evidence": "storing_evidence", "report_field_status": "evaluating",
}

RUN_STATUS_LABELS_HE = {
    "completed": "הושלם",
    "max_steps_finalized": "הושלם (תקציב המחקר נוצל)",
    "no_new_research_finalized": "הושלם (המחקר מוצה)",
    "acquisition_sufficient_finalized": "הושלם (איסוף המקורות הספיק)",
    "under_acquired_finalized": "הושלם (איסוף המקורות מוצה ללא בסיס מינימלי)",
    "completed_unparsed": "הושלם ללא JSON תקין",
    "recovered_finalized": "הושלם לאחר שחזור",
    "finalization_failed": "בניית התוצאה נכשלה",
    "research_failed": "המחקר נכשל",
    "interrupted": "הופסק",
    "incomplete": "לא הושלם",
    "finalization_pending": "ממתין לבניית התוצאה הסופית",
    "error": "נכשל",
}

RESOLUTION_ORIGIN_HE = {
    "primary": "מחקר ראשוני",
    "document_sweep": "בדיקת מועמדים מול המודל",
    "recovery_direct": "Recovery ישיר",
    "recovery_indirect": "נפתר תוך טיפול בשדה אחר",
    "not_applicable": "לא רלוונטי",
    "in_progress": "בטיפול",
    "needs_decision": "דורש הכרעה",
    "open": "עדיין פתוח",
}

# Operational "why" (derived from orchestration metadata; never model reasoning).
WHY_BY_FAILURE_HE = {
    "missing": "עדיין לא נמצאה ראיה עבור השדה \"{field}\".",
    "foreign_market_only": "נמצא מידע על {field}, אבל הוא עדיין לא אומת לשוק הישראלי.",
    "conflicting": "נמצאו כמה ערכים סותרים עבור {field} ונדרש מקור שמכריע ביניהם.",
    "unresolved": "המודל דיווח שהשדה \"{field}\" עדיין לא הוכרע.",
    "variant_not_exact": "נמצא ערך עבור {field}, אבל לגרסה אחרת של הרכב; נדרש מקור לגרסה המדויקת.",
    "weak_provenance": "יש ערך עבור {field} אך ללא ראיה שמורה מאחוריו; נדרש מקור מתועד.",
}
# Recovery clusters (data/enrichment_fields.json recovery_cluster) and tail-triage categories (scheduling only).
CLUSTER_HE = {
    "technical_spec": "מפרט טכני",
    "performance": "ביצועים וצריכה",
    "charging_ev": "טעינה וטווח חשמלי",
    "equipment": "אבזור ונוחות",
    "multimedia": "מולטימדיה",
    "tires_wheels": "צמיגים וחישוקים",
    "commercial": "מחיר ואגרה",
    "warranty": "אחריות",
}
CLUSTER_MODE_HE = {"local_only": "רק במסמכים שכבר הורדו", "web": "כולל חיפוש ברשת"}
WHY_CLUSTER_HE = ("השלמה מקובצת של {count} שדות פתוחים בקבוצה \"{cluster}\" ({mode}): מקור טוב אחד עונה בדרך כלל "
                  "על כמה שדות יחד.")


def cluster_label(name: str | None) -> str:
    return CLUSTER_HE.get(str(name or ""), str(name or ""))


WHY_BY_PHASE_HE = {
    "research": "המערכת אוספת מקורות: מזהה את הגרסה המדויקת ומחפשת מסמכי מפרט רשמיים וישראליים.",
    "deterministic_harvest": "הקוד סורק כל מסמך שכבר הורד ומחלץ ערכים אפשריים לכל השדות, בלי קריאה למודל.",
    "document_sweep": ("הקוד כבר חילץ ערכים אפשריים מהמסמכים. המודל בודק לאיזה שוק וגרסה הם באמת שייכים "
                       "ומחפש במסמכים שכבר הורדו דברים שהקוד פספס."),
    "field_detection": "המערכת בודקת אילו שדות כבר נתמכים בראיות ואילו עדיין דורשים המשך.",
    "field_recovery": "השלמה ממוקדת של שדה שעדיין פתוח לאחר המחקר הראשוני ובדיקת המסמכים.",
    "finalization": "שלב המחקר הסתיים. המודל מרכיב כעת תוצאה מובנית מהראיות שכבר נאספו.",
}

WHY_TITLE_HE = "למה המערכת מבצעת את השלב הזה"
REASONING_TITLE_HE = "חשיבת המודל כפי שהוחזרה מהספק"
NO_REASONING_HE = "הספק לא החזיר תוכן חשיבה עבור התור הזה."
MODEL_THINKING_HE = "המודל חושב…"
DETAIL_LOG_TITLE_HE = "יומן פעילות מפורט"
FIELD_TABLE_TITLE_HE = "התקדמות שדות Level 2"
PROGRESS_CAPTION_HE = "התקדמות זו מודדת מצב מחקר וכיסוי, לא אימות נכונות מול Golden Set."
UNLIMITED_RECOVERY_HE = "ללא מגבלת Recovery כוללת"
FINALIZATION_PENDING_MESSAGE_HE = ("המחקר הושלם וכל הראיות נשמרו.\n\nשלב בניית התוצאה הסופית לא הסתיים.\n"
                                   "ניתן להריץ Finalizer מחדש בלי לבצע שוב את המחקר.")

FIELD_TABLE_COLUMNS_HE = ["קבוצה", "שדה", "מצב", "ערך / ערכים שנמצאו", "מספר ראיות", "שווקים", "מקור אחרון",
                          "סוג מקור", "התאמת גרסה", "ניסיון Recovery", "אופן ההשלמה"]
# Server-side variant binding of the latest evidence item (src/document_binding.py)
VARIANT_MATCH_HE = {"exact": "גרסה מדויקת", "unclear": "גרסה לא ודאית", "different": "גרסה אחרת",
                    "unbound": "המקור אינו מזהה את הדגם"}
# Source authority (src/source_authority.py): who publishes the source, never whether it is right
SOURCE_AUTHORITY_HE = {"government": "ממשלתי", "official_manufacturer": "יצרן רשמי", "official_importer": "יבואן רשמי",
                       "official_media": "הודעות יצרן לעיתונות", "aggregator": "אתר מפרטים", "marketplace": "לוח מודעות",
                       "publisher": "כלי תקשורת", "unknown": "לא מסווג",
                       "government_registry": "מאגר הרישוי הממשלתי"}
EVIDENCE_REJECTED_HE = "ראיות שנדחו בבדיקה"
CANDIDATE_TABLE_COLUMNS_HE = ["שדה", "מועמדים שנמצאו", "מקורות", "ראיה מאומתת", "מצב"]


def model_display_name(model: str | None) -> str:
    """'glm-5.3-flash' -> 'GLM-5.3-Flash' (presentation only)."""
    if not model:
        return "—"
    parts = str(model).split("-")
    return "-".join(p.upper() if p.lower() == "glm" else (p[:1].upper() + p[1:]) for p in parts)


def variant_match_label(value: str | None) -> str:
    return VARIANT_MATCH_HE.get(value or "", "—")


def authority_label(value: str | None) -> str:
    return SOURCE_AUTHORITY_HE.get(value or "", "—")


def state_label(state: str | None) -> str:
    return FIELD_STATE_LABELS_HE.get(state or "", "—")


def phase_label(phase: str | None) -> str:
    return PHASE_LABELS_HE.get(phase or "", "—")


def op_label(op: str | None) -> str:
    return OP_STATE_LABELS_HE.get(op or "", "—")


def tool_action(name: str | None) -> str:
    return TOOL_ACTIONS_HE.get(name or "", "מבצע פעולה")


DECLARATION_LABELS_HE = {**FIELD_STATE_LABELS_HE, "found": "נמצא", "conflict_resolved": "הסתירה הוכרעה"}


def declaration_label(status: str | None) -> str:
    return DECLARATION_LABELS_HE.get(status or "", "—")


def run_status_label(status: str | None) -> str:
    return RUN_STATUS_LABELS_HE.get(status or "", status or "—")


SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def has_raw_identifier(text: str) -> bool:
    """True when user-facing text still contains a snake_case identifier (used by tests)."""
    return bool(SNAKE.search(text or ""))
