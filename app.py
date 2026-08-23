from __future__ import annotations

import os
from typing import Any

import streamlit as st

from openrouter_client import DEFAULT_MODEL, OpenRouterError, chat_completion
from web_search import search_web


st.set_page_config(page_title="Tripy Chat", page_icon="💬", layout="centered")


def _setting(name: str, default: str = "") -> str:
    value: Any = os.getenv(name, default)
    try:
        value = st.secrets.get(name, value)
    except Exception:
        # Streamlit raises when no local secrets file exists. Environment variables
        # remain a valid fallback for local and non-Community-Cloud deployments.
        pass
    return str(value).strip() if value is not None else default


def _render_citations(citations: list[dict[str, str]]) -> None:
    if not citations:
        return
    with st.expander("מקורות"):
        for citation in citations:
            title = citation["title"].replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            st.markdown(f"- [{title}]({citation['url']})")


@st.cache_data(ttl=900, max_entries=128, show_spinner=False)
def _cached_search(query: str) -> list[dict[str, str]]:
    return search_web(query)


def _render_message(message: dict[str, Any]) -> None:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message["role"] == "assistant":
            _render_citations(message.get("citations", []))
            details = []
            if message.get("model"):
                details.append(f"מודל: `{message['model']}`")
            if message.get("web_search_requests"):
                details.append(f"חיפושי רשת: {message['web_search_requests']}")
            if details:
                st.caption(" · ".join(details))


if "messages" not in st.session_state:
    st.session_state.messages = []

api_key = _setting("OPENROUTER_API_KEY")
model = _setting("OPENROUTER_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
app_url = _setting("OPENROUTER_APP_URL")

st.title("Tripy Chat")
st.caption("צ׳אט מינימליסטי דרך OpenRouter")

with st.sidebar:
    st.caption(f"מודל: `{model}`")
    web_search = st.toggle(
        "חיפוש חינמי באינטרנט",
        value=True,
        help="חיפוש ללא מפתח נוסף דרך DDGS. הוא אינו משתמש בכלי החיפוש בתשלום של OpenRouter.",
    )
    if web_search:
        st.caption("ללא חיוב חיפוש של OpenRouter · חיפוש אחד ועד 3 תוצאות · שירות ניסיוני שעלול להיות מוגבל")
    if st.button("נקה שיחה", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

if not api_key:
    st.error(
        "חסר `OPENROUTER_API_KEY`. הוסף אותו ל־Streamlit Secrets או לקובץ המקומי "
        "`.streamlit/secrets.toml`."
    )
    st.stop()

for stored_message in st.session_state.messages:
    _render_message(stored_message)

if prompt := st.chat_input("כתוב הודעה..."):
    user_message = {"role": "user", "content": prompt}
    st.session_state.messages.append(user_message)
    _render_message(user_message)

    with st.chat_message("assistant"):
        with st.spinner("חושב..."):
            try:
                result = chat_completion(
                    api_key=api_key,
                    messages=st.session_state.messages,
                    model=model,
                    web_search=web_search,
                    app_url=app_url or None,
                    search_runner=_cached_search,
                )
            except OpenRouterError as exc:
                st.error(str(exc))
            else:
                assistant_message = {
                    "role": "assistant",
                    "content": result.content,
                    "citations": list(result.citations),
                    "model": result.model,
                    "web_search_requests": result.web_search_requests,
                }
                st.session_state.messages.append(assistant_message)
                st.markdown(result.content)
                _render_citations(assistant_message["citations"])
                details = [f"מודל: `{result.model}`"]
                if result.web_search_requests:
                    details.append(f"חיפושי רשת: {result.web_search_requests}")
                st.caption(" · ".join(details))
