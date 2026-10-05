"""PR #46 search backend responses in each provider's response shape (serper.dev /search, Gemini generateContent with
google_search grounding, Z.ai web_search). Written from the providers' documented formats and the production run
20261005T184853Z observations (search-prime returning a site root with an article's title, ignoring site:), NOT
recorded API traffic: no provider key is available to the test environment.
"""

from __future__ import annotations

CARTUBE_G6 = ("https://www.cartube.co.il/מחירון-רכב-חדש/אקספנג/אקספנג-g6/6062-אקספנג-g6-rwd-core")
ICAR_VERSION = "https://www.icar.co.il/אקספנג/אקספנג_g6/2026/version29854/"
AUTO_I30 = "https://www.auto.co.il/cars/hyundai/i30/2018/530046/"

SERPER_SITE_RESPONSE = {
    "searchParameters": {"q": "site:cartube.co.il אקספנג G6 2026", "gl": "il", "hl": "iw", "type": "search"},
    "organic": [
        {"title": "אקספנג G6 RWD CORE - מחירון ומפרט טכני", "link": CARTUBE_G6,
         "snippet": "מחיר, מפרט טכני ונתונים של אקספנג G6 RWD CORE", "position": 1},
        {"title": "cartube - מחירון רכב חדש", "link": "https://www.cartube.co.il/", "snippet": "", "position": 2},
        {"title": "אקספנג G6 החדש נחשף", "link": "https://www.cartube.co.il", "snippet": "כתבה", "position": 3},
        {"title": "XPeng G6 review", "link": "https://www.carsdirect.com/xpeng-g6", "snippet": "", "position": 4},
    ],
    "credits": 1,
}

GEMINI_RESPONSE = {
    "candidates": [{
        "content": {"role": "model", "parts": [{"text": "The page is https://www.cartube.co.il/guessed-url "
                                                        "and the power is 286 hp."}]},
        "groundingMetadata": {
            "webSearchQueries": ["אקספנג G6 2026 מפרט טכני", "XPeng G6 2026 specifications"],
            "groundingChunks": [
                {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA111",
                         "title": "cartube.co.il"}},
                {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/BBB222",
                         "title": "icar.co.il"}},
                {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/CCC333",
                         "title": "broken.example"}},
            ],
            "searchEntryPoint": {"renderedContent": "<div>chips</div>"},
        },
    }],
    "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 100,
                      "totalTokenCount": 1300},
}
GEMINI_REDIRECTS = {"https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA111": CARTUBE_G6,
                    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/BBB222": ICAR_VERSION}

# Z.ai search-prime as production saw it: the site root in `link` with an article's title, and off-site results for a
# site: query
GLM_RESULTS = [
    {"title": "מתיחת פנים: 2019 ב.מ.וו X1 החדש נחשף", "url": "https://www.cartube.co.il", "snippet": "", "site": ""},
    {"title": "אודי Q3 החדש נחשף רשמית", "url": "https://www.auto.co.il", "snippet": "", "site": ""},
    {"title": "2024 Audi Q3 review", "url": "https://velocityjournal.com/audi-q3", "snippet": "", "site": ""},
    {"title": "Hyundai i30 2018", "url": AUTO_I30, "snippet": "", "site": ""},
]
