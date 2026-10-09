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
    "https://rss.nytimes.com/services/xml/rss/nyt/US.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Education.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Climate.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "https://feeds.npr.org/1001/rss.xml",
    "https://www.theguardian.com/us-news/rss",

    # --- Center / international wire-style ---
    "https://feeds.bbci.co.uk/news/topics/cx1m7zg01xyt/rss.xml",
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",       # WSJ World News (news desk, not opinion)
    "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",   # WSJ Business

    # --- Right-leaning / right-of-center ---
    "https://moxie.foxnews.com/google-publisher/latest.xml",
    "https://www.washingtonexaminer.com/rss?section=/news/",
]

# GUARDRAIL 3 — cross-outlet corroboration. A headline only makes the digest
# if it's been picked up by at least this many DISTINCT outlets — raised from
# 2 to 3 so only stories with real critical-mass coverage (where a GMU expert
# could easily plug in as a source) make the cut, not just any twice-covered item.
MIN_SOURCES = 3

# Friendly outlet names for grouping/corroboration counting.
SOURCE_LABELS = {
    "https://rss.nytimes.com/services/xml/rss/nyt/US.xml": "The New York Times",
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml": "The New York Times",
    "https://rss.nytimes.com/services/xml/rss/nyt/Education.xml": "The New York Times",
    "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml": "The New York Times",
    "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml": "The New York Times",
    "https://rss.nytimes.com/services/xml/rss/nyt/Climate.xml": "The New York Times",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml": "The New York Times",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml": "The New York Times",
    "https://feeds.npr.org/1001/rss.xml": "NPR",
    "https://www.theguardian.com/us-news/rss": "The Guardian",
    "https://feeds.bbci.co.uk/news/topics/cx1m7zg01xyt/rss.xml": "BBC",
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml": "The Wall Street Journal",
    "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml": "The Wall Street Journal",
    "https://moxie.foxnews.com/google-publisher/latest.xml": "Fox News",
    "https://www.washingtonexaminer.com/rss?section=/news/": "Washington Examiner",
}

MAX_HEADLINES = 18
MIN_HEADLINES_WARN = 6
EXPERTS_PER_HEADLINE = 5
EXPERTS_PER_HEADLINE_MIN = 2
STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "be", "been", "with", "at", "by", "from", "as", "it", "its",
    "that", "this", "after", "over", "amid", "new", "says", "say", "said",
    "will", "has", "have", "had", "his", "her", "their", "up", "out", "into",
    "than", "but", "not", "how", "why", "what", "who", "when", "where",
}

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

    # New tags added via the full research pass (Oct 2026)
    "Marketing & Consumer Behavior": "Economy & Business",
    "Supply Chain & Operations Management": "Economy & Business",
    "Workplace Psychology & Organizational Behavior": "Economy & Business",
    "Corporate Social Responsibility & Sustainability": "Economy & Business",
    "Cryptocurrency & Blockchain": "Economy & Business",
    "Corporate Taxation & Tax Policy": "Economy & Business",
    "Nonprofit Sector & Philanthropy": "Economy & Business",
    "Tourism & Hospitality Management": "Economy & Business",
    "Semiconductors & Computing Hardware": "Technology",
    "Materials Science & Mechanical Engineering": "Technology",
    "Computing Systems, Networks & Infrastructure": "Technology",
    "Computer Graphics, Vision & Human-Computer Interaction": "Technology",
    "Critical Infrastructure & Systems Resilience": "Technology",
    "Aviation Safety & Air Transportation Systems": "Technology",
    "Statistics, Data Science & Experimental Design": "Technology",
    "Transportation Engineering & Urban Mobility": "Technology",
    "Environmental & Occupational Health": "Health & Science",
    "Animal Behavior & Sensory Ecology": "Health & Science",
    "Neuroscience & Brain Research": "Health & Science",
    "Biomedical Engineering & Medical Devices": "Health & Science",
    "Archaeology & Paleoanthropology": "Health & Science",
    "Sports Medicine & Youth Athletics": "Health & Science",
    "Science Communication & Literacy": "Health & Science",
    "Creative Writing & Literature": "Arts, Media & Culture",
    "Visual Arts & Art History": "Arts, Media & Culture",
    "History & Historical Scholarship": "Social Issues & Culture",
    "Linguistics & Language Documentation": "Social Issues & Culture",
    "Social Movements & Political Sociology": "Politics & Government",
    "Asia-Pacific & Korea Affairs": "Foreign Policy & Security",
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
    default_source = SOURCE_LABELS.get(url, url)
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
                desc_el = item.find("description")
                source_el = item.find("source")  # Google News aggregator includes the real outlet here
                title = (title_el.text or "").strip() if title_el is not None else ""
                link = (link_el.text or "").strip() if link_el is not None else ""
                pub_dt = parse_pubdate(date_el.text if date_el is not None else None)
                description = (desc_el.text or "").strip() if desc_el is not None and desc_el.text else ""
                source = (source_el.text or "").strip() if source_el is not None and source_el.text else default_source
            elif tag == "entry":
                title_el = item.find("{http://www.w3.org/2005/Atom}title")
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                date_el = (item.find("{http://www.w3.org/2005/Atom}published")
                           or item.find("{http://www.w3.org/2005/Atom}updated"))
                summary_el = item.find("{http://www.w3.org/2005/Atom}summary")
                title = (title_el.text or "").strip() if title_el is not None else ""
                link = link_el.get("href", "") if link_el is not None else ""
                pub_dt = parse_pubdate(date_el.text if date_el is not None else None)
                description = (summary_el.text or "").strip() if summary_el is not None and summary_el.text else ""
                source = default_source
            else:
                continue

            if not title:
                continue
            # Google News titles are usually "Headline - Outlet Name"; strip that
            # suffix now that we've captured the outlet separately, so matching
            # and display both use the clean headline text.
            if source and title.endswith(f" - {source}"):
                title = title[: -(len(source) + 3)].strip()

            # Descriptions sometimes carry basic HTML (links, <p>, entity-encoded
            # bits) — strip tags and collapse whitespace so it's plain matchable text.
            description = re.sub(r"<[^>]+>", " ", description)
            description = re.sub(r"\s+", " ", description).strip()

            # GUARDRAIL 1 enforcement: no date = can't confirm recency = drop.
            if pub_dt is None:
                dropped_no_date += 1
                continue
            if pub_dt < cutoff:
                dropped_stale += 1
                continue
            items.append({"title": title, "link": link, "published": pub_dt.isoformat(),
                          "source": source, "description": description})

        if dropped_no_date or dropped_stale:
            print(f"  [{url}] kept {len(items)}, dropped {dropped_no_date} (no date), {dropped_stale} (older than {RECENCY_HOURS}h)")
        return items
    except Exception as e:
        print(f"  [warn] failed to fetch {url}: {e}", file=sys.stderr)
        return []


def significant_tokens(title):
    words = re.findall(r"[a-z0-9']+", title.lower())
    return {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def cluster_by_story(headlines):
    """Group headlines that are almost certainly describing the same real-world
    story (even though different outlets phrase the headline differently), then
    return one cluster per distinct story with every outlet that covered it.

    This is a simple greedy token-overlap clusterer, not real NLP — it compares
    each headline's significant (non-stopword) words against existing clusters'
    representative headline and joins the first one that clears the similarity
    bar. Good enough at the scale of ~100-300 headlines/day; not meant to be
    bulletproof against every possible phrasing difference.

    Uses overlap COEFFICIENT (shared / smaller-headline's word count), not
    Jaccard (shared / all-words-combined) — different outlets rarely phrase a
    shared story identically, so requiring the shorter headline's words to be
    mostly-covered works much better in practice than requiring a high fraction
    of the COMBINED vocabulary to match, which Jaccard effectively demands and
    which real headlines about the same story routinely fail."""
    OVERLAP_THRESHOLD = 0.22
    MIN_ABSOLUTE_OVERLAP = 2  # guards against two short headlines matching on one generic word

    clusters = []  # each: {"rep": headline_dict, "tokens": set, "sources": {source: headline}}
    for h in headlines:
        toks = significant_tokens(h["title"])
        if not toks:
            continue
        placed = False
        for c in clusters:
            overlap = len(toks & c["tokens"])
            coef = overlap / min(len(toks), len(c["tokens"])) if toks and c["tokens"] else 0
            if overlap >= MIN_ABSOLUTE_OVERLAP and coef >= OVERLAP_THRESHOLD:
                c["sources"].setdefault(h["source"], h)
                c["tokens"] |= toks  # accumulate vocabulary so later same-story headlines match more easily
                # keep the longest/most-detailed title as the representative
                if len(h["title"]) > len(c["rep"]["title"]):
                    c["rep"] = h
                placed = True
                break
        if not placed:
            clusters.append({"rep": h, "tokens": toks, "sources": {h["source"]: h}})
    return clusters


# ---------------------------------------------------------------------------
# Matching (same word-boundary approach as the web tool, for consistency)
# ---------------------------------------------------------------------------

# A handful of taxonomy keywords are DELIBERATE truncated stems, meant to catch
# multiple word forms at once (e.g. "polic" -> police/policy/policing,
# "immigrat" -> immigrant/immigration/immigrate). These need a word boundary
# only on the LEFT so the suffix can vary. Every other keyword gets a full
# word boundary on both sides, which is what prevents bugs like "nato" lighting
# up on the word "senator", or "dance" lighting up inside "Skydance" — both
# real false matches found in production before this fix.
STEM_KEYWORDS = {"polic", "immigrat", "terroris", "radicaliz", "globaliz", "incarcerat", "extremis"}


def load_taxonomy():
    tax = json.load(open(TAXONOMY_PATH))
    return tax["tags"], tax["kw"]


def _keyword_pattern(kw_lower):
    escaped = re.escape(kw_lower)
    if kw_lower == "polic":
        # "polic" is meant to catch police/policing/policeman, but a bare
        # left-boundary match also catches "policy"/"policies" — one of the
        # most common words in news coverage (healthcare policy, foreign
        # policy, tax policy...). Exclude those suffixes explicitly so this
        # stem doesn't drag Criminal Justice & Policing into unrelated
        # policy stories.
        return re.compile(r"\bpolic(?!y|ies)")
    if kw_lower in STEM_KEYWORDS:
        return re.compile(r"\b" + escaped)
    if " " in kw_lower:
        # multi-word phrases already have natural boundaries; \b still helps
        # at the very start/end of the phrase without over-constraining.
        return re.compile(r"\b" + escaped + r"\b")
    return re.compile(r"\b" + escaped + r"\b")


_PATTERN_CACHE = {}


def _compiled(kw_lower):
    if kw_lower not in _PATTERN_CACHE:
        _PATTERN_CACHE[kw_lower] = _keyword_pattern(kw_lower)
    return _PATTERN_CACHE[kw_lower]


def match_tags(text, all_tags, keywords):
    lower = text.lower()
    matched = []
    for tag in all_tags:
        hits = [kw for kw in keywords.get(tag, []) if _compiled(kw.lower()).search(lower)]
        if hits:
            matched.append((tag, len(hits)))
    matched.sort(key=lambda x: -x[1])
    return [t for t, _ in matched]


def tier_rank(tier):
    return {"Primary": 0, "Secondary": 1}.get(tier, 2)


# GUARDRAIL 4 — match score. Tag overlap alone can't tell two experts with the
# same tag apart: both "Workplace Psychology" and "AI Policy & Ethics" might
# carry the same tag, but only one of their actual bios might mention the
# story's specific subject. Where a real research-backed bio exists (the full
# research pass, see GMU_Research_Pass_Tracker.xlsx), we also score how many of
# the headline's significant words appear in that bio text, so a narrow story
# can surface the person whose specific work actually matches — not just
# whoever happens to carry the broader tag. People without a bio yet (most of
# the roster, until the research pass reaches them) just get a bio score of 0
# and fall back to pure tag-match ranking, same as before this guardrail existed.
BIO_SCORE_WEIGHT = 1      # one point per significant headline word also found in the bio
TAG_MATCH_WEIGHT = 5      # one exact tag match outweighs multiple loose bio-word overlaps


def pick_experts(matched_tags, people, headline_text=""):
    """Returns people sorted by (tier, match_score). Each returned person is a
    shallow copy carrying a "_match_score" and "_bio_hits" field — a copy,
    not the original dict, since the same person can be picked for several
    headlines in one run with a different score each time, and mutating the
    shared record from experts_data.json in place would let one headline's
    score leak into another's."""
    tagset = set(matched_tags)
    sig_words = significant_tokens(headline_text) if headline_text else set()
    scored = []
    for p in people:
        hit = [t for t in p.get("tags", []) if t in tagset]
        if not hit:
            continue
        bio = (p.get("bio") or "").lower()
        bio_hits = sum(1 for w in sig_words if w in bio) if (bio and sig_words) else 0
        match_score = len(hit) * TAG_MATCH_WEIGHT + bio_hits * BIO_SCORE_WEIGHT
        p_copy = dict(p)
        p_copy["_match_score"] = match_score
        p_copy["_bio_hits"] = bio_hits
        p_copy["_tag_hits"] = len(hit)
        scored.append(p_copy)
    scored.sort(key=lambda p: (tier_rank(p.get("tier")), -p["_match_score"]))
    return scored


# ---------------------------------------------------------------------------
# Tier 2 — full article text (only fetched for stories that already passed
# the recency + corroboration guardrails, so this stays bounded to ~a few
# dozen fetches/day, not hundreds). Uses trafilatura, a library built
# specifically for pulling clean article text out of arbitrary news-site
# HTML — hand-rolling that extraction reliably across 16 different site
# layouts isn't a fight worth having when a maintained library already does it.
# ---------------------------------------------------------------------------

FULL_TEXT_MAX_CHARS = 3000       # cap so one long-form piece can't dominate matching
FULL_TEXT_TIMEOUT = 15           # seconds per article; a slow/dead page shouldn't stall the run
# Operational kill switch — set DISABLE_FULL_TEXT=1 as a repo/workflow env var
# to fall back to Tier 1 only (headline + RSS summary) without touching code,
# e.g. if full-text fetching starts timing out a lot or a dependency breaks.
FULL_TEXT_ENABLED = os.environ.get("DISABLE_FULL_TEXT", "").lower() not in ("1", "true", "yes")

try:
    import trafilatura
except ImportError:
    trafilatura = None


def fetch_article_text(url):
    """Best-effort full-article fetch. Returns '' (not None) on any failure —
    paywalled sources (NYT, WSJ in particular), dead links, and parsing
    failures are all expected and handled the same way: fall back silently
    to whatever headline/description text is already available."""
    if not FULL_TEXT_ENABLED or not trafilatura or not url:
        return ""
    try:
        downloaded = trafilatura.fetch_url(url, no_ssl=True)
        if not downloaded:
            return ""
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False) or ""
        return text[:FULL_TEXT_MAX_CHARS]
    except Exception as e:
        print(f"  [info] full-text fetch failed for {url}: {e}", file=sys.stderr)
        return ""


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
    print(f"Fetched {len(raw_headlines)} raw headlines within the {RECENCY_HOURS}h window, from {len(RSS_FEEDS)} feeds")

    clusters = cluster_by_story(raw_headlines)
    # GUARDRAIL 3 enforcement: drop any story not corroborated by enough distinct outlets.
    corroborated = [c for c in clusters if len(c["sources"]) >= MIN_SOURCES]
    dropped_single_source = len(clusters) - len(corroborated)
    print(f"Clustered into {len(clusters)} distinct stories; {dropped_single_source} dropped for appearing on fewer than {MIN_SOURCES} outlets")

    scored_headlines = []
    full_text_fetched = 0
    for c in corroborated:
        h = c["rep"]
        # Tier 1: headline + RSS description/summary (free, already downloaded).
        match_text = h["title"] + " " + h.get("description", "")

        # Tier 2: full article text, fetched only now that this story has
        # already cleared recency + corroboration — i.e. only for stories
        # that were going to be considered anyway, not the full raw firehose.
        article_text = fetch_article_text(h["link"])
        if article_text:
            full_text_fetched += 1
            match_text += " " + article_text

        matched = match_tags(match_text, all_tags, keywords)
        if not matched:
            continue
        experts = pick_experts(matched, people, headline_text=match_text)
        if not experts:
            continue
        scored_headlines.append({
            "title": h["title"],
            "link": h["link"],
            "matched_tags": matched[:4],
            "experts": experts[:EXPERTS_PER_HEADLINE],
            "score": len(matched),
            "sources": sorted(c["sources"].keys()),
            "full_text_used": bool(article_text),
        })
    print(f"Full article text successfully fetched for {full_text_fetched}/{len(corroborated)} corroborated stories")

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
            "sources": item["sources"],
            "source_count": len(item["sources"]),
            "experts": [
                {
                    "name": e["name"],
                    "title": e.get("title"),
                    "college": e.get("college"),
                    "credentials": e.get("credentials"),
                    "tier": e.get("tier"),
                    "email": e.get("email"),
                    "profile_url": e.get("profile_url"),
                    "match_score": e.get("_match_score", e.get("_tag_hits", 0) * TAG_MATCH_WEIGHT),
                    "bio_match": e.get("_bio_hits", 0) > 0,
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
        # A small gold badge marks when a match is backed by actual research-pass
        # bio text (not just a tag) — a sign this isn't just a broad-category
        # guess, their specific documented work lines up with this story.
        bio_badge = (
            '<span style="background:#FFCC33;color:#1a1a1a;font-size:9px;font-weight:700;'
            'padding:1px 6px;border-radius:999px;margin-right:5px;" title="Matched on researched bio text, not just a tag">RESEARCH MATCH</span>'
        ) if e.get("bio_match") else ""
        experts_html += (
            f'<div style="padding:4px 0;font-size:13px;">'
            f'<span style="background:{tier_color};color:white;font-size:10px;font-weight:700;'
            f'padding:2px 7px;border-radius:999px;text-transform:uppercase;margin-right:6px;">{e.get("tier","")}</span>'
            f'{bio_badge}'
            f'<strong>{name_html}</strong>{cred_html} &mdash; {e.get("title") or ""} ({e.get("college") or ""}){email_html}'
            f'</div>'
        )
    tags_html = " &middot; ".join(item["tags"])
    sources = item.get("sources") or []
    sources_html = f'<div style="font-size:11px;color:#9a9a9a;margin-bottom:4px;">Reported by {len(sources)} outlets: {", ".join(sources)}</div>' if sources else ""
    link_html = f'<a href="{item["link"]}" style="color:#1a1a1a;text-decoration:none;">{item["title"]}</a>' if item["link"] else item["title"]
    return (
        f'<div style="padding:16px 0;border-bottom:1px solid #e2e2e2;">'
        f'<div style="font-size:16px;font-weight:700;margin-bottom:4px;">{link_html}</div>'
        f'{sources_html}'
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
