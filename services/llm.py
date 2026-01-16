import hashlib
import json
import time
from typing import Any, Dict, Optional

import streamlit as st
from google import genai

from utils.cache import cached_data, cached_resource


def _get_config() -> Dict[str, Any]:
    secrets = st.secrets
    api_key = secrets.get("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError(
            "Missing GOOGLE_API_KEY in Streamlit secrets. "
            "Add it via Streamlit Cloud -> Advanced settings -> Secrets."
        )
    return {
        "api_key": api_key,
        "model": secrets.get("GEMINI_MODEL", "gemini-3-flash-preview"),
        "temperature": float(secrets.get("GEMINI_TEMPERATURE", 0.5)),
        "max_output_tokens": int(secrets.get("GEMINI_MAX_OUTPUT_TOKENS", 2048)),
        "timeout": int(secrets.get("GEMINI_TIMEOUT_SEC", 60)),
        "log_level": secrets.get("LOG_LEVEL", "INFO"),
    }


@cached_resource(ttl_seconds=3600)
def _client(api_key: str, timeout: int):
    return genai.Client(api_key=api_key, http_options={"timeout": timeout})


def _response_text(resp: Any) -> str:
    if not resp:
        return ""
    # google-genai responses typically expose .text
    return getattr(resp, "text", "") or getattr(resp, "candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")


def _hash_key(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@cached_data(ttl_seconds=3600)
def _cached_generate(cache_key: str, system_prompt: str, user_prompt: str, config: Dict[str, Any]) -> str:
    client = _client(config["api_key"], config["timeout"])
    tools = [{"google_search": {}}]
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=config["model"],
                contents=[{"role": "user", "parts": [{"text": user_prompt}]}],
                system_instruction=system_prompt,
                tools=tools,
                generation_config={
                    "temperature": config["temperature"],
                    "max_output_tokens": config["max_output_tokens"],
                },
            )
            text = _response_text(response)
            if text:
                return text
        except Exception as err:  # pragma: no cover - defensive
            last_error = err
            time.sleep(1 + attempt)
    if last_error:
        raise last_error
    return ""


def generate_json(system_prompt: str, user_prompt: str, cache_key_payload: Dict[str, Any]) -> str:
    config = _get_config()
    cache_key = _hash_key({"payload": cache_key_payload, "model": config["model"]})
    return _cached_generate(cache_key, system_prompt, user_prompt, config)


def secrets_status() -> Dict[str, Any]:
    try:
        cfg = _get_config()
        return {"ok": True, "config": cfg}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
