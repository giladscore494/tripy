import hashlib
import importlib.metadata
import json
import time
from typing import Any, Dict, Optional

import httpx
import streamlit as st
from google import genai
from google.genai import types

from utils.cache import cached_data, cached_resource

MIN_TIMEOUT = 30
LOW_TIMEOUT_ATTEMPTS = 2
DEFAULT_MAX_ATTEMPTS = 3
MAX_RETRY_SLEEP_SEC = 5


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
        "configured_timeout": int(secrets.get("GEMINI_TIMEOUT_SEC", 60)),
        "timeout": max(int(secrets.get("GEMINI_TIMEOUT_SEC", 60)), MIN_TIMEOUT),
        "log_level": secrets.get("LOG_LEVEL", "INFO"),
        "library_version": _library_version(),
    }


@cached_resource(ttl_seconds=3600)
def _client(api_key: str, timeout: int):
    return genai.Client(api_key=api_key, http_options={"timeout": timeout})


def _response_text(resp: Any) -> str:
    if not resp:
        return ""
    if isinstance(resp, dict):
        candidates = resp.get("candidates") or []
    else:
        candidates = getattr(resp, "candidates", None) or []
    text = getattr(resp, "text", "") or ""
    if text:
        return text
    if candidates:
        first = candidates[0]
        content = getattr(first, "content", None) or (first.get("content") if isinstance(first, dict) else {})
        parts = getattr(content, "parts", None) or (content.get("parts") if isinstance(content, dict) else []) or []
        for part in parts:
            part_text = getattr(part, "text", None) or (part.get("text") if isinstance(part, dict) else None)
            if part_text:
                return part_text
    return ""


def _build_contents(system_prompt: str, user_prompt: str, include_system_instruction: bool = True):
    if include_system_instruction:
        return [{"role": "user", "parts": [{"text": user_prompt}]}]
    return [
        {"role": "system", "parts": [{"text": system_prompt}]},
        {"role": "user", "parts": [{"text": user_prompt}]},
    ]


def _hash_key(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@cached_data(ttl_seconds=3600)
def _cached_generate(cache_key: str, system_prompt: str, user_prompt: str, config: Dict[str, Any]) -> str:
    client = _client(config["api_key"], config["timeout"])
    tools = [types.Tool(google_search=types.GoogleSearch())]
    system_instruction_supported = True
    contents = _build_contents(system_prompt, user_prompt, include_system_instruction=True)
    try:
        gen_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=config["temperature"],
            max_output_tokens=config["max_output_tokens"],
            tools=tools,
        )
    except TypeError:
        system_instruction_supported = False
        contents = _build_contents(system_prompt, user_prompt, include_system_instruction=False)
        gen_config = types.GenerateContentConfig(
            temperature=config["temperature"],
            max_output_tokens=config["max_output_tokens"],
            tools=tools,
        )

    last_error: Optional[Exception] = None
    max_attempts = LOW_TIMEOUT_ATTEMPTS if config["configured_timeout"] < MIN_TIMEOUT else DEFAULT_MAX_ATTEMPTS
    attempt = 0
    while attempt < max_attempts:
        start = time.time()
        try:
            response = client.models.generate_content(
                model=config["model"],
                contents=contents,
                config=gen_config,
            )
            text = _response_text(response)
            elapsed = time.time() - start
            _log_attempt(attempt + 1, elapsed, config, tools_enabled=True, status="success")
            if text:
                return text
        except TypeError as err:
            elapsed = time.time() - start
            if system_instruction_supported:
                system_instruction_supported = False
                contents = _build_contents(system_prompt, user_prompt, include_system_instruction=False)
                gen_config = types.GenerateContentConfig(
                    temperature=config["temperature"],
                    max_output_tokens=config["max_output_tokens"],
                    tools=tools,
                )
                _log_attempt(
                    attempt + 1,
                    elapsed,
                    config,
                    tools_enabled=True,
                    status="retry",
                    error="system_instruction unsupported, retrying with in-content prompt",
                )
                continue
            _log_attempt(attempt + 1, elapsed, config, tools_enabled=True, status="error", error=str(err))
            last_error = err
        except httpx.ReadTimeout as err:
            elapsed = time.time() - start
            _log_attempt(
                attempt + 1,
                elapsed,
                config,
                tools_enabled=True,
                status="timeout",
                error="Request timed out. Increase GEMINI_TIMEOUT_SEC and/or reduce output size.",
            )
            last_error = TimeoutError(
                "Request timed out. Increase GEMINI_TIMEOUT_SEC and/or reduce output size."
            )
            break
        except Exception as err:  # pragma: no cover - defensive
            elapsed = time.time() - start
            _log_attempt(attempt + 1, elapsed, config, tools_enabled=True, status="error", error=str(err))
            last_error = err
        attempt += 1
        if attempt >= max_attempts:
            break
        sleep_for = _backoff_sleep(attempt, config["timeout"])
        time.sleep(sleep_for)
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
        warning = None
        if cfg["configured_timeout"] < MIN_TIMEOUT:
            warning = f"Configured timeout {cfg['configured_timeout']}s is too low; clamped to {cfg['timeout']}s."
        return {"ok": True, "config": cfg, "warning": warning}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _log_attempt(
    attempt: int, elapsed: float, config: Dict[str, Any], tools_enabled: bool, status: str, error: Optional[str] = None
):
    details = (
        f"Attempt {attempt} ({status}) in {elapsed:.1f}s — model={config['model']}, "
        f"timeout={config['timeout']}s, tools={'on' if tools_enabled else 'off'}"
    )
    if error:
        details += f" | {error}"
    st.sidebar.caption(details)


def _library_version() -> str:
    try:
        return importlib.metadata.version("google-genai")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - defensive
        return "unknown"


def _backoff_sleep(attempt_number: int, timeout: int) -> float:
    """
    Calculate exponential backoff sleep with sane caps.
    attempt_number is 1-based (first retry -> 1).
    """
    return min(2 ** max(attempt_number - 1, 0), timeout / 2, MAX_RETRY_SLEEP_SEC)
