import json
import time
from typing import Dict, Optional

import streamlit as st

from services import planner
from services import export as export_service
from services import llm
from utils import formatting
from utils import validators


st.set_page_config(page_title="TripPilot", page_icon="🛫", layout="wide")

FILTER_HE = {
    "form_title": "תפריט סינון",
    "destination": "יעד",
    "days": "מספר ימים",
    "traveler_type": "סוג מטיילים",
    "budget": "תקציב",
    "pace": "קצב",
    "mobility": "ניידות",
    "interests": "תחומי עניין",
    "constraints": "הגבלות (תזונה, נגישות, אלרגיות)",
    "lodging": "העדפת לינה",
    "dates": "תאריכים (אופציונלי)",
    "open_to_alternatives": "פתוח ליעדים חלופיים?",
    "save_button": "שמור פרטי טיול",
    "save_success": "פרטי הטיול נשמרו.",
}


def _init_state():
    defaults = {
        "started": False,
        "trip_profile": None,
        "chosen_destination": None,
        "itinerary_json": None,
        "revision_history": [],
        "generation_error": None,
        "last_raw_response": None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val
    if not isinstance(st.session_state.get("revision_history"), list):
        st.session_state["revision_history"] = []


def _reset_session_state():
    st.session_state["trip_profile"] = None
    st.session_state["chosen_destination"] = None
    st.session_state["itinerary_json"] = None
    st.session_state["revision_history"] = []
    st.session_state["generation_error"] = None
    st.session_state["last_raw_response"] = None
    st.session_state["started"] = False


def _get_valid_itinerary_from_state() -> Optional[Dict]:
    itinerary = st.session_state.get("itinerary_json")
    if itinerary is not None and not isinstance(itinerary, dict):
        err = f"Internal error: itinerary_json must be an object, got {type(itinerary).__name__}"
        st.session_state["generation_error"] = err
        st.session_state["itinerary_json"] = None
        st.error(err)
        return None
    return itinerary


def _handle_generate(refinement: Optional[str] = None, force_destination: Optional[str] = None):
    st.session_state["generation_error"] = None
    st.session_state["last_raw_response"] = None
    profile: Dict = st.session_state.get("trip_profile") or {}
    ok, msg = validators.validate_profile(profile)
    if not ok:
        st.error(msg)
        return
    destination = force_destination or profile.get("destination")
    with st.spinner("Generating itinerary with Gemini..."):
        start = time.time()
        try:
            itinerary, raw_text, parse_error = planner.generate_itinerary(profile, destination, refinement)
            st.session_state["last_raw_response"] = raw_text
            if parse_error:
                st.session_state["generation_error"] = parse_error
                st.error(f"Generation failed: {parse_error}. Keeping previous itinerary.")
                return
            if not isinstance(itinerary, dict):
                err_msg = f"Invalid itinerary type: {type(itinerary).__name__}"
                st.session_state["generation_error"] = err_msg
                st.error(f"Generation failed: {err_msg}. Keeping previous itinerary.")
                return
            if not itinerary:
                st.session_state["generation_error"] = "No itinerary returned."
                st.error("No itinerary returned. Please retry. Keeping previous itinerary.")
                return
            if "trip_summary" not in itinerary or "days" not in itinerary:
                err_msg = "Invalid itinerary shape returned."
                st.session_state["generation_error"] = err_msg
                st.error(f"Generation failed: {err_msg}. Keeping previous itinerary.")
                return
            st.session_state["itinerary_json"] = itinerary
            st.session_state["chosen_destination"] = itinerary.get("trip_summary", {}).get(
                "destination", destination
            )
            if not isinstance(st.session_state.get("revision_history"), list):
                st.session_state["revision_history"] = []
            st.session_state["revision_history"].append(
                {"timestamp": time.time(), "refinement": refinement, "destination": destination}
            )
        except Exception as exc:  # pragma: no cover - defensive
            st.session_state["generation_error"] = str(exc)
            st.error(f"Generation failed: {exc}. Keeping previous itinerary.")
        finally:
            st.info(f"Completed in {time.time() - start:.1f}s")


def intake_form():
    st.subheader(FILTER_HE["form_title"])
    with st.form("trip_form"):
        col1, col2, col3 = st.columns(3)
        destination = col1.text_input(FILTER_HE["destination"], value="Miami")
        days = col2.number_input(FILTER_HE["days"], min_value=1, max_value=21, value=5)
        traveler_type = col3.selectbox(FILTER_HE["traveler_type"], ["solo", "couple", "friends", "family"])

        budget = col1.selectbox(FILTER_HE["budget"], ["low", "medium", "high"])
        pace = col2.selectbox(FILTER_HE["pace"], ["relaxed", "balanced", "packed"])
        mobility = col3.selectbox(FILTER_HE["mobility"], ["with car", "no car"])

        interests = st.multiselect(
            FILTER_HE["interests"],
            ["beaches", "nightlife", "food", "culture", "shopping", "nature", "sports", "museums", "theme parks"],
            default=["food", "beaches", "culture"],
        )
        constraints = st.text_input(FILTER_HE["constraints"], value="")
        lodging = st.selectbox(FILTER_HE["lodging"], ["hotel", "hostel", "apartment", "flexible"])
        dates = st.text_input(FILTER_HE["dates"])
        open_to_alternatives = st.checkbox(FILTER_HE["open_to_alternatives"], value=True)

        submitted = st.form_submit_button(FILTER_HE["save_button"])
        if submitted:
            profile = {
                "destination": destination,
                "days": int(days),
                "traveler_type": traveler_type,
                "budget": budget,
                "pace": pace,
                "mobility": mobility,
                "interests": interests,
                "constraints": constraints,
                "lodging": lodging,
                "dates": dates,
                "open_to_alternatives": open_to_alternatives,
            }
            st.session_state["trip_profile"] = profile
            st.session_state["chosen_destination"] = destination
            st.success(FILTER_HE["save_success"])


def render_alternatives() -> Optional[str]:
    itinerary = _get_valid_itinerary_from_state()
    if not itinerary or not isinstance(itinerary, dict):
        return None
    alts = itinerary.get("alternatives") or []
    if not alts:
        return None
    st.subheader("Destination alternatives")
    options = [alt.get("destination") for alt in alts if alt.get("destination")]
    if not options:
        return None
    chosen = st.radio("Pick an alternative or keep the original", ["Keep current"] + options, key="alternative_pick")
    if chosen != "Keep current":
        return chosen
    return None


def render_refinement():
    st.subheader("Refinement")
    presets = {
        "None": "",
        "More relaxed": "Make the itinerary more relaxed with lighter pacing.",
        "More packed": "Increase activities while staying realistic.",
        "More nightlife": "Add nightlife and evening entertainment options.",
        "More nature": "Add nature and outdoors focused activities.",
    }
    choice = st.selectbox("Suggested refinements", list(presets.keys()), index=0, key="refinement_choice")
    custom = st.text_input("Custom edit request", key="custom_edit_request")
    refinement = custom.strip() if custom.strip() else presets.get(choice, "")
    return refinement or None


def render_export():
    itinerary = _get_valid_itinerary_from_state()
    if not itinerary:
        return
    st.subheader("Export")
    try:
        pdf_bytes = export_service.itinerary_to_pdf(itinerary)
        ics_bytes = export_service.itinerary_to_ics(itinerary)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.download_button("Download PDF", data=pdf_bytes, file_name="itinerary.pdf", mime="application/pdf")
    st.download_button("Download ICS", data=ics_bytes, file_name="itinerary.ics", mime="text/calendar")


def sidebar():
    st.sidebar.title("Settings")
    status = llm.secrets_status()
    if status["ok"]:
        cfg = status["config"]
        st.sidebar.success("API key detected.")
        st.sidebar.caption(f"Model: {cfg.get('model')}")
        st.sidebar.caption(f"Temperature: {cfg.get('temperature')}")
        st.sidebar.caption(f"Configured timeout: {cfg.get('configured_timeout_sec')}s")
        st.sidebar.caption(f"Effective timeout: {cfg.get('timeout_sec')}s")
        st.sidebar.caption(f"Client timeout: {cfg.get('timeout_ms')}ms")
        st.sidebar.caption(f"google-genai: {cfg.get('library_version')}")
        st.sidebar.caption(f"API key: {formatting.mask_key(cfg.get('api_key'))}")
        if status.get("warning"):
            st.sidebar.warning(status["warning"])
    else:
        st.sidebar.error(status["error"])
    if st.sidebar.button("Reset session"):
        _reset_session_state()
        st.rerun()


def main():
    _init_state()
    sidebar()
    st.title("TripPilot: Plan your trip with Gemini 3 Flash Preview")
    st.write("Tell me your trip goals; I’ll build a grounded, time-aware itinerary.")

    if not st.session_state["started"]:
        if st.button("Start planning"):
            st.session_state["started"] = True
        else:
            st.info("Click **Start planning** to begin.")
            return

    intake_form()

    st.subheader("Itinerary generation")
    refinement_request = render_refinement()
    chosen_alternative = render_alternatives()
    if st.button("Send Request"):
        _handle_generate(refinement=refinement_request, force_destination=chosen_alternative)

    itinerary = _get_valid_itinerary_from_state()
    if st.session_state.get("generation_error"):
        st.error(st.session_state["generation_error"])
    if itinerary:
        formatting.render_itinerary(itinerary, raw_response=st.session_state.get("last_raw_response"))
        render_export()
    elif st.session_state.get("started") and not st.session_state.get("generation_error"):
        st.info("No itinerary yet. Provide details and click **Send Request** to generate one.")
    if st.session_state.get("last_raw_response") and not itinerary:
        with st.expander("Debug: last model output"):
            raw = st.session_state["last_raw_response"]
            limit = formatting.RAW_SNIPPET_LIMIT
            snippet = raw if len(raw) <= limit else raw[:limit] + "... [truncated]"
            st.code(snippet)


if __name__ == "__main__":
    main()
