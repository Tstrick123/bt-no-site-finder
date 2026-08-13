"""The scanner — runs the funnel for one niche across a set of cities.

Funnel:
  Gate A  (once)     national CPC + job value qualify the niche at all
  Gate B  (per city) enough search volume to bother scoring competition
  Tier-2  (survivors) pull local competition -> Rankability -> composite score
Everything is cached, so re-running a scan is nearly free.
"""

import time

from . import scoring
from .cache import Cache
from .cities import load_cities, select_cities
from .config import load_config, load_env
from .dataforseo import DataForSEO
from .places import Places


def resolve_mode(env):
    """Decide mock vs live. Live only if MOCK_MODE=false AND creds are present."""
    live_ok = (not env["mock_requested"]) and env["have_dataforseo"]
    dfs_mock = not live_ok
    places_mock = dfs_mock or not env["have_places"]
    return dfs_mock, places_mock


def run_scan(niche_key, region_key=None, limit=None, progress=None, states=None):
    cfg = load_config()
    env = load_env()
    settings = cfg["settings"]

    if niche_key not in cfg["niches"]:
        raise ValueError(f"Unknown niche '{niche_key}'. Options: {', '.join(cfg['niches'])}")
    niche = cfg["niches"][niche_key]

    dfs_mock, places_mock = resolve_mode(env)
    cache = Cache()
    dfs = DataForSEO(env["dataforseo_login"], env["dataforseo_password"], cache, mock=dfs_mock)
    places = Places(env["google_places_key"], cache, mock=places_mock)

    gate_a = scoring.niche_passes_gate_a(niche["job_value"], settings, niche.get("job_value_high"))

    region = cfg["regions"].get(region_key) if region_key else None
    cities = select_cities(load_cities(), settings, region)
    if states:  # optional extra state filter (e.g. scan just TX + CA)
        cities = cities[cities["state_id"].isin(states)].reset_index(drop=True)
    if limit:
        cities = cities.head(limit)

    results = []
    total = len(cities)
    for idx, row in cities.iterrows():
        city, state, state_name = row["city"], row["state_id"], row["state_name"]
        loc = f"{city},{state_name},United States"
        pop = int(row["population"])
        rec = {
            "niche": niche_key, "city": city, "state": state,
            "county": row.get("county_name"), "population": pop,
            "lat": float(row["lat"]), "lng": float(row["lng"]),
            "national_cpc": niche["national_cpc"],
            "display_cpc": scoring.display_cpc(niche["national_cpc"], pop, settings),
            "mock": "yes" if dfs_mock else "no",
            "scanned_at": time.time(),
        }
        try:
            sv = dfs.search_volume(niche["keywords"], loc, pop_hint=pop)
            vol = sv["total_volume"]
            rec["city_volume"] = vol
            if scoring.demand_gate_b(vol, settings):
                comp = places.competitors(niche["keywords"][0], city, state_name,
                                          float(row["lat"]), float(row["lng"]))
                dshare = dfs.directory_share(niche["keywords"][0], loc)
                rank = scoring.rankability(comp["median_reviews"], comp["max_reviews"],
                                           comp["no_website_share"], dshare)
                dem = scoring.demand_score(vol, settings)
                val = scoring.value_score(niche["national_cpc"], settings)
                opp = scoring.composite(rank, dem, val, settings)
                rec.update({
                    "competitor_count": comp["count"],
                    "median_reviews": comp["median_reviews"],
                    "max_reviews": comp["max_reviews"],
                    "no_website_share": comp["no_website_share"],
                    "directory_share": dshare,
                    "rankability": rank, "demand_score": dem, "value_score": val,
                    "opportunity": opp, "verdict": scoring.verdict(opp, settings),
                    "rev_potential": scoring.rev_potential(vol, niche["job_value"], settings),
                })
            else:
                rec["verdict"] = "LOW DEMAND"
                rec["opportunity"] = None
        except Exception as e:  # one bad city shouldn't kill the whole scan
            rec["verdict"] = "ERROR"
            rec["error"] = str(e)
            rec["opportunity"] = None

        results.append(rec)
        cache.save_result(rec)
        if progress:
            progress(idx + 1, total, city)

    # highest opportunity first; scored cities above unscored ones
    results.sort(key=lambda r: (r.get("opportunity") is not None, r.get("opportunity") or -1),
                 reverse=True)

    return {
        "niche": niche_key,
        "niche_display": niche.get("display_name", niche_key),
        "gate_a_pass": gate_a,
        "dfs_mock": dfs_mock,
        "places_mock": places_mock,
        "region": region_key,
        "count": len(results),
        "results": results,
    }
