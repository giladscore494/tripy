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
        title = src.get("title", "Source")
        url = src.get("url")
        domain = src.get("domain", "")
        label = f"{title} ({domain})" if domain else title
        if url:
            st.markdown(f"- [{label}]({url})", help=url)
        else:
            st.markdown(f"- {label}")


def render_itinerary(itinerary: dict, raw_response: str | None = None):
    if not itinerary:
        return

    summary = itinerary.get("trip_summary", {})
    st.subheader("Trip summary")
    cols = st.columns(3)
    cols[0].markdown(f"**Destination:** {summary.get('destination', 'N/A')}")
    cols[1].markdown(f"**Days:** {summary.get('days', 'N/A')}")
    cols[2].markdown(f"**Pace:** {summary.get('pace', 'N/A')}")
    st.markdown("**Why this fits you:**")
    for focus in summary.get("high_level_focus", []):
        st.markdown(f"- {focus}")

    if itinerary.get("lodging"):
        st.subheader("Lodging recommendations")
        for lodging in itinerary["lodging"]:
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

    if itinerary.get("days"):
        st.subheader("Day-by-day itinerary")
        for day in itinerary["days"]:
            with st.expander(f"Day {day.get('day')}: {day.get('theme', '')}", expanded=False):
                st.markdown("**Activities:**")
                for block in day.get("blocks", []):
                    st.markdown(
                        f"- **{block.get('time', '').title()}**: {block.get('activity')} "
                        f"({block.get('area')}) — {block.get('duration_est')}. "
                        f"Transit: {block.get('transit_note')}. {block.get('notes', '')}"
                    )
                    render_sources(block.get("sources"))
                if day.get("food"):
                    st.markdown("**Food:**")
                    for food in day["food"]:
                        st.markdown(
                            f"- **{food.get('type', '').title()}**: {food.get('name')} "
                            f"({food.get('budget', '')} budget, {food.get('area', '')}) "
                            f"Diet: {', '.join(food.get('diet_fit', []))}"
                        )
                        render_sources(food.get("sources"))
                if day.get("plan_b"):
                    st.markdown("**Plan B:**")
                    for alt in day["plan_b"]:
                        st.markdown(f"- For **{alt.get('reason')}**: {alt.get('alternative')}")
                        render_sources(alt.get("sources"))

    if itinerary.get("assumptions"):
        st.subheader("Assumptions")
        for item in itinerary["assumptions"]:
            st.markdown(f"- {item}")

    if itinerary.get("disclaimer"):
        st.info(itinerary["disclaimer"])

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
