"""
check_urls.py

Tests every URL in county_urls.csv and flags dead ones.
Saves results to url_check_results.csv
"""

import csv
import time
import urllib.request
import urllib.error

INPUT_FILE  = "county_urls.csv"
OUTPUT_FILE = "url_check_results.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

def test_url(url, timeout=10):
    if not url or not url.startswith("http"):
        return "no_url", 0
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "ok", r.status
    except urllib.error.HTTPError as e:
        return f"http_{e.code}", e.code
    except Exception as e:
        err = str(e)
        if "timed out" in err:
            return "timeout", 0
        if "Name or service" in err or "No address" in err:
            return "dns_fail", 0
        return "error", 0

def main():
    rows = []
    with open(INPUT_FILE, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        for row in reader:
            rows.append(dict(row))

    print(f"Testing {len(rows)} URLs...")

    results = []
    ok = dead = blocked = skipped = 0

    for i, row in enumerate(rows):
        county = row["county"]
        state  = row["state"]
        url    = row.get("meeting_url", "").strip()
        vstatus = row.get("url_status", "")

        # Skip ones already marked as skip
        if row.get("verified", "") == "skip" or "skip" in vstatus:
            print(f"[{i+1}/{len(rows)}] SKIP: {county}, {state}")
            results.append({**row, "check_status": "skipped", "http_code": ""})
            skipped += 1
            continue

        print(f"[{i+1}/{len(rows)}] {county}, {state} — {url[:55]}...", end=" ")
        status, code = test_url(url)

        if status == "ok":
            print(f"✓ OK")
            ok += 1
        elif status in ("http_403", "http_401"):
            print(f"? BLOCKED ({code}) — may still be real")
            blocked += 1
        else:
            print(f"✗ DEAD ({status})")
            dead += 1

        results.append({**row, "check_status": status, "http_code": str(code)})
        time.sleep(0.5)

        if (i + 1) % 50 == 0:
            _write(results, fieldnames + ["check_status", "http_code"])
            print(f"  → Progress saved")

    _write(results, fieldnames + ["check_status", "http_code"])

    print(f"\n{'='*50}")
    print(f"✓ OK:           {ok}")
    print(f"? Blocked:      {blocked}")
    print(f"✗ Dead:         {dead}")
    print(f"  Skipped:      {skipped}")
    print(f"\nDead URLs:")
    for r in results:
        if r["check_status"] not in ("ok", "skipped") and not r["check_status"].startswith("http_4"):
            print(f"  {r['county']}, {r['state']}: {r['check_status']} — {r['meeting_url']}")

def _write(rows, fieldnames):
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

if __name__ == "__main__":
    main()
