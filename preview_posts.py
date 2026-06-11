"""One-off demo: pick the best most-recent story from EACH outlet, compose the
full carousel (cover + details slides) and render it. Doesn't touch posting
history; renders into preview/ (gitignored) so demo images never end up in the
repo next to the bot's real output."""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

from src import article, brain, config, feeds, render

config.OUTPUT_DIR = os.path.join(config.ROOT, "preview")
config.MAX_POSTS_PER_RUN = 4
brain._SELECT_PROMPT = brain._SELECT_PROMPT.replace(
    "3. SELECT the {max_posts} most newsworthy remaining stories",
    "3. SELECT exactly ONE story per outlet (Dhaka Tribune, The Daily Star, "
    "Somoy News, Jamuna TV) — the most recent genuinely newsworthy story from "
    "each, {max_posts} total",
)

print("== Fetching feeds (official sites) ==")
candidates = feeds.fetch_all()
print(f"Total: {len(candidates)}")

print("== Phase 1: Gemini selecting one per source ==")
stories = brain.select_stories(feeds.interleave_cap(candidates, 150), history=[])

print("== Phase 2: composing posts from article text ==")
posts = []
for idx, story in enumerate(stories):
    cluster = story["cluster"]
    primary = next((c for c in cluster if c["lang"] == "en"), cluster[0])
    art = article.fetch_article(primary["url"])
    img_url = article.upgrade_thumb(primary.get("image", "")) or art["og_image"]
    image_uri = article.fetch_as_data_uri(img_url)
    gallery = [(image_uri, primary["source"])] if image_uri else []
    post = brain.compose_post(story, art["text"], gallery)
    posts.append(post)
    print(f"\n--- {post['source']} | {post['category']} | {post['template']}")
    print("headline:", post["headline_marked"])
    print("summary :", post["summary"])
    print("details :", len(post["details"]), "paragraphs,",
          sum(len(d) for d in post["details"]), "chars")
    print("caption :", post["caption"][:160], "...")
    print("tweet   :", post["tweet"])
    print("photo   :", "yes" if post["image_data_uri"] else "no",
          "| article text:", len(art["text"]), "chars")

print("\n== Rendering ==")
render.render_posts(posts)
for p in posts:
    print("  ->", " + ".join(p["image_files"]))
