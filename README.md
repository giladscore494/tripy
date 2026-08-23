# Tripy Chat

A minimal Streamlit chat app backed by OpenRouter. The default model is the free
Ox Alpha preview at `stealth/ox-alpha`.

## What it includes

- Streamlit chat history stored only in the current browser session.
- Server-side OpenRouter API key through Streamlit Secrets.
- Optional keyless web search executed by the app with DDGS and Trafilatura.
- Web-search citations and the actual model ID used in each response.
- Explicit limits of one search and three results per message.

> The app does not use OpenRouter's separately billed web-search server tool. Search
> requires no additional API key, but public search backends can rate-limit or block
> automated requests and should be treated as experimental.

> Ox Alpha is a third-party stealth preview. OpenRouter states that its provider
> retains prompts and completions but does not use them for training. Do not send
> sensitive information.

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
   OPENROUTER_MODEL = "stealth/ox-alpha"
   OPENROUTER_APP_URL = "https://your-app.streamlit.app"
   ```

4. Deploy. `requirements.txt` is in the repository root, so Community Cloud will
   install Streamlit, DDGS, and Trafilatura automatically.

The API key stays server-side, but a public app can still consume your OpenRouter
quota. Keep the Streamlit app private unless public usage is intentional.

## Web search behavior

When enabled, the first OpenRouter request exposes a normal client-side function:

```json
{
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "search_web",
        "description": "Search the live public web for current or factual information"
      }
    }
  ]
}
```

If Ox Alpha requests the function, the app runs DDGS locally, reads at most one
public HTML page with Trafilatura, and sends the bounded results back to the model in
a second OpenRouter request. Search results are cached for 15 minutes.

The app enforces one search, three results, a 300-character query, and up to 5,000
characters of extracted page text per message. It rejects local/private destinations,
credential-bearing URLs, oversized responses, and non-HTML content. Tool output is
explicitly marked as untrusted so webpage instructions are not followed.

DDGS is an educational metasearch library without a service-level guarantee. This
keyless path is useful for a personal demo, but a production deployment should use a
supported search API or a self-hosted metasearch service.

## Tests

```bash
python -m unittest discover -s tests -v
```
