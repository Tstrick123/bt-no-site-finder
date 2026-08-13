#!/usr/bin/env python3
"""No-Site Finder (CLI) — build a cold-call list of businesses with no website.

Examples:
    python3 find_leads.py --list
    python3 find_leads.py --niche insulation --region st_george
    python3 find_leads.py --query "roofing contractor" --region st_george
    python3 find_leads.py --query "plumber" --states UT --limit 10 --min-score 40

Runs FREE in mock mode (fake but realistic data) until you set MOCK_MODE=false
and add a Google Places key in .env. The prettier point-and-click version is
the dashboard: double-click "Launch No-Site Finder.command".
"""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nichefinder.config import load_config
from nichefinder.nosite import find_leads

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"

CALL_COLS = ["priority", "score", "name", "phone", "website_status",
             "rating", "reviews", "city", "state", "address"]


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Find local businesses with no website to cold-call.")
    ap.add_argument("--niche", help="niche key for search terms + ticket context (see --list)")
    ap.add_argument("--query", help="any business type to search, e.g. \"roofing contractor\"")
    ap.add_argument("--region", default=None, help="region preset, e.g. st_george")
    ap.add_argument("--states", default=None, help="comma-separated state codes, e.g. UT,AZ")
    ap.add_argument("--limit", type=int, default=None, help="only scan the N biggest towns")
    ap.add_argument("--min-score", type=int, default=0, help="drop leads below this call score (0-100)")
    ap.add_argument("--list", action="store_true", help="list niches + regions, then exit")
    args = ap.parse_args()

    if args.list or (not args.niche and not args.query):
        print("\nNICHES  (key -> name):")
        for k, v in cfg["niches"].items():
            print(f"  {k:24s}  {v['display_name']}")
        print("\nREGIONS:")
        for k, v in cfg["regions"].items():
            print(f"  {k:16s} {v['label']}")
        print("\nExample:  python3 find_leads.py --niche insulation --region st_george")
        print("Example:  python3 find_leads.py --query \"roofing contractor\" --region st_george")
        return

    def prog(i, t, city):
        print(f"  [{i:>3}/{t}] {city}")

    states = [s.strip().upper() for s in args.states.split(",")] if args.states else None
    label = args.query or args.niche
    where = f"region '{args.region}'" if args.region else "all towns in the population band"
    if states:
        where += f" (states: {', '.join(states)})"
    print(f"\nHunting no-website '{label}' across {where} …")
    out = find_leads(query=args.query, niche_key=args.niche, region_key=args.region,
                     limit=args.limit, states=states, progress=prog,
                     min_score=args.min_score)

    mode = "MOCK (fake data — free)" if out["mock"] else "LIVE (real Google Places — spends credit)"
    print(f"\nMode: {mode}")
    print(f"Searched: {', '.join(out['query'])}")
    print(f"Businesses seen: {out['scanned']}   |   No-website leads: {out['lead_count']}")
    if not out["mock"]:
        used = out.get("api_calls", 0)
        free = "  (within the free 1,000/month = $0)" if used <= 1000 else ""
        print(f"Google lookups this run: {used}  (~${out.get('api_cost', 0):.2f} gross){free}")
    b = out["breakdown"]
    print(f"Priority:  🔥 {b.get('HOT', 0)} HOT   ⭐ {b.get('WARM', 0)} WARM   ▫️ {b.get('COOL', 0)} COOL")

    if out.get("fatal_error"):
        print(f"\n⚠️  Google API error — scan stopped early:\n   {out['fatal_error'][:300]}")
        print("   Most common fix: enable 'Places API (New)' on your Google Cloud "
              "project, wait a few minutes, then re-run.")
        return

    leads = out["leads"]
    if not leads:
        print("\nNo no-website leads found. Try a broader --query or drop --min-score.")
        return

    df = pd.DataFrame(leads)
    show = df[[c for c in CALL_COLS if c in df.columns]]
    print("\nTOP CALL LIST:\n")
    with pd.option_context("display.max_rows", 60, "display.width", 200,
                           "display.max_colwidth", 40):
        print(show.head(40).to_string(index=False))

    OUT.mkdir(exist_ok=True)
    tag = (args.niche or args.query).replace(" ", "_") + (f"_{args.region}" if args.region else "")
    csv_path = OUT / f"leads_{tag}.csv"
    df.to_csv(csv_path, index=False)
    print(f"\nSaved call list: {csv_path}")


if __name__ == "__main__":
    main()
