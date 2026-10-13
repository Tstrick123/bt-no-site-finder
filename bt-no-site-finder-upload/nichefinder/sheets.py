"""Optional: mirror the 'Called?' log to a shared Google Sheet.

Uses a no-credentials trick: you make a Google Sheet, paste a tiny Apps Script
into it that appends a row on POST, deploy it as a web app, and paste that URL
into the tool (sidebar → Shared Google Sheet). Both partners paste the SAME
URL, so every call either of you logs lands in ONE shared sheet — no double
calling. No URL set = local-only, nothing breaks.
"""

import os

import requests

_TRUE = {"1", "true", "yes", "y", "called", "x"}


def _with_token(payload):
    """Attach the optional shared secret (SHEET_TOKEN) if one is configured."""
    token = os.environ.get("SHEET_TOKEN", "").strip()
    if token:
        payload = dict(payload)
        payload["token"] = token
    return payload


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
                "assigned_to": str(row.get("assigned_to", "") or ""),
                "status": str(row.get("status", "") or ""),
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
        r = requests.post(url, json=_with_token(row), timeout=timeout)
        if r.status_code in (200, 201, 302):
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return False, str(e)[:160]


def fetch_worked_place_ids(url, timeout=15):
    """Every place_id already in the shared sheet (assigned OR called by anyone).

    This is the company-wide dedup set: leads to skip when assigning, so a
    business the team has already worked never gets handed out twice. Returns
    (set, error_message); any problem returns (set(), msg) so assignment can
    still proceed (the Apps Script also refuses duplicates server-side).
    """
    if not url:
        return set(), "no sheet configured"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return set(), f"HTTP {r.status_code}"
        data = r.json()
        rows = data if isinstance(data, list) else data.get("rows", [])
        out = set()
        for row in rows:
            pid = str(row.get("place_id", "")).strip()
            if pid and pid.lower() != "place_id":
                out.add(pid)
        return out, ""
    except Exception as e:
        return set(), str(e)[:160]


def assign_lead(url, row, timeout=15):
    """Assign ONE lead to a rep (action=assign). Returns (ok, info).

    `info` is the Apps Script's JSON: {assigned: bool, skipped: bool,
    reason, worked_by}. The server refuses to assign a place_id that is already
    claimed or called, so exclusivity holds even if two people click at once.
    """
    if not url:
        return False, {"error": "no sheet configured"}
    payload = dict(row)
    payload["action"] = "assign"
    try:
        r = requests.post(url, json=_with_token(payload), timeout=timeout)
        if r.status_code not in (200, 201, 302):
            return False, {"error": f"HTTP {r.status_code}: {r.text[:120]}"}
        try:
            return True, r.json()
        except Exception:
            return True, {"assigned": True}   # older script with no JSON echo
    except Exception as e:
        return False, {"error": str(e)[:160]}


def onboard_rep(url, name, password, created_by="", reset=False, timeout=15):
    """Create a rep login in the shared sheet's 'Reps' tab (salted-SHA-256 hash).

    reset=False (default) refuses to overwrite an existing name -> returns
    (False, {"reason": "rep_exists"}). Pass reset=True to deliberately change an
    existing rep's password. The password is never stored in the clear.
    """
    if not url:
        return False, {"error": "no sheet configured"}
    payload = {"action": "onboard", "name": name, "password": password,
               "created_by": created_by, "reset": bool(reset)}
    try:
        r = requests.post(url, json=_with_token(payload), timeout=timeout)
        if r.status_code not in (200, 201, 302):
            return False, {"error": f"HTTP {r.status_code}: {r.text[:120]}"}
        try:
            info = r.json()
        except Exception:
            info = {"ok": True}
        return bool(info.get("ok", True)), info
    except Exception as e:
        return False, {"error": str(e)[:160]}


def fetch_roster(url, timeout=15):
    """Active rep names from the sheet (for the assign dropdown). Returns list."""
    if not url:
        return []
    try:
        r = requests.get(url, params={"roster": "1"}, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
        if isinstance(data, list):
            return [str(x) for x in data if str(x).strip()]
        return []
    except Exception:
        return []


# ---- 💬 Social DM list (the sheet's "DMs" tab) ----
DM_STATUSES = ["Sent", "Replied", "Interested", "Sold", "Not interested"]


def fetch_dms(url, timeout=15):
    """Read the shared DM log. Returns (dict, error_message, supported).

    dict maps place_id -> {dm_status, via, messaged_by, facebook, instagram,
    match, note}. `supported` is False when the sheet still runs the OLD Apps
    Script (no DMs tab yet): that script ignores ?dms=1 and answers with the
    call log as a bare list, so we must NOT treat it as DM data, and must not
    POST DMs to it either (the old script would log them as phone calls).
    """
    if not url:
        return {}, "no sheet configured", False
    try:
        r = requests.get(url, params={"dms": "1"}, timeout=timeout)
        if r.status_code != 200:
            return {}, f"HTTP {r.status_code}", False
        data = r.json()
        if not isinstance(data, dict) or "dms" not in data:
            return {}, "sheet script needs the DM update", False
        out = {}
        for row in data.get("dms") or []:
            pid = str(row.get("place_id", "")).strip()
            if not pid or pid.lower() == "place_id":
                continue
            out[pid] = {k: str(row.get(k, "") or "") for k in
                        ("dm_status", "via", "messaged_by", "facebook",
                         "instagram", "match", "note")}
        return out, "", True
    except Exception as e:
        return {}, str(e)[:160], False


def post_dm(url, row, timeout=15):
    """Save ONE DM row (action=dm). Returns (ok, info).

    The Apps Script upserts it into the "DMs" tab by place_id and refuses to
    overwrite a business someone ELSE already messaged (info.skipped=True).
    Only call this after fetch_dms() reported supported=True.
    """
    if not url:
        return False, {"error": "no sheet configured"}
    payload = dict(row)
    payload["action"] = "dm"
    try:
        r = requests.post(url, json=_with_token(payload), timeout=timeout)
        if r.status_code not in (200, 201, 302):
            return False, {"error": f"HTTP {r.status_code}: {r.text[:120]}"}
        try:
            info = r.json()
        except Exception:
            return False, {"error": "sheet didn't confirm the save"}
        return bool(info.get("ok")), info
    except Exception as e:
        return False, {"error": str(e)[:160]}
