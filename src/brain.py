"""Gemini does the thinking, in two phases.

Phase 1 (one call): look at every fresh headline, cluster duplicates across
outlets (including Bangla <-> English), drop topics already posted, pick the
top stories.

Phase 2 (one call per selected story): read the article's actual body text and
write the post — headline with a [[highlighted]] key phrase, summary, the
details-slide paragraphs, caption and tweet, all in English."""
import json
import re
import time
from datetime import datetime, timezone

import requests

from . import budget, config


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of model output that may carry markdown fences
    or stray prose around it (Gemma especially)."""
    text = text.strip()
    text = re.sub(r"^```[a-z]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in model output")

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
   FRESHNESS TIERS (each candidate is labeled): under 2 hours = BEST, post these; 2-5 hours = good; 5-12 hours = BAD, pick only if nothing fresher is share-worthy. Anything older never reaches you. Prefer a strong BEST/good story over a slightly-more-viral BAD one. Skip placeholder stories with no substance yet ("details awaited", "magnitude pending") unless the event itself broke within the last hour. EARTHQUAKES: select ONLY if both the magnitude AND the location/epicenter are already known — never post "tremors felt, details pending". Skip ads, horoscopes, recipes, TV schedules, live-stream pages, opinion teasers and trivial routine items. Fewer than {max_posts} — or zero — is fine if nothing fresh is genuinely share-worthy.
4. Give each selected story a short English topic key for future dedup.

RECENTLY POSTED TOPICS (do not repeat):
{history}

CANDIDATES (index | source | lang | age | category | title | snippet):
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
        "location": {"type": "string", "description": "the city/town this story happened in, for the Instagram location tag — e.g. 'Dhaka', 'Khulna', 'Narayanganj', 'Mexico City'; empty string if there is no single clear place"},
        "story_risk": {"type": "string", "enum": ["clean", "sensitive", "graphic", "do_not_post"]},
        "best_image": {"type": "integer", "description": "1-based index of the attached photo to use as the cover — the most relevant, visually striking AND platform-safe one. 0 if none of the attached photos is suitable or safe."},
    },
    "required": ["headline", "summary", "category", "template", "details", "hook", "hashtags", "tweet", "location", "story_risk", "best_image"],
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
Location rule: name the single city/town where the story happened (for the Instagram location tag). Use the most specific real place ('Narayanganj', not 'Bangladesh'). Empty string when the story spans many places or has no geographic anchor.

Platform safety (this page must never violate Facebook/Instagram policies):
- Never glorify or sensationalize violence; report it neutrally. Attribute every health/medical claim to its source (e.g. "according to the DGHS"). Use strictly neutral wording on political and communal stories.
- story_risk: "clean" for normal news; "sensitive" for violent crime, disasters, communal or health stories (your wording must be extra careful); "graphic" if the story centers on gory/disturbing details (use strictly clinical wording); "do_not_post" ONLY if the story cannot be covered at all without violating platform policy (gratuitous gore, glorifying violence or terrorism, explicit content) — OR if it is an earthquake story where the material does not state BOTH the magnitude and the location/epicenter.
- Soften, never censor: for sensitive/graphic stories use plain clinical wording ("killed", "injured") and skip gory specifics (method details, wound descriptions, suffering) — but keep every word intact and factual. Never mask words with symbols or slang ("k*lled", "unalived"): masking looks spammy and platforms detect it anyway. The goal is that almost every story remains postable through neutral wording.
- best_image: candidate photos from the outlets are attached in order: {photo_list}. Pick the ONE best cover photo (1-based index): most relevant to the story, most visually striking for a social feed, AND safe for Meta — never pick a photo showing blood, dead bodies, graphic injuries, weapons being fired or used on people, strikes/explosions with visible casualties, or nudity. Strongly avoid photos with an embedded news-channel/newspaper logo band, banner strip or watermark when a cleaner alternative is attached. If no attached photo qualifies, answer 0 (the post then runs with a text-only cover).

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
    deadline = time.time() + 120  # hard cap per call — a run must never stall
    cooled = False                # at most ONE throttle cool-off per call
    # try the backup key on the best model before degrading to a lesser model
    for model in models:
        limit = config.GEMINI_DAILY_LIMITS.get(model, config.GEMINI_DEFAULT_DAILY_LIMIT)
        for ki, api_key in enumerate(keys):
            pair = budget.gemini_pair_key(ki, model)
            if budget.remaining(pair, limit) <= 0:
                print(f"  [budget] {model} (key {ki + 1}): daily budget used up, trying next")
                continue
            for attempt in range(2):
                if time.time() > deadline:
                    raise RuntimeError(f"Gemini call exceeded time cap ({last_err})")
                if attempt:
                    time.sleep(2)
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
                        timeout=90,
                    )
                except requests.RequestException as e:
                    # transient network failure — retry, don't crash the lane walk
                    last_err = f"network error on {model}: {str(e)[:80]}"
                    continue
                budget.spend(pair)
                if resp.status_code == 429:
                    text_l = resp.text.lower()
                    if "perday" in text_l or "per day" in text_l or "daily" in text_l:
                        # genuinely out of daily quota — bench this lane
                        budget.exhaust(pair, limit)
                        last_err = f"HTTP 429 on {model} (key {ki + 1}, daily quota)"
                        break
                    # per-minute throttle: cool off once per CALL, otherwise
                    # just move to the next lane (no benching)
                    if not cooled:
                        cooled = True
                        print(f"  [warn] HTTP 429 on {model} (key {ki + 1}), cooling off 15s...")
                        time.sleep(15)
                        last_err = f"HTTP 429 on {model} (key {ki + 1})"
                        continue
                    last_err = f"HTTP 429 on {model} (key {ki + 1}, throttled)"
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
                    last_err = f"HTTP {resp.status_code} on {model} (key {ki + 1}): {resp.text[:160]}"
                    break
                data = resp.json()
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    return _extract_json(text)
                except (KeyError, IndexError, ValueError) as e:
                    last_err = f"unparseable output from {model}: {str(e)[:60]}"
                    break
    # every Gemini lane failed — cross-provider fallbacks (account-auth,
    # immune to the runner-IP throttling that hits Gemini's free tier)
    result = _call_groq(parts, schema, last_err)
    if result is None:
        result = _call_github_models(parts, schema, last_err)
    if result is not None:
        return result
    raise RuntimeError(f"Gemini unavailable after retries ({last_err})")


def _openai_style_content(parts: list) -> tuple:
    """Convert Gemini-style parts to OpenAI-style content; returns
    (content, has_images)."""
    content, has_images = [], False
    for p in parts:
        if "text" in p:
            content.append({"type": "text", "text": p["text"]})
        elif "inline_data" in p:
            d = p["inline_data"]
            has_images = True
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{d['mime_type']};base64,{d['data']}"}})
    return content, has_images


def _call_groq(parts: list, schema: dict, gemini_err: str = ""):
    if not config.GROQ_API_KEY:
        return None
    if budget.remaining("groq", config.GROQ_DAILY_LIMIT) <= 0:
        print("  [budget] Groq fallback budget used up")
        return None
    print(f"  [warn] all Gemini lanes failed ({gemini_err}) — falling back to Groq")
    content, has_images = _openai_style_content(parts)
    # JSON mode needs the schema spelled out in the prompt
    content.insert(0, {"type": "text", "text":
                       "Respond ONLY with a JSON object exactly matching this JSON schema:\n"
                       + json.dumps(schema)})
    model = config.GROQ_VISION_MODEL if has_images else config.GROQ_TEXT_MODEL
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            json={"model": model,
                  "messages": [{"role": "user", "content": content}],
                  "response_format": {"type": "json_object"},
                  "temperature": 0.4},
            timeout=90,
        )
        budget.spend("groq")
        if resp.status_code != 200:
            print(f"  [warn] Groq HTTP {resp.status_code}: {resp.text[:140]}")
            return None
        return _extract_json(resp.json()["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"  [warn] Groq fallback failed: {str(e)[:120]}")
        return None


_GH_MODELS_URL = "https://models.github.ai/inference/chat/completions"


def _call_github_models(parts: list, schema: dict, gemini_err: str = ""):
    if not config.GH_MODELS_TOKEN:
        return None
    if budget.remaining("ghmodels", config.GH_MODELS_DAILY_LIMIT) <= 0:
        print("  [budget] GitHub Models fallback budget used up")
        return None
    print(f"  [warn] all Gemini lanes failed ({gemini_err}) — falling back to GitHub Models")
    content = []
    for p in parts:
        if "text" in p:
            content.append({"type": "text", "text": p["text"]})
        elif "inline_data" in p:
            d = p["inline_data"]
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{d['mime_type']};base64,{d['data']}"}})
    body = {
        "model": config.GH_MODELS_MODEL,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "output", "schema": schema}},
        "temperature": 0.4,
    }
    try:
        resp = requests.post(
            _GH_MODELS_URL,
            headers={"Authorization": f"Bearer {config.GH_MODELS_TOKEN}",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json=body,
            timeout=90,
        )
        budget.spend("ghmodels")
        if resp.status_code != 200:
            print(f"  [warn] GitHub Models HTTP {resp.status_code}: {resp.text[:140]}")
            return None
        return _extract_json(resp.json()["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"  [warn] GitHub Models fallback failed: {str(e)[:120]}")
        return None


_DUP_SCHEMA = {
    "type": "object",
    "properties": {"duplicate_of": {"type": "integer", "description": "1-based index of the already-posted item that reports the SAME event/development as the new story; 0 if none do"}},
    "required": ["duplicate_of"],
}


def check_duplicate(headline: str, summary: str, posted_headlines: list) -> int:
    """Focused pairwise same-event check — catches synonym rewordings that
    word-overlap cannot ('license revoked' vs 'cancels licence')."""
    items = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(posted_headlines))
    prompt = (
        "We already posted these news items:\n"
        f"{items}\n\n"
        "NEW story:\n"
        f"Headline: {headline}\n"
        f"Summary: {summary}\n\n"
        "Does the NEW story report the same event and the same development as any "
        "item above (same thing, possibly reworded or in synonyms)? A genuinely NEW "
        "development of an ongoing story is NOT a duplicate — e.g. the RESULT of a "
        "match after we posted the match preview, a verdict after we posted the "
        "trial, a new decision, a new toll, a reversal that happened AFTER the "
        "posted item. Answer with the item number, or 0 if none."
    )
    result = _call_gemini([{"text": prompt}], _DUP_SCHEMA)
    return int(result.get("duplicate_of", 0))


def select_stories(candidates: list, history: list) -> list:
    """Phase 1 -> [{cluster: [items], topic: str}], newest stories first."""
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")

    recent = [e for e in history if e.get("topic")][-config.HISTORY_FOR_DEDUP:]
    history_lines = "\n".join(
        f"- {e['topic']} ({e.get('headline', '')})" for e in recent
    ) or "(nothing posted yet)"
    now = datetime.now(timezone.utc)

    def _age(c):
        try:
            dt = datetime.fromisoformat(c["published"].replace("Z", "+00:00"))
            hours = (now - dt).total_seconds() / 3600
            if hours <= 2:
                tier = "BEST"
            elif hours <= 5:
                tier = "good"
            else:
                tier = "BAD-avoid"
            return f"{hours:.1f}h ago [{tier}]"
        except Exception:
            # Daily Star's /todays-news page lists today's edition without
            # per-article times
            return "today (exact time unknown)"

    cand_lines = "\n".join(
        f"{i} | {c['source']} | {c['lang']} | {_age(c)} | {c.get('category','')} | {c['title']} | {c['description'][:140]}"
        for i, c in enumerate(candidates)
    )
    # ask for a ranked shortlist larger than we'll post, so stories vetoed by
    # the dedup layers have ranked replacements waiting
    shortlist = min(config.MAX_POSTS_PER_RUN * 3, 6)
    prompt = _SELECT_PROMPT.format(
        brand=config.BRAND_NAME,
        max_posts=shortlist,
        history=history_lines,
        candidates=cand_lines,
    ) + "\nRank your selected stories BEST FIRST (highest viral potential first)."
    result = _call_gemini([{"text": prompt}], _SELECT_SCHEMA)
    stories = []
    for s in result.get("stories", [])[:shortlist]:
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
        "location": (p.get("location") or "").strip()[:60],
        "image_data_uri": image_data_uri,
        "photo_credit": photo_credit,
        "source": sources,
        "url": primary["url"],
        "orig_title": primary["title"],
        "cluster_urls": [c["url"] for c in cluster],
        "cluster_titles": [c["title"] for c in cluster],
    }
