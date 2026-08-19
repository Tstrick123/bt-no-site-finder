"""Google Places API (New) client — local competitor census + review counts.

For a niche in a city it returns: how many businesses show up, their median
and max review counts, and how many have NO website. Those are the raw inputs
to the Rankability score (a field of low-review, no-website shops = winnable).

mock=True gives deterministic fake data (free, no network). mock=False calls
the real Places Text Search endpoint (first 1,000/month are free).
"""

import hashlib
import random

import requests

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ("places.displayName,places.rating,places.userRatingCount,"
              "places.websiteUri,places.businessStatus")


class Places:
    def __init__(self, api_key, cache, mock=True):
        self.api_key = api_key
        self.cache = cache
        self.mock = mock
        self.mode = "mock" if mock else "live"

    def _rng(self, *parts):
        h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
        return random.Random(int(h[:12], 16))

    def competitors(self, query, city, state_name, lat, lng):
        key = f"{self.mode}:places:{city},{state_name}:{query}"
        cached = self.cache.get(key, max_age_days=30)
        if cached is not None:
            return cached
        data = (self._mock(query, city, state_name)
                if self.mock else self._live(query, city, state_name, lat, lng))
        self.cache.set(key, data)
        return data

    def _mock(self, query, city, state_name):
        rng = self._rng("places", query, city, state_name)
        count = rng.randint(3, 20)
        reviews = sorted(max(0, int(rng.gauss(30, 40))) for _ in range(count))
        median = reviews[len(reviews) // 2] if reviews else 0
        return {
            "count": count,
            "median_reviews": median,
            "max_reviews": max(reviews) if reviews else 0,
            "no_website_share": round(rng.uniform(0.05, 0.45), 2),
            "mock": True,
        }

    def _live(self, query, city, state_name, lat, lng):
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }
        body = {
            "textQuery": f"{query} in {city}, {state_name}",
            "maxResultCount": 20,
            "locationBias": {"circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": 40000.0,  # 40 km around the city
            }},
        }
        r = requests.post(PLACES_URL, headers=headers, json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Places API {r.status_code}: {r.text[:200]}")
        places = r.json().get("places") or []
        counts = [p.get("userRatingCount") or 0 for p in places]
        n = len(places)
        no_site = sum(1 for p in places if not p.get("websiteUri"))
        median = sorted(counts)[len(counts) // 2] if counts else 0
        return {
            "count": n,
            "median_reviews": median,
            "max_reviews": max(counts) if counts else 0,
            "no_website_share": round(no_site / n, 2) if n else 0.0,
            "mock": False,
        }
