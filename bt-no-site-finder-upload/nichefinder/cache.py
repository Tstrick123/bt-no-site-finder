"""SQLite cache + results storage.

Two jobs:
  1. api_cache — never pay for the same API answer twice (the #1 cost control).
  2. results  — the last scan for each niche/city, so the dashboard can show
                results instantly without re-scanning.
"""

import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "cache.db"

RESULT_COLUMNS = [
    "niche", "city", "state", "county", "population", "lat", "lng",
    "national_cpc", "display_cpc", "city_volume",
    "rankability", "demand_score", "value_score", "opportunity",
    "rev_potential", "verdict",
    "competitor_count", "median_reviews", "max_reviews",
    "no_website_share", "directory_share", "mock", "scanned_at",
]


class Cache:
    def __init__(self, path=DB_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS api_cache ("
            "  key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at REAL NOT NULL)"
        )
        cols = ", ".join(f"{c} {'INTEGER' if c=='population' else 'REAL' if c not in ('niche','city','state','county','verdict','mock') else 'TEXT'}"
                         for c in RESULT_COLUMNS)
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS results ({cols}, PRIMARY KEY (niche, city, state))"
        )
        # No-Site Finder call log — remembers which leads you've already called.
        # Keyed by Google place_id (the one field Google lets you keep forever).
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS nosite_calls ("
            "  place_id TEXT PRIMARY KEY, called INTEGER DEFAULT 0,"
            "  notes TEXT DEFAULT '', updated_at REAL NOT NULL)"
        )
        # 'posted' marks a call already sent to the shared Google Sheet (dedup).
        try:
            self.conn.execute("ALTER TABLE nosite_calls ADD COLUMN posted INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        # GBP Fix Finder outreach log — remembers who you've already DM'd /
        # emailed / called about their Google Business Profile. Keyed by
        # place_id, kept separate from nosite_calls so the two tools don't mix.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS gbp_outreach ("
            "  place_id TEXT PRIMARY KEY, contacted INTEGER DEFAULT 0,"
            "  channel TEXT DEFAULT '', notes TEXT DEFAULT '', updated_at REAL NOT NULL)"
        )
        # No-Site Finder DM log (the 💬 Social DM list) — local fallback for when
        # no shared Google Sheet is connected. Keyed by place_id like the call log.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS nosite_dms ("
            "  place_id TEXT PRIMARY KEY, dm_status TEXT DEFAULT '', via TEXT DEFAULT '',"
            "  messaged_by TEXT DEFAULT '', facebook TEXT DEFAULT '',"
            "  instagram TEXT DEFAULT '', match TEXT DEFAULT '', note TEXT DEFAULT '',"
            "  updated_at REAL NOT NULL)"
        )
        # tiny key/value store for local UI settings (shared-sheet URL, your name)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.conn.commit()

    # ---- API response cache ----
    def get(self, key, max_age_days=90):
        row = self.conn.execute(
            "SELECT payload, fetched_at FROM api_cache WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        if max_age_days is not None and (time.time() - row["fetched_at"]) > max_age_days * 86400:
            return None
        return json.loads(row["payload"])

    def set(self, key, payload):
        self.conn.execute(
            "INSERT OR REPLACE INTO api_cache(key, payload, fetched_at) VALUES (?,?,?)",
            (key, json.dumps(payload), time.time()),
        )
        self.conn.commit()

    # ---- results table ----
    def save_result(self, record):
        vals = [record.get(c) for c in RESULT_COLUMNS]
        placeholders = ",".join("?" for _ in RESULT_COLUMNS)
        self.conn.execute(
            f"INSERT OR REPLACE INTO results ({','.join(RESULT_COLUMNS)}) VALUES ({placeholders})",
            vals,
        )
        self.conn.commit()

    def load_results(self, niche=None):
        if niche:
            rows = self.conn.execute("SELECT * FROM results WHERE niche=?", (niche,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM results").fetchall()
        return [dict(r) for r in rows]

    def scanned_niches(self):
        rows = self.conn.execute("SELECT DISTINCT niche FROM results").fetchall()
        return [r["niche"] for r in rows]

    # ---- No-Site Finder call log ----
    def save_call_status(self, place_id, called, notes=""):
        self.conn.execute(
            "INSERT OR REPLACE INTO nosite_calls(place_id, called, notes, updated_at) "
            "VALUES (?,?,?,?)",
            (place_id, 1 if called else 0, notes or "", time.time()),
        )
        self.conn.commit()

    def load_call_statuses(self):
        """place_id -> {'called': bool, 'notes': str, 'posted': bool} per logged lead."""
        rows = self.conn.execute(
            "SELECT place_id, called, notes, posted FROM nosite_calls").fetchall()
        return {r["place_id"]: {"called": bool(r["called"]), "notes": r["notes"] or "",
                                "posted": bool(r["posted"])}
                for r in rows}

    def mark_posted(self, place_id):
        self.conn.execute("UPDATE nosite_calls SET posted=1 WHERE place_id=?", (place_id,))
        self.conn.commit()

    # ---- GBP Fix Finder outreach log ----
    def save_outreach(self, place_id, contacted, channel="", notes=""):
        self.conn.execute(
            "INSERT OR REPLACE INTO gbp_outreach(place_id, contacted, channel, notes, updated_at) "
            "VALUES (?,?,?,?,?)",
            (place_id, 1 if contacted else 0, channel or "", notes or "", time.time()),
        )
        self.conn.commit()

    def load_outreach(self):
        """place_id -> {'contacted': bool, 'channel': str, 'notes': str}."""
        rows = self.conn.execute(
            "SELECT place_id, contacted, channel, notes FROM gbp_outreach").fetchall()
        return {r["place_id"]: {"contacted": bool(r["contacted"]),
                                "channel": r["channel"] or "", "notes": r["notes"] or ""}
                for r in rows}

    # ---- No-Site Finder DM log ----
    DM_COLS = ("dm_status", "via", "messaged_by", "facebook", "instagram", "match", "note")

    def save_dm(self, place_id, rec):
        vals = [str(rec.get(c, "") or "") for c in self.DM_COLS]
        self.conn.execute(
            f"INSERT OR REPLACE INTO nosite_dms(place_id, {', '.join(self.DM_COLS)}, updated_at) "
            f"VALUES (?{',?' * len(self.DM_COLS)},?)",
            (place_id, *vals, time.time()),
        )
        self.conn.commit()

    def load_dms(self):
        """place_id -> {dm_status, via, messaged_by, facebook, instagram, match, note}."""
        rows = self.conn.execute(
            f"SELECT place_id, {', '.join(self.DM_COLS)} FROM nosite_dms").fetchall()
        return {r["place_id"]: {c: r[c] or "" for c in self.DM_COLS} for r in rows}

    # ---- local UI settings (shared-sheet URL, caller name) ----
    def get_setting(self, key, default=""):
        row = self.conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO app_settings(key, value) VALUES (?,?)", (key, value or ""))
        self.conn.commit()
