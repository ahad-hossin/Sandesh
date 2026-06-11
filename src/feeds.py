"""Fetch and normalize news items, straight from the official sites.

- Dhaka Tribune : native category RSS feed
- Somoy News    : official Google-News sitemap (news-sitemap.xml)
- Jamuna TV     : official Google-News sitemap (news_sitemap.xml)
- The Daily Star: the /todays-news page (their own daily index)

Everything goes through cloudscraper, which clears the basic bot walls on the
Bangla sites."""
import calendar
import re
from datetime import datetime, timedelta, timezone

import cloudscraper
import feedparser
from bs4 import BeautifulSoup

from . import config

_scraper = cloudscraper.create_scraper()
MAX_PER_SOURCE = 80


def http_get(url: str):
    last = None
    for attempt in range(2):
        try:
            return _scraper.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=45)
        except Exception as e:
            last = e
    raise last


def _cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=config.MAX_ITEM_AGE_HOURS)


def _item(src, title, url, when, desc="", image="", category=""):
    return {
        "source_id": src["id"],
        "source": src["name"],
        "lang": src["lang"],
        "title": re.sub(r"\s+", " ", title).strip(),
        "url": url.strip(),
        # when=None -> publication time genuinely unknown (don't fake "now")
        "published": when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else "",
        "description": desc,
        "image": image,
        "category": category,
    }


# ---------- direct RSS (Dhaka Tribune) ----------

def _fetch_rss(src) -> list:
    parsed = feedparser.parse(http_get(src["url"]).content)
    items = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        when = (datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
                if t else datetime.now(timezone.utc))
        if when < _cutoff():
            continue
        desc = re.sub(r"<[^>]+>", " ", entry.get("summary", "") or "")
        desc = re.sub(r"\s+", " ", desc).strip()[:400]
        desc = re.sub(r"\s*Details$", "", desc)  # Dhaka Tribune 'Details' link text
        # image: media:content (Prothom Alo, full-size and unbranded) beats
        # whatever thumbnail is embedded in the summary HTML (Dhaka Tribune)
        image = ""
        media = entry.get("media_content") or []
        if media and media[0].get("url"):
            image = media[0]["url"]
        if not image:
            m = re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", "") or "")
            image = m.group(1) if m else ""
        items.append(_item(src, title, link, when, desc, image))
    return items[:MAX_PER_SOURCE]


# ---------- Google-News sitemaps (Somoy, Jamuna) ----------

def _parse_iso(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _fetch_news_sitemap(src) -> list:
    xml = http_get(src["url"]).text
    items = []
    for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block)
        title = re.search(r"<news:title>\s*(.*?)\s*</news:title>", block, re.S)
        date = re.search(r"<news:publication_date>\s*(.*?)\s*</news:publication_date>", block)
        img = re.search(r"<image:loc>\s*(.*?)\s*</image:loc>", block)
        if not (loc and title):
            continue
        when = _parse_iso(date.group(1)) if date else datetime.now(timezone.utc)
        if when < _cutoff():
            continue
        url = loc.group(1)
        # jamuna.tv URLs carry the category: https://www.jamuna.tv/<category>/<id>
        cat = ""
        m = re.match(r"https?://[^/]+/([a-z-]+)/", url)
        if m:
            cat = m.group(1)
        t = re.sub(r"<!\[CDATA\[|\]\]>", "", title.group(1)).strip()
        items.append(_item(src, t, url, when,
                           image=img.group(1) if img else "", category=cat))
        if len(items) >= MAX_PER_SOURCE:
            break
    return items


# ---------- The Daily Star /todays-news page ----------

_DS_ARTICLE = re.compile(r"^/[\w/-]+/news/[\w-]+-\d+$")


def _fetch_dailystar_today(src) -> list:
    soup = BeautifulSoup(http_get(src["url"]).text, "html.parser")
    now = datetime.now(timezone.utc)
    items, seen = [], set()
    for h3 in soup.find_all("h3", class_="card-title"):
        a = h3.find("a", href=True)
        if not a:
            continue
        href = a["href"].split("?")[0]
        title = a.get_text(" ", strip=True)
        if not _DS_ARTICLE.match(href) or len(title) < 20 or href in seen:
            continue
        # the card wrapper carries "N hour(s) ago" and the intro text
        card, info, intro = h3, None, None
        for _ in range(5):
            card = card.parent
            if card is None:
                break
            info = card.find(class_="card-info")
            if info:
                intro = card.find(class_="card-intro")
                break
        when = None
        if info:
            m = re.search(r"(\d+)\s*(minute|hour|day)", info.get_text(" ", strip=True).lower())
            if m:
                n, unit = int(m.group(1)), m.group(2)
                when = now - timedelta(**{unit + "s": n})
        if when and when < _cutoff():
            continue
        seen.add(href)
        desc = intro.get_text(" ", strip=True)[:400] if intro else ""
        cat = href.strip("/").split("/")[0]
        items.append(_item(src, title, "https://www.thedailystar.net" + href, when,
                           desc=desc, category=cat))
        if len(items) >= MAX_PER_SOURCE:
            break
    return items


_FETCHERS = {
    "rss": _fetch_rss,
    "news_sitemap": _fetch_news_sitemap,
    "html_todays_news": _fetch_dailystar_today,
}


def interleave_cap(items: list, cap: int = 150) -> list:
    """Round-robin across sources so no outlet crowds the others out of the
    candidate window (Daily Star items are all stamped 'now' and would
    otherwise dominate a plain newest-first cut)."""
    by_src = {}
    for it in items:
        by_src.setdefault(it["source_id"], []).append(it)
    queues = list(by_src.values())
    out = []
    while len(out) < cap and any(queues):
        for q in queues:
            if q and len(out) < cap:
                out.append(q.pop(0))
    return out


def fetch_all() -> list:
    all_items = []
    for src in config.SOURCES:
        try:
            items = _FETCHERS[src["kind"]](src)
        except Exception as e:
            print(f"  [warn] {src['name']}: fetch failed: {e}")
            items = []
        print(f"  {src['name']}: {len(items)} fresh items")
        all_items.extend(items)
    all_items.sort(key=lambda x: x["published"], reverse=True)
    seen, out = set(), []
    for it in all_items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out
