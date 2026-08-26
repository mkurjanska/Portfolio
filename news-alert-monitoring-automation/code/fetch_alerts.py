#!/usr/bin/env python3
"""
fetch_alerts.py
===============
Reads RSS feeds for data center news alerts, fetches full article text for
each entry, and saves them to alerts.csv.

  1. Full-content scraping — fetches the article URL and stores cleaned body text.
  2. Link-dump detection — if a page contains many outbound links whose anchor
     text or surrounding context mentions "data center", each qualifying link is
     expanded into its own row in alerts.csv (the container page itself is
     NOT added as a row).
  3. Duplicate prevention — dedup is checked before fetching so link-dump
     child URLs are never re-added on a later run.

Env vars required:
  DATA_CENTER_ALERT_RSS_URL   — first RSS feed URL
  TALKWALKER_RSS_URL          — second RSS feed URL (optional)

Dependencies: pip install feedparser requests beautifulsoup4 lxml
"""

import csv
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR    = Path(__file__).parent
OUTPUT_FILE = BASE_DIR / "alerts.csv"

FIELDNAMES  = ["date", "title", "link", "summary", "source", "full_text"]

# How many "data center" outbound links must a page have to be treated as
# a link-dump digest rather than a standalone article?
LINK_DUMP_THRESHOLD = 5

# Max chars of article body to store (keeps CSV manageable)
MAX_ARTICLE_CHARS = 20_000

# Phrases that confirm a link / its nearby text is about data centers
DC_TERMS = [
    # Direct DC terms
    "data center", "data centre", "datacenter",
    "data centers", "data centres", "datacenters",
    "server farm", "hyperscale", "colocation", "colo facility",
    "ai campus", "cloud campus",
    # Strong indirect signals - in local/energy news context these almost always mean DCs
    "ai data center", "a.i. data center", "a.i./data center",
    "moratorium on data",
    "pause on data",
    "ban on data",
    # "moratorium" alone is worth keeping - most local news moratorium stories
    # in DC-relevant feeds are about data centers, and the ones that aren't
    # will get filtered by process_alerts.py's relevance check anyway
    "moratorium",
]

# Request headers — polite bot identification
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; DCAlertBot/3.0; "
        "+https://github.com/mkurjanska/Data-Centers-Community-Opposition)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Domains / patterns whose pages are almost never full articles
SKIP_URL_PATTERNS = [
    r"reddit\.com",
    r"linkedin\.com",
    r"indeed\.com",
    r"glassdoor\.com",
    r"ieeexplore\.ieee\.org",
    r"jobs?\.",
    r"/careers?/",
    r"/jobs?/",
    r"twitter\.com",
    r"x\.com/",
    r"facebook\.com",
    r"wired\.com/tag/",         # tag index pages
    r"datacentermap\.com",      # map/directory pages
    r"fortune\.com/company/",   # company profile pages
    r"/tag/",                    # generic tag pages
    r"cdn-cgi/l/email-protection", # cloudflare email obfuscation
    r"pic\.twitter\.com",
    r"louisvilleky\.gov/government/metro-council-district",  # council member pages
]

# Titles that are clearly junk — navigation, social, author names, bare words
SKIP_TITLE_PATTERNS_FETCH = [
    r"^(login|log in|sign in|sign up|donate|subscribe|newsletter|instagram|"
    r"twitter|facebook|youtube|linkedin|bluesky|threads|author|about us|"
    r"contact us|privacy policy|terms of service|related articles|more stories|"
    r"read more|click here|home|news|sports|weather|politics|health|economy|"
    r"environment|government|education|culture|opinion|podcast|video|photos|"
    r"back to top|skip to|menu|search|rss|feed|sitemap|energy)$",
    r"^https?://",                   # URL as title
    r"^@",                           # @handle
    r"^\[email",                     # email protection
    r"^pic\.twitter",                # Twitter pic
    r"^dist\.",                      # "Dist. 15,"
    r"^\d+\s*(MW|GW|kW|acres?)$",   # bare numbers+units
    r"^related articles?$",           # Mirror Indy navigation widget
    r"^more stories?$",
    r"^continue reading$",
]

# Minimum words for a title to be treated as a real article
_MIN_ARTICLE_TITLE_WORDS = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _should_skip_url(url: str) -> bool:
    for pat in SKIP_URL_PATTERNS:
        if re.search(pat, url, re.I):
            return True
    return False


def _dc_mentioned(text: str) -> bool:
    """Return True if any data-center keyword appears in text (case-insensitive)."""
    t = text.lower()
    return any(kw in t for kw in DC_TERMS)


def extract_real_url(url: str) -> str:
    """Unwrap Google Alert redirect URLs."""
    if "google.com/url" in url:
        try:
            params = parse_qs(urlparse(url).query)
            real = params.get("url", [None])[0]
            if real:
                return unquote(real)
        except Exception:
            pass
    return url


def _clean_text(soup: BeautifulSoup) -> str:
    """Strip boilerplate tags and return plain text."""
    for tag in soup(["script", "style", "noscript", "header", "footer",
                     "nav", "aside", "form", "iframe"]):
        tag.decompose()
    text = re.sub(r"\s{2,}", " ", soup.get_text(separator=" ", strip=True))
    return text[:MAX_ARTICLE_CHARS]


def fetch_page(url: str, timeout: int = 20) -> tuple[BeautifulSoup | None, str]:
    """
    Fetch URL and return (BeautifulSoup, final_url).
    Returns (None, url) on failure.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout,
                            allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        return soup, resp.url
    except Exception as exc:
        print(f"    [WARN] Could not fetch {url[:80]}: {exc}")
        return None, url


def _nearest_header_text(tag) -> str:
    """
    Walk backwards through siblings and up through parents to find the
    nearest preceding h1-h4 header. Returns its text (lowercased) or "".
    This lets us detect links that are under a DC-themed section header
    even if the link text itself doesn't mention data centers.
    """
    HEADER_TAGS = {"h1", "h2", "h3", "h4"}
    # Walk previous siblings first
    for sibling in tag.find_previous_siblings():
        if sibling.name in HEADER_TAGS:
            return sibling.get_text(" ", strip=True).lower()
    # Then walk up through parents and check their previous siblings
    for parent in tag.parents:
        for sibling in parent.find_previous_siblings():
            if sibling.name in HEADER_TAGS:
                return sibling.get_text(" ", strip=True).lower()
            # Also check headers inside the sibling
            h = sibling.find(HEADER_TAGS)
            if h:
                return h.get_text(" ", strip=True).lower()
    return ""


def _link_is_dc_relevant(a_tag) -> bool:
    """
    Return True if this link is relevant to data centers, checking:
    1. The anchor text itself
    2. The immediate parent element text
    3. The nearest preceding section header
    """
    anchor_text = a_tag.get_text(" ", strip=True)
    parent_text = a_tag.parent.get_text(" ", strip=True)[:400] if a_tag.parent else ""
    header_text = _nearest_header_text(a_tag)
    combined    = (anchor_text + " " + parent_text + " " + header_text).lower()
    return _dc_mentioned(combined)


def _page_is_dc_digest(soup: BeautifulSoup) -> bool:
    """
    Return True if the overall page is clearly a DC-focused digest —
    i.e. the page title or main heading contains DC terms.
    On such pages we treat ALL outbound article links as potentially
    relevant rather than requiring each link to individually mention DCs.
    """
    # Check page <title>
    title_tag = soup.find("title")
    if title_tag and _dc_mentioned(title_tag.get_text().lower()):
        return True
    # Check h1/h2
    for tag in soup.find_all(["h1", "h2"]):
        if _dc_mentioned(tag.get_text().lower()):
            return True
    return False


def _is_link_dump(soup: BeautifulSoup, base_url: str) -> bool:
    """
    Return True if this page looks like a digest / round-up of many links.
    Checks anchor text, parent text, AND nearest section header for DC terms.
    Also returns True immediately if the page itself is a DC-focused digest.
    """
    # Fast path: page title/H1 is about data centers — definitely a dump
    if _page_is_dc_digest(soup):
        # Still need enough outbound links to be a real digest (not a single article)
        outbound = sum(
            1 for a in soup.find_all("a", href=True)
            if a.get("href", "").startswith("http")
            and urlparse(a["href"]).netloc != urlparse(base_url).netloc
        )
        if outbound >= LINK_DUMP_THRESHOLD:
            return True

    dc_links = 0
    for a in soup.find_all("a", href=True):
        if _link_is_dc_relevant(a):
            dc_links += 1
        if dc_links >= LINK_DUMP_THRESHOLD:
            return True
    return False


# URL path patterns that are never articles
_NAV_URL_PATTERNS = re.compile(
    r"/(about|contact|login|signin|sign-in|signup|sign-up|register|subscribe|"
    r"donate|membership|newsletter|privacy|terms|advertise|careers|jobs|"
    r"author|authors|staff|team|masthead|rss|feed|search|tag|tags|"
    r"category|categories|topics|topic|section|sections|archive|archives|"
    r"page/\d+|instagram|twitter|facebook|youtube|linkedin|bluesky|threads|"
    r"substack\.com/@|profile/|user/)/?$",
    re.I
)

# Anchor text that is clearly navigation, not an article title
_NAV_ANCHOR_PATTERNS = re.compile(
    r"^(login|log in|sign in|sign up|donate|subscribe|newsletter|"
    r"instagram|twitter|facebook|youtube|linkedin|bluesky|threads|"
    r"author|about us|contact us|privacy policy|terms of service|"
    r"related articles|more stories|read more|click here|here|"
    r"home|news|sports|weather|politics|health|economy|environment|"
    r"government|education|culture|opinion|podcast|video|photos|"
    r"back to top|skip to|jump to|menu|search|rss|feed|sitemap)$",
    re.I
)

# Minimum word count for a title to be considered an article headline
_MIN_TITLE_WORDS = 4


def _looks_like_article_link(abs_url: str, anchor_text: str) -> bool:
    """
    Return True if this link plausibly points to a news article rather than
    a nav element, author page, social media link, or site section.
    """
    # Must have enough words in anchor text to be a headline
    words = anchor_text.split()
    if len(words) < _MIN_TITLE_WORDS:
        return False

    # Skip nav anchor text
    if _NAV_ANCHOR_PATTERNS.match(anchor_text.strip()):
        return False

    # Skip nav URL patterns
    parsed = urlparse(abs_url)
    if _NAV_URL_PATTERNS.search(parsed.path):
        return False

    # Skip social media / author profile domains
    nav_domains = {
        "substack.com", "twitter.com", "x.com", "instagram.com",
        "facebook.com", "youtube.com", "linkedin.com", "threads.net",
        "bluesky.social", "bsky.app",
    }
    netloc = parsed.netloc.lstrip("www.")
    if netloc in nav_domains:
        return False

    # Path should look like an article (has some depth and slug-like segment)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(path_parts) < 1:
        return False

    # Last path segment should look like a slug (not just a number or single word)
    last = path_parts[-1]
    if re.match(r"^\d+$", last):   # bare numeric ID without slug — borderline
        if len(path_parts) < 2:
            return False

    return True


def extract_dump_links(soup: BeautifulSoup, base_url: str,
                       existing_links: set[str]) -> list[dict]:
    """
    From a link-dump page, return a list of new entry dicts (one per
    qualifying outbound link that looks like a real article).

    For DC-focused digest pages (title/H1 mentions data centers), ALL
    outbound links that pass the article-link test are included.

    For general pages, only links with DC context in anchor/parent/header
    text are included.
    """
    seen_hrefs: set[str] = set()
    entries    = []
    is_dc_page = _page_is_dc_digest(soup)
    base_netloc = urlparse(base_url).netloc

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue

        abs_url = urljoin(base_url, href)
        if not abs_url.startswith("http"):
            continue

        # Skip same-domain navigation links (only keep articles with depth)
        parsed = urlparse(abs_url)
        if parsed.netloc == base_netloc:
            path_parts = [p for p in parsed.path.strip("/").split("/") if p]
            if len(path_parts) < 2:
                continue

        if abs_url in seen_hrefs or abs_url in existing_links:
            continue

        if _should_skip_url(abs_url):
            continue

        anchor_text = a.get_text(" ", strip=True)

        # Hard filter: must look like a real article link
        if not _looks_like_article_link(abs_url, anchor_text):
            continue

        # On general (non-DC-digest) pages: also require DC context
        if not is_dc_page and not _link_is_dc_relevant(a):
            continue

        seen_hrefs.add(abs_url)
        entries.append({
            "link":  abs_url,
            "title": anchor_text[:200],
        })

    return entries


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Expanded source list — 110+ local, regional, trade, and advocacy RSS feeds
# Organized by category and geography.
# Your two Google/Talkwalker alert URLs are added dynamically from env vars.
# ---------------------------------------------------------------------------

# Feeds that are dedicated data center / energy / watchdog sources —
# every article they publish is potentially relevant, so we save all of them.
# General local news feeds are NOT in this set; their articles are
# pre-filtered by DC keyword before saving.
# NOTE (portfolio redaction): originally 22 named outlets (plus this
# project's own 2 aggregator feeds) across trade press, watchdog/
# investigative outlets, and independent substacks specifically tracking
# data center opposition. Replaced with fillers; set-membership logic
# below is unchanged.
DC_DEDICATED_FEEDS = {
    "Google Alert (data centers)",
    "Talkwalker Alert",
    # Trade / industry (example)
    "Example Trade Publication",
    "Example Industry Digest",
    # Watchdog / investigative (example)
    "Example Watchdog Outlet",
    "Example Investigative Newsroom",
    # Independent substacks focused on DC opposition (example)
    "Example Opposition Newsletter",
}

LOCAL_RSS_FEEDS = {
    # NOTE (portfolio redaction): this dict originally held 408 curated
    # local-news RSS feeds across 20+ states, grouped by region -- the real
    # source list this pipeline has used to process 20,020+ alerts to date.
    # Replaced with fillers below; iteration logic and dict shape unchanged.

    # ── Example State A ──────────────────────────────────────────────────────
    "Example Regional News":       "https://example-news-1.example/feed/",
    "Example County Gazette":      "https://example-news-2.example/rss.xml",

    # ── Example State B ──────────────────────────────────────────────────────
    "Example City Tribune":        "https://example-news-3.example/feed/",
    "Example Public Radio":        "https://example-news-4.example/rss.xml",

    # ── Trade / industry (example) ───────────────────────────────────────────
    "Example Industry Digest":     "https://example-trade-1.example/feed/",

    # ── Multi-state / national (example) ─────────────────────────────────────
    "Example National Outlet":     "https://example-national-1.example/feed/",
}


def main() -> None:
    # ── 1. Gather RSS URLs ──────────────────────────────────────────────────
    # Start with your Google Alert and Talkwalker feeds from env vars
    env_feeds = {
        "Google Alert (data centers)": os.environ.get("DATA_CENTER_ALERT_RSS_URL"),
        "Talkwalker Alert":            os.environ.get("TALKWALKER_RSS_URL"),
    }
    # Merge with the expanded local source list
    all_feeds = {k: v for k, v in env_feeds.items() if v}
    all_feeds.update(LOCAL_RSS_FEEDS)

    rss_urls = list(all_feeds.items())  # list of (name, url) tuples

    if not rss_urls:
        raise ValueError("No RSS URLs found. Check your secrets.")

    print(f"Total RSS sources: {len(rss_urls)}")

    # ── 2. Load existing links to avoid duplicates ──────────────────────────
    existing_links: set[str] = set()
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                link = row.get("link") or row.get("Link") or ""
                if link:
                    existing_links.add(link)
    print(f"Loaded {len(existing_links)} existing links for dedup.")

    # ── 3. Fetch and parse RSS feeds ────────────────────────────────────────
    raw_entries: list[dict] = []
    n_feeds_ok = n_feeds_err = 0
    for feed_name, url in rss_urls:
        print(f"\nFetching [{feed_name}]: {url[:70]}...")
        try:
            feed = feedparser.parse(url)
            source_title = feed.feed.get("title", feed_name)
            n_new = 0
            for entry in feed.entries:
                raw_link = entry.get("link", "")
                real_link = extract_real_url(raw_link)
                rss_title = entry.get("title", "").strip()

                # If RSS title is just the publication name or blank,
                # derive a title from the URL slug instead
                if (not rss_title
                        or rss_title.lower() == source_title.lower()
                        or rss_title.lower() == feed_name.lower()):
                    slug = urlparse(real_link).path.strip("/").split("/")[-1]
                    rss_title = slug.replace("-", " ").replace("_", " ").title() if slug else ""

                # For general news feeds (not DC-dedicated), pre-filter:
                # only keep articles where title or summary mentions data centers.
                # This prevents dumping 100+ unrelated articles per feed.
                if feed_name not in DC_DEDICATED_FEEDS:
                    rss_summary = entry.get("summary", "")
                    check_text = (rss_title + " " + rss_summary).lower()
                    if not _dc_mentioned(check_text):
                        continue

                raw_entries.append({
                    "date":    entry.get("published",
                                         datetime.now(timezone.utc).isoformat()),
                    "title":   rss_title,
                    "link":    real_link,
                    "raw_link": raw_link,
                    "summary": entry.get("summary", ""),
                    "source":  source_title,
                })
                n_new += 1
            print(f"  → {n_new} entries")
            n_feeds_ok += 1
        except Exception as exc:
            print(f"  [WARN] Feed error: {exc}")
            n_feeds_err += 1

    print(f"\nFeeds: {n_feeds_ok} ok, {n_feeds_err} errors")
    print(f"Found {len(raw_entries)} raw RSS entries total.")

    # ── 4. Process each entry ───────────────────────────────────────────────
    new_entries: list[dict] = []

    for entry in raw_entries:
        link  = entry["link"]
        title = re.sub(r"<[^>]+>", "", entry["title"]).strip()
        date  = entry["date"]
        summary = re.sub(r"<[^>]+>", "",
                         entry.get("summary", "")).replace("&nbsp;", " ").strip()

        if not link:
            print(f"  [SKIP] No link for: {title[:70]}")
            continue

        # Skip junk titles — nav labels, social, single words, URLs as titles
        if not title or len(title.split()) < _MIN_ARTICLE_TITLE_WORDS:
            existing_links.add(link)
            continue
        if any(re.search(pat, title, re.I) for pat in SKIP_TITLE_PATTERNS_FETCH):
            print(f"  [SKIP-JUNK-TITLE] {title[:70]}")
            existing_links.add(link)
            continue
        # Apply article-link check to RSS entries too (catches nav items
        # that sneak through as direct RSS entries from malformed feeds)
        if link and not _looks_like_article_link(link, title):
            existing_links.add(link)
            continue

        if link in existing_links:
            print(f"  [DUPE] {title[:70]}")
            continue

        if _should_skip_url(link):
            print(f"  [SKIP-URL] {title[:70]}")
            # Still record as processed so we don't revisit
            existing_links.add(link)
            continue

        print(f"\n→ {title[:80] or link}")

        # ── Fetch full page ─────────────────────────────────────────────────
        time.sleep(0.5)   # polite delay
        soup, final_url = fetch_page(link)

        if soup is None:
            # Couldn't fetch — store what we have from RSS
            row = {
                "date":      date,
                "title":     title,
                "link":      link,
                "summary":   summary,
                "source":    entry["source"],
                "full_text": "",
            }
            new_entries.append(row)
            existing_links.add(link)
            _write_row(row)
            continue

        # ── Link-dump detection ─────────────────────────────────────────────
        if _is_link_dump(soup, final_url):
            print(f"  [LINK-DUMP] Expanding child links...")
            child_entries = extract_dump_links(soup, final_url, existing_links)
            print(f"  Found {len(child_entries)} qualifying child links.")

            for child in child_entries:
                child_link  = child["link"]
                child_title = child["title"]

                # Always DC-filter child articles from digest expansions —
                # even if the parent feed is dedicated (e.g. Google Alert
                # can link to a general local news digest page).
                # Check title first (fast), then fetch if it passes.
                if not _dc_mentioned(child_title.lower()):
                    existing_links.add(child_link)
                    continue

                # Also skip junk titles
                if (len(child_title.split()) < _MIN_ARTICLE_TITLE_WORDS
                        or any(re.search(p, child_title, re.I)
                               for p in SKIP_TITLE_PATTERNS_FETCH)):
                    existing_links.add(child_link)
                    continue

                # Fetch each child article
                time.sleep(0.5)
                child_soup, child_final_url = fetch_page(child_link)
                if child_soup:
                    child_text = _clean_text(child_soup)
                    # Try to get a better title from the page itself
                    h1 = child_soup.find("h1")
                    if h1:
                        page_title = h1.get_text(" ", strip=True)
                        if page_title and len(page_title.split()) >= _MIN_ARTICLE_TITLE_WORDS:
                            child_title = page_title[:200]
                    # Final DC check on full text + title
                    if not _dc_mentioned((child_title + " " + child_text[:500]).lower()):
                        existing_links.add(child_link)
                        continue
                else:
                    child_text = ""

                row = {
                    "date":      date,
                    "title":     child_title,
                    "link":      child_link,
                    "summary":   "",
                    "source":    f"{entry['source']} [via digest: {title[:60]}]",
                    "full_text": child_text,
                }
                new_entries.append(row)
                existing_links.add(child_link)
                _write_row(row)

            # Mark the digest URL itself as seen so we don't re-expand it
            existing_links.add(link)
            continue

        # ── Normal article — store full text ────────────────────────────────
        full_text = _clean_text(soup)

        # Try to get a better title from the page H1 if RSS title was generic
        h1 = soup.find("h1")
        if h1:
            page_title = h1.get_text(" ", strip=True)
            if page_title and len(page_title) > 10:
                title = page_title[:200]

        row = {
            "date":      date,
            "title":     title,
            "link":      link,
            "summary":   summary,
            "source":    entry["source"],
            "full_text": full_text,
        }
        new_entries.append(row)
        existing_links.add(link)
        _write_row(row)

    print(f"\n{'='*60}")
    print(f"Added {len(new_entries)} new entries to {OUTPUT_FILE}")
    print(f"{'='*60}")


def _write_row(row: dict) -> None:
    """Append a single row to the CSV, writing the header if needed."""
    write_header = not OUTPUT_FILE.exists()
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES,
                                extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


if __name__ == "__main__":
    main()
