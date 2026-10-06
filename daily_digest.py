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

import email.utils as emailutils
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# GUARDRAIL 1 — recency window. Any headline whose published timestamp can't
# be confirmed to fall within this many hours of "now" is dropped. Items with
# no parseable date at all are dropped too (treated as "can't verify recency",
# not "assume it's fine").
RECENCY_HOURS = 24

# GUARDRAIL 2 — source breadth. General top-of-day feeds, plus targeted
# category feeds that line up with the Issue Tag taxonomy's subject areas,
# deliberately spanning outlets rated left-leaning, center, and right-leaning
# by independent media-bias trackers (AllSides / Ad Fontes), so the digest
# isn't sourced from one side of the spectrum. Keeping this list grouped and
# labeled by lean (per those trackers' general consensus, not my own judgment)
# so it's easy to see the balance at a glance and adjust later.
RSS_FEEDS = [
    # --- Left-leaning / left-of-center ---
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://feeds.npr.org/1001/rss.xml",
    "https://www.theguardian.com/us/rss",

    # --- Center / international wire-style ---
    "http://feeds.bbci.co.uk/news/rss.xml",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",       # WSJ World News (news desk, not opinion)
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",     # WSJ Markets/Business

    # --- Right-leaning / right-of-center ---
    "https://moxie.foxnews.com/google-publisher/latest.xml",
    "https://www.washingtonexaminer.com/feed",

    # --- Broad daily sweep, not tied to one outlet ---
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
ARCHIVE_DIR = os.path.join(DATA_DIR, "digests")

# Groups every Issue Tag into a broad category, purely for readability when
# rendering the email (and the Past Digests view) — doesn't affect matching.
TAG_CATEGORY = {
    # Politics & Government
    "Elections & Voting": "Politics & Government",
    "Congress & Legislation": "Politics & Government",
    "Presidency & Executive Branch": "Politics & Government",
    "State & Local Government": "Politics & Government",
    "Political Polarization & Extremism": "Politics & Government",
    "Campaigns & Political Communication": "Politics & Government",
    "Supreme Court & Constitutional Law": "Politics & Government",
    "Free Speech / First Amendment": "Politics & Government",
    "Criminal Justice & Policing": "Politics & Government",
    "Immigration Law & Policy": "Politics & Government",
    "Gun Policy & Second Amendment": "Politics & Government",
    "Reproductive Rights": "Politics & Government",
    "Privacy & Surveillance Law": "Politics & Government",
    "Civil Rights & Discrimination": "Politics & Government",

    # Economy & Business
    "Antitrust & Corporate Law": "Economy & Business",
    "Labor Market & Employment": "Economy & Business",
    "Big Tech & Antitrust": "Economy & Business",
    "Consumer Finance & Banking": "Economy & Business",
    "Housing & Real Estate": "Economy & Business",
    "Corporate Governance & Leadership": "Economy & Business",
    "Small Business & Entrepreneurship": "Economy & Business",
    "Trade": "Economy & Business",
    "Tariffs & Globalization": "Economy & Business",
    "Inflation": "Economy & Business",
    "Fed & Monetary Policy": "Economy & Business",

    # Foreign Policy & Security
    "China & U.S.-China Relations": "Foreign Policy & Security",
    "Russia & Ukraine": "Foreign Policy & Security",
    "Middle East": "Foreign Policy & Security",
    "Terrorism & Counterterrorism": "Foreign Policy & Security",
    "Cybersecurity & National Security": "Foreign Policy & Security",
    "Defense & Military Policy": "Foreign Policy & Security",
    "Global Conflict Resolution & Peacebuilding": "Foreign Policy & Security",
    "NATO": "Foreign Policy & Security",
    "Alliances & Multilateralism": "Foreign Policy & Security",

    # Technology
    "Artificial Intelligence Policy & Ethics": "Technology",
    "Cybersecurity (Consumer & Corporate)": "Technology",
    "Social Media & Disinformation": "Technology",
    "Data Privacy": "Technology",
    "Autonomous Systems & Robotics": "Technology",
    "Space Policy & Exploration": "Technology",
    "Misinformation & Media Literacy": "Technology",

    # Health & Science
    "Public Health & Pandemic Preparedness": "Health & Science",
    "Mental Health": "Health & Science",
    "Healthcare Policy & Insurance": "Health & Science",
    "Nutrition & Obesity": "Health & Science",
    "Infectious Disease & Vaccines": "Health & Science",
    "Aging & Long-Term Care": "Health & Science",
    "Reproductive Health": "Health & Science",

    # Climate & Environment
    "Climate Change & Policy": "Climate & Environment",
    "Extreme Weather & Natural Disasters": "Climate & Environment",
    "Energy Policy": "Climate & Environment",
    "Conservation & Biodiversity": "Climate & Environment",

    # Education
    "Higher Education Policy & Affordability": "Education",
    "K-12 Education & School Choice": "Education",
    "Student Debt": "Education",
    "Campus Free Speech & Higher Ed Culture Wars": "Education",

    # Social Issues & Culture
    "Immigration & Demographics": "Social Issues & Culture",
    "Race & Racial Justice": "Social Issues & Culture",
    "Gender & Women's Issues": "Social Issues & Culture",
    "LGBTQ+ Issues": "Social Issues & Culture",
    "Religion & Society": "Social Issues & Culture",

    # Arts, Media & Culture
    "Video Games & Esports": "Arts, Media & Culture",
    "Performing Arts": "Arts, Media & Culture",
    "Film": "Arts, Media & Culture",
    "TV & Media Studies": "Arts, Media & Culture",
}

# Fixed display order for the category groups (anything unmapped falls into "Other")
CATEGORY_ORDER = [
    "Politics & Government", "Economy & Business", "Foreign Policy & Security",
    "Technology", "Health & Science", "Climate & Environment", "Education",
    "Social Issues & Culture", "Arts, Media & Culture", "Other",
]


def category_for_item(item):
    """An item can match multiple tags in different categories; use the
    top-scoring (first) matched tag's category as the item's primary bucket."""
    for tag in item["tags"]:
        if tag in TAG_CATEGORY:
            return TAG_CATEGORY[tag]
    return "Other"

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENTS = [e.strip() for e in os.environ.get("DIGEST_RECIPIENTS", "").split(",") if e.strip()]

UA = "Mozilla/5.0 (compatible; GMUExpertsDigest/1.0)"


# ---------------------------------------------------------------------------
# RSS fetching (stdlib only, no feedparser dependency)
# ---------------------------------------------------------------------------

def parse_pubdate(raw_date):
    """Parse an RSS <pubDate> (RFC 822) or Atom <published>/<updated> (ISO 8601)
    timestamp into an aware UTC datetime. Returns None if it can't be parsed —
    callers treat "can't parse" the same as "too old": drop it."""
    if not raw_date:
        return None
    raw_date = raw_date.strip()
    try:
        dt = emailutils.parsedate_to_datetime(raw_date)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        iso = raw_date.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_feed(url, cutoff):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        items = []
        dropped_no_date = 0
        dropped_stale = 0
        # RSS 2.0 <item>, Atom <entry>
        for item in root.iter():
            tag = item.tag.split("}")[-1]
            if tag == "item":
                title_el = item.find("title")
                link_el = item.find("link")
                date_el = item.find("pubDate")
                title = (title_el.text or "").strip() if title_el is not None else ""
                link = (link_el.text or "").strip() if link_el is not None else ""
                pub_dt = parse_pubdate(date_el.text if date_el is not None else None)
            elif tag == "entry":
                title_el = item.find("{http://www.w3.org/2005/Atom}title")
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                date_el = (item.find("{http://www.w3.org/2005/Atom}published")
                           or item.find("{http://www.w3.org/2005/Atom}updated"))
                title = (title_el.text or "").strip() if title_el is not None else ""
                link = link_el.get("href", "") if link_el is not None else ""
                pub_dt = parse_pubdate(date_el.text if date_el is not None else None)
            else:
                continue

            if not title:
                continue
            # GUARDRAIL enforcement: no date = can't confirm recency = drop.
            if pub_dt is None:
                dropped_no_date += 1
                continue
            if pub_dt < cutoff:
                dropped_stale += 1
                continue
            items.append({"title": title, "link": link, "published": pub_dt.isoformat()})

        if dropped_no_date or dropped_stale:
            print(f"  [{url}] kept {len(items)}, dropped {dropped_no_date} (no date), {dropped_stale} (older than {RECENCY_HOURS}h)")
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

    cutoff = datetime.now(timezone.utc) - timedelta(hours=RECENCY_HOURS)
    print(f"Recency cutoff: only keeping headlines published after {cutoff.isoformat()} ({RECENCY_HOURS}h window)")

    raw_headlines = []
    for feed_url in RSS_FEEDS:
        raw_headlines.extend(fetch_feed(feed_url, cutoff))
    raw_headlines = dedupe_headlines(raw_headlines)
    print(f"Fetched {len(raw_headlines)} unique headlines within the {RECENCY_HOURS}h window, from {len(RSS_FEEDS)} feeds")

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

    items_out = []
    for item in final:
        out = {
            "title": item["title"],
            "link": item["link"],
            "tags": item["matched_tags"],
            "experts": [
                {
                    "name": e["name"],
                    "title": e.get("title"),
                    "college": e.get("college"),
                    "credentials": e.get("credentials"),
                    "tier": e.get("tier"),
                    "email": e.get("email"),
                    "profile_url": e.get("profile_url"),
                }
                for e in item["experts"]
            ],
        }
        out["category"] = category_for_item(out)
        items_out.append(out)

    digest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "headline_count": len(items_out),
        "summary": build_summary_line(items_out),
        "items": items_out,
    }
    return digest


def build_summary_line(items):
    """Rule-based (no external AI call) one-line summary: counts + which
    categories dominated today, so the email has something to skim before
    diving into the full list."""
    if not items:
        return "No headlines matched a GMU expert tag in the last 24 hours."
    from collections import Counter
    cat_counts = Counter(item["category"] for item in items)
    top_cats = [c for c, _ in cat_counts.most_common(3)]
    n = len(items)
    plural = "story" if n == 1 else "stories"
    if len(top_cats) == 1:
        lead = top_cats[0]
    elif len(top_cats) == 2:
        lead = f"{top_cats[0]} and {top_cats[1]}"
    else:
        lead = f"{top_cats[0]}, {top_cats[1]}, and {top_cats[2]}"
    return f"{n} {plural} today, led by coverage of {lead}."


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_item(item):
    experts_html = ""
    for e in item["experts"]:
        name_html = (
            f'<a href="{e["profile_url"]}" style="color:#006633;text-decoration:none;">{e["name"]}</a>'
            if e.get("profile_url") else e["name"]
        )
        cred_html = f', {e["credentials"]}' if e.get("credentials") else ""
        email_html = f' &middot; <a href="mailto:{e["email"]}" style="color:#6b6b6b;">{e["email"]}</a>' if e.get("email") else ""
        tier_color = "#006633" if e.get("tier") == "Primary" else "#7a7a7a"
        experts_html += (
            f'<div style="padding:4px 0;font-size:13px;">'
            f'<span style="background:{tier_color};color:white;font-size:10px;font-weight:700;'
            f'padding:2px 7px;border-radius:999px;text-transform:uppercase;margin-right:6px;">{e.get("tier","")}</span>'
            f'<strong>{name_html}</strong>{cred_html} &mdash; {e.get("title") or ""} ({e.get("college") or ""}){email_html}'
            f'</div>'
        )
    tags_html = " &middot; ".join(item["tags"])
    link_html = f'<a href="{item["link"]}" style="color:#1a1a1a;text-decoration:none;">{item["title"]}</a>' if item["link"] else item["title"]
    return (
        f'<div style="padding:16px 0;border-bottom:1px solid #e2e2e2;">'
        f'<div style="font-size:16px;font-weight:700;margin-bottom:4px;">{link_html}</div>'
        f'<div style="font-size:12px;color:#6b6b6b;margin-bottom:8px;">{tags_html}</div>'
        f'{experts_html}'
        f'</div>'
    )


def render_email_html(digest):
    date_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

    # Group items by category, preserving CATEGORY_ORDER, dropping empty groups.
    by_cat = {}
    for item in digest["items"]:
        by_cat.setdefault(item.get("category", "Other"), []).append(item)

    sections = []
    for cat in CATEGORY_ORDER:
        cat_items = by_cat.get(cat)
        if not cat_items:
            continue
        rows = "".join(_render_item(item) for item in cat_items)
        sections.append(
            f'<h2 style="font-size:14px;text-transform:uppercase;letter-spacing:0.5px;'
            f'color:#006633;border-bottom:2px solid #FFCC33;padding-bottom:6px;margin:24px 0 4px;">'
            f'{cat}</h2>{rows}'
        )

    body = "".join(sections) if sections else '<p style="color:#6b6b6b;">No headlines matched a GMU expert tag today.</p>'
    summary = digest.get("summary", "")

    return f"""\
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f7f7f5;margin:0;padding:0;">
<div style="max-width:680px;margin:0 auto;padding:24px 20px;">
  <div style="background:#006633;color:white;padding:20px 24px;border-radius:10px 10px 0 0;">
    <h1 style="margin:0;font-size:20px;">GMU Experts Daily Digest</h1>
    <p style="margin:4px 0 0;color:#d9f2e4;font-size:13px;">{date_str}</p>
  </div>
  <div style="background:#f0f7f2;padding:14px 24px;border-left:1px solid #e2e2e2;border-right:1px solid #e2e2e2;">
    <p style="margin:0;font-size:14px;color:#1a1a1a;font-style:italic;">{summary}</p>
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


def save_archive_copy(digest):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_path = os.path.join(ARCHIVE_DIR, f"{date_str}.json")
    json.dump(digest, open(archive_path, "w"), indent=1)
    print(f"Archived to {archive_path}")

    # Maintain an index file listing all archived dates, newest first, so the
    # site's Past Digests view doesn't need to guess filenames or hit GitHub's API.
    index_path = os.path.join(ARCHIVE_DIR, "index.json")
    try:
        dates = json.load(open(index_path))
    except (FileNotFoundError, json.JSONDecodeError):
        dates = []
    if date_str not in dates:
        dates.insert(0, date_str)
    json.dump(dates, open(index_path, "w"), indent=1)


def main():
    digest = build_digest()
    json.dump(digest, open(OUT_JSON_PATH, "w"), indent=1)
    print(f"Wrote {OUT_JSON_PATH} with {digest['headline_count']} headlines")
    save_archive_copy(digest)
    send_email(digest)


if __name__ == "__main__":
    main()
