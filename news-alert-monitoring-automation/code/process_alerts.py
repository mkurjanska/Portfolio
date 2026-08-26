#!/usr/bin/env python3
"""
process_alerts.py — Claude AI + multi-dataset + GitHub dedup
==============================================================

Reads newly added rows from News_Alerts/alerts.csv, fetches full article
content when not already present (the `full_text` column fetch_alerts.py
populates), runs relevance analysis, cross-references FOUR reference datasets,
produces a comprehensive report, and opens one GitHub Issue per matched article.

TWO OPERATING MODES
───────────────────
  MODE A — AI-enhanced (Claude API available)
    • Claude reads full article text + relevant dataset rows
    • Returns a structured JSON assessment:
        – article summary
        – dataset relevance (DC list, regulatory actions, county regs, state regs)
        – specific recommended changes (update status, add new facility, etc.)
    • GitHub issue body includes the full AI assessment
    • Falls through to Mode B if API call fails for any reason

  MODE B — keyword-only fallback (no API key, quota exhausted, etc.)
    • Keyword-based relevance
    • Dataset cross-reference by name/county/state string matching
    • Report lists every match found; issue body explains what to check
    • Always runs even if Mode A fails

GITHUB ISSUE DEDUPLICATION
───────────────────────────
  Before opening an issue, the script searches existing open issues for one
  whose title contains the same article URL.  If found, no duplicate is opened.

DATASETS (fetched from GitHub at runtime via GITHUB_TOKEN)
───────────────────────────────────────────────────────────
  1. Final_datasets/DC_facilities_list_UPDATED.csv          (DC facility records)
  2. Final_datasets/dc_regulatory_actions_merged.csv        (moratoria / legislation)
  3. Final_datasets/master_county_regulatory.csv            (county-level regulations)
  4. Final_datasets/legal_state_regulatiory_newest_May2026_minus_sources.csv
                                                            (state-level regulations)
  5. Final_datasets/opposition_events_combined_final.csv    (opposition event log)
  6. Final_datasets/dc_opposition_orgs_merged.csv           (opposition organizations)
  7. Final_datasets/county_org_dataset.csv                  (county nonprofit/org density)

Directory layout (relative to repo root):
  News_Alerts/
    alerts.csv           ← input  (triggers this workflow)
    processed_urls.txt   ← dedup log
    alert_report.csv     ← output: one row per article
    sources/             ← output: JSON per matched article
    index.csv            ← output: running index of DC matches

Env vars required:
  GITHUB_TOKEN    – provided automatically by Actions
  GITHUB_REPO     – e.g. "mkurjanska/Data-Centers-Community-Opposition"
  ANTHROPIC_API_KEY  – optional; enables Mode A

Dependencies: pip install requests beautifulsoup4 lxml anthropic
"""

import csv
import io
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ALERTS_CSV     = Path("News_Alerts/alerts.csv")
REPORT_CSV     = Path("News_Alerts/alert_report.csv")
SOURCES_DIR    = Path("News_Alerts/sources")
INDEX_CSV      = Path("News_Alerts/index.csv")
PROCESSED_URLS = Path("News_Alerts/processed_urls.txt")

SOURCES_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# GitHub dataset paths (relative to repo root, fetched via raw API)
# ---------------------------------------------------------------------------
DATASET_PATHS = {
    "dc_facilities":  "Final_datasets/DC_facilities_list_UPDATED.csv",
    "regulatory":     "Final_datasets/dc_regulatory_actions_merged.csv",
    "county_regs":    "Final_datasets/master_county_regulatory.csv",
    "state_regs":     "Final_datasets/legal_state_regulatiory_newest_May2026_minus_sources.csv",
    "events":         "Final_datasets/opposition_events_combined_final.csv",
    "orgs":           "Final_datasets/dc_opposition_orgs_merged.csv",
    "county_orgs":    "Final_datasets/county_org_dataset.csv",
}

# ---------------------------------------------------------------------------
# Column names in DC_facilities_list_UPDATED.csv
# ---------------------------------------------------------------------------
DC_COL_FACILITY_NUMBER = "Facility_number"
DC_COL_NAME            = "Facility_Name"
DC_COL_STATUS          = "DC_Status"
DC_COL_OPPOSITION      = "Opposition"
DC_COL_OUTCOME         = "Outcome"
DC_COL_STATE           = "State"
DC_COL_COUNTY          = "County"
DC_COL_MUNICIPALITY    = "Municipality"
DC_COL_HYPERSCALER     = "Hyperscaler"
DC_COL_ADDRESS         = "Street_Address"

# ---------------------------------------------------------------------------
# ★  RELEVANCE KEYWORDS  ★
# ---------------------------------------------------------------------------

KEYWORDS_FACILITY = [
    "data center", "data centre", "datacenter",
    "data centers", "data centres", "datacenters",
    "server farm", "server farms",
    "hyperscale", "hyperscaler", "hyperscalers",
    "colocation", "colocation facility", "colo facility",
    "computing campus", "ai campus", "cloud campus", "cloud facility",
    "network operations center",
]

KEYWORDS_FACILITY_EVENT = [
    "proposed", "proposal", "approved", "approval",
    "voted to approve", "voted in favor", "green light",
    "rezoning", "rezoned", "rezone", "zoning change", "zoning variance",
    "zoning approval", "special use permit", "conditional use permit",
    "site plan", "permit application", "permit granted", "permit approved",
    "permit denied", "under construction", "groundbreaking", "broke ground",
    "break ground", "construction begins", "construction started",
    "building permit", "expansion", "expanding", "new facility", "new campus",
    "cancelled", "cancellation", "canceled", "withdrawn", "withdrawal",
    "pulled the application", "application withdrawn",
    "project cancelled", "project canceled", "project shelved",
    "paused", "on hold", "delayed", "moratorium",
    "funded", "funding approved", "investment announced",
    "announced", "plans to build", "plans announced",
    "has received an application", "submitted an application",
    "application filed", "application received",
    "broke ground", "opened", "came online", "went online",
    "went operational", "now operational",
    "pause", "considering a pause", "consider a pause",
    "turning into", "converted to", "converting to", "repurposed",
    "restrict", "restricting", "restrictions on",
    "limiting", "limit data center", "cap on",
    "exploring", "considering", "considers",
]

KEYWORDS_LEGISLATION = [
    "bill", "senate bill", "house bill", "assembly bill",
    "ordinance", "zoning ordinance",
    "legislation", "legislative",
    "moratorium", "ban",
    "law", "statute", "regulation",
    "state law", "state ban", "federal law",
    "won't move forward", "will not move forward",
    "stalled", "failed to pass", "vetoed", "signed into law",
    "ratepayer", "ratepayers", "utility customer",
    "tax incentive", "tax abatement", "tax break", "tax exemption",
    "eminent domain",
    "committee hearing", "joint committee", "hearing testimony",
    "lawmakers", "lawmakers begin", "legislators",
]

KEYWORDS_OPPOSITION_ACTORS = [
    "residents", "neighbors", "community", "coalition",
    "advocacy group", "organizers", "activist", "activists",
    "board of supervisors", "planning commission", "planning board",
    "zoning board", "city council", "county board", "county commissioners",
    "board of commissioners", "town board", "township board",
    "selectboard", "select board",
    "city commission", "county commission", "county council",
    "county supervisors", "supervisors",
    "state legislature", "state house", "state senate",
    "councillors", "councilors", "aldermen",
]

KEYWORDS_OPPOSITION_ACTIONS = [
    "oppose", "opposed", "opposes", "opposition",
    "protest", "protests", "petition", "petitions",
    "public hearing", "town hall", "pushback",
    "voted against", "voted down", "vote to deny", "tabled", "deferred",
    "denial", "denied", "rejected", "rejects", "turned down",
    "blocked", "defeated", "overturned", "revoked", "rescinded",
    "lawsuit", "lawsuits", "filed a lawsuit", "litigation",
    "legal challenge", "legal action", "injunction",
    "court challenge", "court ruling", "court order",
    "appeal", "filed an appeal",
    "noise complaint", "noise concerns",
    "water concerns", "environmental concerns",
    "dispute", "controversy", "backlash", "outcry",
    "sound off", "pushes back", "pushback on",
    "raises concerns", "raise concerns", "raise questions",
    "weighs ban", "weighs moratorium", "weighs restrictions",
    "considers ban", "considers moratorium", "considers restrictions",
    "explores ban", "explores moratorium", "explores limiting",
    "votes to reject", "votes to deny", "votes against",
    "rejects proposal", "denies application",
    "meeting", "public meeting", "hearing", "commission meeting",
    "discussion", "dominates", "debate", "discusses",
    "weighs", "mulls", "considers options",
]

SKIP_URL_PATTERNS = [
    r"reddit\.com",
    r"ieeexplore\.ieee\.org",
    r"welcometothejungle\.com",
    r"datacenterdynamics\.com/.*academy",
    r"linkedin\.com",
    r"indeed\.com",
    r"glassdoor\.com",
    r"jobs\.",
    r"/careers?/",
    r"/jobs?/",
]

SKIP_TITLE_PATTERNS = [
    r"training guide",
    r"technician training",
    r"job posting",
    r"hiring",
    r"we are (seeking|hiring|looking for)",
    r"career opportunity",
    r"permanent.* contract",
    r"^there is currently no rss",
    r"sea ice",
    r"energy management.*deterministic",
    r"uncertainty.aware energy",
]

PROXIMITY_WINDOW = 150


# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------

def load_processed_urls() -> set[str]:
    if not PROCESSED_URLS.exists():
        return set()
    return set(PROCESSED_URLS.read_text(encoding="utf-8").splitlines())


def save_processed_url(url: str) -> None:
    with open(PROCESSED_URLS, "a", encoding="utf-8") as f:
        f.write(url.strip() + "\n")


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def read_csv_dicts(path: Path) -> tuple[list[dict], list[str]]:
    GIT_LFS_PREFIXES = ("version https://git-lfs", "oid sha256:", "size ")
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    lines = raw_text.splitlines(keepends=True)
    while lines and any(lines[0].startswith(p) for p in GIT_LFS_PREFIXES):
        lines.pop(0)
    if not lines:
        return [], []
    cleaned = "".join(lines)
    first_fields = next(csv.reader(io.StringIO(lines[0])), [])
    first_field  = first_fields[0].strip() if first_fields else ""
    has_header   = not bool(re.match(r"^\d{4}-\d{2}-\d{2}", first_field))
    if has_header:
        reader     = csv.DictReader(io.StringIO(cleaned))
        rows       = list(reader)
        fieldnames = list(reader.fieldnames or [])
    else:
        fieldnames = ["date", "title", "link", "summary", "source", "full_text"]
        reader     = csv.DictReader(io.StringIO(cleaned), fieldnames=fieldnames)
        rows       = list(reader)
    return rows, fieldnames


def parse_csv_text(text: str) -> tuple[list[dict], list[str]]:
    """Parse CSV from a string (used for GitHub-fetched datasets)."""
    reader     = csv.DictReader(io.StringIO(text))
    rows       = list(reader)
    fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _gh_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_dataset_from_github(relative_path: str) -> list[dict]:
    """
    Fetch a CSV dataset from the GitHub repo.
    Handles both regular files and Git LFS-tracked files.
    Returns list of dicts (empty list on failure).
    """
    import base64

    repo  = os.environ.get("GITHUB_REPO",  "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repo:
        print(f"  [WARN] GITHUB_REPO not set — cannot fetch {relative_path}")
        return []

    GIT_LFS_PREFIXES = ("version https://git-lfs", "oid sha256:", "size ")

    def _parse_and_strip_lfs(text: str) -> list[dict]:
        """Strip any LFS pointer lines then parse as CSV."""
        lines = text.splitlines(keepends=True)
        while lines and any(lines[0].startswith(p) for p in GIT_LFS_PREFIXES):
            lines.pop(0)
        if not lines:
            return []
        rows, _ = parse_csv_text("".join(lines))
        return rows

    # ── Strategy 1: Contents API (works for files < 1 MB, not in LFS) ──────
    api_url = f"https://api.github.com/repos/{repo}/contents/{relative_path}"
    try:
        resp = requests.get(api_url, headers=_gh_headers(), timeout=20)
        if resp.status_code == 200:
            data = resp.json()

            # Regular file: base64-encoded content inline
            if data.get("encoding") == "base64":
                text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
                # Check if it's actually an LFS pointer
                if text.startswith("version https://git-lfs"):
                    print(f"  [{relative_path}] is LFS-tracked — trying raw download...")
                else:
                    rows = _parse_and_strip_lfs(text)
                    if rows:
                        print(f"  Loaded {len(rows)} rows from {relative_path} (Contents API)")
                        return rows

            # Large file or LFS: fall through to raw URL strategies below
        elif resp.status_code != 404:
            print(f"  [WARN] Contents API returned {resp.status_code} for {relative_path}")
    except Exception as exc:
        print(f"  [WARN] Contents API error for {relative_path}: {exc}")

    # ── Strategy 2: Raw URL via media type header (bypasses LFS pointer) ────
    # Using application/vnd.github.raw+json asks GitHub to serve the actual
    # file bytes even for LFS objects (requires token with repo read scope)
    raw_api_url = api_url  # same endpoint, different Accept header
    try:
        raw_headers = dict(_gh_headers())
        raw_headers["Accept"] = "application/vnd.github.raw+json"
        resp2 = requests.get(raw_api_url, headers=raw_headers, timeout=60)
        if resp2.status_code == 200:
            rows = _parse_and_strip_lfs(resp2.text)
            if rows:
                print(f"  Loaded {len(rows)} rows from {relative_path} (raw API)")
                return rows
            else:
                print(f"  [WARN] Raw API returned empty/unparseable content for {relative_path}")
        else:
            print(f"  [WARN] Raw API returned {resp2.status_code} for {relative_path}")
    except Exception as exc:
        print(f"  [WARN] Raw API error for {relative_path}: {exc}")

    # ── Strategy 3: Git LFS batch API — get a direct download URL ───────────
    if token:
        lfs_url = f"https://github.com/{repo}.git/info/lfs/objects/batch"
        # We need the OID — get it from the Contents API pointer text
        try:
            resp3 = requests.get(api_url, headers=_gh_headers(), timeout=20)
            if resp3.status_code == 200:
                data3 = resp3.json()
                if data3.get("encoding") == "base64":
                    ptr = base64.b64decode(data3["content"]).decode("utf-8", errors="replace")
                    oid_match = re.search(r"oid sha256:([a-f0-9]{64})", ptr)
                    size_match = re.search(r"size (\d+)", ptr)
                    if oid_match and size_match:
                        oid  = oid_match.group(1)
                        size = int(size_match.group(1))
                        lfs_resp = requests.post(
                            lfs_url,
                            headers={
                                "Accept":        "application/vnd.git-lfs+json",
                                "Content-Type":  "application/vnd.git-lfs+json",
                                "Authorization": f"Bearer {token}",
                            },
                            json={"operation": "download", "transfers": ["basic"],
                                  "objects": [{"oid": oid, "size": size}]},
                            timeout=20,
                        )
                        if lfs_resp.status_code == 200:
                            lfs_data = lfs_resp.json()
                            dl_href = (lfs_data.get("objects", [{}])[0]
                                       .get("actions", {})
                                       .get("download", {})
                                       .get("href"))
                            if dl_href:
                                dl = requests.get(dl_href, timeout=60)
                                dl.raise_for_status()
                                rows = _parse_and_strip_lfs(dl.text)
                                if rows:
                                    print(f"  Loaded {len(rows)} rows from {relative_path} (LFS batch API)")
                                    return rows
        except Exception as exc:
            print(f"  [WARN] LFS batch API error for {relative_path}: {exc}")

    print(f"  [WARN] All fetch strategies failed for {relative_path} — skipping.")
    return []


def get_existing_github_issue_titles() -> set[str]:
    """
    Return set of existing open issue titles so we can dedup before creating.
    Paginates through all open issues (up to 500).
    """
    repo  = os.environ.get("GITHUB_REPO", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        return set()
    titles: set[str] = set()
    page = 1
    while page <= 10:   # max 10 pages × 50 = 500 issues
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{repo}/issues",
                headers=_gh_headers(),
                params={"state": "open", "per_page": 50, "page": page},
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"  [WARN] Issues API returned {resp.status_code}: {resp.text[:200]}")
                break
            items = resp.json()
            if not isinstance(items, list):
                print(f"  [WARN] Issues API returned unexpected type: {type(items)}: {str(items)[:200]}")
                break
            for issue in items:
                if isinstance(issue, dict):
                    titles.add(issue.get("title", ""))
            if len(items) < 50:
                break
            page += 1
        except Exception as exc:
            print(f"  [WARN] Could not fetch existing issues (page {page}): {exc}")
            break
    print(f"  Loaded {len(titles)} existing open issue titles for dedup.")
    return titles


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def extract_real_url(url: str) -> str:
    if "google.com/url" in url:
        try:
            params = parse_qs(urlparse(url).query)
            real   = params.get("url", [None])[0]
            if real:
                return unquote(real)
        except Exception:
            pass
    return url


def fetch_url_text(url: str, timeout: int = 20) -> tuple[str, str]:
    """Fetch URL, strip HTML, return (plain_text, final_url)."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": (
                "Mozilla/5.0 (compatible; DCAlertBot/3.0; "
                "+https://github.com/mkurjanska/Data-Centers-Community-Opposition)"
            )},
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "noscript", "header",
                         "footer", "nav", "aside"]):
            tag.decompose()
        text = re.sub(r"\s{2,}", " ", soup.get_text(separator=" ", strip=True))
        return text[:20000], resp.url
    except Exception as exc:
        print(f"  [WARN] Could not fetch {url}: {exc}")
        return "", url


# ---------------------------------------------------------------------------
# ★  RELEVANCE ENGINE  ★
# ---------------------------------------------------------------------------

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").replace("&amp;", "&").strip()


def should_skip(title: str, url: str) -> tuple[bool, str]:
    for pat in SKIP_URL_PATTERNS:
        if re.search(pat, url, re.I):
            return True, f"Skipped: URL pattern ({pat})"
    for pat in SKIP_TITLE_PATTERNS:
        if re.search(pat, title, re.I):
            return True, f"Skipped: title pattern ({pat})"
    return False, ""


def _windows_around_facility(text: str, tight: bool = False) -> list[str]:
    window_size = 50 if tight else PROXIMITY_WINDOW
    text_lower  = text.lower()
    windows     = [text_lower[:500]]
    for fac in KEYWORDS_FACILITY:
        start = 0
        while True:
            idx = text_lower.find(fac.lower(), start)
            if idx == -1:
                break
            lo = max(0, idx - window_size)
            hi = min(len(text_lower), idx + len(fac) + window_size)
            windows.append(text_lower[lo:hi])
            start = idx + 1
    return windows


def _any_kw_in_windows(keywords: list[str], windows: list[str]) -> list[str]:
    hits = []
    for kw in keywords:
        kw_l = kw.lower()
        if any(kw_l in w for w in windows):
            hits.append(kw)
    return hits


US_STATES = {
    "alabama","alaska","arizona","arkansas","california","colorado",
    "connecticut","delaware","florida","georgia","hawaii","idaho",
    "illinois","indiana","iowa","kansas","kentucky","louisiana","maine",
    "maryland","massachusetts","michigan","minnesota","mississippi",
    "missouri","montana","nebraska","nevada","new hampshire","new jersey",
    "new mexico","new york","north carolina","north dakota","ohio",
    "oklahoma","oregon","pennsylvania","rhode island","south carolina",
    "south dakota","tennessee","texas","utah","vermont","virginia",
    "washington","west virginia","wisconsin","wyoming","district of columbia",
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id",
    "il","in","ia","ks","ky","la","me","md","ma","mi","mn","ms",
    "mo","mt","ne","nv","nh","nj","nm","ny","nc","nd","oh","ok",
    "or","pa","ri","sc","sd","tn","tx","ut","vt","va","wa","wv",
    "wi","wy","dc",
}

LOCATION_INDICATORS = [
    "county", "township", "borough", "parish",
    "city of", "town of", "village of",
]


def _window_has_location(window: str) -> bool:
    for state in US_STATES:
        if re.search(r"\b" + re.escape(state) + r"\b", window):
            return True
    for loc in LOCATION_INDICATORS:
        if loc in window:
            return True
    if re.search(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+", window):
        return True
    return False


def check_relevance(title: str, summary: str, article_text: str) -> dict:
    combined       = " ".join([title, summary, article_text])
    combined_lower = combined.lower()
    has_article    = len(article_text.strip()) > 200

    # Whether the TITLE itself contains a location signal — if so, we relax the
    # requirement that a location must appear *within the text window* around
    # the event keyword, since the headline already anchors us to a place.
    title_has_location = _window_has_location(title)

    if re.search(r"application for a data cent", combined_lower):
        fac_hits = [kw for kw in KEYWORDS_FACILITY if kw.lower() in combined_lower]
        if fac_hits:
            return {
                "relevant": True, "criterion": "A",
                "matched_keywords": ["application for a data center"],
                "reason": "Specific facility event — application for a data center.",
            }

    if not any(kw.lower() in combined_lower for kw in KEYWORDS_FACILITY):
        return {
            "relevant": False, "criterion": None,
            "matched_keywords": [],
            "reason": "No data center facility terms found.",
        }

    windows = _windows_around_facility(combined, tight=not has_article)

    event_hits = []
    for kw in KEYWORDS_FACILITY_EVENT:
        kw_l = kw.lower()
        for w in windows:
            # Pass if: location found in window, OR location is in the title
            if kw_l in w and (_window_has_location(w) or title_has_location):
                event_hits.append(kw)
                break
    if event_hits:
        return {
            "relevant": True, "criterion": "A",
            "matched_keywords": event_hits,
            "reason": f"Specific facility event near DC term — keywords: {event_hits[:5]}",
        }

    # Also catch event keywords anywhere in the combined text when title has location
    # (handles short RSS entries where the body is just a summary snippet)
    if title_has_location:
        title_event_hits = [kw for kw in KEYWORDS_FACILITY_EVENT
                            if kw.lower() in combined_lower]
        if title_event_hits:
            return {
                "relevant": True, "criterion": "A",
                "matched_keywords": title_event_hits,
                "reason": (
                    f"Facility event in location-specific headline — "
                    f"keywords: {title_event_hits[:5]}"
                ),
            }

    legis_hits = _any_kw_in_windows(KEYWORDS_LEGISLATION, windows)
    if legis_hits:
        return {
            "relevant": True, "criterion": "B",
            "matched_keywords": legis_hits,
            "reason": f"Legislation/policy near DC term — keywords: {legis_hits[:5]}",
        }

    # Also catch legislation keywords in title even without full article
    title_legis_hits = [kw for kw in KEYWORDS_LEGISLATION if kw.lower() in title.lower()]
    if title_legis_hits and any(kw.lower() in combined_lower for kw in KEYWORDS_FACILITY):
        return {
            "relevant": True, "criterion": "B",
            "matched_keywords": title_legis_hits,
            "reason": f"Legislation keyword in headline — keywords: {title_legis_hits[:5]}",
        }

    actor_hits  = _any_kw_in_windows(KEYWORDS_OPPOSITION_ACTORS,  windows)
    action_hits = _any_kw_in_windows(KEYWORDS_OPPOSITION_ACTIONS, windows)
    if actor_hits and action_hits:
        return {
            "relevant": True, "criterion": "C",
            "matched_keywords": actor_hits + action_hits,
            "reason": (
                f"Organized opposition near DC term — "
                f"actors: {actor_hits[:3]}, actions: {action_hits[:3]}"
            ),
        }

    # Catch opposition/backlash in title even without actor+action pair in body
    title_lower = title.lower()
    title_action_hits = [kw for kw in KEYWORDS_OPPOSITION_ACTIONS if kw.lower() in title_lower]
    if title_action_hits and any(kw.lower() in combined_lower for kw in KEYWORDS_FACILITY):
        return {
            "relevant": True, "criterion": "C",
            "matched_keywords": title_action_hits,
            "reason": f"Opposition action in headline — keywords: {title_action_hits[:5]}",
        }

    return {
        "relevant": False, "criterion": None,
        "matched_keywords": [],
        "reason": (
            "Mentions data centers but no specific event, legislation, or "
            "organized opposition found near facility terms."
        ),
    }


# ---------------------------------------------------------------------------
# ★  DATASET MATCHING  ★
# ---------------------------------------------------------------------------

def _token_overlap(a: str, b: str) -> float:
    tokens_a = set(re.findall(r"\w+", a.lower()))
    tokens_b = set(re.findall(r"\w+", b.lower()))
    if not tokens_a:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a)


def _seq_sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


GENERIC_TOKENS = {
    "data", "center", "datacenter", "datacenters",
    "data center", "data centers",
    "google", "meta", "amazon", "microsoft", "apple", "oracle",
    "ai", "cloud", "campus", "facility",
}

AMBIGUOUS_LOCATION_TOKENS = {
    "washington", "new", "north", "south", "east", "west",
    "central", "national", "american", "united",
}


def match_against_dc_list(
    title: str, summary: str, article_text: str, dc_rows: list[dict]
) -> list[dict]:
    """Score DC facilities against article text. Returns top matches."""
    THRESHOLD   = 0.55
    search_text = " ".join([title, summary, article_text]).lower()

    matches = []
    for row in dc_rows:
        dc_name   = row.get(DC_COL_NAME,        "") or ""
        dc_county = row.get(DC_COL_COUNTY,      "") or ""
        dc_state  = row.get(DC_COL_STATE,        "") or ""
        dc_hyper  = row.get(DC_COL_HYPERSCALER,  "") or ""
        dc_munic  = row.get(DC_COL_MUNICIPALITY, "") or ""

        name_tokens          = set(re.findall(r"\w+", dc_name.lower()))
        meaningful_name_tokens = name_tokens - GENERIC_TOKENS
        if len(meaningful_name_tokens) < 2:
            continue

        meaningful_name = " ".join(meaningful_name_tokens)
        name_overlap    = _token_overlap(meaningful_name, search_text)
        name_sim        = _seq_sim(dc_name, title)
        name_score      = max(name_overlap, name_sim)

        county_tokens    = set(re.findall(r"\w+", dc_county.lower()))
        county_meaningful = county_tokens - AMBIGUOUS_LOCATION_TOKENS
        munic_tokens     = set(re.findall(r"\w+", dc_munic.lower()))
        munic_meaningful = munic_tokens - AMBIGUOUS_LOCATION_TOKENS

        county_in_text = (_token_overlap(" ".join(county_meaningful), search_text)
                          if county_meaningful else 0.0)
        munic_in_text  = (_token_overlap(" ".join(munic_meaningful),  search_text)
                          if munic_meaningful else 0.0)
        state_in_text  = 1.0 if dc_state.strip().lower() in search_text else 0.0

        location_score = max(county_in_text, munic_in_text) * state_in_text

        hyper_tokens = set(re.findall(r"\w+", dc_hyper.lower())) - GENERIC_TOKENS
        hyper_score  = (_token_overlap(" ".join(hyper_tokens), search_text)
                        if hyper_tokens else 0.0)

        score = (0.55 * name_score + 0.30 * location_score + 0.15 * hyper_score)

        if score < THRESHOLD or name_score < 0.35:
            continue

        EVENT_SIGNAL = (KEYWORDS_FACILITY_EVENT + KEYWORDS_LEGISLATION
                        + KEYWORDS_OPPOSITION_ACTIONS)
        if not any(kw.lower() in search_text for kw in EVENT_SIGNAL):
            continue

        confidence = ("high"   if score >= 0.75 else
                      "medium" if score >= 0.62 else "low")
        matches.append({
            "facility_number": row.get(DC_COL_FACILITY_NUMBER, ""),
            "facility_name":   dc_name,
            "dc_status":       row.get(DC_COL_STATUS,     ""),
            "dc_opposition":   row.get(DC_COL_OPPOSITION, ""),
            "dc_outcome":      row.get(DC_COL_OUTCOME,    ""),
            "state":           dc_state,
            "county":          dc_county,
            "municipality":    dc_munic,
            "hyperscaler":     dc_hyper,
            "match_score":     round(score, 3),
            "match_confidence": confidence,
        })

    matches.sort(key=lambda x: x["match_score"], reverse=True)
    matches = [m for m in matches if m["match_confidence"] != "low"]
    return matches[:3]


def match_against_regulatory(
    search_text: str, reg_rows: list[dict], dataset_label: str
) -> list[dict]:
    """
    Loose keyword match of article text against a regulatory dataset.
    Looks for state name + any regulation-type keyword overlap.
    Returns list of matching row summaries.
    """
    if not reg_rows:
        return []

    results = []
    search_lower = search_text.lower()

    # Gather column names from first row to be flexible
    if not reg_rows:
        return []
    sample = reg_rows[0]
    col_names = list(sample.keys())

    # Try to identify state, county, name columns generically
    state_cols  = [c for c in col_names if "state" in c.lower()]
    county_cols = [c for c in col_names if "county" in c.lower()]
    name_cols   = [c for c in col_names if any(x in c.lower() for x in
                   ["name", "title", "regulation", "action", "bill", "ordinance",
                    "law", "moratorium", "jurisdiction"])]

    for row in reg_rows:
        row_state  = " ".join(str(row.get(c, "")) for c in state_cols).lower()
        row_county = " ".join(str(row.get(c, "")) for c in county_cols).lower()
        row_name   = " ".join(str(row.get(c, "")) for c in name_cols).lower()

        # State must match something in the article
        if row_state and not any(s in search_lower for s in row_state.split()
                                  if len(s) > 3):
            continue

        # Name or county tokens overlap with article
        name_tokens = set(re.findall(r"\w{4,}", row_name))
        hits = [t for t in name_tokens if t in search_lower]
        if len(hits) < 2:
            continue

        results.append({
            "dataset":    dataset_label,
            "state":      row_state.strip(),
            "county":     row_county.strip(),
            "name":       row_name.strip()[:120],
            "row_data":   {k: v for k, v in row.items()},
            "match_hits": hits[:6],
        })

    results.sort(key=lambda x: len(x["match_hits"]), reverse=True)
    return results[:3]


# ---------------------------------------------------------------------------
# ★  CLAUDE AI ANALYSIS  ★
# ---------------------------------------------------------------------------

def _call_claude_api(
    article_title: str,
    article_url: str,
    article_text: str,
    dc_matches: list[dict],
    reg_matches: list[dict],
) -> dict | None:
    """
    Call Anthropic Messages API.
    Returns parsed JSON dict from Claude, or None if unavailable / fails.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    # Build a compact representation of matched dataset rows
    dc_match_text = ""
    if dc_matches:
        lines = []
        for m in dc_matches:
            lines.append(
                f"  • #{m['facility_number']} {m['facility_name']} "
                f"({m.get('municipality') or m.get('county')}, {m['state']}) | "
                f"status={m['dc_status']} | opposition={m['dc_opposition']} | "
                f"outcome={m['dc_outcome']} | hyperscaler={m['hyperscaler']} | "
                f"confidence={m['match_confidence']} (score {m['match_score']})"
            )
        dc_match_text = "MATCHED DC FACILITIES:\n" + "\n".join(lines)

    reg_match_text = ""
    if reg_matches:
        lines = []
        for r in reg_matches:
            lines.append(
                f"  • [{r['dataset']}] {r['name']} | state={r['state']} | "
                f"county={r['county']} | matched tokens: {r['match_hits']}"
            )
        reg_match_text = "MATCHED REGULATORY RECORDS:\n" + "\n".join(lines)

    # Truncate article text to keep prompt within limits
    article_snippet = article_text[:6000] if article_text else "(no full text available)"

    prompt = f"""You are an analyst for a database tracking data center development,
community opposition, and regulation in the United States.

You will be given a news article and a set of database records that may match it.
Your job is to assess the article and tell us exactly what (if anything) needs to
be updated in our datasets.

Our datasets are:
1. DC facilities list — tracks individual data center projects (proposed, under
   construction, operational, cancelled, etc.) with fields for status, opposition
   level, and outcome.
2. DC regulatory actions — tracks legislation, moratoria, and regulatory actions
   at state and local levels.
3. County-level regulatory records — zoning decisions, county ordinances.
4. State-level regulatory records — state laws and regulations.
   (Municipality-level dataset does not yet exist — flag if a municipality-level
   action should be recorded.)

ARTICLE TITLE: {article_title}
ARTICLE URL: {article_url}

ARTICLE TEXT (truncated to 6000 chars):
{article_snippet}

{dc_match_text}

{reg_match_text}

Please respond ONLY with a valid JSON object (no markdown fences) with exactly
this structure:

{{
  "summary": "<2-4 sentence plain-English summary of what happened>",
  "relevant": true or false,
  "relevance_reason": "<why relevant or not>",
  "recommended_actions": [
    {{
      "action_type": "UPDATE_DC_STATUS" | "UPDATE_DC_OPPOSITION" | "UPDATE_DC_OUTCOME" |
                     "ADD_NEW_DC" | "UPDATE_REGULATORY_ACTION" | "ADD_REGULATORY_ACTION" |
                     "UPDATE_COUNTY_REG" | "ADD_COUNTY_REG" |
                     "UPDATE_STATE_REG" | "ADD_STATE_REG" |
                     "ADD_MUNICIPALITY_REG" | "VERIFY_ONLY" | "NO_ACTION",
      "dataset": "dc_facilities" | "regulatory" | "county_regs" | "state_regs" | "municipality_regs",
      "facility_number": "<if applicable, else null>",
      "facility_name": "<if applicable, else null>",
      "description": "<specific change needed, e.g. 'Update DC_Status from Proposed to Under Construction'>",
      "confidence": "high" | "medium" | "low"
    }}
  ],
  "new_facility_details": {{
    "name": null,
    "hyperscaler": null,
    "state": null,
    "county": null,
    "municipality": null,
    "status": null,
    "notes": null
  }},
  "notes": "<any other observations>"
}}

If the article is not relevant to any of our datasets, set relevant=false and
recommended_actions to [{{"action_type":"NO_ACTION","dataset":null,"facility_number":null,"facility_name":null,"description":"Not relevant","confidence":"high"}}].
"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 1500,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        raw  = data["content"][0]["text"].strip()
        # Strip any accidental markdown fences
        raw  = re.sub(r"^```[a-z]*\n?", "", raw)
        raw  = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except Exception as exc:
        print(f"  [WARN] Claude API call failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

REPORT_FIELDNAMES = [
    "processed_at", "alert_date", "title", "url",
    "mode",            # "AI" or "keyword"
    "relevant", "criterion", "criterion_label", "reason",
    "matched_keywords",
    "ai_summary", "ai_notes",
    "ai_action_1_type", "ai_action_1_dataset", "ai_action_1_desc", "ai_action_1_conf",
    "ai_action_2_type", "ai_action_2_dataset", "ai_action_2_desc", "ai_action_2_conf",
    "ai_action_3_type", "ai_action_3_dataset", "ai_action_3_desc", "ai_action_3_conf",
    "dc_match_1_number", "dc_match_1_name", "dc_match_1_status",
    "dc_match_1_location", "dc_match_1_hyperscaler", "dc_match_1_confidence",
    "dc_match_2_number", "dc_match_2_name", "dc_match_2_status",
    "dc_match_2_location", "dc_match_2_hyperscaler", "dc_match_2_confidence",
    "dc_match_3_number", "dc_match_3_name", "dc_match_3_status",
    "dc_match_3_location", "dc_match_3_hyperscaler", "dc_match_3_confidence",
    "reg_match_1_dataset", "reg_match_1_name", "reg_match_1_state",
    "reg_match_2_dataset", "reg_match_2_name", "reg_match_2_state",
    "reg_match_3_dataset", "reg_match_3_name", "reg_match_3_state",
    "github_issue_url",
]

CRITERION_LABELS = {
    "A": "Specific facility event",
    "B": "Legislation / policy",
    "C": "Organized opposition",
    "D": "Matched known DC",
}


def append_to_report(
    alert_result: dict,
    dc_matches: list[dict],
    reg_matches: list[dict],
    ai_result: dict | None,
    issue_url: str,
) -> None:
    a  = alert_result["analysis"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    write_header = not REPORT_CSV.exists()

    mode = "AI" if ai_result else "keyword"

    row: dict = {
        "processed_at":    ts,
        "alert_date":      alert_result["date"],
        "title":           alert_result["title"],
        "url":             alert_result["final_url"],
        "mode":            mode,
        "relevant":        "Yes" if a["relevant"] else "No",
        "criterion":       a.get("criterion", ""),
        "criterion_label": CRITERION_LABELS.get(a.get("criterion", ""), ""),
        "reason":          a.get("reason", ""),
        "matched_keywords": "; ".join(a.get("matched_keywords", [])[:10]),
        "ai_summary":      "",
        "ai_notes":        "",
        "github_issue_url": issue_url,
    }

    # AI fields
    if ai_result:
        row["ai_summary"] = ai_result.get("summary", "")
        row["ai_notes"]   = ai_result.get("notes",   "")
        for i, act in enumerate(ai_result.get("recommended_actions", [])[:3], 1):
            row[f"ai_action_{i}_type"]    = act.get("action_type", "")
            row[f"ai_action_{i}_dataset"] = act.get("dataset", "")
            row[f"ai_action_{i}_desc"]    = act.get("description", "")
            row[f"ai_action_{i}_conf"]    = act.get("confidence", "")

    # Blank out unused AI action columns
    for i in range(len(ai_result.get("recommended_actions", []) if ai_result else []) + 1, 4):
        for field in ["type", "dataset", "desc", "conf"]:
            row[f"ai_action_{i}_{field}"] = ""

    # DC matches
    for i, m in enumerate(dc_matches[:3], start=1):
        loc = ", ".join(filter(None, [m.get("municipality") or m.get("county"), m.get("state")]))
        row[f"dc_match_{i}_number"]      = m.get("facility_number", "")
        row[f"dc_match_{i}_name"]        = m.get("facility_name",   "")
        row[f"dc_match_{i}_status"]      = m.get("dc_status",       "")
        row[f"dc_match_{i}_location"]    = loc
        row[f"dc_match_{i}_hyperscaler"] = m.get("hyperscaler",     "")
        row[f"dc_match_{i}_confidence"]  = f"{m.get('match_confidence','')} ({m.get('match_score','')})"
    for i in range(len(dc_matches) + 1, 4):
        for f in ["number", "name", "status", "location", "hyperscaler", "confidence"]:
            row[f"dc_match_{i}_{f}"] = ""

    # Regulatory matches
    for i, r in enumerate(reg_matches[:3], start=1):
        row[f"reg_match_{i}_dataset"] = r.get("dataset", "")
        row[f"reg_match_{i}_name"]    = r.get("name",    "")
        row[f"reg_match_{i}_state"]   = r.get("state",   "")
    for i in range(len(reg_matches) + 1, 4):
        for f in ["dataset", "name", "state"]:
            row[f"reg_match_{i}_{f}"] = ""

    with open(REPORT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def save_source_json(
    alert_result: dict,
    dc_matches: list[dict],
    reg_matches: list[dict],
    ai_result: dict | None,
) -> Path:
    ts_slug    = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe_title = re.sub(r"[^\w\-]", "_", alert_result["title"] or "alert")[:60]
    filename   = SOURCES_DIR / f"{ts_slug}_{safe_title}.json"
    payload = {
        "processed_at":  datetime.now(timezone.utc).isoformat(),
        "title":         alert_result["title"],
        "url":           alert_result["url"],
        "final_url":     alert_result["final_url"],
        "date":          alert_result["date"],
        "summary":       alert_result["summary"],
        "article_text":  alert_result["article_text"][:3000],
        "analysis":      alert_result["analysis"],
        "dc_matches":    dc_matches,
        "reg_matches":   reg_matches,
        "ai_result":     ai_result,
    }
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return filename


def update_index_csv(alert_result: dict, dc_matches: list[dict],
                     json_path: Path) -> None:
    fieldnames = [
        "processed_at", "alert_date", "title", "url",
        "facility_number", "facility_name", "dc_status",
        "state", "county", "hyperscaler",
        "match_confidence", "match_score", "json_file",
    ]
    write_header = not INDEX_CSV.exists()
    with open(INDEX_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for m in dc_matches:
            writer.writerow({
                "processed_at":    datetime.now(timezone.utc).isoformat(),
                "alert_date":      alert_result["date"],
                "title":           alert_result["title"],
                "url":             alert_result["final_url"],
                "facility_number": m["facility_number"],
                "facility_name":   m["facility_name"],
                "dc_status":       m["dc_status"],
                "state":           m["state"],
                "county":          m["county"],
                "hyperscaler":     m["hyperscaler"],
                "match_confidence":m["match_confidence"],
                "match_score":     m["match_score"],
                "json_file":       str(json_path.relative_to(Path("."))),
            })


# ---------------------------------------------------------------------------
# Category classification — assign each alert to one or more of 5 buckets
# ---------------------------------------------------------------------------

CATEGORIES = ["events", "dc_updates", "legislation", "moratoria", "organizations"]

ISSUE_TITLES = {
    "events":        "[Alert] New opposition events to verify",
    "dc_updates":    "[Alert] DC facility updates to verify",
    "legislation":   "[Alert] New legislation to verify",
    "moratoria":     "[Alert] New moratoria to verify",
    "organizations": "[Alert] Organization activity to verify",
}

ISSUE_LABELS = {
    "events":        ["alert", "events"],
    "dc_updates":    ["alert", "dc-updates"],
    "legislation":   ["alert", "legislation"],
    "moratoria":     ["alert", "moratoria"],
    "organizations": ["alert", "organizations"],
}

# Keywords that put an article into Organizations bucket
ORG_KEYWORDS = [
    "coalition", "advocacy group", "advocacy organization",
    "nonprofit", "non-profit", "grassroots",
    "community group", "community organization",
    "residents association", "ratepayer group",
    "environmental group", "environmental organization",
    "lawsuit", "litigation", "legal challenge", "legal action",
    "filed suit", "filed a lawsuit", "brought suit",
    "injunction", "court challenge",
    "lobbying", "lobby", "lobbied",
    "petition drive", "signature drive", "ballot initiative",
    "organize", "organizing", "organizers",
    "campaign", "campaigning",
]

MORATORIUM_KEYWORDS = ["moratorium", "moratoria", "building freeze", "development freeze"]


def classify_alert(
    title: str,
    summary: str,
    article_text: str,
    analysis: dict,
    dc_matches: list[dict],
) -> list[str]:
    """
    Return list of category keys this alert belongs to.
    An alert can belong to multiple categories.
    """
    categories = []
    combined   = " ".join([title, summary, article_text]).lower()
    criterion  = analysis.get("criterion", "")

    # ── Moratoria (check before legislation — more specific) ────────────────
    if any(kw in combined for kw in MORATORIUM_KEYWORDS):
        categories.append("moratoria")

    # ── Legislation (Criterion B, minus moratoria already caught above) ─────
    if criterion == "B":
        categories.append("legislation")

    # ── DC Updates (Criterion A OR matched a known facility) ────────────────
    if criterion == "A" or dc_matches:
        categories.append("dc_updates")

    # ── Organizations ────────────────────────────────────────────────────────
    if any(kw in combined for kw in ORG_KEYWORDS):
        categories.append("organizations")

    # ── Events (Criterion A or C — specific facility/opposition events) ─────
    # Also catch anything with organized opposition even if not Criterion A
    if criterion in ("A", "C"):
        categories.append("events")

    # Fallback: if relevant but nothing matched, put in events
    if not categories and analysis.get("relevant"):
        categories.append("events")

    return list(dict.fromkeys(categories))  # deduplicate, preserve order


# ---------------------------------------------------------------------------
# Per-category processed URL tracking
# ---------------------------------------------------------------------------

def _cat_processed_path(category: str) -> Path:
    return Path(f"News_Alerts/processed_urls_{category}.txt")


def load_category_processed_urls(category: str) -> set[str]:
    p = _cat_processed_path(category)
    if not p.exists():
        return set()
    return set(p.read_text(encoding="utf-8").splitlines())


def save_category_processed_url(category: str, url: str) -> None:
    with open(_cat_processed_path(category), "a", encoding="utf-8") as f:
        f.write(url.strip() + "\n")


def load_all_category_processed() -> dict[str, set[str]]:
    return {cat: load_category_processed_urls(cat) for cat in CATEGORIES}


# ---------------------------------------------------------------------------
# Match against organizations datasets
# ---------------------------------------------------------------------------

def match_against_orgs(
    search_text: str,
    org_rows: list[dict],
    county_org_rows: list[dict],
) -> list[dict]:
    """
    Match article text against organization datasets.
    Returns list of matching org summaries.
    """
    results  = []
    s_lower  = search_text.lower()

    for rows, label in [(org_rows, "dc_opposition_orgs"), (county_org_rows, "county_orgs")]:
        if not rows:
            continue
        sample    = rows[0]
        col_names = list(sample.keys())

        # Identify name/org columns flexibly
        name_cols  = [c for c in col_names if any(x in c.lower() for x in
                      ["name", "org", "organization", "group", "coalition",
                       "association", "title"])]
        state_cols = [c for c in col_names if "state" in c.lower()]
        county_cols= [c for c in col_names if "county" in c.lower()]

        for row in rows:
            org_name   = " ".join(str(row.get(c, "") or "") for c in name_cols).lower()
            org_state  = " ".join(str(row.get(c, "") or "") for c in state_cols).lower()
            org_county = " ".join(str(row.get(c, "") or "") for c in county_cols).lower()

            # State must appear in article
            if org_state:
                state_tokens = [t for t in org_state.split() if len(t) > 3]
                if state_tokens and not any(t in s_lower for t in state_tokens):
                    continue

            # Name tokens overlap
            name_tokens = set(re.findall(r"\w{4,}", org_name))
            hits = [t for t in name_tokens if t in s_lower]
            if len(hits) < 2:
                continue

            results.append({
                "dataset":    label,
                "org_name":   org_name.strip()[:120],
                "state":      org_state.strip(),
                "county":     org_county.strip(),
                "match_hits": hits[:6],
            })

    results.sort(key=lambda x: len(x["match_hits"]), reverse=True)
    return results[:3]


def match_against_events(
    search_text: str,
    event_rows: list[dict],
) -> list[dict]:
    """
    Match article text against opposition events dataset.
    Returns list of matching event summaries.
    """
    if not event_rows:
        return []
    results = []
    s_lower = search_text.lower()
    sample  = event_rows[0]
    col_names = list(sample.keys())

    name_cols   = [c for c in col_names if any(x in c.lower() for x in
                   ["name", "title", "event", "description", "action"])]
    state_cols  = [c for c in col_names if "state"  in c.lower()]
    county_cols = [c for c in col_names if "county" in c.lower()]
    date_cols   = [c for c in col_names if "date"   in c.lower()]

    for row in event_rows:
        ev_name   = " ".join(str(row.get(c, "") or "") for c in name_cols).lower()
        ev_state  = " ".join(str(row.get(c, "") or "") for c in state_cols).lower()
        ev_county = " ".join(str(row.get(c, "") or "") for c in county_cols).lower()
        ev_date   = " ".join(str(row.get(c, "") or "") for c in date_cols).lower()

        if ev_state:
            state_tokens = [t for t in ev_state.split() if len(t) > 3]
            if state_tokens and not any(t in s_lower for t in state_tokens):
                continue

        name_tokens = set(re.findall(r"\w{4,}", ev_name))
        hits = [t for t in name_tokens if t in s_lower]
        if len(hits) < 2:
            continue

        results.append({
            "event_name":  ev_name.strip()[:120],
            "state":       ev_state.strip(),
            "county":      ev_county.strip(),
            "date":        ev_date.strip(),
            "match_hits":  hits[:6],
        })

    results.sort(key=lambda x: len(x["match_hits"]), reverse=True)
    return results[:3]


# ---------------------------------------------------------------------------
# Build one GitHub issue per category
# ---------------------------------------------------------------------------

def _format_article_entry(
    ar: dict,
    dc_matches: list[dict],
    reg_matches: list[dict],
    org_matches: list[dict],
    event_matches: list[dict],
    category: str,
) -> str:
    """Format a single article as a markdown entry for an issue body."""
    a    = ar["analysis"]
    kws  = ", ".join(f"`{k}`" for k in a.get("matched_keywords", [])[:6])
    date = ar.get("date", "")[:10]
    lines = [
        f"### [{ar['title'][:100]}]({ar['final_url']})",
        f"*{date} | keywords: {kws}*",
        "",
    ]

    # AI summary if available
    ai = ar.get("ai_result")
    if ai and ai.get("summary"):
        lines.append(f"> {ai['summary']}")
        lines.append("")

    # Dataset cross-references relevant to this category
    if category in ("dc_updates", "events") and dc_matches:
        lines.append("**Matched DC facilities:**")
        for m in dc_matches:
            loc = ", ".join(filter(None, [m.get("municipality") or m.get("county"), m.get("state")]))
            lines.append(
                f"- #{m['facility_number']} **{m['facility_name']}** ({loc}) — "
                f"status: `{m['dc_status']}` | opposition: `{m['dc_opposition']}` | "
                f"outcome: `{m['dc_outcome']}` | {m['match_confidence']} confidence"
            )
        lines.append("")

    if category in ("legislation", "moratoria") and reg_matches:
        lines.append("**Matched regulatory records:**")
        for r in reg_matches:
            lines.append(
                f"- [{r['dataset']}] **{r['name']}** ({r['state']}) — "
                f"tokens: {', '.join(r['match_hits'])}"
            )
        lines.append("")

    if category == "organizations" and org_matches:
        lines.append("**Matched organizations:**")
        for o in org_matches:
            lines.append(
                f"- [{o['dataset']}] **{o['org_name']}** ({o.get('county','')}, {o['state']}) — "
                f"tokens: {', '.join(o['match_hits'])}"
            )
        lines.append("")

    if category == "events" and event_matches:
        lines.append("**Matched known events:**")
        for e in event_matches:
            lines.append(
                f"- **{e['event_name']}** ({e.get('county','')}, {e['state']}) — "
                f"tokens: {', '.join(e['match_hits'])}"
            )
        lines.append("")

    lines.append("---")
    return "\n".join(lines)


def create_category_issues(
    category_buckets: dict[str, list[dict]],
    existing_issue_titles: set[str],
    ai_available: bool,
) -> dict[str, str]:
    """
    Create one GitHub issue per category that has new alerts.
    Returns dict of category -> issue_url.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPO",  "")
    issue_urls = {}

    if not token or not repo:
        print("[WARN] GITHUB_TOKEN or GITHUB_REPO not set — skipping all issues.")
        return issue_urls

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for category in CATEGORIES:
        alerts = category_buckets.get(category, [])
        if not alerts:
            print(f"  [{category}] No new alerts — skipping issue.")
            continue

        issue_title = ISSUE_TITLES[category]

        # Dedup: skip if an issue with this exact title was opened this run
        if issue_title in existing_issue_titles:
            print(f"  [{category}] Issue already exists this run — skipping.")
            continue

        # Build body
        count = len(alerts)
        header = (
            f"## {issue_title}\n\n"
            f"*Generated: {ts} | {count} new alert(s) | "
            f"Mode: {'AI' if ai_available else 'keyword-only'}*\n\n"
            f"Please review each article below and verify whether it requires "
            f"updates to the relevant dataset.\n\n"
        )

        entries = []
        for ar in alerts:
            entries.append(_format_article_entry(
                ar=ar,
                dc_matches=ar.get("dc_matches", []),
                reg_matches=ar.get("reg_matches", []),
                org_matches=ar.get("org_matches", []),
                event_matches=ar.get("event_matches", []),
                category=category,
            ))

        body = header + "\n".join(entries)

        # GitHub issue body limit is 65536 chars — truncate gracefully
        if len(body) > 65000:
            body = body[:65000] + "\n\n*[truncated — see alert_report.csv for full list]*"

        print(f"  [{category}] Creating issue with {count} alerts...")
        resp = requests.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers=_gh_headers(),
            json={
                "title":  issue_title,
                "body":   body,
                "labels": ISSUE_LABELS[category],
            },
            timeout=15,
        )
        if resp.status_code == 201:
            url = resp.json()["html_url"]
            print(f"  [{category}] Issue opened: {url}")
            issue_urls[category] = url
            existing_issue_titles.add(issue_title)
        else:
            print(f"  [{category}] Issue creation failed: {resp.status_code} {resp.text[:200]}")

    return issue_urls



# ---------------------------------------------------------------------------
# Full-run review issue (all articles, for Claude spot-check)
# ---------------------------------------------------------------------------

FULL_REPORT_ISSUE_TITLE = "[Alert] Full run report — paste into Claude to verify"


def create_full_report_issue(
    all_processed: list[dict],
    existing_issue_titles: set[str],
    run_ts: str,
) -> str:
    """
    Create one GitHub issue containing ALL articles processed this run —
    both relevant and not-relevant — formatted as markdown tables so someone
    can paste the whole thing into Claude and ask it to spot-check the filtering.
    Returns the issue URL or "".
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPO",  "")
    if not token or not repo:
        return ""

    if FULL_REPORT_ISSUE_TITLE in existing_issue_titles:
        print("  [full-report] Issue already exists this run — skipping.")
        return ""

    relevant     = [a for a in all_processed if a["analysis"].get("relevant")]
    not_relevant = [a for a in all_processed if not a["analysis"].get("relevant")]

    sep = "\n"  # newline character for f-string use

    # ── Prompt block ────────────────────────────────────────────────────────
    prompt_block = (
        "## Full run report — " + run_ts + "\n\n"
        "**How to use this issue:**\n"
        "Copy everything below the horizontal rule and paste it into a Claude "
        "conversation with this prompt:\n\n"
        "> *\"Below is a list of news articles about data centers scraped by an "
        "automated alert system. Articles in the first table were flagged as "
        "relevant (facility events, legislation, moratoria, or organized opposition). "
        "Articles in the second table were filtered out. Please review the full list "
        "and tell me: (1) did any filtered-out articles get incorrectly excluded and "
        "should actually be relevant? (2) did any flagged articles get incorrectly "
        "included? (3) are there any patterns in what is being missed?\"*\n\n"
        "---\n\n"
        "**Run stats:** " + str(len(all_processed)) + " total | "
        + str(len(relevant)) + " relevant | "
        + str(len(not_relevant)) + " filtered out\n\n"
    )

    # ── Relevant articles table ──────────────────────────────────────────────
    rel_rows = ["## Flagged as relevant (filter said YES)\n"]
    rel_rows.append("| # | Title | Criterion | Keywords | DC match |")
    rel_rows.append("|---|-------|-----------|----------|----------|")
    for i, ar in enumerate(relevant, 1):
        a     = ar["analysis"]
        t     = ar["title"][:80].replace("|", "/")
        url   = ar.get("final_url", ar.get("url", ""))
        crit  = str(a.get("criterion", "?")) + " — " + CRITERION_LABELS.get(a.get("criterion", ""), "")
        kws   = ", ".join(a.get("matched_keywords", [])[:4])[:60].replace("|", "/")
        dc_m  = ar.get("dc_matches") or []
        dc    = dc_m[0].get("facility_name", "")[:40].replace("|", "/") if dc_m else "—"
        rel_rows.append("| " + str(i) + " | [" + t + "](" + url + ") | " + crit + " | " + kws + " | " + dc + " |")
    rel_rows.append("")

    # ── Not-relevant articles table ──────────────────────────────────────────
    nr_rows = ["## Filtered out (filter said NO)\n"]
    nr_rows.append("| # | Title | Filter reason |")
    nr_rows.append("|---|-------|---------------|")
    for i, ar in enumerate(not_relevant, 1):
        t      = ar["title"][:80].replace("|", "/")
        url    = ar.get("final_url", ar.get("url", ""))
        reason = ar["analysis"].get("reason", "")[:100].replace("|", "/")
        nr_rows.append("| " + str(i) + " | [" + t + "](" + url + ") | " + reason + " |")
    nr_rows.append("")

    body = (
        prompt_block
        + "\n".join(rel_rows)
        + "\n"
        + "\n".join(nr_rows)
        + "\n---\n*Auto-generated by process_alerts.py*"
    )

    # GitHub body limit — if over, drop the not-relevant table with a note
    if len(body) > 65000:
        body = (
            prompt_block
            + "\n".join(rel_rows)
            + "\n## Filtered out\n"
            + str(len(not_relevant)) + " articles filtered out — "
            "body limit reached. See alert_report.csv for full list.\n"
            + "\n---\n*Auto-generated by process_alerts.py*"
        )

    print("  [full-report] Creating issue (" + str(len(all_processed)) + " articles)...")
    resp = requests.post(
        "https://api.github.com/repos/" + repo + "/issues",
        headers=_gh_headers(),
        json={
            "title":  FULL_REPORT_ISSUE_TITLE,
            "body":   body,
            "labels": ["alert", "review"],
        },
        timeout=15,
    )
    if resp.status_code == 201:
        url = resp.json()["html_url"]
        print("  [full-report] Issue opened: " + url)
        existing_issue_titles.add(FULL_REPORT_ISSUE_TITLE)
        return url
    else:
        print("  [full-report] Issue creation failed: " + str(resp.status_code) + " " + resp.text[:200])
        return ""


# ---------------------------------------------------------------------------
# New-row detection
# ---------------------------------------------------------------------------

def get_new_alert_rows() -> list[dict]:
    GIT_LFS_PREFIXES = ("version https://git-lfs", "oid sha256:", "size ")

    result = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD", "--", str(ALERTS_CSV)],
        capture_output=True, text=True,
    )
    added_raw = [
        line[1:]
        for line in result.stdout.splitlines()
        if line.startswith("+")
        and not line.startswith("+++")
        and not any(line[1:].startswith(p) for p in GIT_LFS_PREFIXES)
    ]

    if not ALERTS_CSV.exists():
        print(f"[INFO] {ALERTS_CSV} does not exist yet — nothing to process.")
        return []

    all_rows, _ = read_csv_dicts(ALERTS_CSV)

    if not added_raw:
        print("No new data lines in diff — using all rows (dedup will skip already-processed).")
        return all_rows

    added_set  = set(added_raw)
    fieldnames = ["date", "title", "link", "summary", "source", "full_text"]

    with open(ALERTS_CSV, newline="", encoding="utf-8") as f:
        raw_content = f.read()
    raw_lines = [l for l in raw_content.splitlines(keepends=True)
                 if not any(l.startswith(p) for p in GIT_LFS_PREFIXES)]

    reader   = csv.DictReader(io.StringIO("".join(raw_lines)), fieldnames=fieldnames)
    new_rows = [
        dict(row) for row in reader
        if any(
            ",".join(row.get(f, "") or "" for f in fieldnames) in al
            or al in ",".join(row.get(f, "") or "" for f in fieldnames)
            for al in added_set
        )
    ]

    if not new_rows:
        print("Could not match diff lines — using all rows.")
        return all_rows

    print(f"Found {len(new_rows)} newly added row(s) via git diff.")
    return new_rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import sys

    if "--reset" in sys.argv:
        print("Resetting report, processed_urls files...")
        REPORT_CSV.unlink(missing_ok=True)
        PROCESSED_URLS.write_text("", encoding="utf-8")
        for cat in CATEGORIES:
            _cat_processed_path(cat).unlink(missing_ok=True)
        print("Reset complete. Re-run without --reset to process alerts.")
        return

    # ── 1. Load new rows ────────────────────────────────────────────────────
    new_rows = get_new_alert_rows()
    if not new_rows:
        print("Nothing to process.")
        return

    # ── 2. Load dedup trackers ──────────────────────────────────────────────
    processed_urls     = load_processed_urls()
    cat_processed      = load_all_category_processed()
    print(f"Dedup: {len(processed_urls)} URLs already processed globally.")

    # ── 3. Load reference datasets from GitHub ──────────────────────────────
    print("\nFetching reference datasets from GitHub...")
    dc_rows        = fetch_dataset_from_github(DATASET_PATHS["dc_facilities"])
    reg_rows       = fetch_dataset_from_github(DATASET_PATHS["regulatory"])
    county_rows    = fetch_dataset_from_github(DATASET_PATHS["county_regs"])
    state_rows     = fetch_dataset_from_github(DATASET_PATHS["state_regs"])
    event_rows     = fetch_dataset_from_github(DATASET_PATHS["events"])
    org_rows       = fetch_dataset_from_github(DATASET_PATHS["orgs"])
    county_org_rows= fetch_dataset_from_github(DATASET_PATHS["county_orgs"])

    # Fallback: try loading DC list from local paths
    if not dc_rows:
        for local_dc in [
            Path("News_Alerts/DC_facilities_list_UPDATED.csv"),
            Path("News_Alerts/DC_facilities_list.csv"),
            Path("Final_datasets/DC_facilities_list_UPDATED.csv"),
        ]:
            if local_dc.exists():
                dc_rows, _ = read_csv_dicts(local_dc)
                print(f"  Loaded {len(dc_rows)} facilities from local fallback: {local_dc}")
                break

    print(f"\nDataset summary:")
    print(f"  DC facilities  : {len(dc_rows)} rows")
    print(f"  Regulatory     : {len(reg_rows)} rows")
    print(f"  County regs    : {len(county_rows)} rows")
    print(f"  State regs     : {len(state_rows)} rows")
    print(f"  Events         : {len(event_rows)} rows")
    print(f"  Orgs           : {len(org_rows)} rows")
    print(f"  County orgs    : {len(county_org_rows)} rows")

    # ── 4. Pre-fetch existing GitHub issue titles for dedup ─────────────────
    print("\nFetching existing GitHub issues for dedup...")
    existing_issue_titles = get_existing_github_issue_titles()

    # ── 5. Check whether Claude API is available ────────────────────────────
    claude_available = bool(os.environ.get("ANTHROPIC_API_KEY", ""))
    print(f"\nClaude API: {'available (Mode A)' if claude_available else 'not configured (Mode B)'}")

    # ── 6. Process each alert — classify and match ──────────────────────────
    # category_buckets holds enriched alert dicts ready for issue formatting
    category_buckets: dict[str, list[dict]] = {cat: [] for cat in CATEGORIES}
    all_processed: list[dict] = []  # every article this run, for full-report issue

    n_skipped = n_dupes = n_relevant = 0

    for alert in new_rows:
        title       = _strip_html(alert.get("title",    "") or "")
        raw_url     = alert.get("link", "") or alert.get("url", "") or ""
        date        = alert.get("date", "") or ""
        summary     = _strip_html(alert.get("summary",  "") or "")
        stored_text = alert.get("full_text", "") or ""

        real_url  = extract_real_url(raw_url)
        dedup_key = real_url or title

        if dedup_key and dedup_key in processed_urls:
            print(f"  [SKIP-DUPE] {title[:70]}")
            n_dupes += 1
            continue

        print(f"\n→ {title[:80] or real_url}")

        skip, skip_reason = should_skip(title, real_url)
        if skip:
            print(f"  {skip_reason}")
            n_skipped += 1
            if dedup_key:
                save_processed_url(dedup_key)
                processed_urls.add(dedup_key)
            continue

        # ── Get article text ─────────────────────────────────────────────────
        article_text = stored_text
        final_url    = real_url
        if not article_text and real_url:
            article_text, final_url = fetch_url_text(real_url)

        # ── Relevance check ──────────────────────────────────────────────────
        analysis = check_relevance(title, summary, article_text)
        relevant = analysis["relevant"]
        print(f"  Relevant: {relevant} | {analysis['reason'][:90]}")

        if not relevant:
            not_rel_record = {
                "title": title, "url": raw_url, "final_url": final_url,
                "date": date, "summary": summary,
                "article_text": article_text, "analysis": analysis,
                "dc_matches": [], "reg_matches": [], "org_matches": [],
                "event_matches": [], "ai_result": None, "categories": [],
            }
            all_processed.append(not_rel_record)
            append_to_report(not_rel_record, [], [], None, "")
            if dedup_key:
                save_processed_url(dedup_key)
                processed_urls.add(dedup_key)
            continue

        n_relevant += 1
        search_text = " ".join([title, summary, article_text])

        # ── Dataset matching ─────────────────────────────────────────────────
        dc_matches = match_against_dc_list(title, summary, article_text, dc_rows) if dc_rows else []

        reg_matches_all: list[dict] = []
        for rows, label in [
            (reg_rows,   "regulatory actions"),
            (county_rows,"county regulations"),
            (state_rows, "state regulations"),
        ]:
            reg_matches_all.extend(match_against_regulatory(search_text, rows, label))
        reg_matches_all.sort(key=lambda x: len(x["match_hits"]), reverse=True)
        reg_matches = reg_matches_all[:3]

        org_matches   = match_against_orgs(search_text, org_rows, county_org_rows)
        event_matches = match_against_events(search_text, event_rows)

        if dc_matches:
            print(f"  DC matches: {[m['facility_name'][:40] for m in dc_matches]}")
        if reg_matches:
            print(f"  Reg matches: {[r['name'][:40] for r in reg_matches]}")
        if org_matches:
            print(f"  Org matches: {[o['org_name'][:40] for o in org_matches]}")
        if event_matches:
            print(f"  Event matches: {[e['event_name'][:40] for e in event_matches]}")

        # ── Mode A: Claude AI analysis ───────────────────────────────────────
        ai_result = None
        if claude_available:
            ai_result = _call_claude_api(
                article_title=title,
                article_url=final_url,
                article_text=article_text,
                dc_matches=dc_matches,
                reg_matches=reg_matches,
            )
            if ai_result:
                print(f"  AI: {len(ai_result.get('recommended_actions', []))} action(s)")
            else:
                print("  AI call failed — keyword mode.")

        # ── Classify into categories ─────────────────────────────────────────
        categories = classify_alert(title, summary, article_text, analysis, dc_matches)
        print(f"  Categories: {categories}")

        # Build enriched alert record (carries all match data for issue formatting)
        enriched = {
            "title":         title,
            "url":           raw_url,
            "final_url":     final_url,
            "date":          date,
            "summary":       summary,
            "article_text":  article_text,
            "analysis":      analysis,
            "dc_matches":    dc_matches,
            "reg_matches":   reg_matches,
            "org_matches":   org_matches,
            "event_matches": event_matches,
            "ai_result":     ai_result,
        }

        # Add to each category bucket if not already seen in a previous run
        for cat in categories:
            if dedup_key and dedup_key in cat_processed[cat]:
                print(f"  [SKIP-{cat}] Already included in a previous {cat} issue.")
                continue
            category_buckets[cat].append(enriched)

        # ── Store in full-run list ───────────────────────────────────────────
        enriched["categories"] = categories
        all_processed.append(enriched)

        # ── Save JSON + index ────────────────────────────────────────────────
        if dc_matches or reg_matches:
            json_path = save_source_json(enriched, dc_matches, reg_matches, ai_result)
            update_index_csv(enriched, dc_matches, json_path)

        # ── Append to CSV report ─────────────────────────────────────────────
        append_to_report(enriched, dc_matches, reg_matches, ai_result, "")

        # ── Mark globally processed ──────────────────────────────────────────
        if dedup_key:
            save_processed_url(dedup_key)
            processed_urls.add(dedup_key)

    # ── 7. Create one GitHub issue per category ──────────────────────────────
    print("\nCreating category issues...")
    issue_urls = create_category_issues(
        category_buckets, existing_issue_titles, claude_available
    )

    # ── 8. Create full-run review issue ────────────────────────────────────
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    full_report_url = create_full_report_issue(
        all_processed, existing_issue_titles, run_ts
    )

    # Mark all bucketed alerts as processed per-category
    for cat, alerts in category_buckets.items():
        for ar in alerts:
            key = ar.get("final_url") or ar.get("url") or ar.get("title", "")
            if key:
                save_category_processed_url(cat, key)

    # ── 9. Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"Total rows   : {len(new_rows)}")
    print(f"Dupes skipped: {n_dupes}")
    print(f"Pre-filtered : {n_skipped}")
    print(f"Relevant     : {n_relevant}")
    for cat in CATEGORIES:
        n = len(category_buckets[cat])
        url = issue_urls.get(cat, "")
        print(f"  [{cat:14s}] {n:3d} alerts  {'→ ' + url if url else '(no issue)'}")
    if full_report_url:
        print(f"  Full run report: {full_report_url}")
    print("=" * 60)


if __name__ == "__main__":
    main()
