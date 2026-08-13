"""Optional: mirror the 'Called?' log to a shared Google Sheet.

Uses a no-credentials trick: you make a Google Sheet, paste a tiny Apps Script
into it that appends a row on POST, deploy it as a web app, and paste that URL
into the tool (sidebar → Shared Google Sheet). Both partners paste the SAME
URL, so every call either of you logs lands in ONE shared sheet — no double
calling. No URL set = local-only, nothing breaks.
"""

import requests

_TRUE = {"1", "true", "yes", "y", "called", "x"}


def fetch_calls(url, timeout=15):
    """Read the shared call log back. Returns (dict, error_message).

    dict maps place_id -> {'called': bool, 'notes': str, 'called_by': str} for
    every business either partner has logged, so both computers show the same
    'Called?' marks. Any problem returns ({}, message) and the app falls back to
    the local log — it never breaks the page.
    """
    if not url:
        return {}, "no sheet configured"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return {}, f"HTTP {r.status_code}"
        data = r.json()
        rows = data if isinstance(data, list) else data.get("rows", [])
        out = {}
        for row in rows:
            pid = str(row.get("place_id", "")).strip()
            if not pid or pid.lower() == "place_id":   # skip blanks + leaked header rows
                continue
            called = str(row.get("called", "")).strip().lower() in _TRUE
            out[pid] = {
                "called": called,
                "notes": str(row.get("note", row.get("notes", "")) or ""),
                "called_by": str(row.get("called_by", "") or ""),
            }
        return out, ""
    except Exception as e:
        return {}, str(e)[:160]


def post_call(url, row, timeout=15):
    """POST one called-lead row to the Apps Script web app. Returns (ok, message).

    Apps Script web apps answer a POST with a 302 to a googleusercontent URL that
    then returns 200, so we accept both (requests follows the redirect for us).
    """
    if not url:
        return False, "no sheet configured"
    try:
        r = requests.post(url, json=row, timeout=timeout)
        if r.status_code in (200, 201, 302):
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return False, str(e)[:160]
