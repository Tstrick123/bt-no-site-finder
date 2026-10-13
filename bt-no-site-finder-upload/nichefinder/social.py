"""Social-page finder for the DM list.

For each no-website business it attaches the Facebook page and Instagram
profile, so Tate and Ben can message the owner there instead of cold-calling.

Three sources, cheapest first. A link is only attached when we can trust it:
  1. Google's own link. The listing's website field IS a Facebook/Instagram
     page. Free and certain.
  2. Their free page. A Wix/GoDaddy/Linktree page that links out to FB/IG.
     One free page fetch; the business owns that page, so the link is theirs.
  3. A web search for '"<name>" <city>' limited to facebook.com and
     instagram.com, through Tavily (1,000 free a month, the default), Serper.dev
     or DataForSEO, whichever key is set. A hit is
     kept only when its page title / handle matches the business name, and it's
     labeled "strong match" or "check it" so a person eyeballs the shaky ones.

We never fetch facebook.com or instagram.com ourselves (their terms forbid
scraping and they block it anyway). Every lookup is cached 30 days by place_id,
so the same business is never paid for twice.

mock=True returns deterministic fake search hits (free, no network) and runs
them through the SAME matcher, same pattern as the rest of the tool.
"""

import concurrent.futures as cf
import hashlib
import random
import re
import time
from difflib import SequenceMatcher
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

from .nosite import BUILDER_DOMAINS, _host_of, _matches

SERPER_URL = "https://google.serper.dev/search"
TAVILY_URL = "https://api.tavily.com/search"
DFS_SERP_URL = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"

# $ per search. Tavily: 1,000 free every month (no card), then $0.008 each.
# Serper: $50 buys 50k after the free 2,500 (credits last 6 mo).
# DataForSEO live SERP: $0.002 each ($50 minimum deposit).
COST_PER_SEARCH = {"tavily": 0.0, "serper": 0.001, "dataforseo": 0.002, "mock": 0.0}
TAVILY_FREE_MONTHLY = 1000
SERPER_FREE = 2500
WORKERS = 8                 # parallel lookups (HTTP only; the cache stays on the main thread)
CACHE_DAYS = 30

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# How a link was found (shown in the "How we found it" column).
GOOGLE = "on their Google listing"
SITE = "on their own page"
STRONG = "strong match"
CHECK = "check it"
MANUAL = "added by hand"
SURE = {GOOGLE, SITE, STRONG, MANUAL}

# Hosts whose page we fetch to look for FB/IG links (free builder pages +
# link-in-bio pages). Directories (Yelp etc.) are never fetched.
_LINK_IN_BIO = {"linktr.ee", "linktree.com", "beacons.ai", "lnk.bio", "campsite.bio"}


# ---------------------------------------------------------------------------
#  Facebook + Instagram link parsing
# ---------------------------------------------------------------------------
_FB_HOST_RX = re.compile(r"(^|\.)(facebook\.com|fb\.com|fb\.me)$")
# First path segments that are never a business page.
_FB_NOT_PAGES = {
    "groups", "events", "marketplace", "watch", "gaming", "hashtag", "login",
    "login.php", "sharer", "sharer.php", "share", "share.php", "dialog",
    "plugins", "help", "policies", "privacy", "policy.php", "settings",
    "search", "photo.php", "photo", "photos", "story.php", "permalink.php",
    "media", "reel", "reels", "stories", "live", "ads", "business", "notes",
    "places", "public", "home.php", "l.php", "messages", "friends",
    "fundraisers", "jobs", "legal", "directory", "tr", "video.php", "videos",
    "watchparty", "bookmarks", "careers", "recover", "r.php", "about",
    # page-URL prefixes handled above; if one falls through it's malformed
    "pages", "p", "pg", "people", "profile.php",
    # Facebook's auto-generated SEO pages ("Lakeview Ut Water Damage Repair")
    "fb-answers",
}
# instagram.com/<seg> path segments that are NOT a username.
_IG_NOT_HANDLES = {
    "p", "reel", "reels", "explore", "accounts", "about", "developer",
    "directory", "tv", "stories", "legal", "privacy", "web", "help",
    "session", "emails", "challenge", "oauth", "sharer",
}
# Website-builder / platform accounts that show up in a page's footer. Never a
# lead's own page (same list find_ig_dm_leads.py uses, applied to FB too).
_PLATFORM = {
    "squarespace", "wix", "wixcom", "godaddy", "weebly", "wordpress",
    "wordpressdotcom", "wordpresscom", "shopify", "bigcommerce", "squareup",
    "square", "webflow", "duda", "jimdo", "strikingly", "carrd", "yelp",
    "google", "googlemybusiness", "facebook", "meta", "instagram", "youtube",
    "tiktok", "twitter", "x", "linkedin", "pinterest", "mailchimp", "hubspot",
    "godaddystudio", "glossgenius", "vagaro", "boulevard", "getvagaro",
    "mangomint", "aesthetixmd", "nextdoor", "linktree", "linktr.ee",
}
# Gambling / SEO spam handles injected into hacked sites.
_SPAM_RX = re.compile(r"(togel|gacor|slot88|slotgacor|casino|judi|sbobet|pkv|situs)", re.I)


def _parse(url):
    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    try:
        return urlparse(u)
    except Exception:
        return None


def _fb_slug(seg):
    """('slug', name) or ('id', digits) for one facebook path segment."""
    s = unquote(seg).strip()
    if s.isdigit():
        return ("id", s)
    m = re.search(r"-(\d{6,})$", s)          # old style "Drywall-Paul-123456789"
    if m:
        return ("id", m.group(1))
    if not re.fullmatch(r"[A-Za-z0-9.\-]{3,80}", s):
        return None
    if s.lower() in _PLATFORM or _SPAM_RX.search(s):
        return None
    return ("slug", s)


def _fb_ref(url):
    """('slug', name) or ('id', digits) for a Facebook PAGE link, else None.

    Groups, events, shares, login walls and other non-page links return None.
    A post/photo link (facebook.com/<page>/posts/...) reduces to its page.
    """
    p = _parse(url)
    if not p:
        return None
    host = p.netloc.lower().split("@")[-1].split(":")[0]
    if not _FB_HOST_RX.search(host):
        return None
    segs = [s for s in p.path.split("/") if s]
    if not segs:
        return None
    first = segs[0].lower()
    if first == "profile.php":
        pid = (parse_qs(p.query).get("id") or [""])[0]
        return ("id", pid) if pid.isdigit() else None
    if first == "pg" and len(segs) > 1:                       # /pg/<page>/about
        return _fb_slug(segs[1])
    if first == "p" and len(segs) > 1:                        # /p/Name-100063.../
        m = re.search(r"(\d{6,})$", segs[1])
        return ("id", m.group(1)) if m else None
    if first == "people" and len(segs) > 2 and segs[2].isdigit():
        return ("id", segs[2])                                # /people/Name/1000.../
    if first == "pages" and len(segs) > 2 and segs[1].lower() != "category":
        return ("id", segs[-1]) if segs[-1].isdigit() else None
    if first in _FB_NOT_PAGES:
        return None
    return _fb_slug(segs[0])


def _is_item(url):
    """True for a single post/video/photo/reel link (facebook.com/<page>/posts/<id>,
    instagram.com/p/<code>), as opposed to a profile or one of its tabs.

    An item's search title describes the CONTENT, which may be another page
    talking about the business (a Parade of Homes video about Alcoa), so items
    are judged on the page/handle name alone, never on the caption.
    """
    p = _parse(url)
    if not p:
        return False
    segs = [s for s in p.path.split("/") if s]
    if not segs:
        return False
    first = segs[0].lower()
    if first in ("p", "reel", "reels", "tv") and "instagram" in p.netloc.lower():
        return True
    if first in ("p", "people", "pages", "profile.php"):
        return False                     # FB page URL forms, no item part
    if first == "pg":
        segs = segs[1:]
    return len(segs) >= 3                # <page>/<tab>/<item>


def fb_page(url):
    """Canonical Facebook page URL for any Facebook link, or '' if it isn't a page."""
    ref = _fb_ref(url)
    if not ref:
        return ""
    kind, val = ref
    if kind == "id":
        return f"https://www.facebook.com/profile.php?id={val}"
    return f"https://www.facebook.com/{val}"


def fb_message_url(url):
    """m.me link that opens a Messenger chat with the page (app on a phone)."""
    ref = _fb_ref(url)
    return f"https://m.me/{ref[1]}" if ref else ""


def ig_handle(url, title=""):
    """Instagram username from a profile link ('' if not a profile).

    A post/reel link counts only when the hit's title names the poster
    ("... (@handle) ..."), since a post can belong to anyone.
    """
    p = _parse(url)
    if not p:
        return ""
    host = p.netloc.lower().split("@")[-1].split(":")[0]
    if not (host == "instagram.com" or host.endswith(".instagram.com") or host == "instagr.am"):
        return ""
    segs = [s for s in p.path.split("/") if s]
    if not segs:
        return ""
    h = segs[0]
    if h.lower() in ("p", "reel", "reels", "tv"):
        m = re.search(r"\(@([A-Za-z0-9_.]{2,30})\)", title or "")
        if not m:
            return ""
        h = m.group(1)
    h = h.strip(".").lower()
    if not re.fullmatch(r"[a-z0-9_.]{1,30}", h):
        return ""
    if h in _IG_NOT_HANDLES or h in _PLATFORM or _SPAM_RX.search(h):
        return ""
    return h


def ig_profile_url(handle):
    return f"https://www.instagram.com/{handle}/" if handle else ""


def ig_dm_url(handle):
    """ig.me link that opens an Instagram DM to the account."""
    return f"https://ig.me/m/{handle}" if handle else ""


def fb_search_url(name, city):
    """Facebook's own page search, for a person to find the page by eye."""
    return "https://www.facebook.com/search/pages/?q=" + quote_plus(f"{name} {city}")


def ig_search_url(name, city, state):
    return "https://www.google.com/search?q=" + quote_plus(
        f"{name} {city} {state} site:instagram.com")


def links_in_html(html):
    """First real FB page + IG handle linked from a page's HTML (either may be '')."""
    text = (html or "").replace("\\/", "/")      # JSON-escaped links on Linktree pages
    fb, ig = "", ""
    for m in re.finditer(r"https?://(?:[a-z0-9\-]+\.)?(?:facebook\.com|fb\.com|fb\.me)/[^\s\"'<>\\)]+",
                         text, re.I):
        fb = fb_page(m.group(0))
        if fb:
            break
    for m in re.finditer(r"https?://(?:www\.)?instagram\.com/[^\s\"'<>\\)]+", text, re.I):
        ig = ig_handle(m.group(0))
        if ig:
            break
    return fb, ig


# ---------------------------------------------------------------------------
#  Is this search hit THIS business?
# ---------------------------------------------------------------------------
_LEGAL = {"llc", "inc", "co", "corp", "corporation", "company", "ltd", "pllc",
          "lc", "the", "and", "of", "a", "dba", "l", "c"}


def _stem(t):
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def _tokens(s):
    s = (s or "").lower().replace("&", " and ").replace("'", "").replace("’", "")
    return [_stem(t) for t in re.split(r"[^a-z0-9]+", s) if t]


# Trade + filler words lots of unrelated businesses share. A match on these
# alone proves nothing, so a name needs at least one OTHER word to match.
_GENERIC = {_stem(w) for w in (
    "construction concrete contracting contractor contractors services service "
    "landscaping landscape landscapes roofing roofers plumbing plumber electric "
    "electrical electrician painting painters cleaning cleaners repair repairs "
    "remodeling remodel home homes pros pro solutions group enterprises builders "
    "building design designs lawn care pest control hvac heating air cooling "
    "flooring floors drywall fencing fence fences excavation excavating masonry "
    "handyman maintenance custom quality premier elite affordable best local "
    "sons son brothers bros family llc inc utah company co works outdoor "
    "exteriors exterior interiors interior installation install restoration "
    "detailing auto mobile window windows glass tree trees junk removal moving "
    "movers storage insulation coatings epoxy cabinets countertops pools pool "
    "spa garage door doors solar septic well wells water damage mold "
    "remediation fire flood emergency disaster cleanup abatement renovation "
    "renovations general realty real estate property properties management "
    "law attorney attorneys legal office clinic medical health dental med "
    "aesthetics beauty salon studio coating polishing specialties specialty "
    "restore restoring polish"
).split()}

_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}


_PLACE = {ab.lower() for ab in _STATES} | {
    _stem(t) for nm in _STATES.values() for t in nm.lower().split()}


def _title_head(title):
    """The page's name part of a search title ('Name | Facebook' -> 'Name')."""
    t = re.split(r"\s+[|•·]\s+|\s+-\s+|\s+–\s+|\s+\(@", title or "")[0]
    return t.strip()


def _locality(text, city, state):
    """'local' if the text names our town/state, 'foreign' if it names only
    another state, '' if it says nothing about place."""
    low = (text or "").lower()
    name = _STATES.get((state or "").upper(), "")
    if (city and city.lower() in low) or (name and name.lower() in low) \
            or (state and re.search(rf"\b{re.escape(state.upper())}\b", text or "")):
        return "local"
    for ab, nm in _STATES.items():
        if ab != (state or "").upper() and re.search(rf"\b{re.escape(nm.lower())}\b", low):
            return "foreign"
    return ""


def judge(name, city, state, title, snippet="", handle=""):
    """STRONG / CHECK / '' for one search hit claiming to be business `name`."""
    city_toks = set(_tokens(city)) | {"st", "saint"}
    core = [t for t in _tokens(name) if t not in _LEGAL]
    if not core:
        return ""
    distinct = [t for t in core if t not in _GENERIC and t not in city_toks
                and t not in _PLACE and len(t) > 1]
    head = [t for t in _tokens(_title_head(title)) if t not in _LEGAL]
    title_toks = set(_tokens(title))
    hcompact = re.sub(r"[^a-z0-9]", "", (handle or "").lower())

    def _part(a, b):
        """a word inside a longer word ('brow' in 'eyebrow'), 4+ letters only"""
        return len(a) >= 4 and a in b

    def _seen(t):
        return (t in title_toks or (len(t) >= 3 and t in hcompact)
                or any(_part(t, w) for w in title_toks))

    # The name's distinctive word(s) must show up, or it's someone else.
    if distinct and not any(_seen(t) for t in distinct):
        return ""
    compact = "".join(core)
    compact_and = "".join(t for t in _tokens(name) if t not in _LEGAL - {"and"})
    slug_hit = bool(hcompact) and len(compact) >= 6 and (
        compact in hcompact or compact_and in hcompact
        or (len(hcompact) >= 6 and hcompact in compact))
    seq = SequenceMatcher(None, compact, "".join(head)).ratio() if head else 0.0
    over_name = sum(1 for t in core if _seen(t)) / len(core)
    over_title = (sum(1 for t in head if t in core or any(_part(c, t) for c in core))
                  / len(head)) if head else 0.0
    score = max(seq, (over_name + over_title) / 2, 1.0 if slug_hit else 0.0)

    where = _locality(f"{title} {snippet}", city, state)
    if not distinct:
        # An all-common-words name ("UT Water Restoration") proves nothing by
        # overlap: every restoration page in Utah shares those words. It only
        # matches a near-identical page name, and only strongly when local.
        if not (seq >= 0.85 or slug_hit or (over_name == 1 and over_title == 1)):
            return ""
        if where != "local":
            score = min(score, 0.75)
    if score >= 0.85:
        # Strong needs the NAME itself (near-identical spelling or handle), or
        # matching words AND our town on the page. "Paul's Drywall" for "Drywall
        # Paul" with no location shown is likely, not certain.
        named = seq >= 0.85 or slug_hit
        if where == "foreign" or not (named or where == "local"):
            return CHECK
        return STRONG
    if score >= 0.6:
        return CHECK
    return ""


def pick(results, name, city, state):
    """Best FB page + IG handle for a business from one search's hits.

    Returns {'facebook', 'fb_match', 'instagram', 'ig_match'} ('' when none).
    A strong match beats a check-it; ties go to the higher-ranked hit.
    """
    out = {"facebook": "", "fb_match": "", "instagram": "", "ig_match": ""}
    rank = {STRONG: 2, CHECK: 1}
    for r in results or []:
        link, title, snip = r.get("link", ""), r.get("title", ""), r.get("snippet", "")
        item = _is_item(link)
        ref = _fb_ref(link)
        if ref:
            # An item's title is its caption, so judge it on the page name only.
            verdict = judge(name, city, state, "" if item else title, "" if item else snip,
                            handle=ref[1] if ref[0] == "slug" else "")
            if verdict and rank[verdict] > rank.get(out["fb_match"], 0):
                out["facebook"], out["fb_match"] = fb_page(link), verdict
            continue
        h = ig_handle(link, title)
        if h:
            verdict = judge(name, city, state, "" if item else title, "" if item else snip,
                            handle=h)
            if verdict and rank[verdict] > rank.get(out["ig_match"], 0):
                out["instagram"], out["ig_match"] = h, verdict
    return out


# ---------------------------------------------------------------------------
#  The finder: cache + search backends (live or mock)
# ---------------------------------------------------------------------------
# LLC/Inc anywhere; "Co" only as the last word ("Smith & Co Roofing" keeps it).
_LEGAL_SUFFIX_RX = re.compile(
    r"\b(?:L\.?L\.?C|P\.?L\.?L\.?C|Inc|Corp|Ltd)\b\.?|\bCo\b\.?\s*$", re.I)


def tavily_query(name, city):
    """'"Big D Specialties" Cedar City': the exact name in quotes + the town.

    Calibrated 10-13 on 20 real Utah no-site leads: the quoted form found every
    page the plain "<name> <city> <ST>" query did, plus 3 more (Drywall Paul,
    Big D Specialties, Royal Epoxy), at the same 1 credit.
    """
    clean = _LEGAL_SUFFIX_RX.sub(" ", (name or "").replace('"', ""))
    clean = re.sub(r"\s*,\s*(?=\(|$)", " ", clean)      # comma left dangling
    clean = re.sub(r"\s{2,}", " ", clean).strip(" ,-")
    return f'"{clean}" {city}'.strip()


class SearchError(RuntimeError):
    """A search problem that will hit every lookup (bad key, out of credit)."""


class SocialFinder:
    def __init__(self, env, cache, mock=True):
        self.cache = cache
        self.mock = mock
        self.mode = "mock" if mock else "live"
        self.tavily_key = env.get("tavily_key", "")
        self.serper_key = env.get("serper_key", "")
        self.dfs = ((env.get("dataforseo_login"), env.get("dataforseo_password"))
                    if env.get("have_dataforseo") else None)
        if mock:
            self.backend = "mock"
        elif self.tavily_key:
            self.backend = "tavily"
        elif self.serper_key:
            self.backend = "serper"
        elif self.dfs:
            self.backend = "dataforseo"
        else:
            self.backend = None          # no key: free sources + manual search only

    def cost(self, n):
        return n * COST_PER_SEARCH.get(self.backend or "", 0.0)

    def _key(self, pid):
        return f"{self.mode}:social:{pid}"

    def cached(self, pid):
        return self.cache.get(self._key(pid), max_age_days=CACHE_DAYS)

    def remember(self, pid, rec):
        self.cache.set(self._key(pid), rec)

    # ---- one web search (runs in a worker thread: HTTP only, no cache) ----
    def search(self, name, city, state):
        q = (f"{name.replace(chr(34), '')} {city} {state} "
             "(site:facebook.com OR site:instagram.com)")
        if self.backend == "mock":
            return self._mock(name, city)
        if self.backend == "tavily":
            return self._tavily(tavily_query(name, city))
        if self.backend == "serper":
            r = requests.post(SERPER_URL, timeout=30,
                              headers={"X-API-KEY": self.serper_key,
                                       "Content-Type": "application/json"},
                              json={"q": q, "gl": "us", "hl": "en", "num": 10})
            if r.status_code in (401, 402, 403) or "credit" in r.text[:300].lower():
                raise SearchError(f"Serper {r.status_code}: {r.text[:200]}")
            if r.status_code != 200:
                raise RuntimeError(f"Serper {r.status_code}: {r.text[:200]}")
            return [{"title": i.get("title", ""), "link": i.get("link", ""),
                     "snippet": i.get("snippet", "")}
                    for i in (r.json().get("organic") or [])]
        if self.backend == "dataforseo":
            r = requests.post(DFS_SERP_URL, auth=self.dfs, timeout=60,
                              json=[{"keyword": q, "location_code": 2840,
                                     "language_code": "en", "depth": 10}])
            if r.status_code in (401, 402, 403):
                raise SearchError(f"DataForSEO {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            task = (r.json().get("tasks") or [{}])[0]
            code = task.get("status_code") or 0
            if code // 100 in (401, 402):
                raise SearchError(f"DataForSEO: {task.get('status_message')}")
            if code != 20000:
                raise RuntimeError(f"DataForSEO: {task.get('status_message')}")
            items = ((task.get("result") or [{}])[0] or {}).get("items") or []
            return [{"title": i.get("title", ""), "link": i.get("url", ""),
                     "snippet": i.get("description", "")}
                    for i in items if i.get("type") == "organic"]
        raise SearchError("no search key configured")

    def _tavily(self, q):
        """Tavily search limited to facebook.com + instagram.com (1 credit)."""
        body = {"query": q, "search_depth": "basic", "max_results": 10,
                "include_domains": ["facebook.com", "instagram.com"]}
        headers = {"Authorization": f"Bearer {self.tavily_key}",
                   "Content-Type": "application/json"}
        for attempt in range(3):
            r = requests.post(TAVILY_URL, json=body, headers=headers, timeout=30)
            if r.status_code == 429 and attempt < 2:      # rate limit: wait, retry
                try:
                    wait = float(r.headers.get("Retry-After", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(min(max(wait, 1.0), 10.0))
                continue
            break
        if r.status_code in (401, 432, 433):   # bad key / monthly credit used up
            raise SearchError(f"Tavily {r.status_code}: {r.text[:200]}")
        if r.status_code != 200:
            raise RuntimeError(f"Tavily {r.status_code}: {r.text[:200]}")
        return [{"title": i.get("title", ""), "link": i.get("url", ""),
                 "snippet": i.get("content", "")}
                for i in (r.json().get("results") or [])]

    def _mock(self, name, city):
        h = hashlib.md5(f"social|{name}|{city}".encode()).hexdigest()
        rng = random.Random(int(h[:12], 16))
        slug = re.sub(r"[^a-z0-9]", "", name.lower())[:28]
        hits = []
        if rng.random() < 0.55:
            hits.append({"title": f"{name} | {city} | Facebook",
                         "link": f"https://www.facebook.com/{slug}",
                         "snippet": f"{name}, {city}. 312 likes."})
        if rng.random() < 0.35:
            hits.append({"title": f"{name} (@{slug}) • Instagram photos and videos",
                         "link": f"https://www.instagram.com/{slug}/",
                         "snippet": "120 Followers, 80 Following, 45 Posts"})
        if rng.random() < 0.3:      # an unrelated page the matcher must reject
            hits.append({"title": "Desert Sun Pizza | Facebook",
                         "link": "https://www.facebook.com/desertsunpizza",
                         "snippet": "Pizza place"})
        return hits


def fetch_page(url, timeout=8):
    """HTML of a business's free page ('' on any problem). Thread-safe."""
    p = _parse(url)
    if not p or p.scheme not in ("http", "https"):
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        if r.status_code != 200:
            return ""
        return r.text[:1_500_000]
    except Exception:
        return ""


def from_google(lead):
    """The free first pass: what the Google listing itself links to."""
    rec = {"facebook": "", "fb_match": "", "instagram": "", "ig_match": "",
           "paged": False, "searched": False}
    link = lead.get("website_link", "")
    fb = fb_page(link)
    if fb:
        rec["facebook"], rec["fb_match"] = fb, GOOGLE
    h = ig_handle(link)
    if h:
        rec["instagram"], rec["ig_match"] = h, GOOGLE
    return rec


def _pageable(lead):
    host = _host_of(lead.get("website_link", ""))
    return bool(host) and (_matches(host, BUILDER_DOMAINS) or _matches(host, _LINK_IN_BIO))


def _own_page_label(lead, fb="", ig=""):
    """SITE when a link on their own page looks like theirs, else CHECK.

    Builder templates embed other accounts (a Wix dental site linked a
    'KidsLearningVideo' page), so a named handle must resemble the business.
    A numeric-ID Facebook link carries no name to compare, so it stays SITE.
    """
    ref = _fb_ref(fb) if fb else None
    if fb and (not ref or ref[0] == "id"):
        return SITE
    handle = ref[1] if ref else ig
    sure = judge(lead.get("name", ""), lead.get("city", ""), lead.get("state", ""), "",
                 handle=handle)
    return SITE if sure else CHECK


def needs_lookup(lead, rec, finder):
    """True if a lookup could still add a link this business is missing."""
    if rec.get("facebook") and rec.get("instagram"):
        return False
    if not rec.get("paged") and _pageable(lead) and not finder.mock:
        return True
    return bool(finder.backend) and not rec.get("searched")


def quick_links(lead, finder):
    """Google link + anything already cached. No network, safe to call on every render."""
    hit = finder.cached(lead["place_id"])
    return hit if hit is not None else from_google(lead)


def find_socials(leads, finder, progress=None):
    """Look up FB/IG for `leads` (already ordered best-first).

    Returns (results, info). results maps place_id -> record; info has
    searches (paid lookups made), cost, pages (free pages fetched) and fatal
    (a key/credit error that stopped the searches, else '').
    """
    results = {ld["place_id"]: quick_links(ld, finder) for ld in leads}
    info = {"searches": 0, "cost": 0.0, "pages": 0, "fatal": ""}
    total = len(leads)
    done = 0

    # 1. free pages (Wix/GoDaddy/Linktree...) the business itself controls
    to_page = [ld for ld in leads
               if not finder.mock and not results[ld["place_id"]].get("paged")
               and _pageable(ld)
               and not (results[ld["place_id"]]["facebook"]
                        and results[ld["place_id"]]["instagram"])]
    if to_page:
        with cf.ThreadPoolExecutor(WORKERS) as ex:
            futs = {ex.submit(fetch_page, ld["website_link"]): ld for ld in to_page}
            for f in cf.as_completed(futs):
                ld = futs[f]
                rec = results[ld["place_id"]]
                fb, ig = links_in_html(f.result())
                if fb and not rec["facebook"]:
                    rec["facebook"], rec["fb_match"] = fb, _own_page_label(ld, fb=fb)
                if ig and not rec["instagram"]:
                    rec["instagram"], rec["ig_match"] = ig, _own_page_label(ld, ig=ig)
                rec["paged"] = True
                info["pages"] += 1

    # 2. web search for anyone still missing a link
    to_search = [ld for ld in leads
                 if finder.backend and not results[ld["place_id"]].get("searched")
                 and not (results[ld["place_id"]]["facebook"]
                          and results[ld["place_id"]]["instagram"])]
    skip = total - len(to_search)
    done = skip
    if progress:
        progress(done, total, "")
    if to_search:
        ex = cf.ThreadPoolExecutor(WORKERS)
        futs = {ex.submit(finder.search, ld.get("name", ""), ld.get("city", ""),
                          ld.get("state", "")): ld for ld in to_search}
        try:
            for f in cf.as_completed(futs):
                ld = futs[f]
                rec = results[ld["place_id"]]
                done += 1
                try:
                    hits = f.result()
                except SearchError as e:
                    info["fatal"] = str(e)[:300]
                    for other in futs:
                        other.cancel()
                    break
                except Exception:
                    continue             # one flaky lookup: leave it for next time
                info["searches"] += 1
                found = pick(hits, ld.get("name", ""), ld.get("city", ""), ld.get("state", ""))
                if found["facebook"] and not rec["facebook"]:
                    rec["facebook"], rec["fb_match"] = found["facebook"], found["fb_match"]
                if found["instagram"] and not rec["instagram"]:
                    rec["instagram"], rec["ig_match"] = found["instagram"], found["ig_match"]
                rec["searched"] = True
                if progress:
                    progress(done, total, ld.get("name", ""))
        finally:
            ex.shutdown(wait=True, cancel_futures=True)
    info["cost"] = finder.cost(info["searches"])

    for pid, rec in results.items():       # main thread only: SQLite isn't thread-safe
        if rec.get("paged") or rec.get("searched"):
            finder.remember(pid, rec)
    return results, info


def match_label(rec):
    """One short 'how we found it' string for the table."""
    parts = []
    for tag, link, how in (("FB", "facebook", "fb_match"), ("IG", "instagram", "ig_match")):
        if rec.get(link):
            mark = "✅" if rec.get(how) in SURE else "⚠️"
            parts.append(f"{tag} {mark} {rec.get(how, '')}")
    return " · ".join(parts)
