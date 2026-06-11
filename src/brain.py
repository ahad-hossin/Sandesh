"""Gemini does the thinking, in two phases.

Phase 1 (one call): look at every fresh headline, cluster duplicates across
outlets (including Bangla <-> English), drop topics already posted, pick the
top stories.

Phase 2 (one call per selected story): read the article's actual body text and
write the post — headline with a [[highlighted]] key phrase, summary, the
details-slide paragraphs, caption and tweet, all in English."""
import json
import time

import requests

from . import budget, config

_last_call = 0.0

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "indices of ALL candidates covering this same story",
                    },
                    "topic": {"type": "string", "description": "short unique English topic key, e.g. 'iran downs us helicopter'"},
                },
                "required": ["candidate_ids", "topic"],
            },
        }
    },
    "required": ["stories"],
}

_SELECT_PROMPT = """You are the editor of "{brand}", a Bangladeshi news page that posts in ENGLISH on Instagram, Facebook and X.

Below are fresh candidate stories from 4 Bangladeshi outlets (some headlines in Bangla), plus topics we already posted.

1. CLUSTER candidates covering the SAME story (it often appears on multiple outlets, sometimes Bangla on one, English on another). One cluster = one post.
2. DROP any story we already posted (see list). Same event = duplicate even if worded differently or in another language. This INCLUDES new stages of an event we already covered (announced -> approved -> presented -> reactions are ALL one story, not four). Only re-cover an ongoing event if the development is itself a major standalone story (a verdict, a dramatic reversal, a big new toll) — and even then, at most once.
3. SELECT the {max_posts} remaining stories with the HIGHEST VIRAL POTENTIAL. Rank by how likely Bangladeshi social media users are to share, comment and react:
   - breaking events and big developments in ongoing national dramas
   - stories that affect millions (prices, jobs, transport, weather, disasters)
   - dramatic human stories, big names (politicians, stars, cricketers), surprising numbers
   - national-pride moments and major international news with local relevance
   - a story covered by several outlets at once is a strong viral signal
   Among equally strong stories prefer the most RECENT. Skip ads, horoscopes, recipes, TV schedules, live-stream pages, opinion teasers and trivial routine items. Fewer than {max_posts} — or zero — is fine if nothing is genuinely share-worthy.
4. Give each selected story a short English topic key for future dedup.

RECENTLY POSTED TOPICS (do not repeat):
{history}

CANDIDATES (index | source | lang | published | category | title | snippet):
{candidates}
"""

_COMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "English headline, max 95 chars, with the core message — the part that alone tells the story — wrapped in [[ ]], e.g. '[[Iran downs US military helicopter]] near Gulf, Trump warns'"},
        "summary": {"type": "string", "description": "1-2 English sentences, max 220 chars, for the cover image subtext"},
        "category": {"type": "string", "description": "one word: BANGLADESH, WORLD, POLITICS, SPORT, BUSINESS, TECH, ENTERTAINMENT, HEALTH, ..."},
        "template": {"type": "string", "enum": ["editorial", "impact", "breaking", "sport", "tribute"]},
        "details": {
            "type": "array",
            "items": {"type": "string"},
            "description": "short English paragraphs (2-3 sentences each) telling the full story — as many as the story needs, typically 4-10. They flow across the details slides.",
        },
        "hook": {"type": "string", "description": "1-2 punchy factual lines that open the caption — what shows before '...more', impossible to scroll past"},
        "hashtags": {"type": "string", "description": "4-6 widely-used, non-restricted hashtags separated by spaces, mixing broad reach (#Bangladesh #News) with story-specific tags"},
        "tweet": {"type": "string", "description": "standalone X post, max 270 chars incl. 1-3 hashtags"},
        "story_risk": {"type": "string", "enum": ["clean", "sensitive", "graphic", "do_not_post"]},
        "best_image": {"type": "integer", "description": "1-based index of the attached photo to use as the cover — the most relevant, visually striking AND platform-safe one. 0 if none of the attached photos is suitable or safe."},
    },
    "required": ["headline", "summary", "category", "template", "details", "hook", "hashtags", "tweet", "story_risk", "best_image"],
}

_COMPOSE_PROMPT = """You are the editor of "{brand}", a Bangladeshi news page that posts in ENGLISH.

Write the social post for this story (translate to English where the source is Bangla). Make it as engaging as possible — but FACTS ONLY: never invent, exaggerate or editorialize beyond what the material below supports. No clickbait that the article can't back up.

Headline rules: scroll-stopping, concrete and factual, max 95 chars. Lead with the most striking fact or number. Wrap the headline's CORE MESSAGE in [[ ]] — the contiguous phrase (typically 4-8 words, can be half the headline) that on its own tells the viewer what happened, so reading just the highlight gives the main point and the rest of the headline adds context. Never highlight a fragment that's meaningless alone, and never highlight the entire headline.
Summary rules: the second punch — the detail that makes people need to know more.
Template rules (pick exactly by these):
- "tribute" — the story is about someone dying (death, obituary, tribute, killed)
- "sport" — sports stories
- "breaking" — new and important for everyone to know right now (major national events, emergencies, big sudden developments)
- otherwise pick the better fit for the story's tone: "editorial" (light, clean — calm news, business, policy, culture, human interest) or "impact" (dark, bold — dramatic, hard-hitting, tense stories)
Details rules: short paragraphs (2-3 sentences each) telling the FULL story — what/who/where/numbers/background/what's next. Use as many paragraphs as the story needs (typically 4-10); they flow across the details slides of the carousel. Put the most gripping facts in the first paragraph.
Hook rules: 1-2 lines that open the caption — it's all people see before "...more", so make it impossible to scroll past (a striking fact, number or question; still factual). The full story details follow it automatically, so don't repeat them.
Tweet rules: standalone, lead with the hook, under 270 chars, 1-3 hashtags.

Platform safety (this page must never violate Facebook/Instagram policies):
- Never glorify or sensationalize violence; report it neutrally. Attribute every health/medical claim to its source (e.g. "according to the DGHS"). Use strictly neutral wording on political and communal stories.
- story_risk: "clean" for normal news; "sensitive" for violent crime, disasters, communal or health stories (your wording must be extra careful); "graphic" if the story centers on gory/disturbing details (use strictly clinical wording); "do_not_post" ONLY if the story cannot be covered at all without violating platform policy (gratuitous gore, glorifying violence or terrorism, explicit content).
- Soften, never censor: for sensitive/graphic stories use plain clinical wording ("killed", "injured") and skip gory specifics (method details, wound descriptions, suffering) — but keep every word intact and factual. Never mask words with symbols or slang ("k*lled", "unalived"): masking looks spammy and platforms detect it anyway. The goal is that almost every story remains postable through neutral wording.
- best_image: candidate photos from the outlets are attached in order: {photo_list}. Pick the ONE best cover photo (1-based index): most relevant to the story, most visually striking for a social feed, AND safe for Meta — never pick a photo showing blood, dead bodies, graphic injuries, weapons being fired or used on people, strikes/explosions with visible casualties, or nudity. If no attached photo qualifies, answer 0 (the post then runs with a text-only cover).

STORY HEADLINES (from the outlets):
{titles}

ARTICLE TEXT (may be partial or Bangla; primary source: {primary_source}):
{article}
"""


def _call_gemini(parts: list, schema: dict) -> dict:
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    global _last_call
    models = [config.GEMINI_MODEL] + config.GEMINI_FALLBACK_MODELS
    keys = config.GEMINI_API_KEYS
    last_err = None
    # try the backup key on the best model before degrading to a lesser model
    for model in models:
        limit = config.GEMINI_DAILY_LIMITS.get(model, config.GEMINI_DEFAULT_DAILY_LIMIT)
        for ki, api_key in enumerate(keys):
            pair = budget.gemini_pair_key(ki, model)
            if budget.remaining(pair, limit) <= 0:
                print(f"  [budget] {model} (key {ki + 1}): daily budget used up, trying next")
                continue
            for attempt in range(4):
                if attempt:
                    wait = 2 ** attempt  # 2, 4, 8s
                    print(f"  [warn] Gemini {last_err}, retry {model} (key {ki + 1}) in {wait}s...")
                    time.sleep(wait)
                # respect the free tier's requests-per-minute ceiling
                gap = config.GEMINI_MIN_INTERVAL - (time.time() - _last_call)
                if gap > 0:
                    time.sleep(gap)
                _last_call = time.time()
                try:
                    resp = requests.post(
                        _ENDPOINT.format(model=model),
                        params={"key": api_key},
                        json=body,
                        timeout=120,
                    )
                except requests.RequestException as e:
                    # transient network failure — retry, don't crash the lane walk
                    last_err = f"network error on {model}: {str(e)[:80]}"
                    continue
                budget.spend(pair)
                if resp.status_code == 429:
                    # could be the per-minute limit, not the daily one: cool
                    # off and retry once before benching this key+model pair
                    if attempt == 0:
                        print(f"  [warn] HTTP 429 on {model} (key {ki + 1}), cooling off 35s...")
                        time.sleep(35)
                        last_err = f"HTTP 429 on {model} (key {ki + 1})"
                        continue
                    budget.exhaust(pair, limit)
                    last_err = f"HTTP 429 on {model} (key {ki + 1}, persistent)"
                    break
                if resp.status_code == 403:
                    # usually Google momentarily flagging the CI runner's IP
                    # or this key — move to the next key, don't crash and
                    # don't bench the budget (it usually recovers)
                    last_err = f"HTTP 403 on {model} (key {ki + 1})"
                    break
                if resp.status_code in (500, 502, 503, 504):
                    last_err = f"HTTP {resp.status_code} on {model}"
                    continue
                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code} on {model} (key {ki + 1})"
                    break
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
    raise RuntimeError(f"Gemini unavailable after retries ({last_err})")


def select_stories(candidates: list, history: list) -> list:
    """Phase 1 -> [{cluster: [items], topic: str}], newest stories first."""
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")

    recent = [e for e in history if e.get("topic")][-config.HISTORY_FOR_DEDUP:]
    history_lines = "\n".join(
        f"- {e['topic']} ({e.get('headline', '')})" for e in recent
    ) or "(nothing posted yet)"
    cand_lines = "\n".join(
        f"{i} | {c['source']} | {c['lang']} | {c['published']} | {c.get('category','')} | {c['title']} | {c['description'][:140]}"
        for i, c in enumerate(candidates)
    )
    prompt = _SELECT_PROMPT.format(
        brand=config.BRAND_NAME,
        max_posts=config.MAX_POSTS_PER_RUN,
        history=history_lines,
        candidates=cand_lines,
    )
    result = _call_gemini([{"text": prompt}], _SELECT_SCHEMA)
    stories = []
    for s in result.get("stories", [])[: config.MAX_POSTS_PER_RUN]:
        ids = [i for i in s.get("candidate_ids", []) if 0 <= i < len(candidates)]
        if not ids:
            continue
        stories.append({"cluster": [candidates[i] for i in ids], "topic": s.get("topic", "")})
    return stories


_CAPTION_MAX = 2100  # Instagram allows 2200; keep margin


def _build_caption(hook: str, details: list, hashtags: str, sources: str) -> str:
    """Hook -> full story details -> hashtags -> source credit. Used as-is on
    both Instagram and Facebook; truncated at a paragraph boundary if the full
    story would blow Instagram's caption limit."""
    tail = f"\n\n{hashtags.strip()}\n\nSource: {sources}".rstrip()
    body = hook.strip()
    for para in details:
        candidate = f"{body}\n\n{para}"
        if len(candidate) + len(tail) > _CAPTION_MAX:
            break
        body = candidate
    return body + tail


MAX_IMAGES = 4
_MAX_IMAGE_BYTES = 4_000_000


def compose_post(story: dict, article_text: str, images: list = None) -> dict:
    """Phase 2 -> full post content for one selected story. Every outlet's
    candidate photo rides along in the same request (numbered, in order), and
    Gemini picks the best safe one — relevance + visual punch + platform
    safety judged side by side, no extra API calls."""
    cluster = story["cluster"]
    primary = next((c for c in cluster if c["lang"] == "en"), cluster[0])
    titles = "\n".join(f"- [{c['source']}] {c['title']}" for c in cluster)

    attached = []  # (mime, b64, credit)
    for uri, credit in (images or [])[:MAX_IMAGES]:
        if not uri.startswith("data:"):
            continue
        header, b64 = uri.split(",", 1)
        if len(b64) > _MAX_IMAGE_BYTES * 1.4:
            continue
        attached.append((header.split(":", 1)[1].split(";", 1)[0], b64, credit))

    photo_list = ", ".join(f"{i + 1}: {c}" for i, (_, _, c) in enumerate(attached)) or "(none attached)"
    prompt = _COMPOSE_PROMPT.format(
        brand=config.BRAND_NAME,
        titles=titles,
        primary_source=primary["source"],
        article=article_text[:4500] or "(article text unavailable — use only the headlines)",
        photo_list=photo_list,
    )
    parts = [{"text": prompt}]
    for mime, b64, _ in attached:
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    p = _call_gemini(parts, _COMPOSE_SCHEMA)
    details = [d.strip() for d in p.get("details", []) if d.strip()][:14]
    marked = p["headline"][:130]
    sources = ", ".join(dict.fromkeys(c["source"] for c in cluster))
    caption = _build_caption(p.get("hook", ""), details, p.get("hashtags", ""), sources)
    choice = int(p.get("best_image", 0))
    image_data_uri, photo_credit = "", ""
    if 1 <= choice <= len(attached):
        mime, b64, credit = attached[choice - 1]
        image_data_uri = f"data:{mime};base64,{b64}"
        photo_credit = credit
    return {
        "topic": story["topic"],
        "headline_marked": marked,                      # with [[highlight]] for the image
        "headline": marked.replace("[[", "").replace("]]", ""),
        "summary": p["summary"][:260],
        "category": (p.get("category") or "NEWS").upper()[:18],
        "template": p.get("template", "editorial"),
        "details": details,
        "caption": caption,
        "tweet": p["tweet"][:275],
        "story_risk": p.get("story_risk", "clean"),
        "image_data_uri": image_data_uri,
        "photo_credit": photo_credit,
        "source": sources,
        "url": primary["url"],
        "orig_title": primary["title"],
        "cluster_urls": [c["url"] for c in cluster],
        "cluster_titles": [c["title"] for c in cluster],
    }
