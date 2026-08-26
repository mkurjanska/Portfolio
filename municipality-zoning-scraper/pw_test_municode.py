#!/usr/bin/env python3
"""
Test Municode's internal API to find zoning sections directly.
Municode hosts a REST API at library.municode.com/api/ that returns JSON —
no JavaScript rendering needed, no web search needed.
"""
import requests, json, re

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://library.municode.com/",
}

def find_municode_client(city: str, state: str):
    """Search Municode's client list for a matching city."""
    # Municode exposes a client search endpoint
    url = f"https://library.municode.com/api/search?q={requests.utils.quote(city + ' ' + state)}&type=client"
    print(f"\nSearching clients: {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        print(f"  Status: {r.status_code}")
        if r.ok:
            data = r.json()
            print(f"  Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            print(f"  Content: {json.dumps(data, indent=2)[:2000]}")
            return data
    except Exception as e:
        print(f"  Error: {e}")
    return None

def try_municode_direct_urls(city: str, state: str):
    """Try predictable Municode URL patterns — some return static HTML."""
    state_lower = state.lower()
    city_slug = re.sub(r"[^a-z0-9]", "_", city.lower())
    city_slug2 = re.sub(r"[^a-z0-9]", "-", city.lower())
    city_nospace = re.sub(r"[^a-z0-9]", "", city.lower())

    # Try various slug patterns
    slugs = [city_slug, city_slug2, city_nospace,
             city.lower().replace(" ", ""), city.lower().replace(" ", "_")]

    base_patterns = [
        "https://library.municode.com/{state}/{slug}",
        "https://library.municode.com/{state}/{slug}/codes/code_of_ordinances",
    ]

    print(f"\n\nTesting direct Municode URLs for {city}, {state}:")
    for slug in dict.fromkeys(slugs):  # unique slugs preserving order
        for pat in base_patterns:
            url = pat.format(state=state_lower, slug=slug)
            try:
                r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
                ct = r.headers.get("content-type", "")
                print(f"  [{r.status_code}] {url}")
                if r.ok and "text/html" in ct:
                    # Check if it's real content or the JS angular app
                    if "ng-app" in r.text or "Initializing application" in r.text:
                        print(f"         → Angular JS-rendered (blocked)")
                    elif len(r.text) > 5000:
                        print(f"         → Has content! ({len(r.text)} chars)")
                        return url, r.text
                    else:
                        print(f"         → Small page ({len(r.text)} chars)")
            except Exception as e:
                print(f"  [ERR] {url}: {e}")
    return None, None

def try_municode_api(city: str, state: str):
    """Try Municode's internal product/document API endpoints."""
    state_lower = state.lower()
    city_slug = re.sub(r"[^a-z0-9]", "-", city.lower())

    print(f"\n\nTesting Municode API endpoints:")

    # Try the product list endpoint
    endpoints = [
        f"https://library.municode.com/api/client?q={city}&state={state}",
        f"https://library.municode.com/api/client/search?q={city}+{state}",
        f"https://library.municode.com/api/product?clientId={city_slug}-{state_lower}",
        # The actual API used by their Angular app
        f"https://library.municode.com/api/search?q={city}+{state}+zoning+industrial&type=toc",
        f"https://library.municode.com/api/search?q={city}+{state}+industrial+permitted",
    ]

    for url in endpoints:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            ct = r.headers.get("content-type", "")
            print(f"  [{r.status_code}] {url}")
            if r.ok:
                print(f"         Content-Type: {ct}")
                if "json" in ct:
                    print(f"         JSON: {r.text[:500]}")
                elif "html" in ct:
                    print(f"         HTML snippet: {r.text[:200]}")
        except Exception as e:
            print(f"  [ERR] {e}")

def try_municode_toc_api(city: str, state: str):
    """
    Municode's Angular app calls /api/content/il/normal/codes/... to get TOC.
    Try to find the client code and fetch the table of contents.
    """
    state_lower = state.lower()

    # The actual API used in Municode's SPA — product IDs are like 'il/normal'
    # Try fetching the main page to extract the client/product IDs
    city_slug = re.sub(r"[^a-z0-9]", "_", city.lower())
    city_slug2 = re.sub(r"[^a-z0-9]", "-", city.lower())

    print(f"\n\nTrying to find Municode client code via main page redirect:")
    for slug in [city_slug2, city_slug]:
        url = f"https://library.municode.com/{state_lower}/{slug}"
        try:
            r = requests.get(url, headers={**HEADERS, "Accept": "text/html"},
                           timeout=10, allow_redirects=True)
            print(f"  [{r.status_code}] {url} → {r.url}")
            # Extract any API calls embedded in the page
            matches = re.findall(r'clientId["\s:=]+(["\']?)([a-zA-Z0-9_-]+)\1', r.text)
            if matches:
                print(f"  clientId candidates: {matches}")
            # Look for product code pattern
            prod_matches = re.findall(r'"productId"\s*:\s*"([^"]+)"', r.text)
            if prod_matches:
                print(f"  productId candidates: {prod_matches}")
        except Exception as e:
            print(f"  [ERR] {e}")

# Test with New Carlisle, IN
city, state = "New Carlisle", "IN"
print(f"=== Testing Municode access for {city}, {state} ===")

find_municode_client(city, state)
try_municode_api(city, state)
try_municode_toc_api(city, state)
url, text = try_municode_direct_urls(city, state)
if text:
    print(f"\nFound usable content at: {url}")
    print(f"First 1000 chars:\n{text[:1000]}")
