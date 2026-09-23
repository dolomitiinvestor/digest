#!/usr/bin/env python3
"""
HN daily digest: fetch -> filter -> summarize -> email.

Env vars required:
  ANTHROPIC_API_KEY
  RESEND_API_KEY
  MAIL_TO
  MAIL_FROM       e.g. "digest@yourdomain.com" (or "onboarding@resend.dev" to test)

Usage:
  python hn_digest.py              # fetch, summarize, send
  python hn_digest.py --dry-run    # writes preview.html, no email sent
  python hn_digest.py --no-llm     # skip Claude entirely, just show what survives the filters
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests
import yaml

ALGOLIA = "https://hn.algolia.com/api/v1/search"
FIREBASE = "https://hacker-news.firebaseio.com/v0"
READER = "https://r.jina.ai/"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5"
UA = {"User-Agent": "hn-digest/1.0"}


# ---------------------------------------------------------------- config

def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------- fetch

def fetch_candidates(cfg):
    """Top stories from the last N hours, ranked by points (Algolia's default)."""
    since = int(time.time()) - cfg["lookback_hours"] * 3600
    r = requests.get(
        ALGOLIA,
        params={
            "tags": "story",
            "numericFilters": f"created_at_i>{since},points>={cfg['min_points']}",
            "hitsPerPage": cfg["max_candidates"],
        },
        headers=UA,
        timeout=20,
    )
    r.raise_for_status()
    out = []
    for h in r.json()["hits"]:
        if not h.get("title"):
            continue
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        out.append(
            {
                "id": h["objectID"],
                "title": h["title"],
                "url": url,
                "domain": re.sub(r"^www\.", "", url.split("/")[2]),
                "points": h.get("points", 0),
                "comments": h.get("num_comments", 0),
                "hn_url": f"https://news.ycombinator.com/item?id={h['objectID']}",
                "is_text_post": not h.get("url"),
                "text": h.get("story_text") or "",
            }
        )
    return out


def fetch_article(url, max_chars=12000):
    """Plaintext via Jina Reader. Returns '' on failure — caller degrades gracefully."""
    try:
        r = requests.get(READER + url, headers=UA, timeout=45)
        if r.status_code != 200:
            return ""
        return r.text[:max_chars]
    except requests.RequestException:
        return ""


def fetch_top_comments(story_id, n=5, max_chars=400):
    """Top-level comments in HN rank order. Often more useful than the article."""
    try:
        item = requests.get(f"{FIREBASE}/item/{story_id}.json", headers=UA, timeout=15).json()
        kids = (item or {}).get("kids", [])[:n]
        out = []
        for kid in kids:
            c = requests.get(f"{FIREBASE}/item/{kid}.json", headers=UA, timeout=15).json()
            if not c or c.get("deleted") or c.get("dead") or not c.get("text"):
                continue
            txt = re.sub(r"<[^>]+>", " ", c["text"])
            txt = html.unescape(re.sub(r"\s+", " ", txt)).strip()
            out.append(txt[:max_chars])
        return out
    except (requests.RequestException, ValueError):
        return []


# ---------------------------------------------------------------- filters

def prefilter(stories, cfg):
    """Free keyword/domain pass. Runs before any token is spent."""
    block_kw = [k.lower() for k in cfg.get("block_keywords", [])]
    block_dom = [d.lower() for d in cfg.get("block_domains", [])]
    always = [k.lower() for k in cfg.get("always_include_keywords", [])]

    kept, forced = [], []
    for s in stories:
        blob = f"{s['title']} {s['domain']}".lower()
        if any(k in blob for k in always):
            forced.append(s)
            continue
        if any(k in blob for k in block_kw):
            continue
        if any(d in s["domain"].lower() for d in block_dom):
            continue
        kept.append(s)
    return kept, forced


def rank_with_llm(stories, cfg):
    """One batched call: score every title 0-10 against the interest profile."""
    if not stories:
        return []
    listing = "\n".join(
        f"{i}. [{s['points']}pts] {s['title']} ({s['domain']})" for i, s in enumerate(stories)
    )
    system = (
        "You score Hacker News headlines against a reader's stated interests. "
        "Be strict: a 7+ means the reader would very likely open it. "
        "Reply with ONLY a JSON array, no prose, no markdown fences."
    )
    prompt = f"""Reader's interests:
{cfg['interests']}

Explicitly NOT interested in:
{cfg.get('anti_interests', 'nothing in particular')}

Stories:
{listing}

Return a JSON array of objects: {{"i": <index>, "score": <0-10>, "why": "<8 words max>"}}
Include every index exactly once."""

    raw = claude(prompt, system, max_tokens=4000)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        scores = json.loads(raw)
    except json.JSONDecodeError:
        print("WARN: relevance JSON unparseable, keeping top stories by points", file=sys.stderr)
        return stories[: cfg["max_stories"]]

    for entry in scores:
        idx = entry.get("i")
        if isinstance(idx, int) and 0 <= idx < len(stories):
            stories[idx]["score"] = entry.get("score", 0)
            stories[idx]["why"] = entry.get("why", "")

    hits = [s for s in stories if s.get("score", 0) >= cfg["relevance_threshold"]]
    hits.sort(key=lambda s: (-s.get("score", 0), -s["points"]))
    return hits[: cfg["max_stories"]]


# ---------------------------------------------------------------- claude

def claude(prompt, system, max_tokens=1024, retries=3):
    key = os.environ["ANTHROPIC_API_KEY"]
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    for attempt in range(retries):
        r = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=120,
        )
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(2 ** attempt * 3)
            continue
        if not r.ok:
            raise RuntimeError(f"Anthropic API error {r.status_code}: {r.text[:2000]}")
        return "".join(b["text"] for b in r.json()["content"] if b["type"] == "text")
    raise RuntimeError(f"Anthropic API failed after {retries} attempts: {r.status_code} {r.text[:200]}")


def summarize(story, cfg):
    """Fetch the article + comments, then summarize. One Claude call per story."""
    article = "" if story["is_text_post"] else fetch_article(story["url"])
    if story["is_text_post"]:
        article = re.sub(r"<[^>]+>", " ", story.get("text", ""))[:12000]
    comments = fetch_top_comments(story["id"], n=cfg["comments_per_story"])

    if not article and not comments:
        story["summary"] = "<em>Could not fetch content (paywall, PDF, or video).</em>"
        return story

    system = (
        "You write terse, factual digest entries. No hype, no hedging, no filler openers "
        "like 'This article discusses'. Lead with the actual claim or finding."
    )
    prompt = f"""Title: {story['title']}
Source: {story['domain']}

ARTICLE TEXT:
{article[:12000] or '(unavailable)'}

TOP HN COMMENTS:
{chr(10).join('- ' + c for c in comments) or '(none)'}

Write:
1. {cfg['summary_sentences']} sentences on what the article actually says or claims.
2. One line starting with "HN:" giving the gist of the comment section — especially any
   pushback, correction, or context the article itself lacks. Skip if comments are empty.

Plain text. No headings, no bullets, no preamble."""

    story["summary"] = html.escape(claude(prompt, system, max_tokens=500)).replace("\n", "<br>")
    story["summary"] = re.sub(r"(HN:)", r"<strong>\1</strong>", story["summary"])
    return story


# ---------------------------------------------------------------- output

def render_html(stories, cfg):
    today = dt.date.today().strftime("%a %d %b %Y")
    rows = []
    for s in stories:
        badge = (
            f'<span style="color:#888;font-size:12px"> · relevance {s["score"]}/10</span>'
            if "score" in s
            else ""
        )
        rows.append(f"""
<div style="margin:0 0 28px 0;padding:0 0 24px 0;border-bottom:1px solid #eee">
  <a href="{html.escape(s['url'])}"
     style="font-size:17px;font-weight:600;color:#111;text-decoration:none">{html.escape(s['title'])}</a>
  <div style="font-size:12px;color:#888;margin:5px 0 10px">
    {html.escape(s['domain'])} · {s['points']} points ·
    <a href="{s['hn_url']}" style="color:#ff6600;text-decoration:none">{s['comments']} comments</a>{badge}
  </div>
  <div style="font-size:14.5px;line-height:1.6;color:#333">{s['summary']}</div>
</div>""")

    body = "".join(rows) or "<p>Nothing matched your filters today.</p>"
    return f"""<!doctype html><html><body style="margin:0;background:#fafafa">
<div style="max-width:620px;margin:0 auto;padding:32px 22px;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif">
  <div style="font-size:13px;color:#ff6600;font-weight:700;letter-spacing:.08em;
              text-transform:uppercase;margin-bottom:4px">HN Digest</div>
  <div style="font-size:13px;color:#888;margin-bottom:28px">{today} · {len(stories)} stories</div>
  {body}
  <div style="font-size:11px;color:#aaa;margin-top:8px">
    Filtered on: {html.escape(cfg['interests'][:180])}
  </div>
</div></body></html>"""


SEND_TZ = ZoneInfo("America/New_York")
SEND_HOUR = 7


def next_send_time():
    """Next 7:00am Eastern (EST or EDT, whichever is in effect)."""
    now = dt.datetime.now(SEND_TZ)
    target = now.replace(hour=SEND_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += dt.timedelta(days=1)
    return target


def send_email(body_html, n):
    # Scheduled runs build the digest the evening before and let Resend
    # deliver it at exactly 7am ET — GitHub cron can start hours late.
    # Manual runs send immediately.
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    send_at = next_send_time() if scheduled else dt.datetime.now(SEND_TZ)
    payload = {
        "from": os.environ["MAIL_FROM"],
        "to": [e.strip() for e in os.environ["MAIL_TO"].split(",")],
        "subject": f"HN Digest — {send_at:%b %d} ({n} stories)",
        "html": body_html,
    }
    if scheduled:
        payload["scheduled_at"] = send_at.isoformat()
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    when = f"scheduled for {send_at:%a %b %d %H:%M %Z}" if scheduled else "sent now"
    print(f"Email {when}: {r.json().get('id')}")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write preview.html, don't email")
    ap.add_argument("--no-llm", action="store_true", help="skip filtering and summarizing")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)

    candidates = fetch_candidates(cfg)
    print(f"{len(candidates)} candidates from the last {cfg['lookback_hours']}h")

    kept, forced = prefilter(candidates, cfg)
    print(f"{len(kept)} survived prefilter ({len(forced)} force-included by keyword)")

    if args.no_llm:
        selected = (forced + kept)[: cfg["max_stories"]]
        for s in selected:
            s["summary"] = "<em>(--no-llm: no summary)</em>"
    else:
        selected = forced + rank_with_llm(kept, cfg)
        selected = selected[: cfg["max_stories"]]
        print(f"{len(selected)} passed relevance >= {cfg['relevance_threshold']}, summarizing...")
        with ThreadPoolExecutor(max_workers=4) as pool:
            selected = list(pool.map(lambda s: summarize(s, cfg), selected))

    page = render_html(selected, cfg)

    if args.dry_run:
        with open("preview.html", "w") as f:
            f.write(page)
        print("Wrote preview.html")
    elif not selected and cfg.get("skip_empty_emails", True):
        print("Nothing matched; no email sent.")
    else:
        send_email(page, len(selected))


if __name__ == "__main__":
    main()
