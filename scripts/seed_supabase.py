#!/usr/bin/env python3
"""
Migrate clients.json into Supabase.

Usage:
  export SUPABASE_URL="https://your-project.supabase.co"
  export SUPABASE_KEY="your-anon-or-service-role-key"
  python scripts/seed_supabase.py [--json data/configs/clients.json]

This script is idempotent — it uses upsert so re-running is safe.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from supabase import create_client


def main():
    parser = argparse.ArgumentParser(description="Seed Supabase from clients.json")
    parser.add_argument(
        "--json",
        default=str(Path(__file__).parent.parent / "data" / "configs" / "clients.json"),
        help="Path to clients.json (default: data/configs/clients.json)",
    )
    args = parser.parse_args()

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("ERROR: Set SUPABASE_URL and SUPABASE_KEY environment variables.")
        sys.exit(1)

    sb = create_client(url, key)

    with open(args.json) as f:
        data = json.load(f)

    # 1. Upsert menu categories (must be done first — clients FK references them)
    categories = data.get("menu_categories", {})
    cat_rows = [{"name": name, "slots": slots} for name, slots in categories.items()]
    if cat_rows:
        sb.table("menu_categories").upsert(cat_rows).execute()
    print(f"  Upserted {len(cat_rows)} menu categories")

    # 2. Upsert clients
    client_rows = [
        {"name": c["name"], "menu_category": c["menu_category"]}
        for c in data["clients"]
    ]
    sb.table("clients").upsert(client_rows).execute()
    print(f"  Upserted {len(client_rows)} clients")

    # 3. Upsert slot count overrides
    sco_rows = []
    for client_name, overrides in data.get("slot_count_overrides", {}).items():
        for slot, count in overrides.items():
            sco_rows.append({
                "client_name": client_name,
                "slot": slot,
                "count": int(count),
            })
    if sco_rows:
        sb.table("slot_count_overrides").upsert(sco_rows).execute()
    print(f"  Upserted {len(sco_rows)} slot count overrides")

    # 4. Upsert theme overrides
    to_rows = []
    for client_name, themes in data.get("theme_overrides", {}).items():
        for day, theme in themes.items():
            to_rows.append({
                "client_name": client_name,
                "day": day.lower(),
                "theme": theme,
            })
    if to_rows:
        sb.table("theme_overrides").upsert(to_rows).execute()
    print(f"  Upserted {len(to_rows)} theme overrides")

    # 5. Upsert app settings
    settings = [
        {"key": "core_min_one_slots", "value": json.dumps(data.get("core_min_one_slots", []))},
        {"key": "constant_slots", "value": json.dumps(data.get("constant_slots", []))},
        {"key": "fallback_menu_category", "value": json.dumps(data.get("fallback_menu_category", ""))},
    ]
    sb.table("app_settings").upsert(settings).execute()
    print(f"  Upserted {len(settings)} app settings")

    print("\nDone! Supabase is now populated with your client configuration.")


if __name__ == "__main__":
    main()
