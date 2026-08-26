"""
scrape_county_meetings.py

For each county in county_urls.csv, fetches the meeting/agenda page
and searches for data center keywords. Saves matches to county_meeting_hits.csv.

Runs fresh every time — overwrites county_meeting_hits.csv with current results.

Uses curl_cffi (Chrome TLS impersonation) for counties known to block urllib,
falls back to urllib for all others.
"""

import csv
import io
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# curl_cffi impersonates a real Chrome TLS fingerprint, bypassing Cloudflare/WAF 403s
try:
    from curl_cffi import requests as curl_requests
    CURL_AVAILABLE = True
    print("[INFO] curl_cffi available — will use Chrome impersonation for blocked sites")
except ImportError:
    CURL_AVAILABLE = False
    print("[WARN] curl_cffi not installed — blocked sites will still get 403s")
    print("[WARN] Run: pip install curl-cffi")

BASE_DIR    = Path(__file__).parent
URLS_FILE   = BASE_DIR / "county_urls.csv"
OUTPUT_FILE = BASE_DIR / "county_meeting_hits.csv"

KEYWORDS = [
    # existing
    "data center", "data centre", "datacenter",
    "hyperscale", "colocation", "colo facility",
    "server farm", "cloud campus",
    # new
    "data processing",
    "computer facility", "technology facility",
    "high-performance computing", "high performance computing",
    "hpc facility",
    # legislation-flavored terms
    "data center ordinance", "data center moratorium",
    "data center tax", "data center zoning",
]

# Counties that returned 403 with urllib — use curl_cffi for these
BLOCKED_COUNTIES = {
    ("Coconino",    "AZ"),
    ("Kendall",     "IL"),
    ("Elkhart",     "IN"),
    # ("St. Joseph", "IN"),  # curl 403, urllib may work better
    ("Wyandotte",   "KS"),
    ("Charles",     "MD"),
    ("Carver",      "MN"),
    ("Jackson",     "MO"),
    ("St Louis",    "MO"),
    ("Person",      "NC"),
    ("Cass",        "ND"),
    ("Grand Forks", "ND"),
    ("Douglas",     "NE"),
    ("Atlantic",    "NJ"),
    ("Mercer",      "NJ"),
    ("Middlesex",   "NJ"),
    ("Passaic",     "NJ"),
    ("Somerset",    "NJ"),
    ("Rockland",    "NY"),
    ("Tompkins",    "NY"),
    ("Franklin",    "OH"),
    ("Van Wert",    "OH"),
    ("Berkeley",    "SC"),
    ("Dorchester",  "SC"),
    ("Cameron",     "TX"),
    ("La Salle",    "TX"),
    ("Nueces",      "TX"),
    ("Reeves",      "TX"),
    ("Fauquier",    "VA"),
    ("Frederick",   "VA"),
    ("Prince Edward","VA"),
    ("Prince William","VA"),
    ("Genesee",     "NY"),
    ("Rock",        "WI"),
    # non-AgendaCenter blocked counties
    ("Santa Clara", "CA"),
    ("Larimer",     "CO"),
}

URLLIB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

FIELDNAMES = ["date", "county", "state", "meeting_url",
              "keywords_found", "context", "status"]


def fetch_with_urllib(url: str, timeout: int = 15) -> str:
    try:
        req = urllib.request.Request(url, headers=URLLIB_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        text = re.sub(r'<[^>]+>', ' ', raw)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except Exception as e:
        return f"ERROR: {e}"


def fetch_with_curl(url: str, timeout: int = 15) -> str:
    """Fetch using curl_cffi with Chrome110 TLS impersonation."""
    try:
        resp = curl_requests.get(
            url,
            impersonate="chrome110",
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return f"ERROR: HTTP Error {resp.status_code}"
        raw = resp.text
        text = re.sub(r'<[^>]+>', ' ', raw)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except Exception as e:
        return f"ERROR: {e}"


def fetch_page(url: str, county: str, state: str) -> str:
    """Choose fetch method based on whether the county is known to block urllib."""
    use_curl = CURL_AVAILABLE and (county, state) in BLOCKED_COUNTIES
    if use_curl:
        result = fetch_with_curl(url)
        # if curl also fails, note it clearly
        if result.startswith("ERROR:"):
            return f"ERROR (curl_cffi): {result[7:]}"
        return result
    else:
        return fetch_with_urllib(url)


def find_keyword_hits(text: str) -> list:
    text_lower = text.lower()
    return [kw for kw in KEYWORDS if kw in text_lower]


def extract_context(text: str, keyword: str, window: int = 150) -> str:
    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return ""
    start = max(0, idx - window)
    end   = min(len(text), idx + len(keyword) + window)
    return f"...{text[start:end].strip()}..."


def main():
    print(f"Working directory: {BASE_DIR}")
    print(f"URLs file:         {URLS_FILE}")
    print(f"File exists:       {URLS_FILE.exists()}")

    if not URLS_FILE.exists():
        raise FileNotFoundError(f"Cannot find {URLS_FILE}")

    # Check if file is an LFS pointer — if so, fetch real content via GitHub API
    raw_content = None
    with open(URLS_FILE, "r", encoding="utf-8-sig") as f:
        first_line = f.readline()
    
    if "git-lfs" in first_line:
        print("[WARN] county_urls.csv is an LFS pointer — fetching via GitHub API...")
        github_token = os.environ.get("GITHUB_TOKEN", "")
        github_repo  = os.environ.get("GITHUB_REPOSITORY", "")
        if not github_token or not github_repo:
            raise RuntimeError("LFS pointer detected but GITHUB_TOKEN/GITHUB_REPOSITORY not set")
        api_url = f"https://api.github.com/repos/{github_repo}/contents/News_Alerts/county_urls.csv"
        req = urllib.request.Request(api_url, headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.raw+json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw_content = resp.read().decode("utf-8")
        if "git-lfs" in raw_content[:100]:
            raise RuntimeError("GitHub API also returned LFS pointer — LFS object not stored")
        print(f"[OK] Fetched {len(raw_content)} bytes via GitHub API")
    
    # Load ALL counties with a valid URL
    counties = []
    if raw_content:
        f_obj = io.StringIO(raw_content)
    else:
        f_obj = open(URLS_FILE, "r", encoding="utf-8-sig")
    
    with f_obj:
        reader = csv.DictReader(f_obj)
        print(f"CSV columns: {reader.fieldnames}")
        for row in reader:
            url    = row.get("meeting_url", "").strip()
            status = row.get("check_status", "").strip().lower()
            if not url.startswith("http"):
                continue
            if status == "skipped":
                continue
            counties.append(row)

    blocked_in_list = sum(
        1 for c in counties
        if (c["county"], c["state"]) in BLOCKED_COUNTIES
    )
    print(f"\nLoaded {len(counties)} counties to scrape")
    print(f"  of which {blocked_in_list} will use curl_cffi Chrome impersonation")
    print(f"  and {len(counties) - blocked_in_list} will use standard urllib\n")

    if not counties:
        print("ERROR: No counties loaded — check the CSV file and LFS pull")
        # Write empty output file so git add never fails
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
        return

    match_count  = 0
    error_count  = 0
    all_hits     = []

    for i, county in enumerate(counties):
        name  = county["county"]
        state = county["state"]
        url   = county["meeting_url"]
        method = "curl" if (CURL_AVAILABLE and (name, state) in BLOCKED_COUNTIES) else "urllib"

        print(f"[{i+1}/{len(counties)}] {name}, {state} [{method}] — {url[:55]}...")

        time.sleep(1.5)
        text = fetch_page(url, name, state)

        if text.startswith("ERROR"):
            print(f"  → {text}")
            error_count += 1
            all_hits.append({
                "date":           datetime.now(timezone.utc).isoformat(),
                "county":         name,
                "state":          state,
                "meeting_url":    url,
                "keywords_found": "",
                "context":        "",
                "status":         text,
            })
            continue

        hits = find_keyword_hits(text)
        if hits:
            context = extract_context(text, hits[0])
            print(f"  ✓ MATCH: {hits}")
            match_count += 1
            all_hits.append({
                "date":           datetime.now(timezone.utc).isoformat(),
                "county":         name,
                "state":          state,
                "meeting_url":    url,
                "keywords_found": ", ".join(hits),
                "context":        context[:500],
                "status":         "match",
            })
        else:
            print("  → no keywords found")
            all_hits.append({
                "date":           datetime.now(timezone.utc).isoformat(),
                "county":         name,
                "state":          state,
                "meeting_url":    url,
                "keywords_found": "",
                "context":        "",
                "status":         "no_match",
            })

        # Save progress every 20 counties in case of workflow timeout
        if (i + 1) % 20 == 0:
            with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                writer.writeheader()
                writer.writerows(all_hits)
            print(f"  → Progress saved ({i+1}/{len(counties)} done, "
                  f"{match_count} matches, {error_count} errors so far)")

    # Final save
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_hits)

    print(f"\nDone!")
    print(f"  {match_count} keyword matches found")
    print(f"  {error_count} errors")
    print(f"  {len(counties) - match_count - error_count} clean (no keywords)")
    print(f"  Results saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
