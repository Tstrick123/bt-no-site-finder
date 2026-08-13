"""Optional: mirror the 'Called?' log to a shared Google Sheet.

Uses a no-credentials trick: you make a Google Sheet, paste a tiny Apps Script
into it that appends a row on POST, deploy it as a web app, and paste that URL
into the tool (sidebar → Shared Google Sheet). Both partners paste the SAME
URL, so every call either of you logs lands in ONE shared sheet — no double
calling. No URL set = local-only, nothing breaks.
"""

import requests


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
