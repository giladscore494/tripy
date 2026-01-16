import streamlit as st


def cached_data(ttl_seconds: int = 3600):
    """
    Wrapper for st.cache_data with sensible defaults and no mutation warning.
    """

    def decorator(func):
        return st.cache_data(ttl=ttl_seconds, show_spinner=False)(func)

    return decorator


def cached_resource(ttl_seconds: int = 3600):
    """
    Wrapper for st.cache_resource with sensible defaults.
    """

    def decorator(func):
        return st.cache_resource(ttl=ttl_seconds, show_spinner=False)(func)

    return decorator
