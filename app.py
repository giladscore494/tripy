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
            if not itinerary:
                st.session_state["generation_error"] = "No itinerary returned."
                st.error("No itinerary returned. Please retry. Keeping previous itinerary.")
                return
            st.session_state["itinerary_json"] = itinerary
            st.session_state["chosen_destination"] = itinerary.get("trip_summary", {}).get(
                "destination", destination
            )
            st.session_state["revision_history"].append(
                {"timestamp": time.time(), "refinement": refinement, "destination": destination}
            )
        except Exception as exc:  # pragma: no cover - defensive
            st.session_state["generation_error"] = str(exc)
            st.error(f"Generation failed: {exc}. Keeping previous itinerary.")
        finally:
            st.info(f"Completed in {time.time() - start:.1f}s")


def intake_form():
    st.subheader("Trip intake")
    with st.form("trip_form"):
        col1, col2, col3 = st.columns(3)
        destination = col1.text_input("Destination", value="Miami")
        days = col2.number_input("Number of days", min_value=1, max_value=21, value=5)
        traveler_type = col3.selectbox("Traveler type", ["solo", "couple", "friends", "family"])

        budget = col1.selectbox("Budget", ["low", "medium", "high"])
        pace = col2.selectbox("Pace", ["relaxed", "balanced", "packed"])
        mobility = col3.selectbox("Mobility", ["with car", "no car"])

        interests = st.multiselect(
            "Interests",
            ["beaches", "nightlife", "food", "culture", "shopping", "nature", "sports", "museums", "theme parks"],
            default=["food", "beaches", "culture"],
        )
        constraints = st.text_input("Constraints (dietary, accessibility, allergies)", value="")
        lodging = st.selectbox("Lodging preference", ["hotel", "hostel", "apartment", "flexible"])
        dates = st.text_input("Dates or date range (optional)")
        open_to_alternatives = st.checkbox("Open to alternative destinations?", value=True)

        submitted = st.form_submit_button("Save trip profile")
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
            st.success("Trip profile saved.")


def render_alternatives():
    itinerary = st.session_state.get("itinerary_json") or {}
    alts = itinerary.get("alternatives") or []
    if not alts:
        return
    st.subheader("Destination alternatives")
    options = [alt.get("destination") for alt in alts]
    chosen = st.radio("Pick an alternative or keep the original", ["Keep current"] + options)
    if chosen != "Keep current":
        if st.button(f"Use {chosen} and regenerate"):
            _handle_generate(force_destination=chosen)


def render_refinement():
    st.subheader("Refinement")
    col1, col2, col3, col4 = st.columns(4)
    if col1.button("More relaxed"):
        _handle_generate(refinement="Make the itinerary more relaxed with lighter pacing.")
    if col2.button("More packed"):
        _handle_generate(refinement="Increase activities while staying realistic.")
    if col3.button("More nightlife"):
        _handle_generate(refinement="Add nightlife and evening entertainment options.")
    if col4.button("More nature"):
        _handle_generate(refinement="Add nature and outdoors focused activities.")

    custom = st.text_input("Custom edit request")
    if st.button("Apply custom refinement") and custom:
        _handle_generate(refinement=custom)


def render_export():
    itinerary = st.session_state.get("itinerary_json")
    if not itinerary:
        return
    st.subheader("Export")
    pdf_bytes = export_service.itinerary_to_pdf(itinerary)
    ics_bytes = export_service.itinerary_to_ics(itinerary)
    st.download_button("Download PDF", data=pdf_bytes, file_name="itinerary.pdf", mime="application/pdf")
    st.download_button(
        "Download ICS", data=ics_bytes, file_name="itinerary.ics", mime="text/calendar"
    )


def sidebar():
    st.sidebar.title("Settings")
    status = llm.secrets_status()
    if status["ok"]:
        cfg = status["config"]
        st.sidebar.success("API key detected.")
        st.sidebar.caption(f"Model: {cfg.get('model')}")
        st.sidebar.caption(f"Temperature: {cfg.get('temperature')}")
        st.sidebar.caption(f"Timeout: {cfg.get('timeout')}s")
        st.sidebar.caption(f"google-genai: {cfg.get('library_version')}")
        st.sidebar.caption(f"API key: {formatting.mask_key(cfg.get('api_key'))}")
        if status.get("warning"):
            st.sidebar.warning(status["warning"])
    else:
        st.sidebar.error(status["error"])
    if st.sidebar.button("Reset session"):
        st.session_state["trip_profile"] = None
        st.session_state["chosen_destination"] = None
        st.session_state["itinerary_json"] = None
        st.session_state["revision_history"] = []
        st.session_state["generation_error"] = None
        st.session_state["last_raw_response"] = None
        st.session_state["started"] = False
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
    col_a, col_b = st.columns([1, 2])
    if col_a.button("Generate itinerary"):
        _handle_generate()
    if col_b.button("Regenerate"):
        _handle_generate()

    render_alternatives()

    itinerary = st.session_state.get("itinerary_json") or {}
    if st.session_state.get("generation_error"):
        st.error(st.session_state["generation_error"])
    if itinerary:
        formatting.render_itinerary(itinerary, raw_response=st.session_state.get("last_raw_response"))
        render_refinement()
        render_export()
    elif not st.session_state.get("generation_error"):
        st.info("No itinerary yet. Provide details to generate one.")
    if st.session_state.get("last_raw_response") and not itinerary:
        with st.expander("Debug: last model output"):
            raw = st.session_state["last_raw_response"]
            limit = formatting.RAW_SNIPPET_LIMIT
            snippet = raw if len(raw) <= limit else raw[:limit] + "... [truncated]"
            st.code(snippet)


if __name__ == "__main__":
    main()
