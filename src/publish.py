"""Publish queued posts (cover + details slide) to Instagram, Facebook and X.

- Instagram: carousel post (or single image) via the Graph API. Images must be
  publicly reachable, which is why the workflow pushes output/ to GitHub first.
- Facebook: photos uploaded unpublished, then attached to one Page post.
- X: one tweet with up to 4 images.

Each platform is independent and optional: missing credentials = dry run."""
import os
import time

import requests

from . import budget, config

GRAPH = "https://graph.facebook.com/v21.0"


def _img_paths(post: dict) -> list:
    return [os.path.join(config.OUTPUT_DIR, f) for f in post["image_files"]]


def _img_urls(post: dict) -> list:
    base = config.public_image_base()
    return [f"{base}/{f}" for f in post["image_files"]] if base else []


def post_facebook(post: dict) -> str:
    """Facebook gets the cover image only — the caption carries the full
    story, so the detail slides would be redundant there."""
    if not (config.FB_PAGE_ID and config.META_ACCESS_TOKEN):
        return "dry-run"
    if budget.remaining("facebook", config.FB_DAILY_LIMIT) <= 0:
        return "skipped: daily API budget reached"
    cover = _img_paths(post)[0]
    with open(cover, "rb") as f:
        resp = requests.post(
            f"{GRAPH}/{config.FB_PAGE_ID}/photos",
            data={"caption": post["caption"], "access_token": config.META_ACCESS_TOKEN},
            files={"source": (os.path.basename(cover), f, "image/png")},
            timeout=120,
        )
    resp.raise_for_status()
    budget.spend("facebook")
    return resp.json().get("post_id") or resp.json().get("id", "ok")


def _ig_wait(container: str, tries: int = 8) -> None:
    # frugal polling — Meta's app-level budget is ~200 calls/hour and status
    # checks count against it
    for i in range(tries):
        time.sleep(4 + 2 * i)
        st = requests.get(
            f"{GRAPH}/{container}",
            params={"fields": "status_code", "access_token": config.META_ACCESS_TOKEN},
            timeout=60,
        ).json()
        if st.get("status_code") == "FINISHED":
            return
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"IG container error: {st}")


def _ig_publish(container: str):
    """media_publish with one patient retry if the app-hour budget is hit."""
    for attempt in range(2):
        resp = requests.post(
            f"{GRAPH}/{config.IG_USER_ID}/media_publish",
            data={"creation_id": container, "access_token": config.META_ACCESS_TOKEN},
            timeout=120,
        )
        if resp.status_code == 403 and "request limit" in resp.text.lower() and attempt == 0:
            print("  [warn] Meta app rate limit hit, waiting 90s before retrying publish...")
            time.sleep(90)
            continue
        resp.raise_for_status()
        return resp.json().get("id", "ok")


def post_instagram(post: dict) -> str:
    if not (config.IG_USER_ID and config.META_ACCESS_TOKEN):
        return "dry-run"
    if budget.remaining("instagram", config.IG_DAILY_LIMIT) <= 0:
        return "skipped: daily API budget reached (IG allows 50 posts/24h)"
    urls = _img_urls(post)
    if not urls:
        raise RuntimeError("No public image URL (set PUBLIC_IMAGE_BASE or run in Actions)")
    # IG fetches images by URL; if the repo is private the raw URL 404s
    probe = requests.head(urls[0], timeout=30, allow_redirects=True)
    if probe.status_code != 200:
        return f"skipped: image URL not publicly reachable (HTTP {probe.status_code} — is the repo private?)"

    if len(urls) == 1:
        resp = requests.post(
            f"{GRAPH}/{config.IG_USER_ID}/media",
            data={"image_url": urls[0], "caption": post["caption"],
                  "access_token": config.META_ACCESS_TOKEN},
            timeout=120,
        )
        resp.raise_for_status()
        container = resp.json()["id"]
    else:
        children = []
        for u in urls:
            resp = requests.post(
                f"{GRAPH}/{config.IG_USER_ID}/media",
                data={"image_url": u, "is_carousel_item": "true",
                      "access_token": config.META_ACCESS_TOKEN},
                timeout=120,
            )
            resp.raise_for_status()
            child = resp.json()["id"]
            _ig_wait(child)
            children.append(child)
        resp = requests.post(
            f"{GRAPH}/{config.IG_USER_ID}/media",
            data={"media_type": "CAROUSEL", "children": ",".join(children),
                  "caption": post["caption"], "access_token": config.META_ACCESS_TOKEN},
            timeout=120,
        )
        resp.raise_for_status()
        container = resp.json()["id"]

    _ig_wait(container)
    result = _ig_publish(container)
    budget.spend("instagram")
    return result


def post_x(post: dict) -> str:
    if not (config.X_API_KEY and config.X_API_SECRET and config.X_ACCESS_TOKEN and config.X_ACCESS_SECRET):
        return "dry-run"
    if budget.remaining("x", config.X_DAILY_LIMIT) <= 0:
        return "skipped: daily API budget reached"
    if budget.remaining("x_month", config.X_MONTHLY_LIMIT, "month") <= 0:
        return "skipped: monthly API budget reached (X free tier ~500 posts/month)"
    import tweepy

    auth = tweepy.OAuth1UserHandler(
        config.X_API_KEY, config.X_API_SECRET,
        config.X_ACCESS_TOKEN, config.X_ACCESS_SECRET,
    )
    api_v1 = tweepy.API(auth)  # media upload still lives on v1.1
    media_ids = [api_v1.media_upload(filename=p).media_id for p in _img_paths(post)[:4]]
    client = tweepy.Client(
        consumer_key=config.X_API_KEY,
        consumer_secret=config.X_API_SECRET,
        access_token=config.X_ACCESS_TOKEN,
        access_token_secret=config.X_ACCESS_SECRET,
    )
    text = post.get("tweet") or post["headline"]
    resp = client.create_tweet(text=text[:280], media_ids=media_ids)
    budget.spend("x")
    budget.spend("x_month", period="month")
    return str(resp.data.get("id", "ok"))


def publish_post(post: dict) -> dict:
    """Returns {platform: result-or-error} for one queued post."""
    results = {}
    for name, fn in (("facebook", post_facebook), ("instagram", post_instagram), ("x", post_x)):
        try:
            results[name] = fn(post)
        except Exception as e:
            detail = str(e)
            body = getattr(getattr(e, "response", None), "text", "")
            if body:
                detail += f" :: {body[:300]}"
            results[name] = f"error: {detail}"
            print(f"  [error] {name}: {detail}")
    return results
