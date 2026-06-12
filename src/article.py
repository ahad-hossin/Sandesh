"""Fetch an article page once and extract both the og:image and the body text
(the body feeds the details slide). Generic heuristics that work across all
four outlets; every step degrades gracefully."""
import base64
import re

from bs4 import BeautifulSoup

from . import config
from .feeds import http_get


def fetch_article(url: str) -> dict:
    """Returns {"og_image": str, "text": str}; empty strings on failure."""
    out = {"og_image": "", "text": ""}
    try:
        resp = http_get(url)
        if resp.status_code != 200:
            return out
        html = resp.text
    except Exception as e:
        print(f"  [warn] article fetch failed {url}: {e}")
        return out

    soup = BeautifulSoup(html, "html.parser")

    og = soup.find("meta", attrs={"property": "og:image"}) or \
         soup.find("meta", attrs={"name": "og:image"})
    if og and og.get("content"):
        img = og["content"].strip()
        out["og_image"] = upgrade_thumb(img)

    # body text: prefer <article>/known body containers, else all decent <p>s
    scope = (soup.find("article")
             or soup.find(class_=re.compile(r"(article|news|post|details?)[-_]?(body|content|details)", re.I))
             or soup)
    paras = []
    for p in scope.find_all("p"):
        t = p.get_text(" ", strip=True)
        if len(t) >= 60 and not re.search(r"(copyright|all rights reserved|follow us)", t, re.I):
            paras.append(t)
    text = "\n".join(paras)
    if not text:
        d = soup.find("meta", attrs={"property": "og:description"}) or \
            soup.find("meta", attrs={"name": "description"})
        if d and d.get("content"):
            text = d["content"].strip()
    out["text"] = text[:5000]
    return out


def upgrade_thumb(url: str) -> str:
    """Normalize any candidate image URL: swap branded share-composites for
    the unbranded original (several outlets bake their logo band into the
    social image) and upgrade tiny cache thumbs to full size."""
    url = url or ""
    if "thedailystar.net" in url:
        url = url.replace("/styles/social_share/", "/styles/big_4/")
    url = re.sub(r"(cloudfront\.net)/meta-top/meta_", r"\1/", url)   # Somoy News
    if "assettype.com/bdnews" in url:
        url = url.replace("/PLATE/", "/")                            # bdnews24
    # Dhaka Tribune RSS ships 300x300 cache thumbs; ask the CDN for a big one
    return re.sub(r"/cache/images/\d+x\d+x\d+/", "/cache/images/1100x617x1/", url)


def fetch_as_data_uri(image_url: str) -> str:
    """Download the image and inline it, so the headless renderer never
    depends on a CDN allowing hotlinks from a CI box."""
    if not image_url:
        return ""
    try:
        resp = http_get(image_url)
        if resp.status_code != 200 or not resp.content:
            return ""
        ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if not ctype.startswith("image/") or len(resp.content) > 8_000_000:
            return ""
        b64 = base64.b64encode(resp.content).decode("ascii")
        return f"data:{ctype};base64,{b64}"
    except Exception as e:
        print(f"  [warn] image download failed: {e}")
        return ""
