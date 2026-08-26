"""
process_county_hits.py
----------------------
Runs after scrape_county_meetings.py saves county_meeting_hits.csv.

1. Detects NEW hits (not already in processed_hits.txt)
2. Scrapes full content of each new hit URL
3. Saves scraped content to JSON files
4. Loads reference datasets from repo
5. Matches each hit against datasets by county/state
6. Produces a comprehensive report CSV
7. Creates ONE GitHub issue per category (not one per hit):
   - [County Scrape] DC facility updates to verify
   - [County Scrape] New facilities to verify
   - [County Scrape] New regulations to verify
   - [County Scrape] Opposition events to verify
   - [County Scrape] Monitoring — review needed
   Dedup: checks existing open issue titles so same category issue
   is never created twice in the same run or across runs.

FALLBACK: if Claude API unavailable, uses rule-based analysis.
"""

import base64
import csv
import hashlib
import io
import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
REPO_ROOT   = BASE_DIR.parent

HITS_CSV        = BASE_DIR / "county_meeting_hits.csv"
INDEX_CSV       = BASE_DIR / "index_county_scrape.csv"
SCRAPED_DIR     = BASE_DIR / "scraped_hits"
REPORT_CSV      = BASE_DIR / "county_hits_report.csv"
PROCESSED_FILE  = BASE_DIR / "processed_hits.txt"   # flat dedup log per hit

# ── categories ────────────────────────────────────────────────────────────────
CATEGORIES = ["dc_updates", "new_facility", "legislation", "opposition", "monitoring"]

ISSUE_TITLES = {
    "dc_updates":   "[County Scrape] DC facility updates to verify",
    "new_facility": "[County Scrape] New facilities to verify",
    "legislation":  "[County Scrape] New regulations/ordinances to verify",
    "opposition":   "[County Scrape] Opposition events to verify",
    "monitoring":   "[County Scrape] Items needing review",
}

ISSUE_LABELS = {
    "dc_updates":   ["county-scrape", "dc-updates"],
    "new_facility": ["county-scrape", "new-facility"],
    "legislation":  ["county-scrape", "legislation"],
    "opposition":   ["county-scrape", "opposition"],
    "monitoring":   ["county-scrape", "monitoring"],
}

# ── constants ─────────────────────────────────────────────────────────────────
JS_FALSE_POS   = "dataCenter:t.dc"
HEADERS        = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SCRAPE_TIMEOUT = 15
MIN_PARA_LEN   = 60
MAX_PARAS      = 3
PARA_WINDOW    = 1

DC_KEYWORDS = [
    "data center", "datacenter", "data centre", "colocation", "hyperscale",
    "server farm", "tax abatement", "tax agreement", "ordinance", "moratorium",
]

REPORT_FIELDS = [
    "hit_id", "date_found", "county", "state", "meeting_url", "hit_title",
    "summary", "analysis_mode", "category", "matched_facilities",
    "matched_regulations", "recommended_actions",
]

INDEX_FIELDS = [
    "hit_id", "date_found", "county", "state", "meeting_url",
    "keywords_found", "hit_title", "scraped_file",
]


# ── GitHub helpers ────────────────────────────────────────────────────────────

def _gh_headers(token):
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def gh_get(url, token, timeout=20):
    req = urllib.request.Request(url, headers=_gh_headers(token))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def gh_post(url, token, payload, timeout=15):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={**_gh_headers(token), "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_dataset_from_github(repo, rel_path, token):
    """
    Fetch a CSV dataset from GitHub. Handles LFS via 3-strategy approach
    (same as process_alerts.py).
    """
    GIT_LFS_PREFIXES = ("version https://git-lfs", "oid sha256:", "size ")

    def _strip_and_parse(text):
        lines = text.splitlines(keepends=True)
        while lines and any(lines[0].startswith(p) for p in GIT_LFS_PREFIXES):
            lines.pop(0)
        if not lines:
            return []
        return list(csv.DictReader(io.StringIO("".join(lines))))

    api_url = f"https://api.github.com/repos/{repo}/contents/{rel_path}"

    # Strategy 1: Contents API (inline base64)
    try:
        data = gh_get(api_url, token)
        if data.get("encoding") == "base64":
            text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            if not text.startswith("version https://git-lfs"):
                rows = _strip_and_parse(text)
                if rows:
                    print(f"  Loaded {len(rows)} rows from {rel_path} (Contents API)")
                    return rows
                else:
                    print(f"  [S1] Parsed 0 rows from {rel_path}")
            else:
                print(f"  [S1] LFS pointer detected for {rel_path} — trying raw API")
        else:
            print(f"  [S1] Unexpected encoding: {data.get('encoding')} for {rel_path}")
    except Exception as e:
        print(f"  [S1] Contents API failed for {rel_path}: {e}")

    # Strategy 2: Raw API header
    try:
        raw_headers = {**_gh_headers(token), "Accept": "application/vnd.github.raw+json"}
        req = urllib.request.Request(api_url, headers=raw_headers)
        with urllib.request.urlopen(req, timeout=180) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        rows = _strip_and_parse(text)
        if rows:
            print(f"  Loaded {len(rows)} rows from {rel_path} (raw API)")
            return rows
        else:
            print(f"  [S2] Parsed 0 rows from {rel_path}, text[:100]: {text[:100]}")
    except Exception as e:
        print(f"  [S2] Raw API failed for {rel_path}: {e}")

    # Strategy 3: LFS batch API
    try:
        data = gh_get(api_url, token)
        if data.get("encoding") == "base64":
            ptr = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            oid_m  = re.search(r"oid sha256:([a-f0-9]{64})", ptr)
            size_m = re.search(r"size (\d+)", ptr)
            if oid_m and size_m:
                lfs_url = f"https://github.com/{repo}.git/info/lfs/objects/batch"
                lfs_payload = json.dumps({
                    "operation": "download", "transfers": ["basic"],
                    "objects": [{"oid": oid_m.group(1), "size": int(size_m.group(1))}]
                }).encode()
                lfs_req = urllib.request.Request(
                    lfs_url, data=lfs_payload,
                    headers={
                        "Accept": "application/vnd.git-lfs+json",
                        "Content-Type": "application/vnd.git-lfs+json",
                        "Authorization": f"Bearer {token}",
                    }, method="POST"
                )
                with urllib.request.urlopen(lfs_req, timeout=20) as r:
                    lfs_data = json.loads(r.read())
                dl_href = (lfs_data.get("objects", [{}])[0]
                           .get("actions", {}).get("download", {}).get("href"))
                if dl_href:
                    dl_req = urllib.request.Request(dl_href, headers={"Authorization": f"Bearer {token}"})
                    with urllib.request.urlopen(dl_req, timeout=180) as r:
                        text = r.read().decode("utf-8", errors="replace")
                    rows = _strip_and_parse(text)
                    if rows:
                        print(f"  Loaded {len(rows)} rows from {rel_path} (LFS batch API)")
                        return rows
    except Exception as e:
        print(f"  [S3] LFS batch API failed for {rel_path}: {e}")

    print(f"  [WARN] All 3 fetch strategies failed for {rel_path} — file may not exist or token lacks access")
    return []


def get_existing_issue_titles(repo, token):
    """Fetch all open issue titles for dedup."""
    titles = set()
    page = 1
    while page <= 10:
        try:
            data = gh_get(
                f"https://api.github.com/repos/{repo}/issues"
                f"?state=open&per_page=100&page={page}",
                token
            )
            if not data:
                break
            for issue in data:
                titles.add(issue.get("title", ""))
            if len(data) < 100:
                break
            page += 1
        except Exception as e:
            print(f"  [WARN] Could not fetch existing issues: {e}")
            break
    print(f"  Loaded {len(titles)} existing open issue titles for dedup")
    return titles


# ── dedup helpers ─────────────────────────────────────────────────────────────

def make_hit_id(county, state, url):
    raw = f"{county.strip().lower()}|{state.strip().lower()}|{url.strip()}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def load_processed_ids():
    if not PROCESSED_FILE.exists():
        return set()
    return set(PROCESSED_FILE.read_text(encoding="utf-8").splitlines())


def save_processed_id(hit_id):
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(hit_id.strip() + "\n")


# ── file loading ──────────────────────────────────────────────────────────────

def load_csv_safe(path):
    for enc in ["utf-8", "latin-1", "utf-8-sig"]:
        try:
            with open(path, newline="", encoding=enc) as f:
                return list(csv.DictReader(f))
        except (UnicodeDecodeError, FileNotFoundError):
            continue
    return []


def load_hits():
    if not HITS_CSV.exists():
        return []
    rows = []
    with open(HITS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status", "").strip() != "match":
                continue
            if JS_FALSE_POS in row.get("context", ""):
                continue
            rows.append(row)
    return rows


# ── scraping ──────────────────────────────────────────────────────────────────

def scrape_url(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=SCRAPE_TIMEOUT) as resp:
            raw = resp.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        for ent, rep in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&nbsp;"," ")]:
            text = text.replace(ent, rep)
        text = re.sub(r"&#?\w+;", " ", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception as e:
        return f"[SCRAPE ERROR: {e}]"


def extract_title(content, url):
    for line in content.splitlines():
        line = line.strip()
        if len(line) > 20 and not line.startswith("["):
            return line[:120]
    return url


def is_boilerplate(para):
    lower = para.lower()
    if sum(1 for c in para if c in "{}();:[]") > 8:
        return True
    if any(p in lower for p in ["skip to main","privacy policy","powered by",
                                  "sign in","log in","all rights reserved"]):
        return True
    return False


def extract_summary(context, content, keywords):
    extra_kw = [k.strip().lower() for k in keywords.split(",") if k.strip()]
    all_kw = DC_KEYWORDS + extra_kw

    ctx = re.sub(r"[.#][\w-]+\s*\{[^}]*\}", " ", context)
    ctx = re.sub(r"\w+\.\w+\s*=\s*['\"][^'\"]*['\"];?", " ", ctx)
    ctx = re.sub(r"[ \t]{2,}", " ", ctx)
    ctx = re.sub(r"^\.*\s*", "", ctx).strip()

    parts = []
    if ctx and not ctx.startswith("[SCRAPE ERROR"):
        parts.append(f"[Page snippet]\n{ctx}")

    if content and not content.startswith("[SCRAPE ERROR"):
        raw_paras = re.split(r"\n{2,}", content)
        paras = [p.strip() for p in raw_paras
                 if len(p.strip()) >= MIN_PARA_LEN and not is_boilerplate(p.strip())]
        hit_indices = set()
        for i, para in enumerate(paras):
            if any(kw in para.lower() for kw in all_kw):
                for j in range(max(0, i - PARA_WINDOW), min(len(paras), i + PARA_WINDOW + 1)):
                    hit_indices.add(j)
        groups, result = [], []
        for idx in sorted(hit_indices)[:MAX_PARAS + PARA_WINDOW * 2]:
            if groups and idx == groups[-1][-1] + 1:
                groups[-1].append(idx)
            else:
                groups.append([idx])
        for group in groups[:MAX_PARAS]:
            combined = " ".join(paras[i] for i in group)
            if len(set(ctx.lower().split()) & set(combined.lower().split())) / max(len(combined.split()), 1) < 0.5:
                result.append(combined)
        parts.extend(result)

    return "\n\n".join(parts) if parts else content[:500].strip()


# ── matching ──────────────────────────────────────────────────────────────────

def normalize(text):
    return re.sub(r"[^a-z0-9 ]", "", str(text).lower().strip())


def match_facilities(county, state, summary, facilities):
    matches = []
    summary_lower = summary.lower()
    for f in facilities:
        if str(f.get("State","")).strip().upper() != state.strip().upper():
            continue
        if normalize(f.get("County","")) != normalize(county):
            continue
        name_words  = [w for w in normalize(f.get("Facility_Name","")).split() if len(w) > 4]
        hyper_words = [w for w in normalize(f.get("Hyperscaler","")).split() if len(w) > 4]
        score = sum(1 for w in name_words + hyper_words if w in summary_lower)
        matches.append({
            "facility_number": f.get("Facility_number",""),
            "facility_name":   f.get("Facility_Name",""),
            "status":          f.get("DC_Status",""),
            "hyperscaler":     f.get("Hyperscaler",""),
            "match_score":     score,
        })
    matches.sort(key=lambda x: x["match_score"], reverse=True)
    return matches[:5]


def match_regulations(county, state, summary, regulations):
    matches = []
    for r in regulations:
        r_state  = str(r.get("state","") or r.get("state_dc_merged","") or r.get("State","")).strip().upper()
        r_county = str(r.get("county","") or r.get("county_dc_merged","") or r.get("County","")).strip()
        if r_state != state.strip().upper():
            continue
        if normalize(r_county) != normalize(county):
            continue
        matches.append({
            "action_name": r.get("action_name","") or r.get("Regulation_Name","") or r.get("name",""),
            "action_type": r.get("action_type","") or r.get("Type",""),
            "status":      r.get("status","") or r.get("ordinance_status","") or r.get("Status",""),
            "date":        r.get("date_primary","") or r.get("Date",""),
        })
    return matches[:5]


# ── classification ─────────────────────────────────────────────────────────────

def classify_hit(summary, matched_facilities, matched_regulations):
    s = summary.lower()
    has_ordinance  = any(w in s for w in ["ordinance","zoning","moratorium","ban","restrict","regulation"])
    has_approval   = any(w in s for w in ["approved","approval","permit","granted"])
    has_opposition = any(w in s for w in ["oppose","opposition","protest","concern","reject","denied"])
    has_new_dc     = any(w in s for w in ["proposed","application","development","new facility","campus","construction"])
    has_tax        = any(w in s for w in ["tax abatement","tax agreement","tax incentive"])
    has_update     = any(w in s for w in ["update","progress","amendment"])

    if has_ordinance:
        return "legislation"
    if has_opposition:
        return "opposition"
    if matched_facilities and any(f["match_score"] > 0 for f in matched_facilities):
        return "dc_updates"
    if has_new_dc:
        return "new_facility"
    if has_approval or has_tax or has_update:
        return "dc_updates"
    return "monitoring"


def build_recommended_actions(county, state, summary, matched_facilities, matched_regulations, category):
    actions = []
    s = summary.lower()

    if category == "dc_updates" and matched_facilities:
        top = [f for f in matched_facilities if f["match_score"] > 0]
        if top:
            f = top[0]
            actions.append(f"UPDATE facility #{f['facility_number']} ({f['facility_name'][:50]}): verify DC_Status, opposition, outcome fields")
        else:
            actions.append(f"CHECK: {len(matched_facilities)} existing facilities in {county}, {state} — determine if any relate to this hit")

    elif category == "new_facility":
        actions.append(f"INVESTIGATE: possible new data center in {county}, {state} — verify and add to facilities list if confirmed")

    elif category == "legislation":
        if matched_regulations:
            actions.append(f"UPDATE regulation '{matched_regulations[0]['action_name']}': check if status changed")
        else:
            actions.append(f"ADD new county/state regulation for {county}, {state} to regulatory dataset")
        if "moratorium" in s:
            actions.append("MORATORIUM detected — add to moratoria dataset")

    elif category == "opposition":
        actions.append(f"LOG opposition event for {county}, {state}")
        if matched_facilities:
            f = matched_facilities[0]
            actions.append(f"UPDATE facility #{f['facility_number']}: check opposition/outcome fields")

    if "tax abatement" in s or "tax agreement" in s:
        actions.append("CHECK tax agreement — update tax_abatement fields in facilities list")
    if "approved" in s or "approval" in s:
        actions.append("CHECK approval — update DC_Status and date fields")

    if not actions:
        actions.append(f"REVIEW: data center mention in {county}, {state} — determine relevance")

    return " | ".join(actions)


# ── Claude API ────────────────────────────────────────────────────────────────

def analyse_with_claude(county, state, url, summary, matched_facilities, matched_regulations, datasets_summary):
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic not installed")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    fac_text = "\n".join([
        f"  #{f['facility_number']}: {f['facility_name'][:60]} | {f['status']} | score:{f['match_score']}"
        for f in matched_facilities
    ]) or "  None in this county/state"
    reg_text = "\n".join([
        f"  {r['action_name']} ({r['action_type']}) | {r['status']}"
        for r in matched_regulations
    ]) or "  None in this county/state"

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        messages=[{"role": "user", "content": f"""Data center analyst task.

County meeting hit: {county}, {state}
URL: {url}
Content: {summary[:2000]}

Existing facilities in this county:
{fac_text}

Existing regulations in this county:
{reg_text}

Context: {datasets_summary}

Provide:
1. WHAT IS HAPPENING (1-2 sentences)
2. CATEGORY: one of dc_updates|new_facility|legislation|opposition|monitoring
3. RECOMMENDED ACTIONS (specific, reference facility numbers)

Be concise."""}]
    )
    analysis = msg.content[0].text.strip()
    category = "monitoring"
    for cat in ["new_facility","dc_updates","legislation","opposition","monitoring"]:
        if cat in analysis.lower() or cat.replace("_"," ") in analysis.lower():
            category = cat
            break
    return analysis, category


# ── issue creation ────────────────────────────────────────────────────────────

def format_hit_entry(hit):
    """Format a single hit as a markdown entry for a category issue."""
    lines = [
        f"### {hit['county']}, {hit['state']}",
        f"**Meeting page:** {hit['meeting_url']}",
        f"**Keywords found:** {hit['keywords_found']}",
        f"**Date detected:** {hit['date_found'][:10]}",
        "",
        f"**Page title:** {hit['hit_title']}",
        "",
        "**Content summary:**",
        f"{hit['summary'][:600]}",
        "",
    ]
    if hit.get("matched_facilities"):
        lines.append(f"**Matched facilities (same county):** {hit['matched_facilities'][:200]}")
        lines.append("")
    if hit.get("matched_regulations"):
        lines.append(f"**Matched regulations (same county):** {hit['matched_regulations'][:200]}")
        lines.append("")
    lines.append(f"**Recommended actions:** {hit['recommended_actions'][:400]}")
    lines.append("")
    lines.append("---")
    return "\n".join(lines)


def create_category_issues(category_buckets, existing_issue_titles, repo, token):
    """Create one issue per category that has new hits."""
    issue_urls = {}
    if not token or not repo:
        print("[WARN] No GitHub token/repo — skipping issue creation")
        return issue_urls

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for category in CATEGORIES:
        hits = category_buckets.get(category, [])
        if not hits:
            print(f"  [{category}] No hits — skipping")
            continue

        issue_title = ISSUE_TITLES[category]

        # Dedup: skip if this exact title is already an open issue
        if issue_title in existing_issue_titles:
            print(f"  [{category}] Issue already open — skipping")
            continue

        header = (
            f"## {issue_title}\n\n"
            f"*Generated: {ts} | {len(hits)} hit(s)*\n\n"
            f"Please review each county meeting hit below and verify whether "
            f"it requires updates to the relevant dataset.\n\n"
        )

        entries = [format_hit_entry(h) for h in hits]
        body = header + "\n".join(entries)

        if len(body) > 65000:
            body = body[:65000] + "\n\n*[truncated — see county_hits_report.csv]*"

        print(f"  [{category}] Creating issue with {len(hits)} hit(s)...")
        try:
            resp = gh_post(
                f"https://api.github.com/repos/{repo}/issues",
                token,
                {
                    "title":  issue_title,
                    "body":   body,
                    "labels": ISSUE_LABELS[category],
                }
            )
            url = resp.get("html_url", "")
            print(f"  [{category}] Issue opened: {url}")
            issue_urls[category] = url
            existing_issue_titles.add(issue_title)
            time.sleep(1)
        except Exception as e:
            print(f"  [{category}] Issue creation failed: {e}")

    return issue_urls


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    SCRAPED_DIR.mkdir(exist_ok=True)

    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_repo  = os.environ.get("GITHUB_REPOSITORY", "")

    # Check Claude availability
    claude_available = False
    try:
        import anthropic
        if os.environ.get("ANTHROPIC_API_KEY"):
            claude_available = True
            print("[INFO] Claude API available")
        else:
            print("[WARN] ANTHROPIC_API_KEY not set — using rule-based fallback")
    except ImportError:
        print("[WARN] anthropic not installed — using rule-based fallback")

    # Load reference datasets
    print("\nLoading reference datasets...")
    DATASET_PATHS = {
        "facilities": "Final_datasets/DC_facilities_list_UPDATED.csv",
        "regulatory": "Final_datasets/dc_regulatory_actions_merged.csv",
        "county_reg": "Final_datasets/master_county_regulatory.csv",
        "state_reg":  "Final_datasets/legal_state_regulatiory_newest_May2026_minus_sources.csv",
    }
    datasets = {}
    for key, path in DATASET_PATHS.items():
        local = REPO_ROOT / path
        if local.exists():
            with open(local, "rb") as f:
                head = f.read(50)
            if b"git-lfs" in head:
                datasets[key] = fetch_dataset_from_github(github_repo, path, github_token)
            else:
                datasets[key] = load_csv_safe(local)
        elif github_token and github_repo:
            datasets[key] = fetch_dataset_from_github(github_repo, path, github_token)
        else:
            datasets[key] = []
        print(f"  {key}: {len(datasets[key])} rows")

    facilities  = datasets.get("facilities", [])
    regulations = (datasets.get("regulatory", []) +
                   datasets.get("county_reg", []) +
                   datasets.get("state_reg", []))
    datasets_summary = (
        f"Facilities: {len(facilities)}, "
        f"Regulatory: {len(datasets.get('regulatory',[]))}, "
        f"County regs: {len(datasets.get('county_reg',[]))}, "
        f"State regs: {len(datasets.get('state_reg',[]))}"
    )

    # Load existing open issue titles for dedup
    print("\nFetching existing GitHub issues for dedup...")
    existing_issue_titles = get_existing_issue_titles(github_repo, github_token) if github_token else set()

    # Load hits
    processed_ids = load_processed_ids()
    all_hits      = load_hits()
    new_hits      = [h for h in all_hits
                     if make_hit_id(h["county"], h["state"], h["meeting_url"]) not in processed_ids]

    print(f"\nTotal real hits: {len(all_hits)}")
    print(f"Already processed: {len(processed_ids)}")
    print(f"New hits: {len(new_hits)}")

    if not new_hits:
        print("[INFO] No new hits.")
        return

    # Process each hit
    category_buckets = {cat: [] for cat in CATEGORIES}
    processed = []

    for hit in new_hits:
        county    = hit["county"].strip()
        state     = hit["state"].strip()
        url       = hit["meeting_url"].strip()
        keywords  = hit.get("keywords_found", "data center")
        context   = hit.get("context", "")
        hit_id    = make_hit_id(county, state, url)
        date_found = hit.get("date", datetime.now(timezone.utc).isoformat())

        print(f"\n── {county}, {state} ──")

        # Scrape
        content = scrape_url(url)
        title   = extract_title(content, url)
        print(f"  Title: {title[:80]}")

        # Save JSON
        safe_name = re.sub(r"[^\w]", "_", f"{county}_{state}_{hit_id}")
        json_path = SCRAPED_DIR / f"{safe_name}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "hit_id": hit_id, "county": county, "state": state,
                "meeting_url": url, "date_scraped": datetime.now(timezone.utc).isoformat(),
                "keywords": keywords, "context": context, "title": title, "content": content,
            }, f, indent=2, ensure_ascii=False)

        # Summary + matching
        summary             = extract_summary(context, content, keywords)
        matched_facilities  = match_facilities(county, state, summary, facilities)
        matched_regulations = match_regulations(county, state, summary, regulations)

        fac_str = "; ".join([
            f"#{f['facility_number']} {f['facility_name'][:40]}" +
            (f" [score:{f['match_score']}]" if f["match_score"] > 0 else " [same county]")
            for f in matched_facilities
        ])
        reg_str = "; ".join([
            f"{r['action_name']} ({r['status']})" for r in matched_regulations
        ])

        # Analyse + classify
        analysis_mode = "claude" if claude_available else "fallback"
        if claude_available:
            print("  Analysing with Claude...")
            try:
                recommended_actions, category = analyse_with_claude(
                    county, state, url, summary,
                    matched_facilities, matched_regulations, datasets_summary
                )
                time.sleep(1)
            except Exception as e:
                print(f"  [WARN] Claude failed ({e}) — fallback")
                analysis_mode = "fallback"
                category = classify_hit(summary, matched_facilities, matched_regulations)
                recommended_actions = build_recommended_actions(
                    county, state, summary, matched_facilities, matched_regulations, category)
        else:
            category = classify_hit(summary, matched_facilities, matched_regulations)
            recommended_actions = build_recommended_actions(
                county, state, summary, matched_facilities, matched_regulations, category)

        print(f"  Mode: {analysis_mode} | Category: {category}")

        row = {
            "hit_id":               hit_id,
            "date_found":           date_found,
            "county":               county,
            "state":                state,
            "meeting_url":          url,
            "keywords_found":       keywords,
            "hit_title":            title,
            "scraped_file":         json_path.name,
            "summary":              summary,
            "analysis_mode":        analysis_mode,
            "category":             category,
            "matched_facilities":   fac_str,
            "matched_regulations":  reg_str,
            "recommended_actions":  recommended_actions,
        }
        processed.append(row)
        category_buckets[category].append(row)

        # Mark as processed
        save_processed_id(hit_id)
        processed_ids.add(hit_id)

    # Append to index
    index_exists = INDEX_CSV.exists()
    with open(INDEX_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        if not index_exists:
            writer.writeheader()
        for row in processed:
            writer.writerow({k: row[k] for k in INDEX_FIELDS})
    print(f"\n[OK] Appended {len(processed)} rows to index")

    # Rebuild full report
    all_index = load_csv_safe(INDEX_CSV)
    old_report = {}
    if REPORT_CSV.exists():
        for row in load_csv_safe(REPORT_CSV):
            if row.get("hit_id"):
                old_report[row["hit_id"]] = row
    new_report = {r["hit_id"]: r for r in processed}

    with open(REPORT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for row in all_index:
            hid = row.get("hit_id", "")
            if not hid:
                continue
            data = new_report.get(hid) or old_report.get(hid) or {}
            writer.writerow({k: data.get(k, row.get(k, "")) for k in REPORT_FIELDS})
    print(f"[OK] Report → {REPORT_CSV.name}")

    # Create category issues
    print("\nCreating category issues...")
    issue_urls = create_category_issues(
        category_buckets, existing_issue_titles, github_repo, github_token
    )

    # Summary
    print("\n" + "=" * 55)
    print(f"New hits processed: {len(processed)}")
    for cat in CATEGORIES:
        n   = len(category_buckets[cat])
        url = issue_urls.get(cat, "")
        print(f"  [{cat:12s}] {n:2d} hits  {'→ ' + url if url else '(no issue)'}")
    print("=" * 55)


if __name__ == "__main__":
    main()
