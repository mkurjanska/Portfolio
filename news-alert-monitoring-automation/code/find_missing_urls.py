"""
find_missing_urls.py

For each county with a dead URL, searches DuckDuckGo to find
the correct current meeting/agenda page URL.
Saves results back to county_urls.csv.

Run this in GitHub Actions after check_urls.py finds dead URLs.
"""

import csv
import re
import time
import urllib.request
import urllib.error
import urllib.parse

INPUT_FILE  = "url_check_results.csv"
OUTPUT_FILE = "county_urls.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html",
    "Accept-Language": "en-US,en;q=0.9",
}

DEAD_STATUSES = {'http_404', 'dns_fail', 'error', 'timeout', 'no_url'}

MEETING_KEYWORDS = [
    'agenda', 'minutes', 'meeting', 'calendar', 'commission',
    'supervisors', 'council', 'legistar', 'granicus', 'civicweb',
    'boarddocs', 'civicplus', 'agendacenter', 'civicclerk'
]

def score_url(url, title):
    text = (url + " " + title).lower()
    score = sum(1 for kw in MEETING_KEYWORDS if kw in text)
    # Bonus for .gov domains
    if '.gov' in url:
        score += 2
    # Penalty for social media, news sites
    for bad in ['facebook', 'twitter', 'youtube', 'wikipedia', 'news', 'press']:
        if bad in url.lower():
            score -= 3
    return score

def ddg_search(query, max_results=5):
    encoded = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL
        )
        results = []
        for m in pattern.finditer(html):
            href = m.group(1)
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if href.startswith("http"):
                results.append({"title": title, "url": href})
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        print(f"    Search error: {e}")
        return []

def test_url(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 400
    except urllib.error.HTTPError as e:
        return e.code in (403, 405)
    except Exception:
        return False

def find_best_url(county, state, state_name):
    """Search for the county's meeting agenda page."""
    queries = [
        f"{county} County {state_name} board commissioners meeting agendas minutes site:.gov",
        f"{county} County {state_name} commissioners meeting agendas 2025",
        f"{county} County {state} board supervisors agenda minutes official",
    ]

    best_url = ""
    best_score = 0

    for query in queries:
        time.sleep(2.5)  # Be polite to DDG
        results = ddg_search(query, max_results=6)
        for r in results:
            score = score_url(r["url"], r["title"])
            if score > best_score:
                best_score = score
                best_url = r["url"]
        if best_score >= 4:
            break  # Good enough

    return best_url, best_score

def main():
    # Load check results
    rows = []
    with open(INPUT_FILE, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        for row in reader:
            rows.append(dict(row))

    print(f"Loaded {len(rows)} rows")

    # Find dead ones that need fixing
    to_fix = [
        r for r in rows
        if r.get("check_status", "") in DEAD_STATUSES
        and r.get("meeting_url", "").startswith("http")
    ]
    print(f"Counties needing URL search: {len(to_fix)}")

    fixed = 0
    not_found = []

    for i, row in enumerate(to_fix):
        county     = row["county"]
        state      = row["state"]
        state_name = row.get("state_name", state)
        old_url    = row["meeting_url"]

        print(f"[{i+1}/{len(to_fix)}] {county}, {state} (was: {old_url[:50]})...")

        new_url, score = find_best_url(county, state, state_name)

        if new_url and score >= 3:
            print(f"  ✓ Found (score={score}): {new_url[:70]}")
            # Update in main rows list
            for r in rows:
                if r["county"] == county and r["state"] == state:
                    r["meeting_url"] = new_url
                    r["url_status"]  = "found_via_search"
                    r["verified"]    = "yes"
                    r["check_status"] = "pending_recheck"
            fixed += 1
        else:
            print(f"  ✗ Not found (best score={score})")
            not_found.append(f"{county}, {state}")

        # Save progress every 20
        if (i + 1) % 20 == 0:
            _write(rows, fieldnames)
            print(f"  → Progress saved ({i+1}/{len(to_fix)})")

    _write(rows, fieldnames)

    print(f"\n{'='*50}")
    print(f"Fixed via search:  {fixed}")
    print(f"Still not found:   {len(not_found)}")
    if not_found:
        print("\nStill not found:")
        for c in not_found:
            print(f"  {c}")

def _write(rows, fieldnames):
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

if __name__ == "__main__":
    main()
