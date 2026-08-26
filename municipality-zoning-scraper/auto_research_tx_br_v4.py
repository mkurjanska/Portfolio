#!/usr/bin/env python3
"""
auto_research_tx_br_v4.py
=========================
Improved layered researcher for TX BR status.

Fixes vs v3:
  1. eCode360 / amlegal  → cloudscraper bypasses Cloudflare JS challenges
  2. Municode            → DuckDuckGo search (ddgs) instead of Google (no 429s)
  3. .gov landing pages  → deeper link crawl: follows zoning/ordinance sub-links
                           before giving up; also tries common PDF path patterns

Install:
  pip3 install requests beautifulsoup4 cloudscraper pdfplumber duckduckgo-search selenium

Run:
  python3 auto_research_tx_br_v4.py
"""

import csv
import json
import os
import re
import sys
import time
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing deps. Run: pip3 install requests beautifulsoup4 cloudscraper pdfplumber duckduckgo-search")
    sys.exit(1)

try:
    import cloudscraper
    _SCRAPER = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )
except Exception:
    cloudscraper = None
    _SCRAPER = None

try:
    import pdfplumber
except Exception:
    pdfplumber = None

# DuckDuckGo search (duckduckgo-search >= 4.x uses DDGS class)
try:
    from duckduckgo_search import DDGS
    _DDGS = DDGS()
except Exception:
    _DDGS = None

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
except Exception:
    webdriver = None

# ── paths ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
OUTPUT_JSON = BASE_DIR / "RESEARCH_RESULTS.json"
OUTPUT_CSV  = BASE_DIR / "RESEARCH_RESULTS.csv"
DOWNLOAD_DIR = BASE_DIR / "downloaded_docs"
LOG_FILE    = BASE_DIR / "auto_research_tx_br.log"
DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── NLP patterns ─────────────────────────────────────────────────────────────
BY_RIGHT_PATTERNS = [
    r"\bpermitted\b",
    r"\bpermitted use\b",
    r"\bby[\s-]right\b",
    r"\ballowed by right\b",
    r"\bprincipally permitted\b",
    r"\bpermitted outright\b",
    r"\buse by right\b",
]

CONDITIONAL_PATTERNS = [
    r"\bCUP\b",
    r"\bSUP\b",
    r"\bSE\b",          # Special Exception abbreviation
    r"conditional use permit",
    r"special use permit",
    r"special exception",
    r"special permit",
    r"requires.*approval",
]

INDUSTRIAL_ZONE_PATTERNS = [
    r"\bI-1\b", r"\bI-2\b", r"\bI-3\b", r"\bI-4\b",
    r"\bM-1\b", r"\bM-2\b", r"\bM-3\b",
    r"\bLI\b", r"\bHI\b", r"\bGI\b",
    r"light industrial", r"heavy industrial",
    r"general industrial", r"manufacturing district",
    r"industrial district", r"industrial zone",
    r"industrial park",
]

# Link text / href keywords that suggest a zoning document
DOC_HINTS = [
    "zoning", "ordinance", "code", "use table", "use chart", "district",
    "industrial", "land development", "development code", "municipal code",
    "comprehensive zoning", "zoning ordinance", "land use", "udc", "ldc",
]

# href fragments that strongly suggest we've reached a zoning ordinance page
ZONING_LINK_KEYWORDS = [
    "zoning", "ordinance", "land-use", "land_use", "development-code",
    "development_code", "udc", "ldc", "code-of-ordinances", "municipal-code",
]

ALIASES = {
    "st paul": "st paul",
    "town of pecos city": "pecos city",
    "pecos city": "pecos city",
}

# ── city list (111 TX cities) ─────────────────────────────────────────────────
CITIES = [
    # NOTE (portfolio redaction): originally 111 TX cities with their
    # discovered zoning-code source URL and platform type. Replaced with
    # fillers; loop logic below is unchanged.
    ("Example City",  "TX", "ecode360", "https://ecode360.com/EX0000"),
    ("Sample Town",   "TX", "amlegal",  "https://codelibrary.amlegal.com/codes/sampletown_tx/latest/sampletown_tx/0-0-0-1"),
    ("Demo Village",  "TX", "gov",      "https://www.demovillagetx.example/zoning"),
]


# ── helpers ───────────────────────────────────────────────────────────────────

def normalize_city(x):
    x = x.strip().lower()
    x = x.replace("&", " and ")
    x = re.sub(r"[^\w\s-]", " ", x)
    x = re.sub(r"\s+", " ", x).strip()
    return ALIASES.get(x, x)


def is_pdf_url(url):
    return url.lower().endswith(".pdf") or ".pdf?" in url.lower()


def classify_text(text):
    if not text:
        return "PAGE_FAILED", "Empty response text"
    t = text.lower()
    if "404" in t and ("not found" in t or "page not found" in t):
        return "NOT_FOUND", "Page 404"
    if "access denied" in t or "forbidden" in t:
        return "PAGE_FAILED", "Blocked or denied"
    if len(t.strip()) < 300:
        return "PAGE_FAILED", "Response too short"
    # Detect Cloudflare challenge pages — title is "Just a moment..." or similar
    soup_check = BeautifulSoup(text, "html.parser")
    title_tag = soup_check.title
    if title_tag:
        title_text = title_tag.get_text().strip().lower()
        if "just a moment" in title_text or "please wait" in title_text or "checking your browser" in title_text:
            return "PAGE_FAILED", "Cloudflare challenge page"
    return "OK", ""


def _raw_fetch(url, timeout=25, retries=2):
    """Plain requests fetch."""
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            soup = BeautifulSoup(r.text, "html.parser") if r.text else None
            title = soup.title.get_text(" ", strip=True) if soup and soup.title else ""
            return {
                "ok": r.status_code == 200,
                "status_code": r.status_code,
                "text": r.text if r.status_code == 200 else "",
                "final_url": r.url,
                "title": title,
                "error": "",
            }
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** (attempt - 1))
    return {"ok": False, "status_code": None, "text": "", "final_url": url, "title": "", "error": last_err}


def _cloud_fetch(url, timeout=30):
    """cloudscraper fetch — bypasses Cloudflare JS challenges."""
    if _SCRAPER is None:
        return {"ok": False, "status_code": None, "text": "", "final_url": url, "title": "",
                "error": "cloudscraper not installed"}
    try:
        r = _SCRAPER.get(url, timeout=timeout, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser") if r.text else None
        title = soup.title.get_text(" ", strip=True) if soup and soup.title else ""
        ok = r.status_code == 200
        # If Cloudflare still returned a challenge page, treat as failure
        if ok and ("just a moment" in title.lower() or "cloudflare" in title.lower()):
            ok = False
        return {
            "ok": ok,
            "status_code": r.status_code,
            "text": r.text if ok else "",
            "final_url": r.url,
            "title": title,
            "error": "",
        }
    except Exception as e:
        return {"ok": False, "status_code": None, "text": "", "final_url": url, "title": "", "error": str(e)}


def fetch_url(url, source_type="", timeout=25):
    """
    Smart dispatcher:
      - ecode360 / amlegal  → cloudscraper always
      - everything else     → plain requests first, cloudscraper on 403
    """
    if source_type in ("ecode360", "amlegal"):
        result = _cloud_fetch(url, timeout)
        if not result["ok"] and result["status_code"] in (None, 403):
            # Try Selenium as last resort
            br_result = browser_fetch(url)
            if br_result[0]:
                html = br_result[0]["html"]
                soup = BeautifulSoup(html, "html.parser")
                title = soup.title.get_text(" ", strip=True) if soup.title else ""
                result = {"ok": True, "status_code": 200, "text": html,
                          "final_url": br_result[0]["final_url"], "title": title, "error": ""}
        return result

    result = _raw_fetch(url, timeout=timeout)
    if not result["ok"] and result["status_code"] == 403:
        logging.info(f"  403 on {url} — retrying with cloudscraper")
        result = _cloud_fetch(url, timeout)
    return result


def extract_document_links(base_url, html, max_links=25):
    """Extract links likely to lead to zoning ordinance text/PDFs."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        text = (a.get_text(" ", strip=True) or "").lower()
        full = urljoin(base_url, href)
        if full in seen:
            continue
        blob = f"{href} {text}".lower()
        if any(h in blob for h in DOC_HINTS) or is_pdf_url(full):
            seen.add(full)
            links.append(full)
    return links[:max_links]


def download_file(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30, stream=True, allow_redirects=True)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        ctype = r.headers.get("Content-Type", "").lower()
        ext = ".pdf" if ("pdf" in ctype or is_pdf_url(url)) else ".html"
        name = re.sub(r"[^a-zA-Z0-9._-]+", "_", os.path.basename(urlparse(r.url).path) or "document")
        if not name.lower().endswith(ext):
            name += ext
        path = DOWNLOAD_DIR / name
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return path, ""
    except Exception as e:
        return None, str(e)


def text_from_pdf(path):
    if pdfplumber is None:
        return ""
    try:
        out = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:50]:
                out.append(page.extract_text() or "")
        return "\n".join(out)
    except Exception:
        return ""


def _extract_plain_text(html_or_text):
    """
    If the input looks like HTML, extract visible text only.
    Otherwise return as-is (for PDF-extracted text, plain text docs).
    """
    if html_or_text and ("<html" in html_or_text[:500].lower() or "<!doctype" in html_or_text[:200].lower()):
        soup = BeautifulSoup(html_or_text, "html.parser")
        # Remove scripts, styles, and head entirely
        for tag in soup(["script", "style", "head", "noscript", "meta", "link"]):
            tag.decompose()
        return soup.get_text(separator="\n")
    return html_or_text


def find_evidence(text):
    """
    Return (status, evidence_snippet).

    Requires that BY-RIGHT or CUP language appears WITHIN 300 characters
    of an industrial zone designation — not just anywhere on the page.
    This avoids false positives from nav menus, CSP headers, boilerplate, etc.
    """
    if not text:
        return "UNKNOWN", "No readable text"

    # Strip HTML to plain visible text first
    plain = _extract_plain_text(text)
    if not plain or len(plain.strip()) < 100:
        return "NO_EVIDENCE", "Page has no useful visible text"

    # Must have at least one industrial zone designation in the plain text
    has_industrial = any(re.search(p, plain, re.IGNORECASE) for p in INDUSTRIAL_ZONE_PATTERNS)
    if not has_industrial:
        return "NO_EVIDENCE", "No industrial zone language found on page"

    # Context-window check: look for by-right / CUP terms near an industrial zone term
    # Build a list of (start, end) spans for every industrial zone match
    industrial_spans = []
    for p in INDUSTRIAL_ZONE_PATTERNS:
        for m in re.finditer(p, plain, re.IGNORECASE):
            industrial_spans.append((m.start(), m.end()))

    WINDOW = 400   # characters on either side of an industrial zone mention

    def near_industrial(match_obj):
        ms, me = match_obj.start(), match_obj.end()
        for iz_start, iz_end in industrial_spans:
            if abs(ms - iz_start) <= WINDOW or abs(me - iz_end) <= WINDOW:
                return True
        return False

    # Find by-right and CUP matches that are close to an industrial zone mention
    br_near  = any(near_industrial(m) for p in BY_RIGHT_PATTERNS
                   for m in re.finditer(p, plain, re.IGNORECASE))
    cup_near = any(near_industrial(m) for p in CONDITIONAL_PATTERNS
                   for m in re.finditer(p, plain, re.IGNORECASE))

    # Find the first industrial zone match and grab surrounding context as snippet
    first_iz = min(industrial_spans, key=lambda x: x[0])
    snip_start = max(0, first_iz[0] - 100)
    snip_end   = min(len(plain), first_iz[1] + 300)
    snippet = re.sub(r"\s+", " ", plain[snip_start:snip_end]).strip()

    if br_near and not cup_near:
        return "FOUND_YES", f"By-right language near industrial zone. {snippet}"
    if cup_near and not br_near:
        return "FOUND_NO", f"Conditional-use language near industrial zone. {snippet}"
    if br_near and cup_near:
        return "FOUND_YES", f"Mixed (by-right + CUP) near industrial zone. {snippet}"

    return "NO_EVIDENCE", f"Industrial zone terms present but no clear by-right/CUP language nearby. {snippet}"


def browser_fetch(url):
    """Selenium headless Chrome — last resort."""
    if webdriver is None:
        return None, "Selenium not installed"
    try:
        opts = ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument(f"--user-agent={HEADERS['User-Agent']}")
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(30)
        driver.get(url)
        time.sleep(4)
        html = driver.page_source
        final_url = driver.current_url
        driver.quit()
        return {"html": html, "final_url": final_url}, ""
    except Exception as e:
        return None, str(e)


# ── DuckDuckGo search (replaces googlesearch-python) ─────────────────────────

def ddg_search_candidates(city, state, n=6):
    """Search DuckDuckGo for zoning ordinance documents. Returns list of URLs."""
    if _DDGS is None:
        logging.warning("duckduckgo-search not available; skipping DDG search")
        return []
    query = f'"{city}" {state} zoning ordinance "industrial" "permitted" site:municode.com OR site:ecode360.com OR filetype:pdf'
    try:
        results = list(_DDGS.text(query, max_results=n))
        urls = [r.get("href") or r.get("url", "") for r in results if r]
        return [u for u in urls if u]
    except Exception as e:
        logging.warning(f"DDG search failed for {city}: {e}")
        time.sleep(3)   # back off if we hit a rate limit
        return []


# ── gov deeper-crawl logic ────────────────────────────────────────────────────

def gov_secondary_links(base_url, html, visited):
    """
    From a .gov landing page, extract sub-links that look like they lead to
    actual zoning text, then return the ones we haven't visited yet.
    Priority order: PDF links > links containing strong zoning keywords.
    """
    soup = BeautifulSoup(html, "html.parser")
    pdf_links = []
    page_links = []
    seen = set(visited)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        text = (a.get_text(" ", strip=True) or "").lower()
        full = urljoin(base_url, href)
        if full in seen:
            continue
        blob = f"{href} {text}".lower()

        if is_pdf_url(full):
            if any(k in blob for k in DOC_HINTS):
                pdf_links.append(full)
                seen.add(full)
        elif any(k in blob for k in ZONING_LINK_KEYWORDS):
            page_links.append(full)
            seen.add(full)

    # PDFs first, then page links; cap total to keep runtime reasonable
    return (pdf_links + page_links)[:10]


def process_gov_deep(url, source_type):
    """
    Fetch the .gov page, try to find evidence directly.
    If not found, follow secondary links (PDFs and zoning sub-pages) up to depth 2.
    """
    visited = set()
    queue = [(url, 0)]   # (url, depth)

    out = {
        "status": "PAGE_FAILED",
        "http_status": None,
        "final_url": url,
        "page_title": "",
        "documents_found": [],
        "documents_downloaded": [],
        "evidence": "",
        "error": "",
    }

    while queue:
        current_url, depth = queue.pop(0)
        if current_url in visited:
            continue
        visited.add(current_url)

        if depth > 2:
            continue

        fetched = fetch_url(current_url, source_type=source_type)
        out["http_status"] = fetched["status_code"]
        out["final_url"]   = fetched["final_url"]
        out["page_title"]  = fetched["title"]

        if not fetched["ok"]:
            out["status"] = "PAGE_FAILED"
            out["error"]  = fetched["error"] or f"HTTP {fetched['status_code']}"
            continue

        page_state, note = classify_text(fetched["text"])
        if page_state != "OK":
            out["status"] = page_state
            out["evidence"] = note
            continue

        # Is this a PDF? (some .gov links serve PDFs directly)
        if is_pdf_url(current_url) or "pdf" in fetched.get("title", "").lower():
            path, err = download_file(current_url)
            if path:
                out["documents_downloaded"].append(str(path))
                text = text_from_pdf(path)
                br, ev = find_evidence(text)
                if br.startswith("FOUND"):
                    out["status"] = br
                    out["evidence"] = ev
                    return out
            continue

        # Scan page text directly
        br, ev = find_evidence(fetched["text"])
        if br.startswith("FOUND"):
            out["status"] = br
            out["evidence"] = ev
            return out

        # Collect secondary links for deeper crawl
        if depth < 2:
            doc_links = extract_document_links(fetched["final_url"], fetched["text"])
            out["documents_found"].extend([l for l in doc_links if l not in out["documents_found"]])

            secondary = gov_secondary_links(fetched["final_url"], fetched["text"], visited)
            for sl in secondary:
                if sl not in visited:
                    if is_pdf_url(sl):
                        # Download PDFs immediately
                        path, err = download_file(sl)
                        if path:
                            out["documents_downloaded"].append(str(path))
                            if path.suffix.lower() == ".pdf":
                                text = text_from_pdf(path)
                            else:
                                try:
                                    text = path.read_text(errors="ignore")
                                except Exception:
                                    text = ""
                            br, ev = find_evidence(text)
                            if br.startswith("FOUND"):
                                out["status"] = br
                                out["evidence"] = ev
                                return out
                        visited.add(sl)
                    else:
                        queue.append((sl, depth + 1))

        out["status"] = "NO_EVIDENCE"
        out["evidence"] = ev

        time.sleep(1.0)   # polite crawl delay

    return out


def process_direct(url, source_type=""):
    """
    Standard fetch-and-scan for non-.gov pages.
    Follows up to 8 document links found on the page.
    Uses cloudscraper for ecode360/amlegal.
    """
    fetched = fetch_url(url, source_type=source_type)
    out = {
        "status": "PAGE_FAILED",
        "http_status": fetched["status_code"],
        "final_url": fetched["final_url"],
        "page_title": fetched["title"],
        "documents_found": [],
        "documents_downloaded": [],
        "evidence": "",
        "error": fetched["error"],
    }

    if not fetched["ok"]:
        return out

    page_state, note = classify_text(fetched["text"])
    if page_state != "OK":
        out["status"] = page_state
        out["evidence"] = note
        return out

    docs = extract_document_links(fetched["final_url"], fetched["text"])
    out["documents_found"] = docs[:20]

    br, ev = find_evidence(fetched["text"])
    if br.startswith("FOUND"):
        out["status"] = br
        out["evidence"] = ev
        return out

    for doc_url in docs[:10]:
        local_path, err = download_file(doc_url)
        if not local_path:
            continue
        out["documents_downloaded"].append(str(local_path))
        if local_path.suffix.lower() == ".pdf":
            text = text_from_pdf(local_path)
        else:
            try:
                text = local_path.read_text(errors="ignore")
            except Exception:
                text = ""
        br, ev = find_evidence(text)
        if br.startswith("FOUND"):
            out["status"] = br
            out["evidence"] = ev
            return out

    out["status"] = "NO_EVIDENCE"
    return out


# ── per-city orchestration ────────────────────────────────────────────────────

def research_city(city, state, source_type, url):
    result = {
        "municipality": city,
        "state": state,
        "source_type": source_type,
        "url_checked": url,
        "BR": "UNKNOWN",
        "status_reason": "NO_EVIDENCE",
        "http_status": None,
        "final_url": url,
        "page_title": "",
        "documents_found": [],
        "documents_downloaded": [],
        "evidence": "",
        "error": "",
    }

    logging.info(f"START {city}, {state} | {source_type} | {url}")

    # ── no source ──────────────────────────────────────────────────────────────
    if source_type == "none":
        result["status_reason"] = "NOT_FOUND"
        result["evidence"] = "No source available"
        return result

    # ── municode ───────────────────────────────────────────────────────────────
    if source_type == "municode":
        # 1. Try the direct Municode URL (renders via JS → often fails plain requests, but try)
        direct = process_direct(url, source_type="municode")
        if direct["status"].startswith("FOUND"):
            result.update(direct)
            result["BR"] = "Yes" if "YES" in direct["status"] else "No"
            result["status_reason"] = "FOUND"
            return result

        # 2. DuckDuckGo search for zoning PDF / text version
        time.sleep(2)   # brief pause before search
        candidates = ddg_search_candidates(city, state)
        for cand in candidates:
            if not cand or "municode.com" in cand:
                continue   # skip JS-rendered Municode pages
            cand_result = process_direct(cand, source_type="")
            if cand_result["status"].startswith("FOUND"):
                result.update(cand_result)
                result["BR"] = "Yes" if "YES" in cand_result["status"] else "No"
                result["status_reason"] = "FOUND"
                return result
            time.sleep(1.5)

        # 3. Selenium render of Municode as final attempt
        brow, err = browser_fetch(url)
        if brow:
            br, ev = find_evidence(brow["html"])
            result["final_url"] = brow["final_url"]
            if br.startswith("FOUND"):
                result["BR"] = "Yes" if "YES" in br else "No"
                result["status_reason"] = "FOUND"
                result["evidence"] = ev
                return result
            result["status_reason"] = "NO_EVIDENCE"
            result["evidence"] = ev
        else:
            result["status_reason"] = "PAGE_FAILED"
            result["error"] = err
        return result

    # ── .gov  →  deep crawl ───────────────────────────────────────────────────
    if source_type == "gov":
        deep = process_gov_deep(url, source_type="gov")
        # process_gov_deep uses "status" key internally; normalise to status_reason
        deep_status = deep.pop("status", "NO_EVIDENCE")
        result.update(deep)
        if deep_status.startswith("FOUND"):
            result["BR"] = "Yes" if "YES" in deep_status else "No"
            result["status_reason"] = "FOUND"
        else:
            result["status_reason"] = deep_status
        return result

    # ── ecode360 / amlegal  →  cloudscraper ──────────────────────────────────
    direct = process_direct(url, source_type=source_type)
    result.update(direct)

    if direct["status"].startswith("FOUND"):
        result["BR"] = "Yes" if "YES" in direct["status"] else "No"
        result["status_reason"] = "FOUND"
        return result

    if direct["status"] == "PAGE_FAILED":
        result["status_reason"] = "PAGE_FAILED"
        # Selenium last resort
        brow, err = browser_fetch(url)
        if brow:
            br, ev = find_evidence(brow["html"])
            result["final_url"] = brow["final_url"]
            if br.startswith("FOUND"):
                result["BR"] = "Yes" if "YES" in br else "No"
                result["status_reason"] = "FOUND"
                result["evidence"] = ev
            else:
                result["status_reason"] = "NO_EVIDENCE"
                result["evidence"] = ev
        else:
            result["error"] = err
        return result

    result["status_reason"] = direct.get("status", "NO_EVIDENCE")
    return result


# ── main ──────────────────────────────────────────────────────────────────────

def save_results(results):
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "municipality", "state", "source_type", "BR", "status_reason",
                "http_status", "final_url", "page_title", "documents_found",
                "documents_downloaded", "evidence", "error",
            ],
        )
        writer.writeheader()
        for r in results:
            row = {
                "municipality":         r.get("municipality", ""),
                "state":                r.get("state", ""),
                "source_type":          r.get("source_type", ""),
                "BR":                   r.get("BR", "UNKNOWN"),
                "status_reason":        r.get("status_reason", r.get("status", "")),
                "http_status":          r.get("http_status", ""),
                "final_url":            r.get("final_url", ""),
                "page_title":           r.get("page_title", ""),
                "documents_found":      " | ".join(r.get("documents_found", [])),
                "documents_downloaded": " | ".join(r.get("documents_downloaded", [])),
                "evidence":             r.get("evidence", ""),
                "error":                r.get("error", ""),
            }
            writer.writerow(row)


def main():
    print(f"auto_research_tx_br_v4  —  {len(CITIES)} TX cities")
    print(f"  cloudscraper : {'YES' if _SCRAPER else 'NO (pip install cloudscraper)'}")
    print(f"  DuckDuckGo   : {'YES' if _DDGS else 'NO (pip install duckduckgo-search)'}")
    print(f"  pdfplumber   : {'YES' if pdfplumber else 'NO (pip install pdfplumber)'}")
    print(f"  Selenium     : {'YES' if webdriver else 'NO (pip install selenium)'}")
    print(f"  Output JSON  : {OUTPUT_JSON}")
    print(f"  Output CSV   : {OUTPUT_CSV}")
    print(f"  Log          : {LOG_FILE}\n")

    results = []
    for idx, (city, state, source_type, url) in enumerate(CITIES, start=1):
        print(f"[{idx:3d}/{len(CITIES)}] {city:<26} ({source_type:<8}) ... ", end="", flush=True)
        try:
            result = research_city(city, state, source_type, url)
        except Exception as e:
            logging.exception(f"Unhandled error for {city}")
            result = {
                "municipality": city, "state": state, "source_type": source_type,
                "url_checked": url, "BR": "UNKNOWN", "status_reason": "PAGE_FAILED",
                "http_status": None, "final_url": url, "page_title": "",
                "documents_found": [], "documents_downloaded": [],
                "evidence": f"Unhandled error: {e}", "error": str(e),
            }

        results.append(result)
        br_label = result["BR"]
        status   = result["status_reason"]
        print(f"{status:<18}  BR={br_label}")
        logging.info(f"END   {city}: {status} / BR={br_label}")

        # Save after every city so progress is never lost
        save_results(results)

        # Polite delay (varies by source type)
        delay = 2.0 if source_type in ("ecode360", "amlegal") else 1.5
        time.sleep(delay)

    # ── final summary ─────────────────────────────────────────────────────────
    counts = {}
    for r in results:
        counts[r["status_reason"]] = counts.get(r["status_reason"], 0) + 1

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for k in sorted(counts):
        print(f"  {k:<20}: {counts[k]}")

    found_yes = sum(1 for r in results if r["BR"] == "Yes")
    found_no  = sum(1 for r in results if r["BR"] == "No")
    unknown   = sum(1 for r in results if r["BR"] == "UNKNOWN")
    print(f"\n  BR=Yes   : {found_yes}")
    print(f"  BR=No    : {found_no}")
    print(f"  UNKNOWN  : {unknown}")
    print(f"\nSaved JSON : {OUTPUT_JSON}")
    print(f"Saved CSV  : {OUTPUT_CSV}")
    print(f"Log        : {LOG_FILE}")


if __name__ == "__main__":
    main()
