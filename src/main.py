"""Orchestrator.

  python -m src.main generate   scrape -> Gemini select/dedup/caption -> render PNGs -> queue
  python -m src.main publish    post the queue to IG/FB/X (dry-run without credentials)

The workflow runs generate, commits the images (so Instagram can fetch them by
raw URL), then runs publish and commits the updated state."""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Bangla titles on Windows consoles

from . import article, brain, budget, config, feeds, render, state


def _summary(lines: list) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines)
    print(text)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def generate() -> int:
    # --- budget check: each run costs 1 Gemini call to select + 1 per post ---
    print(f"API budgets: {budget.summary()}")
    gem_left = budget.gemini_remaining_total()
    if gem_left < 2:
        _summary(["## News bot", f"Skipped: Gemini daily budget exhausted ({budget.summary()})"])
        state.save_queue([])
        return 0
    affordable = min(config.MAX_POSTS_PER_RUN, gem_left - 1)
    if affordable < config.MAX_POSTS_PER_RUN:
        print(f"[budget] capping run at {affordable} post(s) (Gemini calls left: {gem_left})")
        config.MAX_POSTS_PER_RUN = affordable

    print("== Fetching feeds ==")
    candidates = feeds.fetch_all()
    print(f"Total fresh candidates: {len(candidates)}")

    history = state.load_history()
    # failed publishes (e.g. during a Meta outage) must not block a retry —
    # dedup only against what actually went out
    history_active = [e for e in history if e.get("status") != "failed"]
    seen = state.seen_keys(history_active)
    fresh = [
        c for c in candidates
        if c["url"] not in seen and state.title_hash(c["title"]) not in seen
    ]
    print(f"After exact-dedup against history: {len(fresh)}")
    if not fresh:
        _summary(["## News bot", "No new candidates this run."])
        state.save_queue([])
        return 0

    print("== Phase 1: Gemini selects and dedupes stories ==")
    try:
        stories = brain.select_stories(feeds.interleave_cap(fresh, 180), history_active)
    except Exception as e:
        # a Gemini outage shouldn't fail the whole run — skip this hour
        _summary(["## News bot", f"Skipped this hour: Gemini unavailable ({e})"])
        state.save_queue([])
        return 0
    print(f"Gemini selected {len(stories)} story(ies)")
    if not stories:
        _summary(["## News bot", "Gemini selected no stories this run."])
        state.save_queue([])
        return 0

    print("== Phase 2: fetch articles, compose posts ==")
    posts = []
    for idx, story in enumerate(stories):
        if len(posts) >= config.MAX_POSTS_PER_RUN:
            break
        cluster = story["cluster"]
        primary = next((c for c in cluster if c["lang"] == "en"), cluster[0])
        art = article.fetch_article(primary["url"])
        art_provider = primary["source"]
        # fall back to another cluster member's page if the primary gave nothing
        if not art["text"] and len(cluster) > 1:
            for c in cluster:
                if c["url"] != primary["url"]:
                    art = article.fetch_article(c["url"])
                    if art["text"]:
                        art_provider = c["source"]
                        break
        # candidate photos in preference order: primary RSS thumb, primary
        # article og:image, then every other outlet's thumb/og:image
        candidates_img = []
        if primary.get("image"):
            candidates_img.append((article.upgrade_thumb(primary["image"]), primary["source"]))
        if art.get("og_image"):
            candidates_img.append((art["og_image"], art_provider))
        for c in cluster:
            if c["url"] == primary["url"]:
                continue
            if c.get("image"):
                candidates_img.append((article.upgrade_thumb(c["image"]), c["source"]))
            else:
                candidates_img.append(("og:" + c["url"], c["source"]))

        def _resolve(ref: str) -> str:
            if ref.startswith("og:"):
                ref = article.fetch_article(ref[3:]).get("og_image", "")
            return article.fetch_as_data_uri(ref) if ref else ""

        # resolve every outlet's candidate photo (up to the model's cap) and
        # let Gemini pick the best safe one inside the compose call
        gallery = []
        for ref, credit in candidates_img:
            if len(gallery) >= brain.MAX_IMAGES:
                break
            uri = _resolve(ref)
            if uri:
                gallery.append((uri, credit))

        try:
            post = brain.compose_post(story, art["text"], gallery)
        except Exception as e:
            print(f"  [warn] compose failed for '{story['topic']}': {e}")
            continue

        # dedup: lexical word-overlap raises the ALARM, the focused AI check
        # is the JUDGE — it knows a match result after a match preview (or a
        # verdict after a trial) is new news, not a duplicate
        lex_alarm = state.is_duplicate(post["headline"], post["topic"], history_active)
        similar = state.top_overlapping(post["headline"], post["topic"], history_active)
        if lex_alarm or similar:
            dup_idx = 0
            if similar:
                try:
                    dup_idx = brain.check_duplicate(post["headline"], post["summary"], similar)
                except Exception as e:
                    print(f"  [warn] AI dedup check failed ({e})")
                    dup_idx = 1 if lex_alarm else 0  # fail closed only on a lexical alarm
            elif lex_alarm:
                dup_idx = 1
            if dup_idx:
                ref = similar[dup_idx - 1][:60] if similar else "(lexical match)"
                print(f"  [dedup] skipping '{post['headline'][:60]}' = same event as '{ref}'")
                continue

        # content safety verdicts (came back in the same compose call)
        if post["story_risk"] == "do_not_post":
            print(f"  [policy] skipping '{post['headline'][:60]}' (cannot be covered within platform rules)")
            state.record(history, post, "policy-skip")
            for url, title in zip(post.get("cluster_urls", []), post.get("cluster_titles", [])):
                if url != post["url"]:
                    state.record(history, {"orig_title": title, "url": url,
                                           "headline": post["headline"], "topic": post["topic"],
                                           "source": post["source"]}, "policy-skip")
            continue
        photo_note = f"photo: {post['photo_credit']}" if post["image_data_uri"] else \
            (f"no photo (Gemini rejected {len(gallery)} candidate(s))" if gallery else "no photo available")
        print(f"  {post['headline'][:60]}... {photo_note}, risk={post['story_risk']}, details={len(post['details'])} paras")
        posts.append(post)

    if not posts:
        state.save_history(history)  # policy-skips must be remembered too
        _summary(["## News bot", "No posts could be composed this run."])
        state.save_queue([])
        return 0

    print("== Rendering post images ==")
    render.cleanup_old_images()
    render.render_posts(posts)

    # the queue holds everything publish needs; data URIs are too big to keep
    queue = []
    for p in posts:
        queue.append({k: v for k, v in p.items() if k != "image_data_uri"})
        # record every clustered title/url so other outlets' copies of the
        # same story are also recognized as posted
        state.record(history, p, "queued")
        for url, title in zip(p.get("cluster_urls", []), p.get("cluster_titles", [])):
            if url != p["url"]:
                state.record(history, {"orig_title": title, "url": url,
                                       "headline": p["headline"], "topic": p["topic"],
                                       "source": p["source"]}, "queued")
    state.save_queue(queue)
    state.save_history(history)

    lines = ["## News bot — generated", ""]
    for p in posts:
        photo = f"photo: {p['photo_credit']}" if p.get("image_data_uri") else "no photo"
        lines.append(f"- **{p['headline']}** ({p['category']}, {p['template']}, "
                     f"risk: {p['story_risk']}, {photo}, {len(p['image_files'])} slides) — {p['source']}")
    lines += ["", f"Budgets after run: {budget.summary()}"]
    _summary(lines)
    return 0


def publish() -> int:
    from . import publish as pub

    queue = state.load_queue()
    if not queue:
        print("Queue is empty, nothing to publish.")
        return 0

    history = state.load_history()
    lines = ["## News bot — published", ""]
    for post in queue:
        print(f"== Publishing: {post['headline'][:70]} ==")
        results = pub.publish_post(post)
        ok = [k for k, v in results.items()
              if not str(v).startswith(("error", "skipped", "dry-run"))]
        dry = all(str(v).startswith(("dry-run", "skipped")) for v in results.values())
        status = "dry-run" if dry else ("posted" if ok else "failed")
        # update the matching queued history entries
        for e in history:
            if e.get("url") in ([post["url"]] + post.get("cluster_urls", [])) and e.get("status") == "queued":
                e["status"] = status
                e["results"] = {k: str(v)[:160] for k, v in results.items()}
        lines.append(f"- **{post['headline']}** → " + ", ".join(f"{k}: {v if str(v).startswith(('dry', 'error')) else 'ok'}" for k, v in results.items()))

    state.save_history(history)
    state.save_queue([])
    lines += ["", f"Budgets after publish: {budget.summary()}"]
    _summary(lines)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "generate"
    if cmd == "generate":
        sys.exit(generate())
    elif cmd == "publish":
        sys.exit(publish())
    else:
        print(f"Unknown command: {cmd} (use 'generate' or 'publish')")
        sys.exit(1)
