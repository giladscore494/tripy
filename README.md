# Tripy Chat

A minimal Streamlit chat app with a model selector for the free Ox Alpha preview
through OpenRouter and Kimi K3 through Moonshot's direct API.

## What it includes

- Streamlit chat history stored only in the current browser session.
- Server-side OpenRouter and Moonshot API keys through Streamlit Secrets.
- `stealth/ox-alpha` with optional keyless web search executed by the app with
  DDGS and Trafilatura.
- `kimi-k3` with Moonshot's official `moonshot/web-search:latest` Formula tool.
- Web-search citations and the actual model ID used in each response.
- A model selector in the Streamlit sidebar.
- An explicit limit of one web search per message.

> The app does not use OpenRouter's separately billed web-search server tool. Search
> requires no additional API key, but public search backends can rate-limit or block
> automated requests and should be treated as experimental.

> Kimi K3 and its search path are billed and rate-limited according to the Moonshot
> account. Moonshot currently describes Formula official tools as free for a limited
> time, but availability and pricing can change. Kimi's documentation also marks web
> search as experimental and not recommended for near-term production workflows.

> Ox Alpha is a third-party stealth preview. OpenRouter states that its provider
> retains prompts and completions but does not use them for training. Do not send
> sensitive information.

## Run locally

1. Install the dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml` and replace
   the placeholders with the API keys you want to use. Never commit `secrets.toml`.

3. Start the app:

   ```bash
   streamlit run app.py
   ```

You can override the provider model IDs with `OPENROUTER_MODEL` and `KIMI_MODEL`.
The app also supports the same settings as environment variables.

## Deploy on Streamlit Community Cloud

1. Select this repository and `app.py` as the entrypoint.
2. Use Python 3.12 in **Advanced settings**.
3. Paste this into **Secrets**, using your real key:

   ```toml
   OPENROUTER_API_KEY = "sk-or-v1-replace-me"
   OPENROUTER_MODEL = "stealth/ox-alpha"
   MOONSHOT_API_KEY = "sk-replace-with-your-kimi-key"
   KIMI_MODEL = "kimi-k3"
   OPENROUTER_APP_URL = "https://your-app.streamlit.app"
   ```

4. Deploy. `requirements.txt` is in the repository root, so Community Cloud will
   install Streamlit, DDGS, and Trafilatura automatically.

The API keys stay server-side, but a public app can still consume both your
OpenRouter quota and your Moonshot balance. Keep the Streamlit app private unless
public usage is intentional.

## Ox Alpha web search

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

## Kimi K3 web search

When Kimi K3 is selected, the app calls Moonshot directly at
`https://api.moonshot.ai/v1` using `MOONSHOT_API_KEY`. If search is enabled, it:

1. Fetches the official tool declaration from
   `moonshot/web-search:latest`.
2. Sends that declaration to `kimi-k3` through Chat Completions.
3. Executes at most one requested `web_search` Formula Fiber.
4. Returns the complete assistant tool-call message and Fiber result to Kimi until
   the model produces its final answer.

Kimi K3 runs with `reasoning_effort="low"` and a bounded completion budget for a
responsive chat experience. Search results are processed by Moonshot rather than by
the app's DDGS/Trafilatura path.

## Tests

```bash
python -m unittest discover -s tests -v
```
