"""
verify_legistar_clients.py

Uses the Legistar Web API to verify which counties are REAL Legistar clients.
Real clients return JSON data from /v1/{client}/bodies
Fake clients return a 404 or error.

Run this once to clean up county_urls.csv.
"""

import csv
import time
import json
import urllib.request
import urllib.error

INPUT_FILE  = "county_urls.csv"
OUTPUT_FILE = "county_urls.csv"

def slugify(text):
    return text.lower().replace(" ","").replace("'","").replace(".","").replace("-","")

def check_legistar_client(client_slug):
    """Returns True if this is a real Legistar client."""
    url = f"https://webapi.legistar.com/v1/{client_slug}/bodies"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            # Real client returns a list of bodies
            return isinstance(data, list) and len(data) > 0
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False

def main():
    rows = []
    with open(INPUT_FILE, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            rows.append(dict(row))

    print(f"Loaded {len(rows)} counties")
    real_count = 0
    fake_count = 0
    kept_count = 0

    for i, row in enumerate(rows):
        county   = row["county"]
        state    = row["state"]
        url      = row.get("meeting_url", "")
        platform = row.get("platform", "")

        # Only verify Legistar URLs
        if "legistar" not in url:
            print(f"[{i+1}] SKIP (not legistar): {county}, {state}")
            kept_count += 1
            continue

        # Extract client slug from URL
        client = url.replace("https://","").split(".")[0]

        print(f"[{i+1}/{len(rows)}] Checking {client} ({county}, {state})...", end=" ")
        is_real = check_legistar_client(client)

        if is_real:
            row["url_status"] = "verified_legistar_api"
            row["verified"]   = "yes"
            print(f"✓ REAL")
            real_count += 1
        else:
            # Mark as needing fix
            row["meeting_url"] = ""
            row["url_status"]  = "fake_legistar"
            row["verified"]    = "no"
            print(f"✗ FAKE - cleared")
            fake_count += 1

        time.sleep(0.3)

        # Save progress every 30
        if (i + 1) % 30 == 0:
            _write(rows, fieldnames)
            print(f"  → Saved progress")

    _write(rows, fieldnames)
    print(f"\nDone!")
    print(f"  Real Legistar clients: {real_count}")
    print(f"  Fake (cleared):        {fake_count}")
    print(f"  Non-Legistar (kept):   {kept_count}")
    print(f"\nCounties with no URL now (need manual research):")
    for row in rows:
        if not row.get("meeting_url","").startswith("http"):
            print(f"  {row['county']}, {row['state']}")

def _write(rows, fieldnames):
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

if __name__ == "__main__":
    main()
