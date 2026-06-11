"""Posted-history persistence. The JSON file is committed back to the repo by
the workflow, so history survives between Actions runs."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from . import config


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_title(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    return re.sub(r"[^\wঀ-৿ ]", "", t)


def title_hash(title: str) -> str:
    return hashlib.sha1(norm_title(title).encode("utf-8")).hexdigest()[:16]


def load_history() -> list:
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(history: list) -> None:
    history = history[-config.HISTORY_KEEP:]
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)


def seen_keys(history: list) -> set:
    keys = set()
    for e in history:
        if e.get("url"):
            keys.add(e["url"])
        if e.get("title_hash"):
            keys.add(e["title_hash"])
    return keys


def record(history: list, item: dict, status: str) -> None:
    history.append({
        "title_hash": title_hash(item.get("orig_title") or item.get("headline", "")),
        "url": item.get("url", ""),
        "headline": item.get("headline", ""),
        "topic": item.get("topic", ""),
        "source": item.get("source", ""),
        "status": status,  # queued | posted | dry-run | failed
        "at": _now(),
    })


_STOPWORDS = {
    "the", "a", "an", "in", "on", "at", "of", "for", "to", "by", "with", "and",
    "or", "as", "is", "are", "was", "were", "be", "been", "after", "over",
    "amid", "against", "regarding", "about", "from", "its", "his", "her",
}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _tokens(text: str) -> set:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return {_stem(w) for w in words if w not in _STOPWORDS and len(w) > 2}


def _numbers(text: str) -> set:
    """Distinctive figures ('9.38', '404', '6.5') are the strongest same-story
    signal there is. Plain years are too generic to count."""
    out = set()
    for n in re.findall(r"\d+(?:\.\d+)?", text or ""):
        if re.fullmatch(r"(19|20)\d{2}", n) or len(n) < 2:
            continue
        out.add(n)
    return out


def is_duplicate(headline: str, topic: str, history: list, threshold: float = 0.5) -> bool:
    """Deterministic backstop for Gemini's semantic dedup. Two triggers:
    high word overlap (same story reworded), or a shared distinctive figure
    plus several shared content words (same event at a different stage,
    e.g. 'budget to be presented' -> 'budget unveiled')."""
    new_words = _tokens(headline) | _tokens(topic)
    new_nums = _numbers(headline) | _numbers(topic)
    if not new_words:
        return False
    for e in history:
        old_text = f"{e.get('headline', '')} {e.get('topic', '')}"
        old_words = _tokens(old_text)
        if not old_words:
            continue
        shared = new_words & old_words
        jaccard = len(shared) / len(new_words | old_words)
        # containment on headlines alone: topics vary in wording and would
        # dilute the ratio (a reworded headline shares most of its words)
        hl_new, hl_old = _tokens(headline), _tokens(e.get("headline", ""))
        shared_hl = hl_new & hl_old
        containment = (len(shared_hl) / min(len(hl_new), len(hl_old))
                       if hl_new and hl_old else 0.0)
        shared_nums = new_nums & _numbers(old_text)
        if (jaccard >= threshold
                or (shared_nums and len(shared) >= 3)
                or (containment >= 0.55 and len(shared_hl) >= 4)):
            print(f"  [dedup] '{headline[:60]}' matches posted '{e.get('headline', '')[:60]}' "
                  f"(jaccard={jaccard:.2f}, containment={containment:.2f}, figures={sorted(shared_nums)})")
            return True
    return False


def top_overlapping(headline: str, topic: str, history: list, k: int = 8) -> list:
    """Recent history entries sharing at least 2 content words with the new
    story — candidates for the AI same-event check."""
    new_words = _tokens(headline) | _tokens(topic)
    scored = []
    for e in history[-200:]:
        old_words = _tokens(f"{e.get('headline', '')} {e.get('topic', '')}")
        shared = len(new_words & old_words)
        if shared >= 2 and e.get("headline"):
            scored.append((shared, e["headline"]))
    scored.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, h in scored:
        if h not in seen:
            seen.add(h)
            out.append(h)
        if len(out) >= k:
            break
    return out


def load_queue() -> list:
    if os.path.exists(config.QUEUE_FILE):
        with open(config.QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_queue(queue: list) -> None:
    os.makedirs(os.path.dirname(config.QUEUE_FILE), exist_ok=True)
    with open(config.QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=1)
