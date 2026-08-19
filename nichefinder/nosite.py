"""No-Site Finder — the cold-call lead engine.

Sweeps the Google-listed businesses for a trade + town and keeps only the ones
with NO real website: exactly the people you can cold-call to sell a website.

The twist most tools miss: a business whose Google listing links only to a
Facebook page, a directory (Yelp/Angi), or a free page-builder counts as "no
real site" here — those are your HOTTEST leads (they already want to be found
online, they just lack the asset you sell). So we CLASSIFY the website link, we
don't just check whether one exists.

Core signal reused from places.py: Google's Places API returns each business's
`websiteUri`. Absent — or pointing at social/directory/builder — means a lead.

mock=True gives deterministic fake businesses (free, no network), so the whole
tool runs for free until you flip MOCK_MODE=false in .env. Same pattern as the
rest of Niche Finder.
"""

import hashlib
import math
import random
from urllib.parse import urlparse

import requests

from .cache import Cache
from .cities import load_cities, select_cities
from .config import load_config, load_env

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"

# We ask Google for exactly the fields a caller needs. websiteUri +
# nationalPhoneNumber are billed at the Enterprise tier (~$35 / 1,000 Text
# Search calls) — the first 1,000 calls/month are free, which covers a couple
# of metros. nextPageToken (top-level, no "places." prefix) drives pagination.
FIELD_MASK = (
    "nextPageToken,"
    "places.id,places.displayName,places.nationalPhoneNumber,"
    "places.formattedAddress,places.websiteUri,places.rating,"
    "places.userRatingCount,places.businessStatus,places.googleMapsUri,"
    "places.primaryType,places.location"
)

MAX_PAGES = 3        # Google caps Text Search at 3 pages (~60 businesses/query)
PAGE_SIZE = 20
MAX_RADIUS_KM = 50   # drop any business farther than this from the town center.
                     # Google's location bias is soft and sometimes returns a
                     # match a whole metro away (a Vegas shop for a Mesquite
                     # search) — this + a hard search box keeps the list local.


def _haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km between two lat/lng points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

# Live-cost estimate. A Places Text Search call that asks for the phone +
# website (Enterprise-tier) fields runs ~$35 / 1,000 calls, and the first
# 1,000 calls per month are free — which covers a St. George sweep many times over.
PRICE_PER_CALL = 0.035
FREE_CALLS_PER_MONTH = 1000


def estimate_calls(n_cities, n_queries):
    """(low, high) Google API calls for a live run: each town × search phrase
    is 1–3 pages = 1–3 calls."""
    base = max(0, n_cities) * max(1, n_queries)
    return base, base * MAX_PAGES


def call_cost(calls):
    """Gross $ for N live calls (before the free monthly allotment)."""
    return round(calls * PRICE_PER_CALL, 2)


def _is_fatal(msg):
    """A systemic error (bad key / API not enabled / billing / quota) — one that
    will fail for EVERY town, so we stop the scan and surface it loudly rather
    than silently returning 0 leads."""
    m = msg.lower()
    return any(s in m for s in (
        "403", "401", "permission_denied", "request_denied", "api key",
        "api_key", "not been used", "is disabled", "service_disabled",
        "billing", "quota", "resource_exhausted", "429"))

# ---------------------------------------------------------------------------
#  Website classification — the heart of the tool
# ---------------------------------------------------------------------------
# Human-readable flavor labels (they double as the call-sheet "Website" column).
NONE = "No website"
SOCIAL = "Facebook / social only"
DIRECTORY = "Directory listing only"
BUILDER = "Free page only"
REAL = "Has a real site"          # the ONLY flavor that is NOT a lead

SOCIAL_DOMAINS = {
    "facebook.com", "m.facebook.com", "fb.me", "fb.com", "instagram.com",
    "linktr.ee", "linktree.com", "tiktok.com", "nextdoor.com", "twitter.com",
    "x.com", "youtube.com", "youtu.be", "pinterest.com", "snapchat.com",
}
DIRECTORY_DOMAINS = {
    "yelp.com", "yellowpages.com", "superpages.com", "angi.com",
    "angieslist.com", "homeadvisor.com", "thumbtack.com", "houzz.com",
    "bbb.org", "mapquest.com", "manta.com", "porch.com", "networx.com",
    "bark.com", "chamberofcommerce.com", "buildzoom.com", "expertise.com",
    "birdeye.com", "nicelocal.com", "cylex.us.com", "yellowbook.com",
    "hotfrog.com", "merchantcircle.com",
}
BUILDER_DOMAINS = {
    "business.site", "godaddysites.com", "wixsite.com", "wix.com",
    "weebly.com", "sites.google.com", "wordpress.com", "strikingly.com",
    "mystrikingly.com", "jimdosite.com", "square.site", "blogspot.com",
    "webnode.com", "webs.com", "yolasite.com", "carrd.co", "glossgenius.com",
}


def _host_of(url):
    """Bare lowercase hostname of a URL, minus a leading www."""
    if not url:
        return ""
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    try:
        host = urlparse(u).netloc.split("@")[-1].split(":")[0].lower().strip()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _matches(host, domain_set):
    """True if host IS one of the domains or a subdomain of one."""
    return any(host == d or host.endswith("." + d) for d in domain_set)


def classify_website(url):
    """Bucket a Google websiteUri into a lead 'flavor'.

    Returns NONE / SOCIAL / DIRECTORY / BUILDER / REAL. Everything except REAL
    is a callable no-real-site lead. Never trust mere PRESENCE of a link —
    Google auto-fills Facebook/directory URLs, so we always classify the domain.
    """
    host = _host_of(url)
    if not host:
        return NONE
    if _matches(host, SOCIAL_DOMAINS):
        return SOCIAL
    if _matches(host, DIRECTORY_DOMAINS):
        return DIRECTORY
    if _matches(host, BUILDER_DOMAINS):
        return BUILDER
    return REAL


def is_lead(flavor, phone, status):
    """A row is a callable lead iff it's operational, reachable, and no real site."""
    if status and status not in ("OPERATIONAL", "BUSINESS_STATUS_UNSPECIFIED"):
        return False
    if not phone:
        return False
    return flavor != REAL


def score_lead(flavor, rating, reviews):
    """0–100 call-worthiness. Higher = call first."""
    rating = rating or 0
    reviews = reviews or 0
    # Flavor: social = proven online intent (hottest); a total blank = clean
    # pitch; a directory link means they may already buy Angi/Thumbtack leads.
    score = {SOCIAL: 45, NONE: 30, BUILDER: 25, DIRECTORY: 10}.get(flavor, 0)
    # Review fit: proven demand + cash flow, not so big they already have help.
    if rating >= 4.0 and 8 <= reviews <= 75:
        score += 35
    elif reviews > 75:
        score += 15          # established, but may already have an agency
    elif 1 <= reviews < 8:
        score += 12
    # Reputation penalty — bad reviews poison the leads you'd rent them.
    if reviews >= 3 and 0 < rating < 3.7:
        score -= 20
    return max(0, min(100, score))


def priority(score):
    if score >= 65:
        return "HOT"
    if score >= 40:
        return "WARM"
    return "COOL"


# ---------------------------------------------------------------------------
#  The finder — harvest businesses for a trade in a town (live or mock)
# ---------------------------------------------------------------------------
class NoSiteFinder:
    def __init__(self, api_key, cache, mock=True):
        self.api_key = api_key
        self.cache = cache
        self.mock = mock
        self.mode = "mock" if mock else "live"
        self.calls = 0          # real (uncached) Google API calls this run

    def _rng(self, *parts):
        h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
        return random.Random(int(h[:12], 16))

    def businesses(self, query, city, state_name, lat, lng):
        """Every Google business for `query` in a town (cached 30d). Raw rows."""
        key = f"{self.mode}:nosite:{city},{state_name}:{query}"
        cached = self.cache.get(key, max_age_days=30)
        if cached is not None:
            return cached
        rows = (self._mock(query, city, state_name)
                if self.mock else self._live(query, city, state_name, lat, lng))
        self.cache.set(key, rows)
        return rows

    def _live(self, query, city, state_name, lat, lng):
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }
        body = {
            "textQuery": f"{query} in {city}, {state_name}",
            "maxResultCount": PAGE_SIZE,
            "locationBias": {"circle": {   # soft bias for recall; the real cutoff
                "center": {"latitude": lat, "longitude": lng},   # is MAX_RADIUS_KM
                "radius": 40000.0,
            }},
        }
        rows, token, pages = [], None, 0
        while pages < MAX_PAGES:
            if token:
                body["pageToken"] = token
            r = requests.post(PLACES_URL, headers=headers, json=body, timeout=60)
            self.calls += 1     # a billable page fetch (only when not cached)
            if r.status_code != 200:
                raise RuntimeError(f"Places API {r.status_code}: {r.text[:200]}")
            data = r.json()
            for p in (data.get("places") or []):
                rows.append({
                    "place_id": p.get("id"),
                    "name": (p.get("displayName") or {}).get("text", ""),
                    "phone": p.get("nationalPhoneNumber", ""),
                    "address": p.get("formattedAddress", ""),
                    "website": p.get("websiteUri", ""),
                    "rating": p.get("rating"),
                    "reviews": p.get("userRatingCount") or 0,
                    "status": p.get("businessStatus", ""),
                    "maps_url": p.get("googleMapsUri", ""),
                    "primary_type": p.get("primaryType", ""),
                    "lat": (p.get("location") or {}).get("latitude"),
                    "lng": (p.get("location") or {}).get("longitude"),
                })
            pages += 1
            token = data.get("nextPageToken")
            if not token:
                break
        return rows

    # ---- mock data (deterministic, free) ----
    STREETS = ["Main St", "Bluff St", "Red Rock Dr", "Sunset Blvd", "Dixie Dr",
               "Riverside Dr", "Telegraph St", "Sand Hollow Rd", "Canyon View Dr",
               "Valley View Dr", "Snow Canyon Pkwy", "Brigham Rd", "Tonaquint Dr"]
    SURNAMES = ["Hansen", "Barlow", "Stratton", "Iverson", "Mecham", "Palmer",
                "Snow", "Gubler", "Hafen", "Leavitt", "Prince", "Cottam",
                "Empey", "Reber", "Bundy", "Whitehead", "Larson", "Frei"]
    ADJ = ["Red Rock", "Desert", "Canyon", "Summit", "Dixie", "Zion", "Pioneer",
           "Southern Utah", "Legacy", "Blackridge", "Vista", "Sandstone"]

    def _core_word(self, trade):
        """Turn a search phrase into a display trade word ('roofing ...')."""
        drop = {"contractor", "contractors", "company", "companies", "service",
                "services", "near", "me", "installer", "installers", "repair",
                "installation", "cost", "the", "and", "&"}
        words = [w for w in trade.replace("/", " ").split() if w.lower() not in drop]
        core = " ".join(words[:2]) if words else trade
        return core.title()

    def _mock_site(self, rng, name_slug):
        roll = rng.random()
        if roll < 0.35:
            return f"https://www.{name_slug}.com"                 # REAL
        if roll < 0.58:
            return ""                                             # NONE
        if roll < 0.78:
            return f"https://www.facebook.com/{name_slug}"        # SOCIAL
        if roll < 0.90:
            return f"https://www.yelp.com/biz/{name_slug}-mock"   # DIRECTORY
        return f"https://{name_slug}.godaddysites.com"            # BUILDER

    def _mock(self, query, city, state_name):
        rng = self._rng("nosite", query, city, state_name)
        core = self._core_word(query)
        n = rng.randint(6, 18)
        rows = []
        for i in range(n):
            pattern = rng.randint(0, 3)
            if pattern == 0:
                name = f"{rng.choice(self.SURNAMES)} {core}"
            elif pattern == 1:
                name = f"{rng.choice(self.ADJ)} {core}"
            elif pattern == 2:
                name = f"{city} {core} Pros"
            else:
                name = f"{rng.choice(self.SURNAMES)} & Sons {core}"
            slug = name.lower().replace(" ", "").replace("&", "and")[:24]
            rows.append({
                "place_id": "mock_" + hashlib.md5(f"{city}{query}{i}".encode()).hexdigest()[:16],
                "name": name,
                "phone": f"(435) {rng.randint(200, 989)}-{rng.randint(1000, 9999)}",
                "address": f"{rng.randint(20, 4990)} {rng.choice(self.STREETS)}, {city}, {state_name}",
                "website": self._mock_site(rng, slug),
                "rating": round(min(5.0, max(2.8, rng.gauss(4.4, 0.4))), 1),
                "reviews": max(0, int(abs(rng.gauss(28, 35)))),
                "status": "OPERATIONAL",
                "maps_url": "https://maps.google.com/",
                "primary_type": core.lower().replace(" ", "_"),
            })
        return rows


# ---------------------------------------------------------------------------
#  Orchestrator — loop towns, harvest, classify, gate, dedup, score
# ---------------------------------------------------------------------------
def category_queries(niche_cfg, max_q=3):
    """Pick a few business-enumerating search phrases from a niche's keywords."""
    kws = niche_cfg.get("keywords", []) or []
    if not kws:
        return []
    prefer = ("contractor", "company", "installer", "service", "near me", "repair")
    picked = [k for k in kws if any(w in k.lower() for w in prefer)] or kws
    out = []
    for k in [kws[0]] + picked:          # primary keyword always leads
        if k not in out:
            out.append(k)
        if len(out) >= max_q:
            break
    return out


def _places_mock(env):
    """No-Site Finder only needs Google Places — live iff MOCK_MODE=false AND key set."""
    return env["mock_requested"] or not env["have_places"]


def find_leads(query=None, niche_key=None, region_key=None, limit=None,
               states=None, progress=None, min_score=0):
    """Return a ranked cold-call list of no-website businesses.

    Search term comes from `query` (any free text) if given, else from the
    niche's keywords. `niche_key` still adds ticket context to each row.
    """
    cfg = load_config()
    env = load_env()
    settings = cfg["settings"]

    niche = cfg["niches"].get(niche_key) if niche_key else None
    if query and query.strip():
        queries = [query.strip()]
    elif niche:
        queries = category_queries(niche)
    else:
        raise ValueError("Give a --query (business type) or a --niche.")

    mock = _places_mock(env)
    cache = Cache()
    finder = NoSiteFinder(env["google_places_key"], cache, mock=mock)

    region = cfg["regions"].get(region_key) if region_key else None
    cities = select_cities(load_cities(), settings, region)
    if states:
        cities = cities[cities["state_id"].isin(states)].reset_index(drop=True)
    if limit:
        cities = cities.head(limit)

    seen = {}            # place_id -> best lead record (dedup)
    scanned = 0
    errors = []          # per-town error messages (transient API hiccups)
    fatal = None         # a systemic error (bad key / API off) → abort the scan
    total = len(cities)
    for idx, row in cities.iterrows():
        city, state, state_name = row["city"], row["state_id"], row["state_name"]
        lat, lng = float(row["lat"]), float(row["lng"])
        try:
            for q in queries:
                for b in finder.businesses(q, city, state_name, lat, lng):
                    scanned += 1
                    blat, blng = b.get("lat"), b.get("lng")
                    if (blat is not None and blng is not None
                            and _haversine_km(lat, lng, blat, blng) > MAX_RADIUS_KM):
                        continue   # Google ranged out of the target area — drop it
                    flavor = classify_website(b.get("website"))
                    if not is_lead(flavor, b.get("phone"), b.get("status")):
                        continue
                    score = score_lead(flavor, b.get("rating"), b.get("reviews"))
                    if score < min_score:
                        continue
                    pid = b.get("place_id") or f"{b['name']}|{city}"
                    if pid in seen and seen[pid]["score"] >= score:
                        continue
                    seen[pid] = {
                        "place_id": pid,
                        "name": b.get("name", ""),
                        "phone": b.get("phone", ""),
                        "website_status": flavor,
                        "rating": b.get("rating"),
                        "reviews": b.get("reviews", 0),
                        "score": score,
                        "priority": priority(score),
                        "address": b.get("address", ""),
                        "city": city, "state": state,
                        "lat": blat if blat is not None else lat,
                        "lng": blng if blng is not None else lng,
                        "niche": niche_key or query,
                        "est_job_value": (niche or {}).get("job_value"),
                        "maps_url": b.get("maps_url", ""),
                        "website_link": b.get("website", ""),
                    }
        except Exception as e:      # one bad town shouldn't kill the whole sweep
            msg = str(e)
            errors.append(f"{city}: {msg}")
            if _is_fatal(msg):      # bad key / API off / billing → stop hammering
                fatal = msg
                if progress:
                    progress(total, total, city)
                break
        if progress:
            progress(idx + 1, total, city)

    leads = [r for r in seen.values() if r["score"] >= 0]
    leads.sort(key=lambda r: r["score"], reverse=True)
    return {
        "query": queries,
        "niche": niche_key,
        "niche_display": (niche or {}).get("display_name") if niche else (query or ""),
        "region": region_key,
        "mock": mock,
        "cities": total,
        "scanned": scanned,
        "lead_count": len(leads),
        "breakdown": summarize(leads),
        "api_calls": finder.calls,               # real Google calls made (0 if cached/mock)
        "api_cost": call_cost(finder.calls),     # gross $, before the free 1,000/mo
        "errors": errors,
        "fatal_error": fatal,
        "leads": leads,
    }


def summarize(leads):
    """Counts by flavor + priority for the dashboard header."""
    out = {"HOT": 0, "WARM": 0, "COOL": 0,
           SOCIAL: 0, NONE: 0, DIRECTORY: 0, BUILDER: 0}
    for r in leads:
        out[r["priority"]] = out.get(r["priority"], 0) + 1
        out[r["website_status"]] = out.get(r["website_status"], 0) + 1
    return out
