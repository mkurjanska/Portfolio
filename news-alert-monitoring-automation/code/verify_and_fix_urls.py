"""
verify_and_fix_urls.py

Runs in GitHub Actions to:
1. Test every URL in county_urls.csv
2. Flag ones that don't work
3. Try alternative URLs for failed ones
4. Apply known-good curated URLs for key counties
5. Save updated county_urls.csv with status column

Run this BEFORE the scraper.
"""

import csv
import time
import urllib.request
import urllib.error

INPUT_FILE  = "county_urls.csv"
OUTPUT_FILE = "county_urls.csv"  # Overwrites in place

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# -----------------------------------------------------------------------
# Manually verified URLs for key data center counties
# These override whatever the auto-finder found
# -----------------------------------------------------------------------
KNOWN_GOOD_URLS = {
    # NOTE (portfolio redaction): originally 65 hand-verified overrides in
    # this table specifically -- part of a county meeting-source tracking
    # system that covers 2,247 county source URLs overall (see
    # county_urls.csv in the full project). Replaced with fillers; lookup
    # logic below is unchanged.
    "Example County|ST":  "https://example-county.example/Calendar.aspx",
    "Sample County|ST":   "https://sample-county.example/AgendaCenter",
}

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

def slugify(text):
    return text.lower().replace(" ","").replace("'","").replace(".","").replace("-","")

def hyphenate(text):
    return text.lower().replace(" ","-").replace("'","").replace(".","")

def test_url(url, timeout=10):
    """
    Returns: 'ok', 'blocked_403', 'dead', 'error'
    403 from Legistar is treated as potentially_ok since they block bots
    but the page may still exist.
    """
    if not url or not url.startswith("http"):
        return "no_url"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "ok"
    except urllib.error.HTTPError as e:
        if e.code == 403:
            # Legistar always returns 403 to bots - treat as potentially valid
            return "blocked_403"
        if e.code == 404:
            return "dead_404"
        return f"http_{e.code}"
    except Exception as e:
        err = str(e)
        if "timed out" in err:
            return "timeout"
        return "error"

def find_alternative_urls(county, state):
    """Try alternative URL patterns when the main one fails."""
    sc = slugify(county)
    hc = hyphenate(county)
    sl = state.lower()

    alternatives = [
        # AgendaCenter (CivicPlus) - very common
        f"https://www.{sc}county{sl}.gov/AgendaCenter",
        f"https://www.{sc}county.gov/AgendaCenter",
        f"https://www.co.{hc}.{sl}.us/AgendaCenter",
        f"https://www.{hc}county.gov/AgendaCenter",
        f"https://{sc}county.gov/AgendaCenter",
        # Direct gov sites
        f"https://www.{sc}county{sl}.gov/government/agendas",
        f"https://www.{sc}county.gov/government/meetings",
        f"https://www.co.{hc}.{sl}.us/agendas",
        f"https://www.{hc}co.gov/agendas",
        # Legistar with county suffix
        f"https://{sc}county.legistar.com/Calendar.aspx",
        # Granicus
        f"https://{sc}county.granicus.com/ViewPublisher.php?view_id=1",
        f"https://{sc}.granicus.com/ViewPublisher.php?view_id=1",
    ]
    return alternatives

def main():
    # Load existing data
    rows = []
    fieldnames = []
    with open(INPUT_FILE, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            rows.append(dict(row))

    # Add status column if not present
    if "url_status" not in fieldnames:
        fieldnames.append("url_status")
    if "alt_url_tried" not in fieldnames:
        fieldnames.append("alt_url_tried")

    total = len(rows)
    fixed = 0
    failed = 0
    blocked = 0
    ok = 0

    for i, row in enumerate(rows):
        county = row["county"]
        state  = row["state"]
        key    = f"{county}|{state}"
        url    = row["meeting_url"]

        print(f"[{i+1}/{total}] {county}, {state}...", end=" ")

        # Step 1: Apply known-good URL if we have one
        if key in KNOWN_GOOD_URLS:
            row["meeting_url"] = KNOWN_GOOD_URLS[key]
            row["url_status"]  = "verified_manual"
            row["platform"]    = "verified"
            row["confidence"]  = "high"
            row["verified"]    = "yes"
            print(f"✓ MANUAL OVERRIDE")
            ok += 1
            continue

        # Step 2: Test the current URL
        status = test_url(url)
        row["url_status"] = status

        if status == "ok":
            row["verified"] = "yes"
            print(f"✓ OK")
            ok += 1

        elif status == "blocked_403":
            # Legistar/bot-blocking sites - can't verify but may be real
            # Mark as unverified but keep
            print(f"? BLOCKED (keeping)")
            blocked += 1

        else:
            # Dead or errored - try alternatives
            print(f"✗ {status} - trying alternatives...")
            alternatives = find_alternative_urls(county, state)
            found = False
            for alt_url in alternatives:
                alt_status = test_url(alt_url, timeout=6)
                if alt_status == "ok":
                    print(f"    ✓ Found alternative: {alt_url}")
                    row["meeting_url"]   = alt_url
                    row["url_status"]    = "fixed_alternative"
                    row["platform"]      = "alt_found"
                    row["confidence"]    = "medium"
                    row["verified"]      = "yes"
                    row["alt_url_tried"] = alt_url
                    found = True
                    fixed += 1
                    break
                time.sleep(0.2)

            if not found:
                row["url_status"]    = f"failed_{status}"
                row["verified"]      = "no"
                row["alt_url_tried"] = "all_failed"
                print(f"    ✗ No working URL found")
                failed += 1

        time.sleep(0.3)

        # Save progress every 25
        if (i + 1) % 25 == 0:
            _write(rows, fieldnames)
            print(f"  → Progress saved ({i+1}/{total})")

    _write(rows, fieldnames)

    print(f"\n{'='*50}")
    print(f"RESULTS:")
    print(f"  ✓ Verified OK:        {ok}")
    print(f"  ? Blocked (kept):     {blocked}")
    print(f"  ✓ Fixed w/alt URL:    {fixed}")
    print(f"  ✗ No URL found:       {failed}")
    print(f"  Total:                {total}")
    print(f"\nSaved to {OUTPUT_FILE}")

def _write(rows, fieldnames):
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

if __name__ == "__main__":
    main()
