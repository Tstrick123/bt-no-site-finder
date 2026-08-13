"""DataForSEO client — search volume + CPC, and organic directory-share.

Runs in one of two modes:
  * mock=True  -> deterministic FAKE-but-realistic data, no network, free.
  * mock=False -> real live API calls (costs money, needs credentials).

All answers are cached in SQLite so we never pay for the same lookup twice.
"""

import hashlib
import random

import requests

DFS_BASE = "https://api.dataforseo.com/v3"

# Domains that mean "a directory ranks here, not a real local competitor" —
# a high share of these in the organic results = a weak, winnable field.
DIRECTORY_DOMAINS = {
    "yelp.com", "angi.com", "angieslist.com", "thumbtack.com", "homeadvisor.com",
    "bbb.org", "houzz.com", "porch.com", "networx.com", "buildzoom.com",
    "expertise.com", "facebook.com", "nextdoor.com", "mapquest.com", "manta.com",
    "yellowpages.com", "birdeye.com", "trustanalytica.com",
}


class DataForSEO:
    def __init__(self, login, password, cache, mock=True):
        self.login = login
        self.password = password
        self.cache = cache
        self.mock = mock
        # namespace the cache so mock rows never get served in live mode (or vice-versa)
        self.mode = "mock" if mock else "live"

    # deterministic RNG so mock data is stable across runs
    def _rng(self, *parts):
        h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
        return random.Random(int(h[:12], 16))

    # ---------- search volume + CPC ----------
    def search_volume(self, keywords, location_name, pop_hint=30000):
        key = f"{self.mode}:dfs_sv:{location_name}:" + "|".join(sorted(keywords))
        cached = self.cache.get(key, max_age_days=90)
        if cached is not None:
            return cached
        data = (self._mock_sv(keywords, location_name, pop_hint)
                if self.mock else self._live_sv(keywords, location_name))
        self.cache.set(key, data)
        return data

    def _mock_sv(self, keywords, location_name, pop_hint):
        scale = max(0.15, pop_hint / 40000)          # bigger city -> more volume
        per = {}
        total = 0
        for kw in keywords:
            rng = self._rng("sv", kw, location_name)
            base = rng.choice([20, 40, 70, 110, 170, 260, 390])
            head_bonus = 1.6 if len(kw.split()) <= 2 else 1.0   # short "head" terms get more volume
            vol = int(base * scale * head_bonus * rng.uniform(0.6, 1.4))
            vol = int(round(vol / 10) * 10)
            per[kw] = {"volume": vol, "cpc": round(rng.uniform(3, 18), 2),
                       "competition": round(rng.uniform(0.2, 0.95), 2)}
            total += vol
        return {"keywords": per, "total_volume": total, "mock": True}

    def _live_sv(self, keywords, location_name):
        url = f"{DFS_BASE}/keywords_data/google_ads/search_volume/live"
        body = [{"keywords": list(keywords), "location_name": location_name, "language_code": "en"}]
        r = requests.post(url, auth=(self.login, self.password), json=body, timeout=90)
        r.raise_for_status()
        task = (r.json().get("tasks") or [{}])[0]
        if task.get("status_code") != 20000:
            raise RuntimeError(f"DataForSEO search_volume error: {task.get('status_message')}")
        per = {}
        total = 0
        for it in (task.get("result") or []):
            vol = it.get("search_volume") or 0
            per[it.get("keyword")] = {"volume": vol, "cpc": it.get("cpc"),
                                      "competition": it.get("competition_index")}
            total += vol
        return {"keywords": per, "total_volume": total, "mock": False}

    # ---------- keyword discovery (like Keyword Planner's "get ideas") ----------
    def keyword_suggestions(self, seed, location_name="United States", limit=40):
        key = f"{self.mode}:dfs_kwsug:{location_name}:{seed.strip().lower()}"
        cached = self.cache.get(key, max_age_days=90)
        if cached is not None:
            return cached
        data = (self._mock_kwsug(seed, limit)
                if self.mock else self._live_kwsug(seed, location_name, limit))
        self.cache.set(key, data)
        return data

    def _mock_kwsug(self, seed, limit):
        seed = seed.strip().lower()
        pre = ["", "best ", "affordable ", "local ", "emergency "]
        post = ["", " near me", " cost", " contractor", " company", " service",
                " services", " installation", " repair", " prices", " quote", " estimate"]
        out, seen = [], set()
        for p in pre:
            for s in post:
                kw = (p + seed + s).strip()
                if kw in seen:
                    continue
                seen.add(kw)
                rng = self._rng("kwsug", kw)
                vol = int(round(rng.choice([10, 20, 40, 70, 110, 170, 260, 390, 590])
                                * rng.uniform(0.6, 1.4) / 10) * 10)
                out.append({"keyword": kw, "volume": vol,
                            "cpc": round(rng.uniform(3, 20), 2),
                            "competition": round(rng.uniform(0.2, 0.95), 2)})
        out.sort(key=lambda x: x["volume"], reverse=True)
        return {"seed": seed, "suggestions": out[:limit], "mock": True}

    def _live_kwsug(self, seed, location_name, limit):
        url = f"{DFS_BASE}/keywords_data/google_ads/keywords_for_keywords/live"
        body = [{"keywords": [seed], "location_name": location_name,
                 "language_code": "en", "sort_by": "search_volume"}]
        r = requests.post(url, auth=(self.login, self.password), json=body, timeout=90)
        r.raise_for_status()
        task = (r.json().get("tasks") or [{}])[0]
        if task.get("status_code") != 20000:
            raise RuntimeError(f"DataForSEO keyword ideas error: {task.get('status_message')}")
        out = []
        for it in (task.get("result") or [])[:limit]:
            out.append({"keyword": it.get("keyword"), "volume": it.get("search_volume") or 0,
                        "cpc": it.get("cpc"), "competition": it.get("competition_index")})
        return {"seed": seed, "suggestions": out, "mock": False}

    # ---------- organic directory share ----------
    def directory_share(self, keyword, location_name):
        key = f"{self.mode}:dfs_dir:{location_name}:{keyword}"
        cached = self.cache.get(key, max_age_days=30)
        if cached is not None:
            return cached
        val = (self._mock_dir(keyword, location_name)
               if self.mock else self._live_dir(keyword, location_name))
        self.cache.set(key, val)
        return val

    def _mock_dir(self, keyword, location_name):
        return round(self._rng("dir", keyword, location_name).uniform(0.2, 0.8), 2)

    def _live_dir(self, keyword, location_name):
        url = f"{DFS_BASE}/serp/google/organic/live/regular"
        body = [{"keyword": keyword, "location_name": location_name,
                 "language_code": "en", "depth": 10}]
        r = requests.post(url, auth=(self.login, self.password), json=body, timeout=90)
        r.raise_for_status()
        task = (r.json().get("tasks") or [{}])[0]
        if task.get("status_code") != 20000:
            raise RuntimeError(f"DataForSEO serp error: {task.get('status_message')}")
        result = (task.get("result") or [{}])[0]
        organic = [i for i in (result.get("items") or []) if i.get("type") == "organic"]
        if not organic:
            return 0.0
        dirs = sum(1 for i in organic
                   if any(d in (i.get("domain") or "").lower() for d in DIRECTORY_DOMAINS))
        return round(dirs / len(organic), 2)
