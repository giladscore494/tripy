from __future__ import annotations

import os
from typing import Any, Mapping

import streamlit as st

from image_processing import (
    MAX_SESSION_IMAGE_BYTES,
    MEBIBYTE,
    SUPPORTED_IMAGE_EXTENSIONS,
    ImageInputError,
    prepare_uploads,
)
from kimi_client import DEFAULT_MODEL as DEFAULT_KIMI_MODEL
from kimi_client import KimiError, chat_completion as kimi_chat_completion
from openrouter_client import DEFAULT_MODEL as DEFAULT_OPENROUTER_MODEL
from openrouter_client import OpenRouterError, chat_completion as openrouter_chat_completion
from web_search import search_web


st.set_page_config(page_title="Tripy Chat", page_icon="💬", layout="centered")

PROVIDER_LABELS = {
    "openrouter": "Ox Alpha · OpenRouter",
    "kimi": "Kimi K3 · Moonshot",
}


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


def _session_image_usage() -> tuple[int, set[str]]:
    total_bytes = 0
    source_hashes: set[str] = set()
    for message in st.session_state.messages:
        images = message.get("images")
        if not isinstance(images, list):
            continue
        for image in images:
            if not isinstance(image, Mapping):
                continue
            data = image.get("data")
            if isinstance(data, bytes):
                total_bytes += len(data)
            source_sha256 = image.get("source_sha256")
            if isinstance(source_sha256, str):
                source_hashes.add(source_sha256)
    return total_bytes, source_hashes


def _render_images(images: Any) -> None:
    if not isinstance(images, list) or not images:
        return
    valid_images = [image for image in images if isinstance(image, Mapping)]
    if not valid_images:
        return

    with st.expander(f"תמונות ({len(valid_images)})", expanded=len(valid_images) <= 4):
        columns = st.columns(min(3, len(valid_images)))
        for index, image in enumerate(valid_images):
            data = image.get("data")
            if not isinstance(data, bytes):
                continue
            name = image.get("name") if isinstance(image.get("name"), str) else "תמונה"
            width = image.get("width")
            height = image.get("height")
            dimensions = f" · {width}×{height}" if isinstance(width, int) and isinstance(height, int) else ""
            columns[index % len(columns)].image(
                data,
                caption=f"{index + 1}. {name}{dimensions}",
                use_container_width=True,
            )


def _render_message(message: dict[str, Any]) -> None:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        _render_images(message.get("images"))
        if message["role"] == "assistant":
            _render_citations(message.get("citations", []))
            details = []
            if message.get("model"):
                details.append(f"מודל: `{message['model']}`")
            if message.get("web_search_requests"):
                details.append(f"חיפושי רשת: {message['web_search_requests']}")
            if message.get("estimated_input_tokens"):
                details.append(f"טוקני קלט מוערכים: {message['estimated_input_tokens']:,}")
            if details:
                st.caption(" · ".join(details))


if "messages" not in st.session_state:
    st.session_state.messages = []

openrouter_api_key = _setting("OPENROUTER_API_KEY")
moonshot_api_key = _setting("MOONSHOT_API_KEY")
openrouter_model = _setting("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL) or DEFAULT_OPENROUTER_MODEL
kimi_model = _setting("KIMI_MODEL", DEFAULT_KIMI_MODEL) or DEFAULT_KIMI_MODEL
app_url = _setting("OPENROUTER_APP_URL")

st.title("Tripy Chat")
st.caption("צ׳אט מינימליסטי דרך OpenRouter או Kimi API")

with st.sidebar:
    provider = st.selectbox(
        "מודל",
        options=list(PROVIDER_LABELS),
        format_func=PROVIDER_LABELS.get,
    )
    model = openrouter_model if provider == "openrouter" else kimi_model
    st.caption(f"מודל: `{model}`")
    if provider == "openrouter":
        web_search = st.toggle(
            "חיפוש חינמי באינטרנט",
            value=True,
            help=(
                "חיפוש ללא מפתח נוסף דרך DDGS. "
                "הוא אינו משתמש בכלי החיפוש בתשלום של OpenRouter."
            ),
        )
        if web_search:
            st.caption(
                "ללא חיוב חיפוש של OpenRouter · חיפוש אחד ועד 3 תוצאות · "
                "שירות ניסיוני שעלול להיות מוגבל"
            )
    else:
        web_search = st.toggle(
            "חיפוש מובנה של Kimi",
            value=True,
            help="כלי web-search הרשמי של Moonshot דרך Formula API.",
        )
        if web_search:
            st.caption(
                "חיפוש רשמי של Moonshot · עד חיפוש אחד להודעה · "
                "הזמינות והתמחור נקבעים על ידי Kimi"
            )
        image_bytes, _ = _session_image_usage()
        st.caption(
            "אפשר לצרף כמה תמונות שרוצים עד מגבלת הנפח · "
            "25MB לקובץ · "
            f"{image_bytes / MEBIBYTE:.1f}/{MAX_SESSION_IMAGE_BYTES / MEBIBYTE:.0f}MB בשיחה · "
            "התמונות מוקטנות ל־4K וה־EXIF מוסר"
        )
    if st.button("נקה שיחה", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

api_key = openrouter_api_key if provider == "openrouter" else moonshot_api_key
required_secret = "OPENROUTER_API_KEY" if provider == "openrouter" else "MOONSHOT_API_KEY"
if not api_key:
    st.error(
        f"חסר `{required_secret}`. הוסף אותו ל־Streamlit Secrets או לקובץ המקומי "
        "`.streamlit/secrets.toml`."
    )
    st.stop()

for stored_message in st.session_state.messages:
    _render_message(stored_message)

submission = st.chat_input(
    "כתוב הודעה או צרף תמונות..." if provider == "kimi" else "כתוב הודעה...",
    key=f"chat_input_{provider}",
    accept_file="multiple" if provider == "kimi" else False,
    file_type=list(SUPPORTED_IMAGE_EXTENSIONS) if provider == "kimi" else None,
)

if submission is not None:
    if provider == "kimi":
        prompt = str(getattr(submission, "text", "") or "").strip()
        uploaded_files = list(getattr(submission, "files", []) or [])
    else:
        prompt = str(submission).strip()
        uploaded_files = []

    image_records: list[dict[str, Any]] = []
    if uploaded_files:
        existing_image_bytes, existing_source_hashes = _session_image_usage()
        try:
            prepared_batch = prepare_uploads(
                uploaded_files,
                existing_image_bytes=existing_image_bytes,
                existing_source_hashes=existing_source_hashes,
            )
        except ImageInputError as exc:
            st.error(str(exc))
            st.stop()
        image_records = [image.as_session_record() for image in prepared_batch.images]
        if prepared_batch.duplicate_names:
            st.info(
                f"דילגתי על {len(prepared_batch.duplicate_names)} תמונות כפולות שכבר נמצאות בשיחה."
            )

    if not prompt:
        prompt = (
            f"נתח את {len(image_records)} התמונות המצורפות."
            if image_records
            else "נתח את התמונות שכבר צורפו בשיחה."
        )

    user_message: dict[str, Any] = {"role": "user", "content": prompt}
    if image_records:
        user_message["images"] = image_records
    st.session_state.messages.append(user_message)
    _render_message(user_message)

    with st.chat_message("assistant"):
        with st.spinner("חושב..."):
            try:
                if provider == "openrouter":
                    result = openrouter_chat_completion(
                        api_key=api_key,
                        messages=st.session_state.messages,
                        model=model,
                        web_search=web_search,
                        app_url=app_url or None,
                        search_runner=_cached_search,
                    )
                else:
                    result = kimi_chat_completion(
                        api_key=api_key,
                        messages=st.session_state.messages,
                        model=model,
                        web_search=web_search,
                    )
            except (OpenRouterError, KimiError) as exc:
                st.error(str(exc))
            else:
                assistant_message = {
                    "role": "assistant",
                    "content": result.content,
                    "citations": list(result.citations),
                    "model": result.model,
                    "web_search_requests": result.web_search_requests,
                }
                estimated_input_tokens = getattr(result, "estimated_input_tokens", None)
                if estimated_input_tokens is not None:
                    assistant_message["estimated_input_tokens"] = estimated_input_tokens
                if provider == "kimi":
                    assistant_message["provider_message"] = result.provider_message
                st.session_state.messages.append(assistant_message)
                st.markdown(result.content)
                _render_citations(assistant_message["citations"])
                details = [f"מודל: `{result.model}`"]
                if result.web_search_requests:
                    details.append(f"חיפושי רשת: {result.web_search_requests}")
                if estimated_input_tokens is not None:
                    details.append(f"טוקני קלט מוערכים: {estimated_input_tokens:,}")
                st.caption(" · ".join(details))
