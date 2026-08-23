# Tripy Chat

A minimal Streamlit chat app backed by OpenRouter. The default model is the free
`openai/gpt-oss-120b:free` endpoint.

## What it includes

- Streamlit chat history stored only in the current browser session.
- Server-side OpenRouter API key through Streamlit Secrets.
- Optional API-level web search using the current `openrouter:web_search` server tool.
- Web-search citations and the actual model ID used in each response.
- Explicit limits of one search and three results per message.

> The model endpoint is free but rate-limited. OpenRouter web search has a separate
> cost, so the search toggle is off by default.

## Run locally

1. Install the dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml` and replace
   the placeholder with your OpenRouter key. Never commit `secrets.toml`.

3. Start the app:

   ```bash
   streamlit run app.py
   ```

You can override the default model with `OPENROUTER_MODEL`. The app also supports
the same settings as environment variables.

## Deploy on Streamlit Community Cloud

1. Select this repository and `app.py` as the entrypoint.
2. Use Python 3.12 in **Advanced settings**.
3. Paste this into **Secrets**, using your real key:

   ```toml
   OPENROUTER_API_KEY = "sk-or-v1-replace-me"
   OPENROUTER_MODEL = "openai/gpt-oss-120b:free"
   OPENROUTER_APP_URL = "https://your-app.streamlit.app"
   ```

4. Deploy. `requirements.txt` is in the repository root, so Community Cloud will
   install the required Streamlit version automatically. The OpenRouter client uses
   Python's standard library and adds no extra HTTP dependency.

The API key stays server-side, but a public app can still consume your OpenRouter
quota. Keep the Streamlit app private unless public usage is intentional.

## Web search behavior

When enabled, requests include:

```json
{
  "tools": [
    {
      "type": "openrouter:web_search",
      "parameters": {
        "engine": "parallel",
        "mode": "basic",
        "max_results": 3,
        "max_total_results": 3,
        "max_uses": 1,
        "search_context_size": "low"
      }
    }
  ],
  "max_tool_calls": 1
}
```

The model decides whether a search is necessary. OpenRouter currently marks server
tools as beta, so check its documentation if the API schema changes.

## Tests

```bash
python -m unittest discover -s tests -v
```
