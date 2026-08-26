"""
find_county_urls.py — no search engine needed

Strategy:
  For each county, try a ranked list of known platform URL patterns.
  Verify each with a quick HTTP HEAD request.
  Save results to county_urls.csv with confidence scores.
  Resumes from where it left off if interrupted.
"""

import csv
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

INPUT_FILE  = "counties_combined.csv"
OUTPUT_FILE = "county_urls.csv"

STATE_NAMES = {
    "AL":"Alabama","AR":"Arkansas","AZ":"Arizona","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DC":"District of Columbia",
    "DE":"Delaware","FL":"Florida","GA":"Georgia","IA":"Iowa",
    "ID":"Idaho","IL":"Illinois","IN":"Indiana","KS":"Kansas",
    "KY":"Kentucky","LA":"Louisiana","MA":"Massachusetts","MD":"Maryland",
    "ME":"Maine","MI":"Michigan","MN":"Minnesota","MO":"Missouri",
    "MS":"Mississippi","MT":"Montana","NC":"North Carolina","ND":"North Dakota",
    "NE":"Nebraska","NJ":"New Jersey","NM":"New Mexico","NV":"Nevada",
    "NY":"New York","OH":"Ohio","OK":"Oklahoma","OR":"Oregon",
    "PA":"Pennsylvania","SC":"South Carolina","SD":"South Dakota",
    "TN":"Tennessee","TX":"Texas","UT":"Utah","VA":"Virginia",
    "WA":"Washington","WI":"Wisconsin","WV":"West Virginia","WY":"Wyoming",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

def slugify(text):
    """'Prince William' -> 'princewilliam'"""
    return text.lower().replace(" ", "").replace("'", "").replace(".", "").replace("-", "")

def hyphenate(text):
    """'Prince William' -> 'prince-william'"""
    return text.lower().replace(" ", "-").replace("'", "").replace(".", "")

def url_live(url, timeout=8):
    """Return True if URL returns HTTP 200 or 301/302."""
    try:
        req = urllib.request.Request(url, headers=HEADERS, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 400
    except urllib.error.HTTPError as e:
        # 403 might still mean the page exists
        return e.code in (403, 405)
    except Exception:
        return False

def candidate_urls(county, state):
    """
    Return a ranked list of (url, platform, confidence) tuples to try.
    Ordered from most to least likely.
    """
    sc  = slugify(county)       # 'princewilliam'
    hc  = hyphenate(county)     # 'prince-william'
    sl  = state.lower()         # 'va'

    candidates = [
        # --- Legistar (large counties, cities) ---
        (f"https://{sc}.legistar.com/Calendar.aspx",           "legistar",   "high"),
        (f"https://{sc}county.legistar.com/Calendar.aspx",     "legistar",   "high"),
        (f"https://{hc}.legistar.com/Calendar.aspx",           "legistar",   "high"),

        # --- Granicus ---
        (f"https://{sc}county.granicus.com/ViewPublisher.php?view_id=1",  "granicus", "high"),
        (f"https://{hc}county.granicus.com/ViewPublisher.php?view_id=1",  "granicus", "high"),

        # --- CivicPlus / CivicWeb ---
        (f"https://{sc}.civicweb.net/portal/",                 "civicweb",   "high"),
        (f"https://{hc}.civicweb.net/portal/",                 "civicweb",   "high"),
        (f"https://{sc}county.civicweb.net/portal/",           "civicweb",   "high"),

        # --- BoardDocs ---
        (f"https://go.boarddocs.com/{sl}/{sc}/Board.nsf/Public",  "boarddocs", "medium"),

        # --- Municode ---
        (f"https://library.municode.com/{sl}/{hc}_county",     "municode",   "medium"),

        # --- Direct .gov guesses ---
        (f"https://www.{sc}county{sl}.gov/meetings",           "gov_direct", "medium"),
        (f"https://www.{sc}county.gov/meetings",               "gov_direct", "medium"),
        (f"https://www.co.{hc}.{sl}.us/meetings",              "gov_direct", "medium"),
        (f"https://www.co.{hc}.{sl}.us/agendas",               "gov_direct", "medium"),
        (f"https://{sc}county{sl}.gov/agendas",                "gov_direct", "medium"),
        (f"https://www.{hc}county.gov/agendas",                "gov_direct", "medium"),
        (f"https://www.{hc}county.gov/government/meetings",    "gov_direct", "medium"),
        (f"https://www.{hc}co.gov/meetings",                   "gov_direct", "low"),
        (f"https://www.{sc}co.{sl}.gov/agendas",               "gov_direct", "low"),
    ]
    return candidates

def find_url(county, state):
    """Try each candidate URL and return the first live one."""
    for url, platform, confidence in candidate_urls(county, state):
        if url_live(url):
            print(f"    ✓ Found ({platform}): {url}")
            return url, platform, confidence
        time.sleep(0.3)
    return "", "not_found", "low"

def load_existing():
    found = {}
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                key = f"{row['county']}|{row['state']}"
                found[key] = row
    return found

def write_all(results):
    fieldnames = [
        "county","state","state_name","meeting_url","platform",
        "confidence","verified","notes","updated_at"
    ]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

def main():
    counties = []
    with open(INPUT_FILE, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            counties.append(row)

    existing = load_existing()
    results  = list(existing.values())
    already  = set(existing.keys())
    new_count = 0

    for i, row in enumerate(counties):
        county = row["county"].strip()
        state  = row["state"].strip().upper()
        key    = f"{county}|{state}"

        if key in already:
            print(f"[{i+1}/{len(counties)}] SKIP: {county}, {state}")
            continue

        state_name = STATE_NAMES.get(state, state)
        print(f"[{i+1}/{len(counties)}] {county} County, {state_name}...")

        url, platform, confidence = find_url(county, state)

        if not url:
            print(f"    ✗ No URL found")

        results.append({
            "county":      county,
            "state":       state,
            "state_name":  state_name,
            "meeting_url": url,
            "platform":    platform,
            "confidence":  confidence,
            "verified":    "no",
            "notes":       "",
            "updated_at":  datetime.now(timezone.utc).isoformat(),
        })
        already.add(key)
        new_count += 1

        # Save every 20 counties
        if new_count % 20 == 0:
            write_all(results)
            print(f"  → Progress saved ({len(results)} total)")

    write_all(results)

    found   = sum(1 for r in results if r["meeting_url"])
    missing = sum(1 for r in results if not r["meeting_url"])
    print(f"\nDone! {found} URLs found, {missing} not found out of {len(results)} counties.")
    print(f"Results saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
