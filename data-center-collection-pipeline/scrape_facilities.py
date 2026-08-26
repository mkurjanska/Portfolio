"""Scrapes data center facility records for a given state from a facility directory site:
resumable batched runs, list-page status extraction, and detail-page field extraction via
regex against unstructured page text."""
import os
import re
import time
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

BASE_URL = "https://example-facility-directory.example"  # portfolio redaction: real source anonymized
_UA = UserAgent()


def _headers():
    """A fresh User-Agent per request lowers the chance of being rate-limited or blocked
    partway through a long batched run."""
    return {"User-Agent": _UA.random}


def load_existing_csv(csv_file):
    if os.path.exists(csv_file):
        try:
            if os.path.getsize(csv_file) == 0:
                return pd.DataFrame(), set()
            df = pd.read_csv(csv_file)
            if df.empty:
                return pd.DataFrame(), set()
            print(f"loaded {len(df)} existing records")
            return df, set(df['Facility_Name'].astype(str).str.strip().tolist())
        except Exception:
            os.remove(csv_file)
    return pd.DataFrame(), set()


def save_batch(df_new, csv_file):
    if df_new.empty:
        return
    temp_file = csv_file + '.tmp'
    try:
        df_new.to_csv(temp_file, index=False)
        if os.path.exists(csv_file) and os.path.getsize(csv_file) > 10:
            df_existing = pd.read_csv(csv_file)
            df_combined = pd.concat([df_existing, df_new], ignore_index=True).drop_duplicates(subset=['Facility_Name'])
            df_combined.to_csv(csv_file, index=False)
            print(f"appended {len(df_new)} -> {len(df_combined)} total")
            os.remove(temp_file)
            return
        os.rename(temp_file, csv_file)
        print(f"saved {len(df_new)} records")
    except Exception as e:
        print(f"save error: {e}")


def scrape_list_page_status(list_url):
    """Extracts each facility's Status from the main list page's table, falling back to
    scanning near each facility link for a status keyword if the table isn't parseable."""
    try:
        response = requests.get(list_url, headers=_headers(), timeout=15)
        soup = BeautifulSoup(response.content, 'lxml')

        status_map = {}
        table = soup.find('table')
        if table:
            for row in table.find_all('tr'):
                cells = row.find_all(['td', 'th'])
                if len(cells) > 2:
                    name_cell = cells[0].get_text(strip=True)
                    status_cell = None
                    for cell in cells[1:3]:
                        cell_text = cell.get_text(strip=True).lower()
                        if any(word in cell_text for word in ['operational', 'proposed', 'construction']):
                            status_cell = cell.get_text(strip=True)
                            break
                    if name_cell and status_cell:
                        status_map[name_cell] = status_cell

        for link in soup.find_all('a', href=re.compile(r'/data-center/project/')):
            name = link.get_text(strip=True)
            sibling_text = link.parent.get_text()
            for keyword, status in [('operational', 'Operational'), ('proposed', 'Proposed'),
                                     ('construction', 'Construction')]:
                if keyword in sibling_text.lower():
                    status_map.setdefault(name, status)
                    break

        return status_map
    except Exception as e:
        print(f"list page status scrape failed: {e}")
        return {}


DATE_PATTERNS = {
    'Application_Date': [
        r'Application Date[:\s|]*([0-9/:\-\s]+)',
        r'Filed[:\s|]*([0-9/:\-\s]+)',
        r'Timeline.*?(\d{4})',
        r'(\d{4})[,\s]*application',
    ],
    'Approval_Date': [
        r'Approval Date[:\s|]*([0-9/:\-\s]+)',
        r'Approved[:\s|]*([0-9/:\-\s]+)',
        r'Timeline.*?(\d{4})',
        r'(\d{4})[,\s]*approval',
    ],
    'Operational_Date': [
        r'Actual Operational Date[:\s|]*([0-9/:\-\s]+)',
        r'Operational Date[:\s|]*([0-9/:\-\s]+)',
        r'Timeline.*?(\d{4})',
        r'(\d{4})[,\s]*operational',
    ],
}


def _extract_dates(page_text):
    dates = {}
    for field, patterns in DATE_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                year_match = re.search(r'\d{4}', match.group(1))
                if year_match:
                    dates[field] = year_match.group(0)
                break
    return dates


def scrape_detail_page(full_url, facility_name, status_map, state_name):
    """Extracts county, address, zip, grid operator, hyperscaler status, and the three
    timeline dates from a facility's detail page via regex against the unstructured page
    text -- Status comes from the list page instead, since the detail page doesn't carry it
    reliably."""
    try:
        time.sleep(1.5)
        response = requests.get(full_url, headers=_headers(), timeout=15)
        soup = BeautifulSoup(response.content, 'lxml')
        page_text = soup.get_text()

        county = capacity = 'N/A'
        title = soup.find('title')
        if title:
            title_text = title.get_text()
            county_match = re.search(r'in ([A-Za-z\s]+County),?\s*[A-Z]{2}', title_text)
            if county_match:
                county = county_match.group(1).strip()
            cap_match = re.search(r'\(([^)]*MW[^)]*)\)', title_text)
            if cap_match:
                capacity = cap_match.group(1).strip()

        full_address = 'N/A'
        addr_match = re.search(r'Address[:\s|]*([^\n]{10,})(?=ZIP Code|Business Information)', page_text, re.IGNORECASE)
        if addr_match:
            full_address = re.sub(r'\s+', ' ', addr_match.group(1)).strip()

        zip_code = 'N/A'
        zip_match = re.search(r'ZIP Code[:\s|]*(\d{5})', page_text)
        if zip_match:
            zip_code = zip_match.group(1)

        grid_operator = 'N/A'
        grid_match = re.search(r'Grid[:\s|]*Operator[:\s|]*([A-Za-z\s]{2,30})', page_text, re.IGNORECASE)
        if grid_match:
            grid_operator = grid_match.group(1).strip()
        elif 'PJM' in page_text.upper():
            grid_operator = 'PJM'

        hyperscaler = 'No'
        hyperscaler_match = re.search(r'Hyperscaler[:\s|]*([Yy]es|[Nn]o)', page_text, re.IGNORECASE)
        if hyperscaler_match:
            hyperscaler = hyperscaler_match.group(1).title()

        dates = _extract_dates(page_text)

        return {
            'Facility_Name': facility_name,
            'Full_Address': full_address[:150],
            'State': state_name,
            'Status': status_map.get(facility_name, 'N/A'),
            'County': county,
            'Zip_Code': zip_code,
            'Grid_Operator': grid_operator,
            'Hyperscaler': hyperscaler,
            'Application_Date': dates.get('Application_Date', 'N/A'),
            'Approval_Date': dates.get('Approval_Date', 'N/A'),
            'Operational_Date': dates.get('Operational_Date', 'N/A'),
            'Power_Capacity': capacity,
        }
    except Exception as e:
        print(f"{facility_name[:40]}: {e}")
        return None


def run(state_name, state_abbr, csv_file, batch_size=30, resume_from=None):
    """Scrapes one batch of up to batch_size not-yet-seen facilities for a state, appending
    each result to csv_file every 5 records so an interrupted run loses almost nothing."""
    list_url = f"{BASE_URL}/data-center/state/{state_abbr}"
    print(f"=== {state_name} data centers ===")
    print(f"batch size: {batch_size}")

    _, scraped_names = load_existing_csv(csv_file)
    print(f"already known: {len(scraped_names)}")

    status_map = scrape_list_page_status(list_url)
    print(f"found status for {len(status_map)} facilities on the list page")

    response = requests.get(list_url, headers=_headers())
    soup = BeautifulSoup(response.content, 'lxml')
    facility_links = [
        (link.get_text(strip=True), urljoin(BASE_URL, link['href']))
        for link in soup.find_all('a', href=True)
        if '/data-center/project/' in link['href'] and len(link.get_text(strip=True)) > 3
    ]
    print(f"total facilities listed: {len(facility_links)}")

    if resume_from:
        start_idx = next((i for i, (name, _) in enumerate(facility_links)
                           if resume_from.lower() in name.lower()), 0)
        facility_links = facility_links[start_idx:]
        print(f"resuming from: {facility_links[0][0] if facility_links else '(end of list)'}")

    to_scrape = [(name, url) for name, url in facility_links
                 if name.strip() not in scraped_names][:batch_size]
    print(f"batch: {len(to_scrape)} facilities")

    new_records = []
    for i, (facility_name, full_url) in enumerate(to_scrape, 1):
        print(f"[{i:2d}/{len(to_scrape)}] {facility_name[:45]}")
        record = scrape_detail_page(full_url, facility_name, status_map, state_name)
        if record:
            new_records.append(record)
        if i % 5 == 0 and new_records:
            save_batch(pd.DataFrame(new_records[-5:]), csv_file)

    if new_records:
        save_batch(pd.DataFrame(new_records), csv_file)
        print(f"total saved: {len(pd.read_csv(csv_file))} records in {csv_file}")
        if len(to_scrape) == batch_size:
            print(f"next run: resume_from='{to_scrape[-1][0]}'")

    return new_records


if __name__ == "__main__":
    run(state_name="Virginia", state_abbr="VA", csv_file="va_data_centers_enhanced.csv",
        batch_size=30, resume_from="unnamed")
