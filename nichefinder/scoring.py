"""The scoring math — gates, rankability, composite score, rent-potential.

This is the brain of the tool. See README.md 'How the score works'.
Everything is driven by the numbers in config.yaml -> settings.
"""

import math


# ---- Gate A: does this niche produce jobs at/above the ticket floor? ----
def niche_passes_gate_a(job_value, settings, job_value_high=None):
    # A niche qualifies if its jobs CAN reach the floor — use the HIGH end of its
    # range, because you qualify individual jobs at the sales level ($1k+ = worth it).
    ceiling = max(job_value or 0, job_value_high or 0)
    return ceiling >= settings["min_job_value"]


# ---- Gate B: does this city have enough demand to bother scoring competition? ----
def demand_gate_b(total_volume, settings):
    return total_volume >= settings["min_city_volume"]


# ---- CPC display (per-city CPC is unreliable; show national x metro-tier) ----
def metro_tier(population):
    if population >= 250_000:
        return "large"
    if population >= 60_000:
        return "mid"
    return "small"


def display_cpc(national_cpc, population, settings):
    mult = settings["metro_multipliers"][metro_tier(population)]
    return round(national_cpc * mult, 2)


# ---- Rankability: 0..100, higher = weaker field = easier to rank & rent ----
def rankability(median_reviews, max_reviews, no_website_share, directory_share):
    if median_reviews < 10:
        s_median = 100
    elif median_reviews <= 30:
        s_median = 70
    elif median_reviews <= 80:
        s_median = 40
    else:
        s_median = 10

    if max_reviews < 50:
        s_max = 100
    elif max_reviews <= 150:
        s_max = 60
    else:
        s_max = 20

    s_site = 100 * no_website_share      # more competitors WITHOUT a website = weaker field
    s_dir = 100 * directory_share        # more directories (Yelp/Angi) ranking = weaker real competition

    return round(0.35 * s_median + 0.25 * s_max + 0.15 * s_site + 0.25 * s_dir, 1)


# ---- Demand score: 0..100 from monthly search volume ----
def demand_score(total_volume, settings):
    if total_volume <= 0:
        return 0.0
    ref = settings["volume_ref"]
    return round(max(0.0, min(100.0, 100 * math.log10(total_volume + 1) / math.log10(ref))), 1)


# ---- Value score: 0..100 from the niche's national CPC (click value) ----
def value_score(national_cpc, settings):
    ref = settings["cpc_ref"]
    return round(max(0.0, min(100.0, 100 * national_cpc / ref)), 1)


# ---- Composite opportunity score ----
def composite(rank, demand, value, settings):
    w = settings["weights"]
    return round(w["rankability"] * rank + w["demand"] * demand + w["value"] * value, 1)


def verdict(score, settings):
    b = settings["verdict_bands"]
    if score >= b["go"]:
        return "GO"
    if score >= b["maybe"]:
        return "MAYBE"
    return "AVOID"


# ---- Rent potential ($/mo) — what the leads from this city could be worth ----
def rev_potential(total_volume, job_value, settings):
    m = settings["lead_model"]
    lead_value = job_value * m["close_rate"] * m["margin"]
    est_leads = total_volume * m["click_through_rate"] * m["lead_conversion"]
    return round(est_leads * lead_value)
