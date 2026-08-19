"""Loads the US cities list and filters it to the target set.

Uses data/uscities.csv if you've dropped in the full SimpleMaps file;
otherwise falls back to the bundled data/uscities_starter.csv (SW Utah).
Both share the same columns: city, state_id, state_name, county_name,
population, lat, lng, timezone, ranking.
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
FULL = ROOT / "data" / "uscities.csv"
STARTER = ROOT / "data" / "uscities_starter.csv"

# Columns the scanner needs; a full SimpleMaps CSV has extras we simply ignore.
NEEDED = ["city", "state_id", "state_name", "county_name",
          "population", "lat", "lng"]


def cities_source():
    """Which CSV we're actually using (full if present, else starter)."""
    return FULL if FULL.exists() else STARTER


def load_cities():
    path = cities_source()
    df = pd.read_csv(path)
    for col in NEEDED:
        if col not in df.columns:
            raise ValueError(f"{path.name} is missing required column '{col}'")
    df = df.dropna(subset=["population", "lat", "lng"])
    df["population"] = df["population"].astype(int)
    df["county_name"] = df["county_name"].fillna("")   # some sources have no county
    return df


def select_cities(df, settings, region=None):
    """Filter cities by the population band + (optional) region preset."""
    pop_min = settings["population_min"]
    pop_max = settings["population_max"]
    states = None
    include_only = None

    if region:
        pop_min = region.get("population_min", pop_min)
        pop_max = region.get("population_max", pop_max)
        states = region.get("states")
        include_only = region.get("include_only")

    out = df[(df["population"] >= pop_min) & (df["population"] <= pop_max)].copy()

    if states:
        out = out[out["state_id"].isin(states)]

    if include_only:
        wanted = {n.strip().lower() for n in include_only}
        out = out[out["city"].str.strip().str.lower().isin(wanted)]

    return out.sort_values("population", ascending=False).reset_index(drop=True)
