#!/usr/bin/env python3
"""
br_sweep.py — By-Right Industrial Zoning sweep tool
====================================================
Reads a BR CSV, finds UNKNOWN rows, and for each city:
  1. Scrapes text from tiered sources (Municode deep-link, eCode360,
     amlegal, city website, PDF)
  2. Extracts ALL evidence windows (400 chars before/after each key phrase
     hit, plus full HTML table blocks) — stored raw in JSON for later review
  3. Makes its own Yes / No / UNKNOWN determination with a confidence score
     so results can be checked against that evidence independently

OUTPUT per city: one JSON file in <out_dir>/cities/<state>/<city>.json
SUMMARY JSON: <out_dir>/br_results.json (all cities, resumable)

TIERS (in order):
  1. Municode deep section URL — via DDG search for nodeId URLs
     (only deep direct links bypass Angular; skips generic landing page)
  2. eCode360 — static HTML, best coverage
  3. amlegal — static HTML
  4. City website / zoning code host — DDG search, broad queries
  5. PDF — DDG search for filetype:pdf, or known PDF URL from notes
  [Municode generic landing page NEVER attempted — always JS-rendered]

USAGE:
  pip3 install requests beautifulsoup4 pdfplumber
  python3 br_sweep.py --csv muni_br_final3.csv --state TX --out results/
  python3 br_sweep.py --csv muni_br_final3.csv --all-states --limit 50 --out results/
  python3 br_sweep.py --city "Lufkin" --state TX --out results/
  python3 br_sweep.py --apply results/br_results.json --csv muni_br_final3.csv --out muni_updated.csv
  python3 br_sweep.py --report results/br_results.json
"""

import argparse
import csv
import io
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ─── Playwright availability check ────────────────────────────────────────────
# Playwright drives a real Chromium browser to bypass bot detection on Bing/DDG.
# Install: pip install playwright && playwright install chromium
try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

# playwright-stealth patches navigator.webdriver etc. to avoid bot detection.
# Install: pip install playwright-stealth
try:
    from playwright_stealth import stealth_sync
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False

# Set to False via --no-browser flag to skip Playwright and use HTML scraping only
USE_BROWSER_SEARCH = True

# ─── Constants ────────────────────────────────────────────────────────────────

VERSION = "3.2.1"
TAG = f"[BR-SWEEP-AUTO {date.today().isoformat()}]"

EVIDENCE_WINDOW   = 400   # chars before/after a keyword hit to capture
PROXIMITY_WINDOW  = 600   # chars an IZ district name must be near a P/CUP hit
REQUEST_TIMEOUT   = 25    # seconds per HTTP request
PAUSE_BETWEEN     = 1.5   # seconds between requests (be polite)
MAX_PDF_PAGES     = 80    # cap PDF pages to extract

# Forbidden domains — never cite, never fetch
FORBIDDEN_DOMAINS = {
    "wikipedia.org", "zoneomics.com", "grokipedia.com",
    "tshaonline.org", "directory.tml.org",
    "municipalcodeonline.com", "energyzoning.org",
}

# Signatures indicating a page is NOT genuine zoning content — parked domains,
# casinos/gambling sites, hijacked domains, error pages that returned HTTP 200.
# These pages often contain enough generic words ("district", "permitted") to
# fool a loose keyword filter, so they're checked explicitly and reject outright.
ANTI_PATTERN_SIGNATURES = [
    r"\bcasino\b", r"\bslot\s+machine", r"\bsportsbook\b", r"\bgambl(?:e|ing)\b",
    r"\bpoker\b", r"\bjackpot\b", r"\bfree\s+spins?\b", r"\bplay\s+now\b",
    r"\bthis\s+domain\s+(is|may\s+be)\s+for\s+sale\b", r"\bdomain\s+parking\b",
    r"\bbuy\s+this\s+domain\b", r"\brelated\s+searches\b.{0,50}\bads?\b",
    r"\b404\s+(not\s+found|error)\b", r"\bpage\s+not\s+found\b",
    r"\bthe\s+page\s+you\s+(are\s+looking\s+for|requested)\s+.{0,30}not\s+found\b",
    r"\ban\s+error\s+occurred\b", r"\bunable\s+to\s+(display|process)\s+this\s+page\b",
    r"\baccess\s+denied\b", r"\bforbidden\b.{0,20}\byou\s+don.t\s+have\s+permission\b",
]

# At least one of these HARD industrial-zone terms must appear — generic words
# like "district" or "zoning" alone are not enough (too many false positives).
IZ_HARD_TERMS = [
    r"\bI-?1\b", r"\bI-?2\b", r"\bI-?3\b", r"\bM-?1\b", r"\bM-?2\b", r"\bM-?3\b",
    r"\blight\s+industrial\b", r"\bheavy\s+industrial\b", r"\bgeneral\s+industrial\b",
    r"\bindustrial\s+district\b", r"\bmanufacturing\s+district\b",
    r"\bindustrial\s+zone\b", r"\bindustrial\s+park\b", r"\bindustrial\s+use\b",
]

# ─── Search API keys ──────────────────────────────────────────────────────────
# ScrapingBee — Google Search API, bypasses bot detection reliably (paid).
# This is the PRIMARY search backend when a key is set. The script will ask
# at startup whether to keep this key or paste a new one for this run.
# Sign up at https://scrapingbee.com
SCRAPINGBEE_API_KEY = ""   # ← paste your key here, e.g. "abc123..."

# Serper.dev — free tier: 2,500 searches on signup, no credit card needed.
# Sign up at https://serper.dev, copy your API key here.
# Used as a fallback if ScrapingBee is unset or returns no results.
SERPER_API_KEY = ""   # ← paste your key here, e.g. "abc123..."

# Municode generic landing page — always JS-rendered, skip it
# (deep nodeId URLs may still work, so we only block the root pattern)
MUNICODE_GENERIC_PATTERNS = [
    r"library\.municode\.com/[^/]+/[^/]+/codes/code_of_ordinances$",
    r"library\.municode\.com/[^/]+/[^/]+/?$",
]

# Signatures that mean a fetched page is JS-rendered / blocked
JS_BLOCKER_SIGNATURES = [
    "Initializing application",
    "ng-app=",
    "ng-strict-di",
    "mcc.library_desktop",
    "Content cannot be found or you are not authorized",
    "window.__INITIAL_STATE__",
    "Please enable JavaScript",
    "Just a moment",          # Cloudflare challenge
    "cf-browser-verification",
    "Enable JavaScript and cookies",
]

# ─── District / use patterns ──────────────────────────────────────────────────

IZ_DISTRICT_PATTERNS = [
    r"\bI-?1\b", r"\bI-?2\b", r"\bI-?3\b",
    r"\bM-?1\b", r"\bM-?2\b", r"\bM-?3\b",
    r"\bLI\b", r"\bHI\b", r"\bGI\b",
    r"\blight\s+industrial\b", r"\bheavy\s+industrial\b",
    r"\bgeneral\s+industrial\b", r"\bindustrial\s+district\b",
    r"\bmanufacturing\s+district\b",
    r"\bI\s*[Dd]istrict\b", r"\bM\s*[Dd]istrict\b",
    # Hyphenated form only ("I-N") -- the bare "IN" would match every English word "in"
    r"\bI-?N\b",  # Freeport-style
    r"\bIP\b",    # Industrial Park
    r"\bO-?I\b",  # Office-Industrial
    r"\bML\b",    # Light Manufacturing (must appear in zoning context)
    r"\bLM\b",
    r"\bindustrial\s+park\b",
    r"\bindustrial\s+zone\b",
    r"\bmanufacturing\s+zone\b",
]

BY_RIGHT_PATTERNS = [
    r"\bpermitted\s+use[s]?\b",
    r"\bpermitted\s+by\s+right\b",
    r"\bby[- ]right\b",
    r"(?<!\w)P(?!\w)",           # lone "P" cell in use table
    r"\bpermitted\s+in\b",
    r"\ballow(?:ed|s)\s+by\s+right\b",
    r"\bprincipal\s+permitted\b",
    r"\bpermitted\s+principal\b",
    r"=\s*[Pp]\b",
    r"\|\s*P\s*\|",
    r"^\s*P\s*$",
    r"\bpermitted\s+outright\b",
    r"\buse\s+by\s+right\b",
]

CUP_PATTERNS = [
    r"\bconditional\s+use\s+permit\b",
    r"\bconditional\s+use\b",
    r"\bCUP\b",
    r"\bspecial\s+use\s+permit\b",
    r"\bSUP\b",
    r"\bspecific\s+use\s+permit\b",
    r"\bspecial\s+exception\b",
    r"(?<!\w)C(?!\w)",           # lone "C" cell
    r"(?<!\w)S(?!\w)",           # lone "S" cell
    r"=\s*[CcSs]\b",
    r"\|\s*[CS]\s*\|",
    r"^\s*[CS]\s*$",
    r"\bspecial\s+permit\b",
    r"\bboard\s+of\s+adjustment\b",
]

INDUSTRIAL_USE_KEYWORDS = [
    "manufactur", "fabricat", "assembl", "warehouse", "warehousing",
    "distribution", "machine shop", "welding", "processing", "packaging",
    "industrial use", "light industrial", "freight terminal",
    "bottling", "food processing", "printing", "metal fabricat",
    "general industrial", "industrial park", "storage yard",
    "contractor", "motor freight", "truck terminal", "wholesale",
    "auto repair", "auto body", "equipment storage", "lumber",
    "electronics assembl", "furniture manufactur",
]

# ─── State name mapping ───────────────────────────────────────────────────────

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

# ─── HTTP helpers ─────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # Do NOT set Accept-Encoding — let requests handle decompression automatically.
    # Explicitly requesting gzip without letting requests decode it produces binary garbage.
}


def is_forbidden(url: str) -> bool:
    domain = urlparse(url).netloc.lower()
    return any(fd in domain for fd in FORBIDDEN_DOMAINS)


def is_municode_generic(url: str) -> bool:
    """Return True if the URL is a Municode generic landing page (always JS-rendered)."""
    for pat in MUNICODE_GENERIC_PATTERNS:
        if re.search(pat, url):
            return True
    return False


def fetch_raw(url: str, timeout: int = REQUEST_TIMEOUT) -> tuple[bool, str, str]:
    """
    Fetch URL; return (success, raw_html, error_reason).
    Does NOT strip HTML — caller decides what to do with raw content.
    """
    if is_forbidden(url):
        return False, "", "forbidden_domain"
    if is_municode_generic(url):
        return False, "", "municode_generic_js_page"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        time.sleep(PAUSE_BETWEEN)

        if resp.status_code == 403:
            return False, "", "403_forbidden"
        if resp.status_code == 404:
            return False, "", "404_not_found"
        if resp.status_code == 429:
            return False, "", "429_rate_limited"
        if resp.status_code != 200:
            return False, "", f"http_{resp.status_code}"

        raw = resp.text

        for sig in JS_BLOCKER_SIGNATURES:
            if sig in raw:
                return False, "", "js_rendered_page"

        return True, raw, ""

    except requests.exceptions.Timeout:
        return False, "", "timeout"
    except requests.exceptions.ConnectionError:
        return False, "", "connection_error"
    except Exception as e:
        return False, "", f"error:{type(e).__name__}"


def html_to_text(raw_html: str) -> str:
    """Strip HTML to plain text, preserving table structure as best we can."""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s{3,}", "  ", text)
    return text


def extract_tables_from_html(raw_html: str) -> list[str]:
    """
    Extract all <table> blocks as plain text.
    Returns list of table strings — these are the most reliable BR evidence.
    """
    soup = BeautifulSoup(raw_html, "html.parser")
    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            tables.append("\n".join(rows))
    return tables


def fetch_text(url: str, timeout: int = REQUEST_TIMEOUT) -> tuple[bool, str, str, list[str]]:
    """
    Fetch a URL; return (success, plain_text, error_reason, table_texts).
    table_texts is a list of raw table content blocks — key evidence source.
    """
    ok, raw, reason = fetch_raw(url, timeout)
    if not ok:
        return False, "", reason, []

    text = html_to_text(raw)
    if len(text) < 100:
        return False, "", "page_too_short", []

    # Reject HTTP-200 error pages / parked domains / hijacked domains that
    # returned success but aren't real content (e.g. Phoenix IL Municode
    # nodeId that resolved to a "content not found" body, not a 404 status).
    for pat in ANTI_PATTERN_SIGNATURES:
        if re.search(pat, text[:3000], re.IGNORECASE):
            return False, "", "error_or_parked_page", []

    tables = extract_tables_from_html(raw)
    return True, text, "", tables


def fetch_pdf_text(url: str) -> tuple[bool, str, str]:
    """Download a PDF; return (success, text, reason)."""
    try:
        import pdfplumber

        resp = requests.get(url, headers=HEADERS, timeout=60)
        time.sleep(PAUSE_BETWEEN)
        if resp.status_code != 200:
            return False, "", f"http_{resp.status_code}"

        pages_text = []
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages[:MAX_PDF_PAGES]:
                t = page.extract_text()
                if t:
                    pages_text.append(t)

        full = "\n".join(pages_text)
        if len(full) < 200:
            return False, "", "pdf_no_text_layer"

        return True, full, ""

    except ImportError:
        return False, "", "pdfplumber_not_installed"
    except Exception as e:
        return False, "", f"pdf_error:{type(e).__name__}"


def web_search(query: str, n: int = 6) -> list[str]:
    """
    Primary search function. Tries backends in order:
      1. ScrapingBee Google Search API (if SCRAPINGBEE_API_KEY is set) — primary
      2. Playwright real-browser Bing (if playwright installed and USE_BROWSER_SEARCH=True)
      3. Serper.dev JSON API (if SERPER_API_KEY is set)
      4. Bing HTML scraping (may be bot-detected)
      5. DuckDuckGo HTML (may fail with VPN active)
    """
    if SCRAPINGBEE_API_KEY:
        results = _scrapingbee_search(query, n)
        if results:
            return results
    if USE_BROWSER_SEARCH and _PLAYWRIGHT_AVAILABLE:
        results = _playwright_search(query, n)
        if results:
            return results
    if SERPER_API_KEY:
        results = _serper_search(query, n)
        if results:
            return results
    results = _bing_search(query, n)
    if results:
        return results
    return _ddg_search(query, n)


def _decode_bing_href(href: str) -> str:
    """
    Bing wraps result URLs in tracking redirects: /ck/a?...&u=a1<base64url>...
    Decode the real URL from the u= parameter.
    """
    import base64
    from urllib.parse import unquote, urlparse, parse_qs

    href = (href or "").strip()

    if "bing.com/ck/a" in href:
        try:
            parsed = urlparse(href)
            params = parse_qs(parsed.query)
            u = params.get("u", [""])[0]
            if u.startswith("a1"):
                b64 = u[2:].replace("-", "+").replace("_", "/")
                b64 += "=" * (4 - len(b64) % 4)
                decoded = base64.b64decode(b64).decode("utf-8", errors="ignore")
                if decoded.startswith("http"):
                    return decoded
            return href
        except Exception:
            return href
    return href


def _extract_bing_links(soup) -> list[str]:
    """Extract result URLs from a Bing search results page (real-browser rendered)."""
    links = []
    seen = set()

    def _add(href: str):
        href = _decode_bing_href(href)
        if not href.startswith("http"):
            return
        if any(skip in href for skip in ("bing.com", "microsoft.com")):
            return
        if is_forbidden(href):
            return
        if href not in seen:
            seen.add(href)
            links.append(href)

    for li in soup.select("li.b_algo"):
        h2a = li.select_one("h2 a[href]")
        if h2a:
            _add(h2a["href"])
            continue
        for a in li.find_all("a", href=True):
            _add(a["href"])

    if not links:
        for a in soup.select("h2 a, h3 a"):
            _add(a.get("href", ""))

    return links


def _scrapingbee_search(query: str, n: int = 6) -> list[str]:
    """
    ScrapingBee Bing search — fetches real Bing HTML through rotating US proxies,
    bypassing bot detection. We parse li.b_algo from the returned HTML ourselves.
    render_js=false costs 1 credit (vs 5 for JS render) — sufficient for Bing.
    Sign up at https://scrapingbee.com for free trial credits.
    """
    if not SCRAPINGBEE_API_KEY:
        return []
    try:
        bing_url = f"https://www.bing.com/search?q={quote_plus(query)}&count={n}&mkt=en-US&setlang=en&cc=US"
        resp = requests.get(
            "https://app.scrapingbee.com/api/v1/",
            params={
                "api_key": SCRAPINGBEE_API_KEY,
                "url": bing_url,
                "render_js": "false",
                "country_code": "us",
            },
            timeout=30,
        )
        time.sleep(0.5)

        if resp.status_code == 401:
            print("\n  [SCRAPINGBEE ERROR] Invalid API key\n")
            return []
        if resp.status_code == 429:
            print("\n  [SCRAPINGBEE ERROR] Credits exhausted\n")
            return []
        if resp.status_code != 200:
            print(f"\n  [SCRAPINGBEE ERROR] HTTP {resp.status_code}: {resp.text[:300]}\n")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        links = _extract_bing_links(soup)
        return links[:n]

    except Exception as e:
        print(f"\n  [SCRAPINGBEE ERROR] {e}\n")
        return []


def _extract_google_links(soup) -> list[str]:
    """Extract result URLs from a Google search results page."""
    links = []
    seen = set()
    for a in soup.select("div#search a[href]"):
        href = a.get("href", "")
        if not href.startswith("http"):
            continue
        if is_forbidden(href):
            continue
        if href not in seen:
            seen.add(href)
            links.append(href)
    return links


def _playwright_search(query: str, n: int = 6) -> list[str]:
    """
    Search using a real headless Chromium browser via Playwright.
    Tries Bing first (real browser bypasses bot detection), falls back to Google.
    DDG deliberately skipped — blocked by VPN from EU.
    Free forever, no API key. Requires: pip install playwright && playwright install chromium
    Also: pip install playwright-stealth  (patches navigator.webdriver to avoid detection)
    """
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            if _STEALTH_AVAILABLE:
                stealth_sync(page)

            bing_err = ""
            search_url = f"https://www.bing.com/search?q={quote_plus(query)}&count={n}&mkt=en-US&setlang=en&cc=US&FORM=HDRSC1"
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                for consent_sel in ("#bnp_btn_accept", "button[id*='accept']", "#accept-button"):
                    try:
                        btn = page.query_selector(consent_sel)
                        if btn:
                            btn.click()
                            page.wait_for_timeout(500)
                    except Exception:
                        continue
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                html = page.content()
                soup = BeautifulSoup(html, "html.parser")
                links = _extract_bing_links(soup)
                if links:
                    browser.close()
                    return links[:n]
                bing_err = "no b_algo links in page"
            except Exception as e:
                bing_err = str(e)[:120]

            google_err = ""
            try:
                google_url = f"https://www.google.com/search?q={quote_plus(query)}&num={n}"
                page.goto(google_url, wait_until="domcontentloaded", timeout=20000)
                for consent_sel in (
                    "button[aria-label='Accept all']", "button[jsname='b3VHJd']",
                    "#L2AGLb", "form[action*='consent'] button",
                ):
                    try:
                        btn = page.query_selector(consent_sel)
                        if btn:
                            btn.click()
                            page.wait_for_timeout(1000)
                            break
                    except Exception:
                        continue
                try:
                    page.wait_for_selector("div#search a", timeout=8000)
                except Exception:
                    pass
                html = page.content()
                soup = BeautifulSoup(html, "html.parser")
                links = _extract_google_links(soup)
                if links:
                    browser.close()
                    return links[:n]
                google_err = "no result links in page"
            except Exception as e:
                google_err = str(e)[:120]

            browser.close()
            print(f"\n  [BROWSER DEBUG] Bing: {bing_err} | Google: {google_err}\n")

        print(f"\n  [BROWSER WARNING] No links found for: {query[:80]}\n")
        return []

    except Exception as e:
        print(f"\n  [BROWSER ERROR] {e}\n")
        return []


def playwright_fetch_html(url: str, timeout_ms: int = 20000) -> tuple[bool, str, str]:
    """
    Fetch a URL using a real Playwright browser — bypasses Cloudflare/bot blocks
    that reject plain requests. Returns (success, raw_html, error_reason).
    Used as fallback when requests gets 403/blocked on amlegal, ecode360, etc.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return False, "", "playwright_not_available"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            if _STEALTH_AVAILABLE:
                stealth_sync(page)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            html = page.content()
            browser.close()

        if len(html) < 200:
            return False, "", "page_too_short"
        for sig in JS_BLOCKER_SIGNATURES:
            if sig in html:
                return False, "", "js_rendered_page"
        return True, html, ""

    except Exception as e:
        return False, "", f"playwright_fetch_error:{type(e).__name__}"


def _serper_search(query: str, n: int = 6) -> list[str]:
    """
    Serper.dev Google Search API — works reliably from EU without VPN.
    Free tier: 2,500 searches. Sign up at https://serper.dev (no credit card).
    Set SERPER_API_KEY at the top of this file.
    """
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": n},
            timeout=15,
        )
        time.sleep(0.3)  # Serper is fast; short pause is enough
        if resp.status_code == 401:
            print("\n  [SERPER ERROR] Invalid API key — check SERPER_API_KEY\n")
            return []
        if resp.status_code == 403:
            print("\n  [SERPER ERROR] API key quota exceeded or plan issue\n")
            return []
        if resp.status_code != 200:
            print(f"\n  [SERPER ERROR] HTTP {resp.status_code}\n")
            return []

        data = resp.json()
        links = []
        seen = set()

        for item in data.get("organic", []):
            url = item.get("link", "")
            if not url.startswith("http"):
                continue
            if is_forbidden(url):
                continue
            if url not in seen:
                seen.add(url)
                links.append(url)

        return links[:n]

    except Exception as e:
        print(f"\n  [SERPER ERROR] {e}\n")
        return []


def _bing_search(query: str, n: int = 6) -> list[str]:
    """
    Bing HTML scraping — fallback when no API key is set.
    Note: Bing often serves bot-detection JS pages instead of results.
    Use Serper.dev for reliable results.
    """
    search_url = f"https://www.bing.com/search?q={quote_plus(query)}&count={n}"
    bing_headers = {
        **HEADERS,
        "Referer": "https://www.bing.com/",
    }
    try:
        resp = requests.get(search_url, headers=bing_headers, timeout=15)
        time.sleep(PAUSE_BETWEEN)
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        links = []
        seen = set()

        def _add(href: str):
            href = href.strip()
            if not href.startswith("http"):
                return
            if "bing.com" in href or "microsoft.com" in href:
                return
            if is_forbidden(href):
                return
            if href not in seen:
                seen.add(href)
                links.append(href)

        # Primary: result anchor tags
        for li in soup.select("li.b_algo"):
            for a in li.find_all("a", href=True):
                _add(a["href"])

        # Fallback selectors
        if not links:
            for tag in soup.select("h2 a, h3 a"):
                _add(tag.get("href", ""))

        if not links:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("http") and "bing.com" not in href:
                    _add(href)

        if not links:
            snippet = resp.text[:300].replace("\n", " ")
            print(f"\n  [BING WARNING] No links found for query: {query[:80]}")
            print(f"  [BING RAW] {snippet[:200]}\n")

        return links[:n]

    except Exception as e:
        print(f"\n  [BING ERROR] {e}\n")
        return []


def _ddg_search(query: str, n: int = 6) -> list[str]:
    """DuckDuckGo HTML search — secondary fallback."""
    search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
        time.sleep(PAUSE_BETWEEN)
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        links = []
        seen = set()

        def _add(href: str):
            href = href.strip()
            if "duckduckgo.com/l/" in href:
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    from urllib.parse import unquote
                    href = unquote(m.group(1))
            if not href.startswith("http"):
                return
            if is_forbidden(href):
                return
            if href not in seen:
                seen.add(href)
                links.append(href)

        for a in soup.select("a.result__url"):
            _add(a.get("href", ""))
        if not links:
            for a in soup.select("a.result__a"):
                _add(a.get("href", ""))
        if not links:
            for div in soup.select("div.result"):
                for a in div.find_all("a", href=True):
                    href = a["href"]
                    if href.startswith("http") or "duckduckgo.com/l/" in href:
                        _add(href)
        if not links:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "duckduckgo.com" not in href and href.startswith("http"):
                    _add(href)

        if not links:
            snippet = resp.text[:300].replace("\n", " ")
            print(f"\n  [DDG WARNING] No links found for query: {query[:80]}")
            print(f"  [DDG RAW] {snippet}\n")

        return links[:n]

    except Exception as e:
        print(f"\n  [DDG ERROR] {e}\n")
        return []


# ─── Evidence extraction ──────────────────────────────────────────────────────

def extract_windows(text: str, patterns: list[str], window: int = EVIDENCE_WINDOW) -> list[dict]:
    """
    For each pattern match in text, extract a window of chars around it.
    Returns list of {pattern, match, start, end, window_text}.
    Deduplicates overlapping windows.
    """
    hits = []
    seen_positions = set()

    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
            pos = m.start()
            # Skip if very close to an already-captured position
            if any(abs(pos - p) < 50 for p in seen_positions):
                continue
            seen_positions.add(pos)

            start = max(0, pos - window)
            end   = min(len(text), pos + window)
            snippet = text[start:end]
            snippet = re.sub(r"\s{2,}", " ", snippet).strip()

            hits.append({
                "pattern":     pat,
                "match":       m.group(),
                "position":    pos,
                "window_text": f"...{snippet}...",
            })

    return hits


def find_industrial_spans(text: str) -> list[tuple[int, int, str]]:
    """Return list of (start, end, matched_text) for all IZ district pattern matches."""
    spans = []
    for pat in IZ_DISTRICT_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            spans.append((m.start(), m.end(), m.group()))
    spans.sort(key=lambda x: x[0])
    return spans


def check_proximity(text: str, patterns: list[str], window: int = PROXIMITY_WINDOW) -> list[str]:
    """Return pattern matches that occur within `window` chars of any IZ district name."""
    iz_spans = find_industrial_spans(text)
    if not iz_spans:
        return []

    hits = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
            pos = m.start()
            for iz_start, iz_end, _ in iz_spans:
                if abs(pos - iz_start) <= window or abs(pos - iz_end) <= window:
                    hits.append(m.group())
                    break
    return list(set(hits))


def tables_containing_iz(tables: list[str]) -> list[str]:
    """Filter table strings to those that contain an industrial district pattern."""
    result = []
    for t in tables:
        for pat in IZ_DISTRICT_PATTERNS:
            if re.search(pat, t, re.IGNORECASE):
                result.append(t)
                break
    return result


# ─── Core analysis ────────────────────────────────────────────────────────────

def analyze_text(text: str, tables: list[str] = None) -> dict:
    """
    Given plain text (and optionally extracted table strings), determine BR value.

    Returns full analysis dict including:
      - All raw evidence windows (for later review)
      - The script's own determination + confidence + reason
    """
    tables = tables or []

    # ── 1. Find industrial district references ────────────────────────────
    iz_spans = find_industrial_spans(text)
    iz_district_found = bool(iz_spans)
    district_names    = list({s[2] for s in iz_spans})

    # Also check tables for IZ patterns
    iz_tables = tables_containing_iz(tables)

    # ── 2. Extract raw evidence windows around key phrases ─────────────
    # These are stored verbatim so they can be reviewed independently
    br_windows  = extract_windows(text, BY_RIGHT_PATTERNS)
    cup_windows = extract_windows(text, CUP_PATTERNS)
    use_windows = extract_windows(text, [re.escape(kw) for kw in INDUSTRIAL_USE_KEYWORDS])

    # ── 3. Proximity-gated hits (script's own logic) ──────────────────
    br_hits_near_iz  = check_proximity(text, BY_RIGHT_PATTERNS)
    cup_hits_near_iz = check_proximity(text, CUP_PATTERNS)

    # Industrial use keywords near both an IZ district AND a P indicator
    industrial_uses_confirmed = []
    for kw in INDUSTRIAL_USE_KEYWORDS:
        for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
            pos = m.start()
            near_iz = any(
                abs(pos - iz_start) <= PROXIMITY_WINDOW
                for iz_start, iz_end, _ in iz_spans
            )
            if near_iz:
                window_text = text[max(0, pos - 200):min(len(text), pos + 200)]
                near_p = bool(re.search(
                    r"\bP\b|\bpermitted\b|\bby.?right\b|\bpermitted\s+use\b",
                    window_text, re.IGNORECASE
                ))
                if near_p:
                    industrial_uses_confirmed.append(m.group())

    industrial_uses_confirmed = list(set(industrial_uses_confirmed))

    # Also check tables — tables with IZ + "P" are strong evidence
    table_br_hits = []
    for t in iz_tables:
        if re.search(r"\bP\b|\bpermitted\b", t, re.IGNORECASE):
            table_br_hits.append(t[:600])  # cap table snippet length

    # ── 4. "All uses require CUP" detection (→ BR=No) ─────────────────
    all_uses_cup = bool(re.search(
        r"(all|any|every)\s+(use|uses)(\s+of\s+property)?\s+(shall|must|require[sd]?|is\s+required)"
        r"\s+(a\s+)?(CUP|SUP|conditional|specific\s+use|special)",
        text, re.IGNORECASE
    )) or bool(re.search(
        r"(specific\s+use\s+permit|SUP|CUP)\s+shall\s+be\s+required\s+for\s+(all|any|every)\s+use",
        text, re.IGNORECASE
    ))

    # ── 5. Determination logic ────────────────────────────────────────────
    has_br   = bool(br_hits_near_iz)
    has_cup  = bool(cup_hits_near_iz)
    has_uses = bool(industrial_uses_confirmed)
    has_table_br = bool(table_br_hits)

    if has_br and has_uses:
        determination = "Yes"
        confidence    = "high"
        reason = (
            f"Industrial uses ({', '.join(industrial_uses_confirmed[:4])}) "
            f"confirmed near by-right indicators in district(s): {district_names}"
        )
    elif has_table_br and iz_district_found:
        determination = "Yes"
        confidence    = "high"
        reason = (
            f"Table block containing industrial district + permitted-use indicators found. "
            f"Districts: {district_names}"
        )
    elif has_br and not has_cup:
        determination = "Yes"
        confidence    = "medium"
        reason = (
            f"By-right indicators found near IZ district(s) {district_names}; "
            f"no CUP/SUP language detected"
        )
    elif has_br and has_cup:
        determination = "Yes"
        confidence    = "medium"
        reason = (
            f"Both P (by-right) and CUP/SUP indicators near IZ district(s) {district_names}. "
            f"Mixed use table — at least some uses are by-right"
        )
    elif all_uses_cup and iz_district_found:
        determination = "No"
        confidence    = "high"
        reason = (
            f"'All/any uses require CUP/SUP' language near IZ district(s) {district_names}. "
            f"No by-right industrial uses."
        )
    elif has_cup and not has_br and iz_district_found:
        determination = "UNKNOWN"
        confidence    = "low"
        reason = (
            f"CUP language near IZ district(s) {district_names} but no P indicators found. "
            f"Possibly all CUP but text insufficient to confirm — check manually."
        )
    elif iz_district_found and not has_br and not has_cup:
        determination = "UNKNOWN"
        confidence    = "low"
        reason = (
            f"IZ district found ({district_names}) but no clear P or CUP use table indicators. "
            f"Page may be a district description without the use table."
        )
    else:
        determination = "UNKNOWN"
        confidence    = "low"
        reason        = "No industrial district name found in text."

    return {
        # Raw evidence — preserved for independent review
        "evidence": {
            "by_right_windows":         br_windows[:20],    # cap at 20 hits
            "cup_windows":              cup_windows[:20],
            "industrial_use_windows":   use_windows[:20],
            "iz_tables":                table_br_hits[:5],  # cap at 5 table blocks
        },
        # Script analysis
        "industrial_district_found": iz_district_found,
        "district_names":            district_names,
        "br_hits_near_iz":           br_hits_near_iz,
        "cup_hits_near_iz":          cup_hits_near_iz,
        "industrial_uses_confirmed": industrial_uses_confirmed,
        "all_uses_cup_detected":     all_uses_cup,
        "table_br_hits_count":       len(table_br_hits),
        # Script determination
        "determination": determination,
        "confidence":    confidence,
        "reason":        reason,
    }


# ─── Tier functions ───────────────────────────────────────────────────────────

def _base_result(source_type: str, source_url: str = "", fetch_error: str = "") -> dict:
    return {
        "source_type":   source_type,
        "source_url":    source_url,
        "source_found":  bool(source_url),
        "text_found":    False,
        "fetch_error":   fetch_error,
        "evidence":      {"by_right_windows": [], "cup_windows": [],
                          "industrial_use_windows": [], "iz_tables": []},
        "industrial_district_found": False,
        "district_names":            [],
        "br_hits_near_iz":           [],
        "cup_hits_near_iz":          [],
        "industrial_uses_confirmed": [],
        "all_uses_cup_detected":     False,
        "table_br_hits_count":       0,
        "determination": "UNKNOWN",
        "confidence":    "low",
        "reason":        fetch_error or "No result",
    }


def tier_municode_deep(city: str, state: str, known_url: Optional[str] = None) -> dict:
    """
    Tier 1: Municode deep section URL.
    The Angular app is unreadable BUT direct nodeId section URLs sometimes
    serve static content. We search DDG for deep section URLs with nodeId params.
    Never attempt generic Municode landing pages.
    """
    state_name = STATE_NAMES.get(state, state)
    candidates = []

    # Use known URL from notes if it's a deep Municode link (has nodeId)
    if known_url and "library.municode.com" in known_url and "nodeId" in known_url:
        candidates.append(known_url)

    # Search DDG for Municode section URLs containing the industrial district section
    for query in [
        f'site:library.municode.com "{city}" {state} industrial zoning "nodeId"',
        f'site:library.municode.com "{city}" {state_name} industrial "permitted"',
        f'library.municode.com "{city}" {state} zoning industrial district',
    ]:
        for url in web_search(query, n=4):
            if "library.municode.com" in url and "nodeId" in url:
                if url not in candidates:
                    candidates.append(url)

    if not candidates:
        return _base_result("municode_deep", fetch_error="No deep Municode nodeId URL found via DDG")

    for url in candidates[:4]:
        if is_municode_generic(url):
            continue
        ok, text, reason, tables = fetch_text(url)
        if not ok:
            continue
        if not _is_zoning_page(text):
            continue
        if not _matches_target_municipality(text, city, state):
            continue
        analysis = analyze_text(text, tables)
        if analysis["industrial_district_found"] or analysis["determination"] != "UNKNOWN":
            return {
                **_base_result("municode_deep", source_url=url),
                "text_found": True,
                "source_text": text,
                **analysis,
            }

    return _base_result(
        "municode_deep",
        source_url=candidates[0],
        fetch_error="Municode deep URLs fetched but no IZ text found (likely still JS-rendered)",
    )


def _is_zoning_page(text: str) -> bool:
    """
    Quick sanity check: does this page look like genuine zoning content?
    Rejects pages that are building codes, subdivision regs, utilities, casino/
    gambling sites, parked domains, or HTTP-200 error pages — a loose keyword
    count alone lets these through, so a page must also clear a hard-term and
    a minimum-hit-count bar below.

    Requires:
      1. No anti-pattern hit (casino/parked-domain/error-page signature)
      2. At least one HARD industrial-zone term (I-1, M-1, "light industrial", etc.)
      3. At least 4 generic zoning-signal hits
    """
    sample = text[:5000]

    # Reject outright if any anti-pattern (casino, parked domain, error page) hits
    for pat in ANTI_PATTERN_SIGNATURES:
        if re.search(pat, sample, re.IGNORECASE):
            return False

    # Must contain at least one hard industrial-zone term — generic zoning
    # vocabulary alone (e.g. a residential-only page) is not sufficient
    has_hard_term = any(re.search(pat, text, re.IGNORECASE) for pat in IZ_HARD_TERMS)
    if not has_hard_term:
        return False

    zoning_signals = [
        r"\bzon(?:ing|e)\b", r"\bdistrict\b", r"\bpermitted\s+use", r"\bland\s+use\b",
        r"\boverlay\b", r"\bsetback\b", r"\buse\s+table\b", r"\buse\s+regulation",
        r"\bprincipal\s+use\b", r"\bconditional\s+use\b", r"\bspecial\s+use\b",
    ]
    hits = sum(1 for pat in zoning_signals if re.search(pat, sample, re.IGNORECASE))
    return hits >= 4


MUNI_SUFFIX_PATTERN = re.compile(
    r"^(?:city|town|village|township|borough|the)\s+of\s+|"
    r"\s+(?:city|town|village|township|borough|cdp|area|unincorporated)$",
    re.IGNORECASE,
)


def _normalize_city_name(city: str) -> str:
    """Strip common municipal suffixes/prefixes so name-matching isn't thrown
    off by e.g. 'City of Springfield' vs 'Springfield', or a CSV row labeled
    'Cedar Rapids area' vs a source page that just says 'Cedar Rapids'."""
    c = MUNI_SUFFIX_PATTERN.sub("", city.strip())
    return c.strip()


def _matches_target_municipality(text: str, city: str, state: str) -> bool:
    """
    Guard against cross-jurisdiction false positives — e.g. a web search for
    "Seminole, TX" landing on a page about Seminole County, FLORIDA, or a
    search for "Wink, TX" landing on Saginaw, TX's zoning code. Nothing else
    verifies the fetched page is actually about the target municipality,
    rather than just A municipality with plausible-looking zoning text.

    Requires:
      1. The (normalized) city name appears somewhere in the fetched text
      2. If the target state's name/abbreviation is absent from the text, but
         a DIFFERENT state's full name appears near a city-name mention,
         reject — strong signal this is the wrong place entirely.
    """
    norm_city = _normalize_city_name(city)
    if not norm_city:
        return True  # nothing to validate against, don't block

    city_pat = r"\s+".join(re.escape(w) for w in norm_city.split())
    city_matches = list(re.finditer(rf"\b{city_pat}\b", text, re.IGNORECASE))
    if not city_matches:
        return False

    target_state_name = STATE_NAMES.get(state.upper(), state)
    has_target_state = (
        re.search(rf"\b{re.escape(target_state_name)}\b", text, re.IGNORECASE) is not None
        or re.search(rf"\b{re.escape(state.upper())}\b", text) is not None
    )
    if not has_target_state:
        other_state_names = [n for abbr, n in STATE_NAMES.items() if abbr != state.upper()]
        for m in city_matches:
            window = text[max(0, m.start() - 150):m.start() + 150]
            for other_name in other_state_names:
                if re.search(rf"\b{re.escape(other_name)}\b", window, re.IGNORECASE):
                    return False

    return True


def tier_ecode360(city: str, state: str, known_url: Optional[str] = None) -> dict:
    """Tier 2: eCode360 — static HTML, most reliable source."""
    state_name = STATE_NAMES.get(state, state)
    candidates = []

    if known_url and "ecode360.com" in known_url:
        candidates.append(known_url)

    for query in [
        f'site:ecode360.com "{city}" {state} industrial zoning district "permitted"',
        f'site:ecode360.com "{city}" {state_name} industrial zoning',
        f'site:ecode360.com "{city}" {state_name} zoning',
        f'site:ecode360.com "{city}" {state}',
    ]:
        for url in web_search(query, n=5):
            if "ecode360.com" in url and url not in candidates:
                candidates.append(url)

    tried_urls = set()
    i = 0
    while i < len(candidates) and len(tried_urls) < 8:
        url = candidates[i]
        i += 1
        if url in tried_urls:
            continue
        tried_urls.add(url)

        ok, text, reason, tables = fetch_text(url)
        if not ok:
            continue

        # Reject pages that are not zoning content (e.g. building code, utilities)
        if not _is_zoning_page(text):
            # Try to follow links to zoning sections from this page
            try:
                ok2, raw, _ = fetch_raw(url)
                if ok2:
                    soup = BeautifulSoup(raw, "html.parser")
                    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
                    for a in soup.find_all("a", href=True):
                        link_text = a.get_text().lower()
                        href = a["href"]
                        if any(kw in link_text for kw in ["zoning", "industrial", "manufactur", "land use", "use regulation"]):
                            full = href if href.startswith("http") else urljoin(base, href)
                            if "ecode360.com" in full and full not in tried_urls:
                                candidates.append(full)
            except Exception:
                pass
            continue  # Skip this non-zoning page for analysis

        if not _matches_target_municipality(text, city, state):
            continue  # Page is zoning content but for a different municipality

        analysis = analyze_text(text, tables)
        if analysis["industrial_district_found"] or analysis["determination"] != "UNKNOWN":
            return {
                **_base_result("ecode360", source_url=url),
                "text_found": True,
                "source_text": text,
                **analysis,
            }

        # Zoning page found but no IZ hits — follow links to sub-sections
        try:
            ok2, raw, _ = fetch_raw(url)
            if ok2:
                soup = BeautifulSoup(raw, "html.parser")
                base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
                for a in soup.find_all("a", href=True):
                    link_text = a.get_text().lower()
                    href = a["href"]
                    if any(kw in link_text for kw in ["zoning", "industrial", "manufactur", "land use", "use regulation", "permitted"]):
                        full = href if href.startswith("http") else urljoin(base, href)
                        if "ecode360.com" in full and full not in tried_urls:
                            candidates.append(full)
        except Exception:
            pass

    if not candidates:
        return _base_result("ecode360", fetch_error="No eCode360 page found for this city")

    return _base_result(
        "ecode360",
        source_url=candidates[0],
        fetch_error="eCode360 page found but no industrial district text extracted",
    )


def _amlegal_fetch(url: str) -> tuple[bool, str, str, list]:
    """
    Fetch an amlegal URL — tries requests first, falls back to Playwright.
    codelibrary.amlegal.com often blocks plain requests with a bot check.
    """
    ok, text, reason, tables = fetch_text(url)
    if ok:
        return ok, text, reason, tables

    if _PLAYWRIGHT_AVAILABLE and USE_BROWSER_SEARCH:
        ok2, html2, err2 = playwright_fetch_html(url)
        if ok2:
            text2 = html_to_text(html2)
            tables2 = extract_tables_from_html(html2)
            if len(text2) >= 100:
                return True, text2, "", tables2

    return False, "", reason, []


def tier_amlegal(city: str, state: str) -> dict:
    """Tier 3: American Legal Publishing (library.amlegal.com + codelibrary.amlegal.com)."""
    city_slug  = re.sub(r"[^a-z0-9]", "_", city.lower())
    city_slug2 = re.sub(r"[^a-z0-9]", "-", city.lower())
    state_lower = state.lower()
    state_name  = STATE_NAMES.get(state, state)

    # Try both old library.amlegal.com and newer codelibrary.amlegal.com
    candidates = [
        f"https://library.amlegal.com/codes/{state_lower}/{city_slug}/",
        f"https://library.amlegal.com/codes/{state_lower}/{city_slug2}/",
        f"https://codelibrary.amlegal.com/codes/{city_slug}/latest/",
        f"https://codelibrary.amlegal.com/codes/{city_slug2}/latest/",
    ]
    # Search both amlegal subdomains
    for q in [
        f'site:library.amlegal.com "{city}" {state_name} zoning industrial',
        f'site:codelibrary.amlegal.com "{city}" {state_name} zoning',
        f'site:codelibrary.amlegal.com "{city}" {state} zoning industrial',
    ]:
        for url in web_search(q, n=4):
            if url not in candidates:
                candidates.append(url)

    for url in candidates[:8]:
        ok, text, reason, tables = _amlegal_fetch(url)
        if not ok:
            continue
        if not _is_zoning_page(text):
            # Try following internal links to find the zoning section
            ok2, raw2, _ = fetch_raw(url)
            if ok2:
                soup2 = BeautifulSoup(raw2, "html.parser")
                for a in soup2.find_all("a", href=True):
                    href = urljoin(url, a["href"])
                    link_text = a.get_text(strip=True).lower()
                    if any(kw in link_text for kw in ("zoning", "land use", "planning")):
                        ok3, text3, _, tables3 = _amlegal_fetch(href)
                        if ok3 and _is_zoning_page(text3):
                            text, tables = text3, tables3
                            break
            if not _is_zoning_page(text):
                continue
        if not _matches_target_municipality(text, city, state):
            continue
        analysis = analyze_text(text, tables)
        if analysis["industrial_district_found"] or analysis["determination"] != "UNKNOWN":
            return {
                **_base_result("amlegal", source_url=url),
                "text_found": True,
                "source_text": text,
                **analysis,
            }

    return _base_result("amlegal", fetch_error="No amlegal page found or no usable IZ text")


def tier_web_search(city: str, state: str) -> dict:
    """
    Tier 4: Broad web search for city zoning code.
    Tries multiple query angles; skips Municode generic URLs.
    Fetches up to 10 unique domains. Validates zoning page content before analysis.
    """
    state_name = STATE_NAMES.get(state, state)

    queries = [
        # Most targeted — zoning use table
        f'"{city}" {state_name} zoning ordinance industrial district "permitted use"',
        f'"{city}" {state} "light industrial" OR "I-1" OR "M-1" "permitted use" zoning',
        # By-right specific
        f'"{city}" {state} "by right" industrial zoning',
        f'"{city}" {state_name} "by right" industrial development zoning',
        f'"{city}" zoning code "permitted uses" industrial "by right"',
        # City gov site
        f'site:*.gov "{city}" {state} zoning industrial "permitted"',
        # Simple unquoted — catches plain city zoning pages (like Google's top result)
        f'{city} {state_name} zoning ordinance industrial',
        f'{city} {state_name} by right industrial zoning',
        # Direct city zoning page — broadest fallback
        f'{city} {state_name} zoning department planning industrial uses',
        # Broad fallback — any hosted zoning code
        f'"{city}" {state_name} zoning code industrial manufacturing permitted uses',
        f'"{city}" {state} municipal code zoning industrial district',
    ]

    seen_domains: set = set()
    all_urls: list = []

    for q in queries:
        for url in web_search(q, n=6):
            if "library.municode.com" in url:
                continue  # always skip Municode (JS-rendered)
            if is_forbidden(url):
                continue
            domain = urlparse(url).netloc
            if domain not in seen_domains:
                all_urls.append(url)
                seen_domains.add(domain)
        if len(all_urls) >= 12:
            break

    for url in all_urls[:12]:
        ok, text, reason, tables = fetch_text(url)
        if not ok:
            continue
        if not _is_zoning_page(text):
            continue  # skip non-zoning pages
        if not _matches_target_municipality(text, city, state):
            continue  # right-looking zoning page, wrong municipality/state
        analysis = analyze_text(text, tables)
        if analysis["industrial_district_found"] or analysis["determination"] != "UNKNOWN":
            return {
                **_base_result("web_search", source_url=url),
                "text_found": True,
                "source_text": text,
                **analysis,
            }

    return _base_result("web_search", fetch_error="Web search returned no usable zoning text")


def tier_pdf(city: str, state: str, known_pdf_urls: list = None) -> dict:
    """
    Tier 5: PDF — either known URLs from notes, or discovered via DDG.
    """
    state_name = STATE_NAMES.get(state, state)
    candidates = list(known_pdf_urls or [])

    # Search DDG for zoning PDFs
    for query in [
        f'"{city}" "{state}" "zoning ordinance" "industrial" filetype:pdf',
        f'"{city}" {state} zoning code industrial "permitted uses" filetype:pdf',
        f'"{city}" {state} "by right" industrial zoning filetype:pdf',
        f'"{city}" {state_name} "by right industrial" zoning ordinance filetype:pdf',
        f'"{city}" {state} site:*.gov zoning ordinance filetype:pdf',
        f'"{city}" {state_name} "zoning ordinance" industrial district permitted',
    ]:
        for url in web_search(query, n=4):
            if url not in candidates:
                candidates.append(url)

    for url in candidates[:5]:
        ok, text, reason = fetch_pdf_text(url)
        if not ok:
            continue
        if not _is_zoning_page(text):
            continue
        if not _matches_target_municipality(text, city, state):
            continue
        analysis = analyze_text(text)  # PDFs have no HTML tables
        return {
            **_base_result("pdf", source_url=url),
            "text_found": True,
            "source_text": text,
            **analysis,
        }

    return _base_result("pdf", fetch_error="No readable PDF found (scanned or not found)")


# ─── Main per-city sweep ──────────────────────────────────────────────────────

def extract_urls_from_text(text: str) -> list[str]:
    return re.findall(r"https?://[^\s\"'<>,;)]+", text)


SNAPSHOT_SNIPPET_LEN = 600  # chars for the CSV-inline evidence snippet


def save_snapshot(out_dir: Path, city: str, state: str, source_type: str, text: str) -> str:
    """
    Save the full fetched page text to disk so the evidence behind a
    determination survives even if the source page later changes or goes
    down. Returns the path (relative to out_dir) for recording in the result.
    """
    snap_dir = out_dir / "snapshots" / state.upper()
    snap_dir.mkdir(parents=True, exist_ok=True)
    city_slug = re.sub(r"[^a-z0-9]", "_", city.lower())
    snap_path = snap_dir / f"{city_slug}__{source_type}.txt"
    with open(snap_path, "w", encoding="utf-8") as f:
        f.write(text)
    return str(snap_path.relative_to(out_dir))


def build_evidence_snippet(result: dict, length: int = SNAPSHOT_SNIPPET_LEN) -> str:
    """
    Build a short, human-checkable evidence snippet from the strongest
    available signal — prefers table hits (most reliable), then by-right
    windows, then CUP windows. Capped at `length` chars for CSV readability;
    the full source text is preserved separately via save_snapshot().
    """
    evidence = result.get("evidence", {})
    pieces = []

    for t in evidence.get("iz_tables", [])[:2]:
        pieces.append(t)
    for w in evidence.get("by_right_windows", [])[:2]:
        pieces.append(w.get("window_text", ""))
    if not pieces:
        for w in evidence.get("cup_windows", [])[:2]:
            pieces.append(w.get("window_text", ""))

    combined = " | ".join(p for p in pieces if p)
    combined = re.sub(r"\s{2,}", " ", combined).strip()
    if len(combined) > length:
        combined = combined[:length].rsplit(" ", 1)[0] + "..."
    return combined


def sweep_city(city: str, state: str,
               existing_notes: str = "", source_urls: str = "",
               out_dir: Optional[Path] = None) -> dict:
    """
    Run full tiered sweep for one city. Returns complete result dict.
    Each tier is tried in order; the first one that finds IZ text is used.
    If all tiers fail, returns UNKNOWN with full audit trail.
    """
    base = {
        "municipality": city,
        "state":        state,
        "sweep_date":   date.today().isoformat(),
        "version":      VERSION,
    }

    # Collect hint URLs from CSV notes and source_urls columns
    all_hint_text = (existing_notes or "") + " " + (source_urls or "")
    all_urls = [u.rstrip(".,;)\"'") for u in extract_urls_from_text(all_hint_text)]

    known_ecode   = next((u for u in all_urls if "ecode360.com" in u), None)
    known_municode = next(
        (u for u in all_urls if "library.municode.com" in u and "nodeId" in u), None
    )
    known_pdfs = [
        u for u in all_urls
        if (u.lower().endswith(".pdf") or "/pdf" in u.lower())
        and "municode.com" not in u
        and "ecode360.com" not in u
        and not is_forbidden(u)
    ]

    tried = []  # log of which tiers ran

    def _finalize(r: dict) -> dict:
        """
        Attach a saved evidence snapshot + inline snippet before returning.
        Strips the (potentially large) raw source_text out of the result so
        the aggregate br_results.json stays small — the full text lives in
        the snapshot file on disk instead.
        """
        result = {**base, **r, "tiers_tried": tried}
        source_text = result.pop("source_text", None)
        if source_text:
            result["evidence_snippet"] = build_evidence_snippet(result)
            if out_dir is not None:
                try:
                    result["snapshot_path"] = save_snapshot(
                        out_dir, city, state, result.get("source_type", "unknown"), source_text
                    )
                except Exception as e:
                    result["snapshot_path"] = ""
                    result["snapshot_error"] = str(e)
            else:
                result["snapshot_path"] = ""
        else:
            result["evidence_snippet"] = ""
            result["snapshot_path"] = ""
        return result

    # ── Tier 1: Municode deep section URL ─────────────────────────────────
    r = tier_municode_deep(city, state, known_url=known_municode)
    tried.append(f"municode_deep:{r['fetch_error'] or 'ok'}")
    if r["determination"] != "UNKNOWN" or r["industrial_district_found"]:
        return _finalize(r)

    # ── Tier 2: eCode360 ──────────────────────────────────────────────────
    r = tier_ecode360(city, state, known_url=known_ecode)
    tried.append(f"ecode360:{r['fetch_error'] or 'ok'}")
    if r["determination"] != "UNKNOWN" or r["industrial_district_found"]:
        return _finalize(r)

    # ── Tier 3: amlegal ───────────────────────────────────────────────────
    r = tier_amlegal(city, state)
    tried.append(f"amlegal:{r['fetch_error'] or 'ok'}")
    if r["determination"] != "UNKNOWN" or r["industrial_district_found"]:
        return _finalize(r)

    # ── Tier 4: Web search ────────────────────────────────────────────────
    r = tier_web_search(city, state)
    tried.append(f"web_search:{r['fetch_error'] or 'ok'}")
    if r["determination"] != "UNKNOWN" or r["industrial_district_found"]:
        return _finalize(r)

    # ── Tier 5: PDF ───────────────────────────────────────────────────────
    r = tier_pdf(city, state, known_pdf_urls=known_pdfs)
    tried.append(f"pdf:{r['fetch_error'] or 'ok'}")
    if r["determination"] != "UNKNOWN" or r["industrial_district_found"]:
        return _finalize(r)

    # ── All tiers exhausted ───────────────────────────────────────────────
    r["source_type"] = "none"
    r["reason"] = (
        "All tiers exhausted. No accessible zoning text found. "
        "Tiers tried: " + ", ".join(tried) + ". Manual lookup required."
    )
    return _finalize(r)


# ─── CSV helpers ──────────────────────────────────────────────────────────────

def load_csv(path: Path) -> tuple[list, list]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames), list(reader)


def save_csv(path: Path, fieldnames: list, rows: list) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ─── Run sweep ────────────────────────────────────────────────────────────────

def run_sweep(csv_path: Path, out_dir: Path, state_filter: Optional[str],
              city_filter: Optional[str] = None,
              limit: Optional[int] = None, resume: bool = True) -> None:

    out_dir.mkdir(parents=True, exist_ok=True)
    city_dir = out_dir / "cities"
    city_dir.mkdir(exist_ok=True)
    results_path = out_dir / "br_results.json"

    # Load existing results:
    #   resume=True  → keep ALL prior results (skip everything already processed)
    #   resume=False → keep only confirmed Yes/No results (re-research UNKNOWNs)
    existing_results = {}
    if results_path.exists():
        with open(results_path) as f:
            saved = json.load(f)
        if resume:
            for r in saved:
                key = (r["municipality"].strip().lower(), r["state"].strip().upper())
                existing_results[key] = r
            print(f"Resuming: {len(existing_results)} cities already processed")
        else:
            confirmed = [r for r in saved if r.get("determination") in ("Yes", "No")]
            for r in confirmed:
                key = (r["municipality"].strip().lower(), r["state"].strip().upper())
                existing_results[key] = r
            print(f"Fresh start: preserving {len(confirmed)} confirmed Yes/No results, re-researching UNKNOWNs")

    fieldnames, rows = load_csv(csv_path)

    # Build target list
    targets = []
    for row in rows:
        state = row.get("State", "").strip()
        br    = row.get("By_Right_Industrial_Zoning", "").strip()
        city  = row.get("Municipality", "").strip()

        if city_filter and city.lower() != city_filter.lower():
            continue
        if not city_filter and br not in ("UNKNOWN", ""):
            continue
        if state_filter and state.upper() != state_filter.upper():
            continue

        key = (city.lower(), state.upper())
        if key in existing_results:
            continue
        targets.append(row)

    if limit:
        targets = targets[:limit]

    print(f"Cities to sweep: {len(targets)}")
    print("=" * 60)

    all_results = list(existing_results.values())

    for i, row in enumerate(targets, 1):
        city     = row["Municipality"].strip()
        state    = row["State"].strip()
        notes    = row.get("Additional_Notes", "")
        src_urls = row.get("Source_URLs", "")

        print(f"[{i}/{len(targets)}] {city}, {state} ...", end=" ", flush=True)

        try:
            result = sweep_city(city, state, existing_notes=notes, source_urls=src_urls, out_dir=out_dir)
            # Gate: a Yes/No determination MUST have a non-empty source_url, or the
            # evidence trail is unverifiable. Downgrade to UNKNOWN rather than silently apply.
            if result["determination"] in ("Yes", "No") and not result.get("source_url", "").strip():
                orig_det = result["determination"]
                orig_reason = result.get("reason", "")
                result["determination"] = "UNKNOWN"
                result["confidence"] = "low"
                result["reason"] = (
                    f"REJECTED: determination was {orig_det!r} but source_url was empty. "
                    f"Original reason: {orig_reason}"
                )
        except Exception as e:
            result = {
                "municipality": city,
                "state":        state,
                "sweep_date":   date.today().isoformat(),
                "version":      VERSION,
                "source_type":  "error",
                "source_url":   "",
                "source_found": False,
                "text_found":   False,
                "fetch_error":  str(e),
                "tiers_tried":  [],
                "evidence":     {"by_right_windows": [], "cup_windows": [],
                                 "industrial_use_windows": [], "iz_tables": []},
                "industrial_district_found": False,
                "district_names":            [],
                "br_hits_near_iz":           [],
                "cup_hits_near_iz":          [],
                "industrial_uses_confirmed": [],
                "all_uses_cup_detected":     False,
                "table_br_hits_count":       0,
                "determination": "UNKNOWN",
                "confidence":    "low",
                "reason":        f"Exception: {e}",
            }

        all_results.append(result)

        det  = result["determination"]
        conf = result.get("confidence", "?")
        src  = result.get("source_type", "?")
        print(f"{det} [{conf}] via {src}")

        # Save individual city JSON
        state_dir = city_dir / state.upper()
        state_dir.mkdir(exist_ok=True)
        city_slug = re.sub(r"[^a-z0-9]", "_", city.lower())
        city_json = state_dir / f"{city_slug}.json"
        with open(city_json, "w") as f:
            json.dump(result, f, indent=2)

        # Save master results file (safe resumption)
        with open(results_path, "w") as f:
            json.dump(all_results, f, indent=2)

    print(f"\nDone. Results saved to {results_path}")
    print(f"Individual city JSONs in: {city_dir}/")
    _print_summary(all_results)


# ─── Apply mode ───────────────────────────────────────────────────────────────

def apply_results(json_path: Path, csv_path: Path, out_path: Path,
                  min_confidence: str = "medium") -> None:
    """Apply JSON results back to CSV. Only updates UNKNOWN rows with sufficient confidence."""
    CONF_ORDER = {"high": 2, "medium": 1, "low": 0}
    min_level  = CONF_ORDER.get(min_confidence, 1)

    with open(json_path) as f:
        results = json.load(f)

    fieldnames, rows = load_csv(csv_path)
    lookup = {
        (r["municipality"].strip().lower(), r["state"].strip().upper()): r
        for r in results
    }

    applied = skipped_conf = skipped_conflict = skipped_no_source = 0

    for row in rows:
        city  = row.get("Municipality", "").strip()
        state = row.get("State", "").strip()
        key   = (city.lower(), state)

        if key not in lookup:
            continue

        res        = lookup[key]
        current_br = row.get("By_Right_Industrial_Zoning", "").strip()

        if current_br != "UNKNOWN":
            if current_br != res["determination"]:
                skipped_conflict += 1
                print(f"  CONFLICT {city}: CSV={current_br}, JSON={res['determination']} — keeping CSV")
            continue

        if res["determination"] == "UNKNOWN":
            continue

        # Gate: never apply a Yes/No without a real, non-empty source URL -- otherwise
        # the source ends up buried only in Additional_Notes, or missing entirely.
        source_url = res.get("source_url", "").strip()
        if not source_url:
            skipped_no_source += 1
            print(f"  SKIP {city}: determination={res['determination']} but source_url is empty")
            continue

        conf_level = CONF_ORDER.get(res.get("confidence", "low"), 0)
        if conf_level < min_level:
            skipped_conf += 1
            print(f"  SKIP {city}: confidence={res.get('confidence')} < {min_confidence}")
            continue

        row["By_Right_Industrial_Zoning"] = res["determination"]
        row["By_Right_Industrial_Date"]   = res["sweep_date"]

        # Write the source URL to the actual Source_URLs column (not just
        # buried in notes) — merge with any existing sources, don't overwrite.
        existing_src = row.get("Source_URLs", "").strip()
        if source_url not in existing_src:
            row["Source_URLs"] = (existing_src + " | " + source_url).strip(" | ")

        evidence_snippet = res.get("evidence_snippet", "")
        snapshot_path     = res.get("snapshot_path", "")

        note_parts = [
            res.get("reason", "")[:200],
            f"Source: {source_url}",
            f"Evidence: {evidence_snippet}" if evidence_snippet else "",
            f"Snapshot: {snapshot_path}" if snapshot_path else "",
            TAG,
        ]
        new_note = " | ".join(p for p in note_parts if p.strip())
        existing = row.get("Additional_Notes", "").strip()
        if TAG not in existing:
            row["Additional_Notes"] = (existing + " | " + new_note).strip(" | ")

        applied += 1
        print(f"  APPLIED {city} ({state}): {res['determination']} [{res.get('confidence')}]")

    save_csv(out_path, fieldnames, rows)
    print(
        f"\nApplied: {applied} | Skipped (low conf): {skipped_conf} | "
        f"Skipped (no source): {skipped_no_source} | Conflicts: {skipped_conflict}"
    )
    print(f"Output: {out_path}")


# ─── Report mode ─────────────────────────────────────────────────────────────

def report(json_path: Path) -> None:
    with open(json_path) as f:
        results = json.load(f)

    print(f"\n{'='*60}")
    print(f"REPORT: {json_path}  ({len(results)} cities)")
    print(f"{'='*60}")
    _print_summary(results)

    print("\nSample YES cities:")
    for r in [x for x in results if x["determination"] == "Yes"][:5]:
        print(f"  {r['municipality']}, {r['state']} [{r['confidence']}] via {r['source_type']}")
        print(f"    {r['reason'][:120]}")

    print("\nSample NO cities:")
    for r in [x for x in results if x["determination"] == "No"][:5]:
        print(f"  {r['municipality']}, {r['state']} [{r['confidence']}] via {r['source_type']}")
        print(f"    {r['reason'][:120]}")

    print("\nSample UNKNOWN cities (tiers tried):")
    for r in [x for x in results if x["determination"] == "UNKNOWN"][:5]:
        print(f"  {r['municipality']}, {r['state']}")
        print(f"    Tiers: {r.get('tiers_tried', [])}")
        print(f"    Reason: {r['reason'][:120]}")


def _print_summary(results: list) -> None:
    yes  = sum(1 for r in results if r["determination"] == "Yes")
    no   = sum(1 for r in results if r["determination"] == "No")
    unk  = sum(1 for r in results if r["determination"] == "UNKNOWN")

    print(f"\nSummary: Yes={yes} | No={no} | UNKNOWN={unk} | Total={len(results)}")

    src_counts: dict = {}
    for r in results:
        s = r.get("source_type", "unknown")
        src_counts[s] = src_counts.get(s, 0) + 1
    print("By source: " + " | ".join(f"{k}={v}" for k, v in src_counts.items()))

    conf_counts: dict = {}
    for r in results:
        c = r.get("confidence", "?")
        conf_counts[c] = conf_counts.get(c, 0) + 1
    print("By confidence: " + " | ".join(f"{k}={v}" for k, v in conf_counts.items()))


# ─── Interactive startup prompt ───────────────────────────────────────────────

def interactive_startup(csv_path: Path, out_dir: Path,
                        state_filter: Optional[str]) -> tuple[bool, Optional[int]]:
    """
    Ask the user two questions at startup:
      1. Fresh start (overwrite previous results) or continue from last saved row?
      2. How many rows to process this run?

    Returns (resume: bool, limit: Optional[int]).
    """
    results_path = out_dir / "br_results.json"

    # ── Count available targets ───────────────────────────────────────────
    try:
        _, rows = load_csv(csv_path)
    except FileNotFoundError:
        print(f"\nERROR: CSV not found: {csv_path}")
        sys.exit(1)

    total_unknown = sum(
        1 for r in rows
        if r.get("By_Right_Industrial_Zoning", "").strip() in ("UNKNOWN", "")
        and (not state_filter or r.get("State", "").strip().upper() == state_filter.upper())
    )

    already_done = 0
    if results_path.exists():
        try:
            with open(results_path) as f:
                saved = json.load(f)
            already_done = len(saved)
        except Exception:
            already_done = 0

    remaining = max(0, total_unknown - already_done)

    print()
    print("=" * 60)
    print(f"  br_sweep.py v{VERSION} — By-Right Industrial Zoning Sweep")
    print("=" * 60)
    scope = f"state={state_filter}" if state_filter else "all states"
    print(f"  CSV:          {csv_path}")
    print(f"  Scope:        {scope}")
    print(f"  UNKNOWN rows: {total_unknown:,} total")
    if results_path.exists():
        print(f"  Already done: {already_done:,}  (saved in {results_path})")
        print(f"  Remaining:    {remaining:,}")
    print("=" * 60)

    # ── Question 1: fresh start or continue? ─────────────────────────────
    if results_path.exists() and already_done > 0:
        print()
        print("Previous results found. How do you want to proceed?")
        print("  [1] Continue from last saved row  (skip already-processed cities)")
        print("  [2] Redo UNKNOWNs from start      (keep confirmed Yes/No, re-research UNKNOWNs)")
        while True:
            choice = input("Enter 1 or 2: ").strip()
            if choice in ("1", "2"):
                break
            print("Please enter 1 or 2.")
        resume = (choice == "1")
        if resume:
            print(f"  → Continuing. Will process up to {remaining:,} remaining cities.")
        else:
            print(f"  → Re-researching UNKNOWNs. Confirmed Yes/No results will be preserved.")
            remaining = total_unknown
    else:
        resume = False
        print()
        print(f"No previous results found — starting fresh.")

    # ── Question 2: how many rows? ────────────────────────────────────────
    available = remaining if resume else total_unknown
    print()
    print(f"How many cities should this run process?")
    print(f"  Maximum available: {available:,}")
    print(f"  (Enter a number, or press Enter to process all {available:,})")
    while True:
        raw = input(f"Number of cities [all]: ").strip()
        if raw == "":
            limit = None
            print(f"  → Processing all {available:,} cities.")
            break
        try:
            limit = int(raw)
            if limit <= 0:
                print("Please enter a positive number.")
                continue
            if limit > available:
                print(f"  That's more than available ({available:,}). Capping at {available:,}.")
                limit = None
            else:
                print(f"  → Processing {limit:,} cities this run.")
            break
        except ValueError:
            print("Please enter a valid number or press Enter.")

    print()
    return resume, limit


def maybe_update_api_key() -> None:
    """
    Ask at startup whether to keep the built-in ScrapingBee API key or swap
    in a new one for this run — lets you rotate keys without editing the
    file. Skips the prompt automatically if stdin isn't a TTY (e.g. a
    scheduled/piped run) so nothing hangs waiting for input.
    """
    global SCRAPINGBEE_API_KEY

    if not sys.stdin.isatty():
        return

    masked = (
        f"{SCRAPINGBEE_API_KEY[:6]}...{SCRAPINGBEE_API_KEY[-4:]}"
        if SCRAPINGBEE_API_KEY else "(none set)"
    )
    print()
    print(f"ScrapingBee API key currently: {masked}")
    try:
        raw = input("Press Enter to keep it, or paste a new key: ").strip()
    except EOFError:
        raw = ""
    if raw:
        SCRAPINGBEE_API_KEY = raw
        print("  → API key updated for this run.")
    else:
        print("  → Keeping existing key.")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=f"br_sweep.py v{VERSION} — By-Right Industrial Zoning sweep",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  # Sweep all TX unknowns
  python3 br_sweep.py --csv muni_br_final3.csv --state TX --out results/

  # Sweep a single city
  python3 br_sweep.py --csv muni_br_final3.csv --city "Lufkin" --state TX --out results/

  # Sweep first 20 cities across all states (test run)
  python3 br_sweep.py --csv muni_br_final3.csv --all-states --limit 20 --out results/

  # Apply confirmed results (medium+ confidence) back to CSV
  python3 br_sweep.py --apply results/br_results.json \\
      --csv muni_br_final3.csv --out muni_updated.csv

  # Apply only high-confidence results
  python3 br_sweep.py --apply results/br_results.json \\
      --csv muni_br_final3.csv --out muni_updated.csv --min-confidence high

  # Print summary report
  python3 br_sweep.py --report results/br_results.json
"""
    )

    parser.add_argument("--csv",            help="Input CSV file path")
    parser.add_argument("--state",          help="Filter to one state (e.g. TX)")
    parser.add_argument("--city",           help="Sweep a single city by name")
    parser.add_argument("--all-states",     action="store_true", help="Sweep all states")
    parser.add_argument("--limit",          type=int, help="Max cities to sweep")
    parser.add_argument("--out",            default="results", help="Output directory")
    parser.add_argument("--no-resume",      action="store_true", help="Ignore existing results, start fresh")
    parser.add_argument("--no-browser",     action="store_true", help="Skip Playwright browser search, use HTML scraping only")
    parser.add_argument("--apply",          help="JSON results file to apply back to CSV")
    parser.add_argument("--min-confidence", default="medium",
                        choices=["high", "medium", "low"],
                        help="Minimum confidence to apply (default: medium)")
    parser.add_argument("--report",         help="Print report from a JSON results file")

    args = parser.parse_args()

    # Wire up global search mode flag
    global USE_BROWSER_SEARCH
    if args.no_browser:
        USE_BROWSER_SEARCH = False

    # Ask whether to keep or change the ScrapingBee API key — every run,
    # not just once. Skipped for --report (no network needed) and non-TTY
    # stdin (e.g. piped/scheduled runs) so it never hangs unattended.
    if not args.report:
        maybe_update_api_key()

    # Print active search backend
    if SCRAPINGBEE_API_KEY:
        print("  [Search] Primary: ScrapingBee Google Search API")
    if USE_BROWSER_SEARCH and _PLAYWRIGHT_AVAILABLE:
        print("  [Search] Fallback: Playwright real-browser Bing (bot-detection bypass)")
    elif USE_BROWSER_SEARCH and not _PLAYWRIGHT_AVAILABLE:
        print("  [Search] Playwright not installed — falling back to HTML scraping")
        print("  [Search] To enable browser search: pip install playwright && playwright install chromium")
    if SERPER_API_KEY:
        print("  [Search] Serper.dev API key set — using as search fallback")
    if not SCRAPINGBEE_API_KEY and not (USE_BROWSER_SEARCH and _PLAYWRIGHT_AVAILABLE) and not SERPER_API_KEY:
        print("  [Search] WARNING: No reliable search backend! Bing/DDG may be bot-detected.")
        print("  [Search] Fix: pip install playwright && playwright install chromium")

    # ── Report mode ─────────────────────────────────────────────────────
    if args.report:
        report(Path(args.report))
        return

    # ── Apply mode ──────────────────────────────────────────────────────
    if args.apply:
        if not args.csv:
            parser.error("--apply requires --csv")
        out_csv = Path(args.out) if args.out.endswith(".csv") else Path(args.csv).with_suffix(".updated.csv")
        apply_results(
            json_path=Path(args.apply),
            csv_path=Path(args.csv),
            out_path=out_csv,
            min_confidence=args.min_confidence,
        )
        return

    # ── Single-city mode (no CSV needed) ────────────────────────────────
    if args.city and args.state and not args.csv:
        print(f"Sweeping: {args.city}, {args.state}")
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        result = sweep_city(args.city, args.state, out_dir=out_dir)
        city_slug = re.sub(r"[^a-z0-9]", "_", args.city.lower())
        out_file = out_dir / f"{city_slug}_{args.state.lower()}.json"
        with open(out_file, "w") as f:
            json.dump(result, f, indent=2)
        print(json.dumps(result, indent=2))
        print(f"\nSaved to {out_file}")
        return

    # ── Sweep mode ──────────────────────────────────────────────────────
    if not args.csv:
        parser.error("--csv is required for sweep mode")
    if not args.state and not args.all_states and not args.city:
        parser.error("Specify --state TX, --all-states, or --city")

    state_filter = args.state.upper() if args.state else None
    csv_path     = Path(args.csv)
    out_dir      = Path(args.out)

    # ── Interactive startup (skipped for single-city --city runs) ───────
    if args.city and args.state:
        # Single-city: no prompt needed, just run
        resume = not args.no_resume
        limit  = args.limit
    elif args.no_resume:
        # Caller explicitly said --no-resume; skip prompt
        resume = False
        limit  = args.limit
    else:
        resume, limit = interactive_startup(csv_path, out_dir, state_filter)

    # ── Determine output CSV path ────────────────────────────────────────
    # Default output is muni_br_final4.csv in the same folder as the input CSV
    input_stem = csv_path.stem                          # e.g. "muni_br_final3"
    # Strip trailing digit, replace with next version
    base_stem  = re.sub(r"\d+$", "", input_stem)       # e.g. "muni_br_final"
    try:
        current_num = int(re.search(r"\d+$", input_stem).group())
    except (AttributeError, ValueError):
        current_num = 3
    next_num   = current_num + 1
    output_csv = csv_path.parent / f"{base_stem}{next_num}.csv"

    print(f"Output CSV will be: {output_csv}")
    print()

    run_sweep(
        csv_path=csv_path,
        out_dir=out_dir,
        state_filter=state_filter,
        city_filter=args.city,
        limit=limit,
        resume=resume,
    )

    # ── After sweep, auto-apply medium+ confidence results ───────────────
    results_path = out_dir / "br_results.json"
    if results_path.exists():
        print()
        print(f"Applying confirmed results to {output_csv} ...")
        apply_results(
            json_path=results_path,
            csv_path=csv_path,
            out_path=output_csv,
            min_confidence="medium",
        )


if __name__ == "__main__":
    main()
