"""Central configuration. Everything tunable lives here or in env vars."""
import os

# Load a local .env file if present (for testing on your PC; Actions uses secrets)
_env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_env_file):
    with open(_env_file, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# --- Gemini ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_KEY_2 = os.environ.get("GEMINI_API_KEY_2", "")  # backup keys
GEMINI_API_KEY_3 = os.environ.get("GEMINI_API_KEY_3", "")
GEMINI_API_KEYS = [k for k in (GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3) if k]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# tried in order when the primary model keeps returning 429/5xx.
# gemma-4-31b-it is the capacity workhorse: 1,500 req/day free (vs 20 for
# the Gemini models) with vision. 2.0-flash removed (free tier gives it 0/day).
GEMINI_FALLBACK_MODELS = [
    m.strip() for m in os.environ.get(
        "GEMINI_FALLBACK_MODELS", "gemma-4-31b-it,gemini-2.5-flash-lite"
    ).split(",") if m.strip()
]

# --- Posting volume ---
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "2"))
MAX_ITEM_AGE_HOURS = int(os.environ.get("MAX_ITEM_AGE_HOURS", "24"))

# --- API budgets (calls per UTC day unless noted) ---
# Google slashed the Gemini free tier: ~20 requests/day per model PER PROJECT
# (keys in the same project share the pool — assume shared and budget each
# key at a third). Groq (800/day) carries the bulk once Gemini's small daily
# allowance is spent. Override any of these via env.
GEMINI_DEFAULT_DAILY_LIMIT = 6
GEMINI_DAILY_LIMITS = {
    "gemini-2.5-flash": int(os.environ.get("GEMINI_25_FLASH_DAILY", "6")),
    "gemma-4-31b-it": int(os.environ.get("GEMMA_4_31B_DAILY", "450")),
    "gemini-2.5-flash-lite": int(os.environ.get("GEMINI_25_LITE_DAILY", "6")),
}
GEMINI_MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", "6.5"))  # sec between calls (10 RPM)

# Groq: first cross-provider fallback when every Gemini lane fails.
# Account-authenticated (no runner-IP issues), fast, generous free tier.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_TEXT_MODEL = os.environ.get("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile")
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
GROQ_DAILY_LIMIT = int(os.environ.get("GROQ_DAILY_LIMIT", "800"))

# GitHub Models: last-resort fallback when every Gemini lane fails. Runs on
# GitHub's own infra, authenticated by the workflow token (no IP-reputation
# problems on Actions runners). Free tier is modest — reserve for outages.
GH_MODELS_TOKEN = os.environ.get("GH_MODELS_TOKEN", "")
GH_MODELS_MODEL = os.environ.get("GH_MODELS_MODEL", "openai/gpt-4o-mini")
GH_MODELS_DAILY_LIMIT = int(os.environ.get("GH_MODELS_DAILY_LIMIT", "140"))

IG_DAILY_LIMIT = int(os.environ.get("IG_DAILY_LIMIT", "45"))    # Meta hard limit: 50 posts/24h
FB_DAILY_LIMIT = int(os.environ.get("FB_DAILY_LIMIT", "90"))    # generous self-imposed cap
X_DAILY_LIMIT = int(os.environ.get("X_DAILY_LIMIT", "16"))      # keeps X free tier viable
X_MONTHLY_LIMIT = int(os.environ.get("X_MONTHLY_LIMIT", "480")) # X free tier: ~500 writes/month

# --- Brand (shown on the rendered post image) ---
BRAND_NAME = os.environ.get("BRAND_NAME", "SANDESH")
ACCENT = os.environ.get("ACCENT", "oklch(0.55 0.19 27)")  # News Red
BRAND_TZ_OFFSET_HOURS = int(os.environ.get("BRAND_TZ_OFFSET_HOURS", "6"))  # Bangladesh = UTC+6

# --- History / dedup ---
HISTORY_KEEP = 600          # entries kept in posted.json
HISTORY_FOR_DEDUP = 120     # recent topics shown to Gemini for dedup

# --- Sources (all official-site channels) ---
# kind "rss"              -> direct RSS feed
# kind "news_sitemap"     -> the site's Google-News sitemap XML
# kind "html_todays_news" -> The Daily Star's /todays-news index page
SOURCES = [
    {
        "id": "dhakatribune",
        "name": "Dhaka Tribune",
        "kind": "rss",
        "url": "https://www.dhakatribune.com/feed/bangladesh",
        "lang": "en",
    },
    {
        "id": "thedailystar",
        "name": "The Daily Star",
        "kind": "html_todays_news",
        "url": "https://www.thedailystar.net/todays-news",
        "lang": "en",
    },
    {
        "id": "somoynews",
        "name": "Somoy News",
        "kind": "news_sitemap",
        "url": "https://www.somoynews.tv/news-sitemap.xml",
        "lang": "bn",
    },
    {
        "id": "jamuna",
        "name": "Jamuna TV",
        "kind": "news_sitemap",
        "url": "https://www.jamuna.tv/news_sitemap.xml",
        "lang": "bn",
    },
    {
        "id": "prothomalo",
        "name": "Prothom Alo",
        "kind": "rss",
        "url": "https://www.prothomalo.com/feed",
        "lang": "bn",
    },
    {
        "id": "bdnews24",
        "name": "bdnews24.com",
        "kind": "news_sitemap",
        "url": "https://bdnews24.com/news_sitemap.xml",
        "lang": "en",
    },
    {
        "id": "banglatribune",
        "name": "Bangla Tribune",
        "kind": "news_sitemap",
        "url": "https://www.banglatribune.com/news-sitemap.xml",
        "lang": "bn",
    },
    {
        "id": "ittefaq",
        "name": "Daily Ittefaq",
        "kind": "news_sitemap",
        "url": "https://www.ittefaq.com.bd/news-sitemap.xml",
        "lang": "bn",
    },
]

# --- Social credentials (set as GitHub secrets; pipeline dry-runs without them) ---
IG_USER_ID = os.environ.get("IG_USER_ID", "")            # Instagram Business account ID
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")  # long-lived Page token (works for IG + FB)
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")

X_API_KEY = os.environ.get("X_API_KEY", "")
X_API_SECRET = os.environ.get("X_API_SECRET", "")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN", "")
X_ACCESS_SECRET = os.environ.get("X_ACCESS_SECRET", "")

# --- Paths ---
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT, "state", "posted.json")
QUEUE_FILE = os.path.join(ROOT, "state", "queue.json")
OUTPUT_DIR = os.path.join(ROOT, "output")
TEMPLATE_FILE = os.path.join(ROOT, "templates", "post.html")

# Public base URL for generated images (needed by Instagram, which fetches by
# URL). Images live on the orphan 'images' branch, which the workflow
# force-overwrites every run so old image blobs never pile up in git history.
def public_image_base() -> str:
    explicit = os.environ.get("PUBLIC_IMAGE_BASE", "")
    if explicit:
        return explicit.rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        return f"https://raw.githubusercontent.com/{repo}/images"
    return ""

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
