"""
GMU Experts Daily Digest
=========================
Fetches today's top headlines from a handful of free public RSS feeds,
matches each headline against the GMU Issue Tag taxonomy, surfaces 2-5
matching GMU experts per headline, writes:
  - today.json   (consumed by index.html's "Today's Picks" section)
  - a standalone digest email, sent via Gmail SMTP to the configured recipients

Run manually:   python3 daily_digest.py
Run in CI:      see .github/workflows/daily-digest.yml
"""

import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "https://feeds.npr.org/1001/rss.xml",
    "http://feeds.bbci.co.uk/news/rss.xml",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://news.google.com/rss/search?q=when:24h&hl=en-US&gl=US&ceid=US:en",
]

MAX_HEADLINES = 18
MIN_HEADLINES_WARN = 6
EXPERTS_PER_HEADLINE = 5
EXPERTS_PER_HEADLINE_MIN = 2

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
EXPERTS_PATH = os.path.join(DATA_DIR, "experts_data.json")
TAXONOMY_PATH = os.path.join(DATA_DIR, "taxonomy_full.json")
OUT_JSON_PATH = os.path.join(DATA_DIR, "today.json")

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENTS = [e.strip() for e in os.environ.get("DIGEST_RECIPIENTS", "").split(",") if e.strip()]

UA = "Mozilla/5.0 (compatible; GMUExpertsDigest/1.0)"


# ---------------------------------------------------------------------------
# RSS fetching (stdlib only, no feedparser dependency)
# ---------------------------------------------------------------------------

def fetch_feed(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        items = []
        # RSS 2.0 <item>, Atom <entry>
        for item in root.iter():
            tag = item.tag.split("}")[-1]
            if tag == "item":
                title_el = item.find("title")
                link_el = item.find("link")
                title = (title_el.text or "").strip() if title_el is not None else ""
                link = (link_el.text or "").strip() if link_el is not None else ""
                if title:
                    items.append({"title": title, "link": link})
            elif tag == "entry":
                title_el = item.find("{http://www.w3.org/2005/Atom}title")
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                title = (title_el.text or "").strip() if title_el is not None else ""
                link = link_el.get("href", "") if link_el is not None else ""
                if title:
                    items.append({"title": title, "link": link})
        return items
    except Exception as e:
        print(f"  [warn] failed to fetch {url}: {e}", file=sys.stderr)
        return []


def dedupe_headlines(headlines):
    seen = set()
    out = []
    for h in headlines:
        key = re.sub(r"[^a-z0-9]", "", h["title"].lower())[:50]
        if key and key not in seen:
            seen.add(key)
            out.append(h)
    return out


# ---------------------------------------------------------------------------
# Matching (same keyword-substring approach as the web tool, for consistency)
# ---------------------------------------------------------------------------

def load_taxonomy():
    tax = json.load(open(TAXONOMY_PATH))
    return tax["tags"], tax["kw"]


def match_tags(text, all_tags, keywords):
    lower = text.lower()
    matched = []
    for tag in all_tags:
        hits = [kw for kw in keywords.get(tag, []) if kw.lower() in lower]
        if hits:
            matched.append((tag, len(hits)))
    matched.sort(key=lambda x: -x[1])
    return [t for t, _ in matched]


def tier_rank(tier):
    return {"Primary": 0, "Secondary": 1}.get(tier, 2)


def pick_experts(matched_tags, people):
    tagset = set(matched_tags)
    scored = []
    for p in people:
        hit = [t for t in p.get("tags", []) if t in tagset]
        if hit:
            scored.append((p, len(hit)))
    scored.sort(key=lambda x: (tier_rank(x[0].get("tier")), -x[1]))
    return [p for p, _ in scored]


# ---------------------------------------------------------------------------
# Build today's digest
# ---------------------------------------------------------------------------

def build_digest():
    all_tags, keywords = load_taxonomy()
    people = json.load(open(EXPERTS_PATH))

    raw_headlines = []
    for feed_url in RSS_FEEDS:
        raw_headlines.extend(fetch_feed(feed_url))
    raw_headlines = dedupe_headlines(raw_headlines)
    print(f"Fetched {len(raw_headlines)} unique headlines from {len(RSS_FEEDS)} feeds")

    scored_headlines = []
    for h in raw_headlines:
        matched = match_tags(h["title"], all_tags, keywords)
        if not matched:
            continue
        experts = pick_experts(matched, people)
        if not experts:
            continue
        scored_headlines.append({
            "title": h["title"],
            "link": h["link"],
            "matched_tags": matched[:4],
            "experts": experts[:EXPERTS_PER_HEADLINE],
            "score": len(matched),
        })

    scored_headlines.sort(key=lambda x: -x["score"])
    final = scored_headlines[:MAX_HEADLINES]

    if len(final) < MIN_HEADLINES_WARN:
        print(f"  [warn] only {len(final)} headlines matched an expert tag today", file=sys.stderr)

    digest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "headline_count": len(final),
        "items": [
            {
                "title": item["title"],
                "link": item["link"],
                "tags": item["matched_tags"],
                "experts": [
                    {
                        "name": e["name"],
                        "title": e.get("title"),
                        "college": e.get("college"),
                        "tier": e.get("tier"),
                        "email": e.get("email"),
                        "profile_url": e.get("profile_url"),
                    }
                    for e in item["experts"]
                ],
            }
            for item in final
        ],
    }
    return digest


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_email_html(digest):
    date_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    rows = []
    for item in digest["items"]:
        experts_html = ""
        for e in item["experts"]:
            name_html = (
                f'<a href="{e["profile_url"]}" style="color:#006633;text-decoration:none;">{e["name"]}</a>'
                if e.get("profile_url") else e["name"]
            )
            email_html = f' &middot; <a href="mailto:{e["email"]}" style="color:#6b6b6b;">{e["email"]}</a>' if e.get("email") else ""
            tier_color = "#006633" if e.get("tier") == "Primary" else "#7a7a7a"
            experts_html += (
                f'<div style="padding:4px 0;font-size:13px;">'
                f'<span style="background:{tier_color};color:white;font-size:10px;font-weight:700;'
                f'padding:2px 7px;border-radius:999px;text-transform:uppercase;margin-right:6px;">{e.get("tier","")}</span>'
                f'<strong>{name_html}</strong> &mdash; {e.get("title") or ""} ({e.get("college") or ""}){email_html}'
                f'</div>'
            )
        tags_html = " &middot; ".join(item["tags"])
        link_html = f'<a href="{item["link"]}" style="color:#1a1a1a;text-decoration:none;">{item["title"]}</a>' if item["link"] else item["title"]
        rows.append(
            f'<div style="padding:16px 0;border-bottom:1px solid #e2e2e2;">'
            f'<div style="font-size:16px;font-weight:700;margin-bottom:4px;">{link_html}</div>'
            f'<div style="font-size:12px;color:#6b6b6b;margin-bottom:8px;">{tags_html}</div>'
            f'{experts_html}'
            f'</div>'
        )

    body = "".join(rows) if rows else '<p style="color:#6b6b6b;">No headlines matched a GMU expert tag today.</p>'

    return f"""\
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f7f7f5;margin:0;padding:0;">
<div style="max-width:680px;margin:0 auto;padding:24px 20px;">
  <div style="background:#006633;color:white;padding:20px 24px;border-radius:10px 10px 0 0;">
    <h1 style="margin:0;font-size:20px;">GMU Experts Daily Digest</h1>
    <p style="margin:4px 0 0;color:#d9f2e4;font-size:13px;">{date_str}</p>
  </div>
  <div style="background:white;padding:8px 24px 20px;border-radius:0 0 10px 10px;">
    {body}
  </div>
  <p style="text-align:center;color:#9a9a9a;font-size:11px;margin-top:16px;">
    Generated automatically from public RSS headlines and the GMU Experts Database.
  </p>
</div>
</body></html>
"""


def send_email(digest):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and RECIPIENTS):
        print("  [info] email not sent: GMAIL_USER / GMAIL_APP_PASSWORD / DIGEST_RECIPIENTS not all set", file=sys.stderr)
        return False

    date_str = datetime.now(timezone.utc).strftime("%b %d, %Y")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"GMU Experts Daily Digest — {date_str}"
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(render_email_html(digest), "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, RECIPIENTS, msg.as_string())
    print(f"Email sent to {RECIPIENTS}")
    return True


def main():
    digest = build_digest()
    json.dump(digest, open(OUT_JSON_PATH, "w"), indent=1)
    print(f"Wrote {OUT_JSON_PATH} with {digest['headline_count']} headlines")
    send_email(digest)


if __name__ == "__main__":
    main()
