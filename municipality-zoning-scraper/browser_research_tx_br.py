#!/usr/bin/env python3
"""
browser_research_tx_br.py
=========================
Use a real Chromium browser (via Playwright) to research By-Right industrial
zoning for the 47 TX cities that were UNKNOWN after the automated run.

HOW TO INSTALL (one-time, run in your terminal):
  pip install playwright
  playwright install chromium

HOW TO RUN:
  python3 browser_research_tx_br.py

The script will:
  1. Open a browser window (headless=False so you can watch it work)
  2. Navigate to each city's Municode / eCode360 / .gov page
  3. Wait for JS to render, then extract and analyse the text
  4. Save results to  browser_research_results.json
  5. Apply confirmed findings to muni_current.csv → muni_br_tx2.csv

CONFIGURATION — edit the paths below to match your machine:
"""

import asyncio
import csv
import json
import re
import shutil
import time
from pathlib import Path

# ── EDIT THESE PATHS ─────────────────────────────────────────────────────────
SRC_CSV = Path("muni_current.csv")
DST_CSV = Path("muni_br_tx2.csv")
RESULTS_JSON = Path("browser_research_results.json")
# ─────────────────────────────────────────────────────────────────────────────

TAG = "[BR-SWEEP-BROWSER 2026-07-28]"

# ── CITIES TO RESEARCH ───────────────────────────────────────────────────────
# Format: (display_name, source_type, url_or_search_term)
# source_type: "municode" | "ecode360" | "gov" | "search"
CITIES = [
    # NOTE (portfolio redaction): originally 47 TX cities still UNKNOWN
    # after the automated pass, each with its (source_type, url_or_query)
    # target for the browser-driven follow-up. Replaced with fillers.
    ("Example City",  "municode", "https://library.municode.com/tx/example_city"),
    ("Sample Town",   "municode", "https://library.municode.com/tx/sample_town"),
]

# ── PATTERN MATCHING ─────────────────────────────────────────────────────────
# Industrial zone designations
IZ_PATTERNS = [
    r'\bM-?[123]\b', r'\bLI\b', r'\bHI\b', r'\bI-?[123]\b', r'\bIM-?[12]\b',
    r'\bBP\b', r'\bIND\b', r'\blight\s+industrial\b', r'\bheavy\s+industrial\b',
    r'\blight\s+manufactur\w*\b', r'\bheavy\s+manufactur\w*\b',
    r'\bindustrial\s+district\b', r'\bmanufacturing\s+district\b',
]

# By-right indicators
BR_PATTERNS = [
    r'\bpermitted\s+by\s+right\b', r'\bpermitted\s+use\b', r'\bby[\s-]right\b',
    r'\b(?:P|X|A|✓)\s*[=:–—]\s*(?:permitted|allowed)\b',
    r'(?:P|X|A)\s+by\s+right',
    r'\ball\s+uses?\s+(?:are\s+)?permitted\b',
    r'\bpermissive[\s-]by[\s-]default\b',
    r'(?:^\s*|\s)P(?:\s*$|\s+(?:permitted|allowed))',  # standalone P in table
    r'\bpermitted\b(?!\s+(?:only\s+)?with\s+(?:a\s+)?(?:CUP|SUP|special\s+use|conditional))',
]

# Conditional use indicators
CUP_PATTERNS = [
    r'\bconditional\s+use\s+permit\b', r'\bCUP\b', r'\bSUP\b',
    r'\bspecial\s+use\s+permit\b', r'\bspecial\s+exception\b',
    r'\ball\s+uses?\s+require\s+(?:a\s+)?(?:CUP|SUP|special)\b',
]

WINDOW = 600  # characters on either side of industrial zone mention to look for BR/CUP


def find_evidence(text):
    """Analyse plain text; return (BR_value, reason)."""
    if not text or len(text.strip()) < 100:
        return "UNKNOWN", "Page text too short or empty"

    # Find industrial zone mentions
    iz_spans = []
    for pat in IZ_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            iz_spans.append((m.start(), m.end()))

    if not iz_spans:
        return "UNKNOWN", "No industrial zone language found"

    def near_iz(match_obj):
        ms, me = match_obj.start(), match_obj.end()
        return any(abs(ms - s) <= WINDOW or abs(me - e) <= WINDOW
                   for s, e in iz_spans)

    br_hits = [m.group() for pat in BR_PATTERNS
               for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE)
               if near_iz(m)]
    cup_hits = [m.group() for pat in CUP_PATTERNS
                for m in re.finditer(pat, text, re.IGNORECASE)
                if near_iz(m)]

    if br_hits and not cup_hits:
        return "Yes", f"By-right indicators near industrial zones: {br_hits[:3]}"
    if br_hits and cup_hits:
        return "Yes", f"Mixed: by-right ({br_hits[:2]}) + conditional ({cup_hits[:2]}) — BR=Yes because P uses exist"
    if cup_hits and not br_hits:
        return "No", f"Only conditional use indicators found near industrial zones: {cup_hits[:3]}"
    return "UNKNOWN", "Industrial zones found but no clear permitted/conditional language"


# ── MUNICODE NAVIGATION HELPERS ───────────────────────────────────────────────

MUNICODE_ZONING_KEYWORDS = [
    "zoning", "land use", "land development", "development code",
    "unified development", "UDC", "UDO",
]

MUNICODE_INDUSTRIAL_KEYWORDS = [
    "industrial", "manufacturing", "light industrial", "heavy industrial",
    "I-1", "I-2", "M-1", "M-2", "LI", "HI",
]


async def municode_get_zoning_text(page, base_url):
    """
    Navigate Municode library, click into zoning chapter, extract use-table text.
    Returns plain text string or None.
    """
    try:
        await page.goto(base_url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        # Check if city exists
        content = await page.content()
        if "404" in content[:2000] or "not found" in content[:500].lower():
            return None, "City not found on Municode"

        # Find zoning link in the TOC
        links = await page.query_selector_all("a")
        zoning_link = None
        for link in links:
            text = (await link.inner_text()).strip().lower()
            if any(kw in text for kw in MUNICODE_ZONING_KEYWORDS):
                zoning_link = link
                break

        if zoning_link:
            await zoning_link.click()
            await page.wait_for_timeout(3000)

        # Now look for industrial uses section
        links = await page.query_selector_all("a")
        industrial_link = None
        for link in links:
            text = (await link.inner_text()).strip().lower()
            if any(kw in text for kw in MUNICODE_INDUSTRIAL_KEYWORDS):
                industrial_link = link
                break

        if industrial_link:
            await industrial_link.click()
            await page.wait_for_timeout(3000)

        # Extract all visible text
        text = await page.evaluate("() => document.body.innerText")
        return text, "OK"

    except Exception as e:
        return None, str(e)


async def search_duckduckgo(page, query):
    """Search DuckDuckGo and return text from top result."""
    try:
        search_url = f"https://duckduckgo.com/?q={query.replace(' ', '+')}&ia=web"
        await page.goto(search_url, wait_until="networkidle", timeout=20000)
        await page.wait_for_timeout(2000)

        # Get first result link
        result = await page.query_selector("[data-testid='result-title-a']")
        if not result:
            result = await page.query_selector(".result__a")
        if not result:
            return None, "No search results found"

        href = await result.get_attribute("href")
        if not href:
            return None, "No href on result"

        # Skip forbidden sources
        forbidden = ["wikipedia.org", "zoneomics.com", "grokipedia.com",
                     "tshaonline.org", "directory.tml.org",
                     "municipalcodeonline.com", "energyzoning.org"]
        if any(f in href for f in forbidden):
            # Try second result
            results = await page.query_selector_all("[data-testid='result-title-a']")
            for r in results[1:4]:
                href2 = await r.get_attribute("href")
                if href2 and not any(f in href2 for f in forbidden):
                    href = href2
                    break
            else:
                return None, f"Only forbidden sources found"

        await page.goto(href, wait_until="networkidle", timeout=25000)
        await page.wait_for_timeout(2000)
        text = await page.evaluate("() => document.body.innerText")
        return text, f"From: {href}"

    except Exception as e:
        return None, str(e)


async def fetch_gov(page, url):
    """Fetch a .gov or city website, do shallow crawl for zoning content."""
    try:
        await page.goto(url, wait_until="networkidle", timeout=25000)
        await page.wait_for_timeout(2000)
        text = await page.evaluate("() => document.body.innerText")

        # If landing page has little content, look for zoning links
        if len(text.strip()) < 500 or not any(kw in text.lower()
                for kw in ["industrial", "manufacturing", "zoning ordinance", "permitted"]):
            links = await page.query_selector_all("a")
            for link in links:
                link_text = (await link.inner_text()).strip().lower()
                if any(kw in link_text for kw in ["zoning", "land use", "development code", "ordinance"]):
                    href = await link.get_attribute("href")
                    if href and href.startswith("http"):
                        try:
                            await page.goto(href, wait_until="networkidle", timeout=20000)
                            await page.wait_for_timeout(2000)
                            text = await page.evaluate("() => document.body.innerText")
                            break
                        except Exception:
                            continue

        return text, f"From: {url}"
    except Exception as e:
        return None, str(e)


# ── MAIN RESEARCH LOOP ────────────────────────────────────────────────────────

async def research_all():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed.")
        print("Run:  pip install playwright && playwright install chromium")
        return []

    results = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,          # set True to hide browser window
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        for city, source_type, url_or_query in CITIES:
            print(f"\n{'─'*60}")
            print(f"  {city}  [{source_type}]")
            print(f"  {url_or_query[:80]}")

            text, fetch_note = None, ""

            if source_type == "municode":
                text, fetch_note = await municode_get_zoning_text(page, url_or_query)
            elif source_type == "ecode360":
                try:
                    await page.goto(url_or_query, wait_until="networkidle", timeout=30000)
                    await page.wait_for_timeout(3000)
                    # Click zoning / land use section
                    links = await page.query_selector_all("a")
                    for link in links:
                        lt = (await link.inner_text()).strip().lower()
                        if any(kw in lt for kw in ["zoning", "land use", "development"]):
                            await link.click()
                            await page.wait_for_timeout(2500)
                            break
                    text = await page.evaluate("() => document.body.innerText")
                    fetch_note = f"eCode360: {url_or_query}"
                except Exception as e:
                    fetch_note = str(e)
            elif source_type == "gov":
                text, fetch_note = await fetch_gov(page, url_or_query)
            else:  # search
                text, fetch_note = await search_duckduckgo(page, url_or_query)

            br_val, reason = find_evidence(text) if text else ("UNKNOWN", fetch_note)

            result = {
                "municipality": city,
                "source_type": source_type,
                "url": url_or_query,
                "BR": br_val,
                "reason": reason,
                "fetch_note": fetch_note,
                "text_length": len(text) if text else 0,
            }
            results.append(result)
            print(f"  → BR={br_val}  |  {reason[:100]}")

            # Brief pause between requests
            await asyncio.sleep(1.5)

        await browser.close()

    return results


# ── APPLY RESULTS TO CSV ──────────────────────────────────────────────────────

def apply_to_csv(results):
    if not SRC_CSV.exists():
        print(f"ERROR: Source CSV not found: {SRC_CSV}")
        return

    shutil.copy(SRC_CSV, DST_CSV)
    with open(DST_CSV, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    confirmed = [r for r in results if r["BR"] in ("Yes", "No")]
    changed = 0

    for r in confirmed:
        city_key = r["municipality"].strip().lower()
        for row in rows:
            if row.get("State", "").strip().lower() != "tx":
                continue
            if row.get("Municipality", "").strip().lower() != city_key:
                continue
            current = row.get("By_Right_Industrial_Zoning", "").strip()
            if current == r["BR"]:
                break
            row["By_Right_Industrial_Zoning"] = r["BR"]
            note = f"{TAG} {r['reason'][:200]}"
            existing = row.get("Additional_Notes", "")
            row["Additional_Notes"] = (
                note if not existing
                else existing if TAG in existing
                else f"{existing} | {note}"
            )
            changed += 1
            break

    with open(DST_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n✓ Applied {changed} confirmed results → {DST_CSV}")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  TX BR Browser Research Script")
    print("  47 UNKNOWN cities — JS-rendering via Playwright")
    print("=" * 60)

    results = asyncio.run(research_all())

    # Save raw results
    RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_JSON, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nRaw results saved → {RESULTS_JSON}")

    # Print summary
    yes_cities  = [r["municipality"] for r in results if r["BR"] == "Yes"]
    no_cities   = [r["municipality"] for r in results if r["BR"] == "No"]
    unk_cities  = [r["municipality"] for r in results if r["BR"] == "UNKNOWN"]

    print(f"\nSUMMARY")
    print(f"  BR=Yes:    {len(yes_cities):3d}  {yes_cities}")
    print(f"  BR=No:     {len(no_cities):3d}  {no_cities}")
    print(f"  UNKNOWN:   {len(unk_cities):3d}  {unk_cities}")

    # Apply to CSV
    if yes_cities or no_cities:
        apply_to_csv(results)
    else:
        print("\nNo confirmed results to apply.")


if __name__ == "__main__":
    main()