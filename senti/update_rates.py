#!/usr/bin/env python3
"""
Senti rate sync script.

Updates senti/rates.json with:
  - T-Bill rates (91/182/364-day): scraped directly from CBK's official
    auction-results PDFs (centralbank.go.ke). This is the primary regulator
    source. The CBK results-listing page is fetched with a normal browser
    User-Agent (their WAF blocks generic/bot clients but not this), the PDF
    links on it are parsed, and we walk them newest-first, downloading and
    parsing each until one contains a valid "Weighted Average Interest Rate
    of accepted bids" block (CBK sometimes lists a not-yet-settled auction
    first).
  - Money Market Fund effective annual yields: scraped from Serrari Group's
    public MMF comparator table (serrarigroup.com/ke/mmf), which publishes a
    daily EAR figure for most CMA-regulated Kenyan MMFs, matched to Senti's
    fund list by fund name. Cytonn itself does not publish a public daily
    rate anywhere (app/USSD/portal only), so there is no first-party source
    for MMF rates - Serrari is a third-party aggregator, used here as the
    best available free public source.

    "dailyRate" (the gross rate before daily compounding) is not published
    anywhere separately, so it is derived mathematically from the EAR:
        dailyRate = 36500 * ((1 + EAR/100)^(1/365) - 1)
    This matches the relationship already present between dailyRate and
    effectiveYield in the existing rates.json to within ~0.2pp.

  Zimele Money Market Fund is not tracked by Serrari and has no other public
  feed, so it is left untouched - a warning is logged, not a failure.

Design goals: never crash the whole run over one bad source, never write a
change that fails a sanity check, and only commit rates.json if something
actually changed (handled by the GitHub Actions workflow, not this script).
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

RATES_PATH = Path(__file__).resolve().parent / "rates.json"

CBK_TBILLS_PAGE = "https://www.centralbank.go.ke/bills-bonds/treasury-bills/"
SERRARI_MMF_PAGE = "https://serrarigroup.com/ke/mmf"

# Senti fund id -> substring to match against Serrari's "Fund Name" column.
MMF_NAME_MATCH = {
    "cytonn-mmf": "cytonn",
    "etica-mmf": "etica",
    "nabo-mmf": "nabo",
    "sanlam-mmf": "sanlam",
    "cic-mmf": "cic money market",
    "coop-mmf": "co-op money market",
    "icea-lion-mmf": "icea lion",
    "old-mutual-mmf": "old mutual",
    # "zimele-mmf" intentionally omitted: not tracked by Serrari, no public feed found.
}

# Sanity bounds: refuse to write a rate outside this range (guards against a
# parsing failure silently writing garbage, e.g. picking up a stray number).
MIN_SANE_RATE = 1.0
MAX_SANE_RATE = 30.0


def log(msg):
    print(msg, file=sys.stderr)


def fetch_tbill_rates():
    """Return {91: x, 182: y, 364: z} weighted-average accepted-bid rates,
    or {} if nothing could be parsed."""
    try:
        resp = requests.get(CBK_TBILLS_PAGE, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log(f"[tbills] failed to fetch CBK listing page: {e}")
        return {}

    # Links look like:
    # /uploads/91_day_historical_treasury_bill_results/<id>_RESULTS <91>-091 <182>-182 <364>-364 DATED <DD-MM-YYYY>.pdf
    links = re.findall(
        r'href="(/uploads/91_day_historical_treasury_bill_results/[^"]+\.pdf)"',
        resp.text,
    )
    if not links:
        log("[tbills] no PDF links found on CBK page - page structure may have changed")
        return {}

    def date_key(href):
        m = re.search(r"DATED\s+(\d{2})-(\d{2})-(\d{4})", href)
        if not m:
            return (0, 0, 0)
        d, mo, y = m.groups()
        return (int(y), int(mo), int(d))

    # Newest first.
    links = sorted(set(links), key=date_key, reverse=True)

    pattern = re.compile(
        r"Weighted Average Interest Rate of\s*\n?\s*"
        r"(\d+\.\d+)%\s+(\d+\.\d+)%\s+(\d+\.\d+)%\s*\n?\s*accepted bids",
        re.IGNORECASE,
    )

    for href in links[:6]:  # only bother trying a handful of the most recent
        pdf_url = urljoin("https://www.centralbank.go.ke", href)
        try:
            r = requests.get(pdf_url, headers=HEADERS, timeout=30)
            r.raise_for_status()
        except Exception as e:
            log(f"[tbills] failed to fetch {pdf_url}: {e}")
            continue

        try:
            import pdfplumber
            import io

            with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                text = pdf.pages[0].extract_text() or ""
        except Exception as e:
            log(f"[tbills] failed to parse PDF {pdf_url}: {e}")
            continue

        m = pattern.search(text)
        if not m:
            log(f"[tbills] no results block in {pdf_url} yet, trying next")
            continue

        rate_91, rate_182, rate_364 = (round(float(x), 2) for x in m.groups())
        log(f"[tbills] parsed from {pdf_url}: 91={rate_91} 182={rate_182} 364={rate_364}")
        return {91: rate_91, 182: rate_182, 364: rate_364}

    log("[tbills] exhausted candidate PDFs without a match")
    return {}


def fetch_mmf_ears():
    """Return {senti_fund_id: ear_percent} for whatever funds we can match."""
    try:
        resp = requests.get(SERRARI_MMF_PAGE, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log(f"[mmf] failed to fetch Serrari MMF page: {e}")
        return {}

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log("[mmf] beautifulsoup4 not installed")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        log("[mmf] no table found on Serrari page - page structure may have changed")
        return {}

    rows = tables[0].find_all("tr")
    parsed = {}  # fund name (lowercase) -> ear
    for row in rows[1:]:
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        fund_name, ear_str = cells[4], cells[2]
        m = re.match(r"([\d.]+)%", ear_str)
        if not m:
            continue
        parsed[fund_name.lower()] = float(m.group(1))

    if not parsed:
        log("[mmf] table found but no rows parsed - format may have changed")
        return {}

    results = {}
    for fund_id, needle in MMF_NAME_MATCH.items():
        match = next((ear for name, ear in parsed.items() if needle in name), None)
        if match is None:
            log(f"[mmf] could not find a match for {fund_id} (looking for '{needle}')")
            continue
        results[fund_id] = match

    return results


def daily_rate_from_ear(ear_percent):
    """Invert daily compounding to get the nominal gross daily rate implied
    by a published effective annual rate."""
    return round(36500 * ((1 + ear_percent / 100) ** (1 / 365) - 1), 2)


def main():
    data = json.loads(RATES_PATH.read_text())
    changed = False

    tbill_rates = fetch_tbill_rates()
    for tbill in data.get("tbills", []):
        new_rate = tbill_rates.get(tbill.get("termDays"))
        if new_rate is None:
            continue
        if not (MIN_SANE_RATE <= new_rate <= MAX_SANE_RATE):
            log(f"[tbills] refusing out-of-range rate {new_rate} for {tbill['id']}")
            continue
        if tbill["rate"] != new_rate:
            log(f"[tbills] {tbill['id']}: {tbill['rate']} -> {new_rate}")
            tbill["rate"] = new_rate
            changed = True

    mmf_ears = fetch_mmf_ears()
    for fund in data.get("mmf", []):
        ear = mmf_ears.get(fund["id"])
        if ear is None:
            continue
        if not (MIN_SANE_RATE <= ear <= MAX_SANE_RATE):
            log(f"[mmf] refusing out-of-range EAR {ear} for {fund['id']}")
            continue
        new_daily = daily_rate_from_ear(ear)
        if fund["effectiveYield"] != ear or fund["dailyRate"] != new_daily:
            log(f"[mmf] {fund['id']}: daily {fund['dailyRate']}->{new_daily}, "
                f"effective {fund['effectiveYield']}->{ear}")
            fund["dailyRate"] = new_daily
            fund["effectiveYield"] = ear
            changed = True

    if changed:
        RATES_PATH.write_text(json.dumps(data, indent=2) + "\n")
        log("rates.json updated")
    else:
        log("no changes")


if __name__ == "__main__":
    main()
