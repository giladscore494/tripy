# TripPilot (Streamlit)

TripPilot is a Streamlit MVP that interviews a traveler, optionally suggests alternative destinations, and generates a grounded, day-by-day itinerary using Google Gemini 3 Flash Preview with web search.

## Features
- Wizard-style intake for destination, dates/days, traveler type, budget, interests, pace, mobility, constraints, and lodging preference.
- Optional alternative destinations with rationale.
- Grounded itinerary with morning/afternoon/evening blocks, lodging set, restaurants, transit notes, and per-day Plan B.
- Refinement buttons (“more relaxed/packed/nightlife/nature”) plus custom edit requests.
- PDF and ICS exports.
- Caching and basic retry; friendly errors and debug JSON expander.

## Setup

1) Install dependencies
```bash
pip install -r requirements.txt
```

2) Configure secrets (required)

Create `.streamlit/secrets.toml` locally (do **not** commit it) using the template below, or paste the same TOML into Streamlit Cloud **Advanced settings → Secrets**.

```toml
GOOGLE_API_KEY = "PASTE_KEY_HERE"
GEMINI_MODEL = "gemini-3-flash-preview"
# GEMINI_TEMPERATURE = 0.5
# GEMINI_MAX_OUTPUT_TOKENS = 2048
# GEMINI_TIMEOUT_SEC = 60
# LOG_LEVEL = "INFO"
```

3) Run the app
```bash
streamlit run app.py
```
Open the provided local URL in your browser.

## Notes & limitations
- Uses Google Gemini with web search grounding; all outputs should cite sources when available.
- If search/grounding is unavailable, the app labels assumptions accordingly.
- No background jobs; generation happens on demand. Cached calls reduce repeat requests.
- The app avoids unsafe content and does not guarantee availability or prices—please verify details.
