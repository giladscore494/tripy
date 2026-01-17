import json
import streamlit as st

RAW_SNIPPET_LIMIT = 4000

def mask_key(key: str, shown: int = 4) -> str:
    if not key:
        return ""
    if len(key) <= shown:
        return "*" * len(key)
    return "*" * (len(key) - shown) + key[-shown:]


def render_sources(sources):
    if not sources:
        return
    for src in sources:
        if not isinstance(src, dict):
            continue
        title = src.get("title", "Source")
        url = src.get("url")
        domain = src.get("domain", "")
        label = f"{title} ({domain})" if domain else title
        if url:
            # st.markdown does not support a help tooltip; show the URL in the link itself.
            st.markdown(f"- [{label}]({url})")
        else:
            st.markdown(f"- {label}")


def render_itinerary(itinerary: dict, raw_response: str | None = None):
    if not itinerary:
        return

    summary = itinerary.get("trip_summary", {})
    if not isinstance(summary, dict):
        summary = {}
    st.subheader("Trip summary")
    cols = st.columns(3)
    cols[0].markdown(f"**Destination:** {summary.get('destination', 'N/A')}")
    cols[1].markdown(f"**Days:** {summary.get('days', 'N/A')}")
    cols[2].markdown(f"**Pace:** {summary.get('pace', 'N/A')}")
    st.markdown("**Why this fits you:**")
    for focus in summary.get("high_level_focus", []):
        st.markdown(f"- {focus}")

    lodging_list = itinerary.get("lodging") if isinstance(itinerary.get("lodging"), list) else []
    if lodging_list:
        st.subheader("Lodging recommendations")
        for lodging in lodging_list:
            if not isinstance(lodging, dict):
                st.markdown(f"- {lodging}")
                continue
            with st.expander(lodging.get("name", "Option")):
                st.markdown(f"**Type:** {lodging.get('type')}")
                st.markdown(f"**Area:** {lodging.get('area')}")
                st.markdown(f"**Why:** {lodging.get('why')}")
                st.markdown(f"**Price note:** {lodging.get('price_note')}")
                st.markdown("**Pros:**")
                for p in lodging.get("pros", []):
                    st.markdown(f"- {p}")
                st.markdown("**Cons:**")
                for c in lodging.get("cons", []):
                    st.markdown(f"- {c}")
                st.markdown("**Sources:**")
                render_sources(lodging.get("sources"))

    days_list = itinerary.get("days") if isinstance(itinerary.get("days"), list) else []
    if days_list:
        st.subheader("Day-by-day itinerary")
        for day in days_list:
            if not isinstance(day, dict):
                continue
            title = day.get("title") or day.get("theme", "")
            with st.expander(f"Day {day.get('day')}: {title}", expanded=False):
                st.markdown("**Activities:**")
                blocks = day.get("blocks") if isinstance(day.get("blocks"), list) else []
                if not blocks:
                    for slot in ("morning", "afternoon", "evening"):
                        slot_items = day.get(slot) if isinstance(day.get(slot), list) else []
                        for item in slot_items:
                            activity = item.get("activity") if isinstance(item, dict) else str(item)
                            notes = item.get("notes", "") if isinstance(item, dict) else ""
                            area = item.get("area", "") if isinstance(item, dict) else ""
                            duration = item.get("duration_est", "") if isinstance(item, dict) else ""
                            st.markdown(
                                f"- **{slot.title()}**: {activity} ({area}) — {duration}. {notes}"
                            )
                            if isinstance(item, dict):
                                render_sources(item.get("sources"))
                else:
                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        st.markdown(
                            f"- **{block.get('time', '').title()}**: {block.get('activity')} "
                            f"({block.get('area')}) — {block.get('duration_est')}. "
                            f"Transit: {block.get('transit_note')}. {block.get('notes', '')}"
                        )
                        render_sources(block.get("sources"))
                if day.get("food"):
                    st.markdown("**Food:**")
                    for food in day.get("food", []):
                        if not isinstance(food, dict):
                            st.markdown(f"- {food}")
                            continue
                        st.markdown(
                            f"- **{food.get('type', '').title()}**: {food.get('name')} "
                            f"({food.get('budget', '')} budget, {food.get('area', '')}) "
                            f"Diet: {', '.join(food.get('diet_fit', []))}"
                        )
                        render_sources(food.get("sources"))
                if day.get("plan_b"):
                    st.markdown("**Plan B:**")
                    for alt in day.get("plan_b", []):
                        if not isinstance(alt, dict):
                            st.markdown(f"- {alt}")
                            continue
                        st.markdown(f"- For **{alt.get('reason')}**: {alt.get('alternative')}")
                        render_sources(alt.get("sources"))

    assumptions = itinerary.get("assumptions") if isinstance(itinerary.get("assumptions"), list) else []
    if assumptions:
        st.subheader("Assumptions")
        for item in assumptions:
            st.markdown(f"- {item}")

    disclaimer = itinerary.get("disclaimer") if isinstance(itinerary.get("disclaimer"), str) else ""
    if disclaimer:
        st.info(disclaimer)

    with st.expander("Debug: raw JSON"):
        st.code(json.dumps(itinerary, indent=2))
    if raw_response:
        snippet = (
            raw_response
            if len(raw_response) <= RAW_SNIPPET_LIMIT
            else raw_response[:RAW_SNIPPET_LIMIT] + "... [truncated]"
        )
        with st.expander("Debug: raw model output"):
            st.code(snippet)
