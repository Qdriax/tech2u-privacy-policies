#!/usr/bin/env python3
"""
BuildTally Rate Pack Sync & Validation Script.

Maintains buildtally/rates.json with:
  - Strict schema and data validation for Kenya 47 counties and benchmark materials.
  - Quarterly version bump & integrity signature calculation (SHA-256).
  - CLI options for automated GitHub Actions workflows and manual updates.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

RATES_PATH = Path(__file__).resolve().parent / "rates.json"

REQUIRED_COUNTIES_COUNT = 47
REQUIRED_MATERIAL_KEYS = {"id", "code", "name", "category", "unit", "baselinePrice"}

def calculate_rates_signature(rates_list: list) -> str:
    """Computes deterministic SHA-256 hash over normalized rates JSON content."""
    normalized = json.dumps(rates_list, sort_keys=True, separators=(",", ":"))
    return f"sha256-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"

def get_current_quarter_version() -> str:
    """Calculates current quarter version (e.g. KE-2026.09, KE-2026.12, KE-2027.03)."""
    now = datetime.now(timezone.utc)
    month = now.month
    if month <= 3:
        q_month = "03"
    elif month <= 6:
        q_month = "06"
    elif month <= 9:
        q_month = "09"
    else:
        q_month = "12"
    return f"KE-{now.year}.{q_month}"

def validate_rate_pack(data: dict) -> list[str]:
    """Validates structure and content of the rate pack. Returns list of errors."""
    errors = []

    version = data.get("ratePackVersion", "")
    if not re.match(r"^KE-\d{4}\.\d{2}$", version):
        errors.append(f"Invalid ratePackVersion format: '{version}'. Expected 'KE-YYYY.MM'")

    rates = data.get("rates", [])
    if not isinstance(rates, list) or len(rates) == 0:
        errors.append("Rate pack must contain a non-empty 'rates' list.")
    else:
        for i, item in enumerate(rates):
            if not isinstance(item, dict):
                errors.append(f"Rate item at index {i} is not an object.")
                continue
            missing = REQUIRED_MATERIAL_KEYS - set(item.keys())
            if missing:
                errors.append(f"Rate item '{item.get('code', i)}' is missing fields: {missing}")
            price = item.get("baselinePrice")
            if not isinstance(price, (int, float)) or price <= 0:
                errors.append(f"Rate item '{item.get('code', i)}' has invalid baselinePrice: {price}")

    counties = data.get("counties", [])
    if not isinstance(counties, list) or len(counties) != REQUIRED_COUNTIES_COUNT:
        errors.append(f"Expected {REQUIRED_COUNTIES_COUNT} counties, found {len(counties) if isinstance(counties, list) else 0}.")

    return errors

def load_rates() -> dict:
    if not RATES_PATH.exists():
        print(f"Error: {RATES_PATH} does not exist.")
        sys.exit(1)
    with open(RATES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_rates(data: dict) -> None:
    with open(RATES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

def main():
    parser = argparse.ArgumentParser(description="Validate and update BuildTally Kenya Rate Packs")
    parser.add_argument("--validate-only", action="store_true", help="Only validate rates.json without modifying")
    parser.add_argument("--bump-auto", action="store_true", help="Auto-bump version if current quarter is newer")
    parser.add_argument("--set-version", type=str, help="Manually set a new ratePackVersion (e.g. KE-2027.03)")
    args = parser.parse_args()

    data = load_rates()
    current_version = data.get("ratePackVersion", "KE-2026.09")

    # Initial Validation
    errors = validate_rate_pack(data)
    if errors:
        print("Validation errors detected:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

    print(f"Current BuildTally Rate Pack: {current_version} ({len(data['rates'])} materials, {len(data['counties'])} counties)")

    if args.validate_only:
        print("Validation passed successfully.")
        return

    changed = False
    new_version = None

    if args.set_version:
        new_version = args.set_version
    elif args.bump_auto:
        quarter_ver = get_current_quarter_version()
        if quarter_ver > current_version:
            new_version = quarter_ver

    if new_version and new_version != current_version:
        print(f"Bumping version: {current_version} -> {new_version}")
        data["ratePackVersion"] = new_version
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        data["releaseDate"] = now_iso
        data["description"] = f"Kenya Construction Benchmark Rates ({new_version})"
        for r in data["rates"]:
            r["lastUpdated"] = now_iso
        changed = True

    # Always ensure signature matches rates content
    calculated_sig = calculate_rates_signature(data["rates"])
    if data.get("signature") != calculated_sig:
        data["signature"] = calculated_sig
        changed = True

    if changed:
        save_rates(data)
        print(f"Updated {RATES_PATH.name} successfully.")
    else:
        print("No changes required. Rate pack is up-to-date and signed.")

if __name__ == "__main__":
    main()
