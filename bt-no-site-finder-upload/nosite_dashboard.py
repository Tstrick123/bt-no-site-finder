"""No-Site Finder — the point-and-click cold-call list builder.

Run it with:   streamlit run nosite_dashboard.py
(or just double-click "Launch No-Site Finder.command")

Pick a trade + a town, click Find leads, and get a ranked list of businesses
that are on Google but have NO real website — the exact people to cold-call to
sell a website. Mark who you've called; download the list as a spreadsheet.
"""

import os
import secrets
import sys
from datetime import date

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nichefinder.cache import Cache
from nichefinder.cities import cities_source
from nichefinder.config import load_config, load_env
from nichefinder.nosite import (_places_mock, call_cost, category_queries,
                                estimate_calls, find_leads)
from nichefinder.sheets import (assign_lead, fetch_roster,
                                fetch_worked_place_ids, onboard_rep, post_call)
from nichefinder.theme import goal_background

_PW_WORDS = ["red", "blue", "gold", "sun", "peak", "zion", "canyon", "summit",
             "rock", "desert", "mesa", "ridge", "coral", "sage", "storm"]


def _same_name(a, b):
    """Match rep/caller names the way the Apps Script does (trim + lowercase)."""
    return (a or "").strip().lower() == (b or "").strip().lower()


def _gen_password():
    """A short, friendly, copy-pasteable password (e.g. 'zion-coral-4f9a')."""
    return (f"{secrets.choice(_PW_WORDS)}-{secrets.choice(_PW_WORDS)}-"
            f"{secrets.token_hex(2)}")


def _regen_pw():
    # Runs as a button on_click callback (before the widget re-instantiates), so
    # it's allowed to set the widget-keyed session value.
    st.session_state["onb_pw"] = _gen_password()


def _do_onboard():
    """Onboard-button callback. Runs BEFORE the widgets re-instantiate, so it can
    safely reset the name/password fields (a fresh password every time, so two
    reps never share one). Stashes the result in onb_flash for display."""
    url = _sheet_url()
    name = (st.session_state.get("onb_name") or "").strip()
    pw = st.session_state.get("onb_pw") or ""
    reset = bool(st.session_state.get("onb_reset"))
    who = st.session_state.get("caller", "") or env.get("caller_name", "")
    if not url or not name:
        st.session_state["onb_flash"] = {"error": "Enter the rep's name first."}
        return
    ok, info = onboard_rep(url, name, pw, created_by=who, reset=reset)
    if ok:
        _roster.clear()          # show the new rep in the Assign dropdown right away
        st.session_state["onb_flash"] = {
            "name": name, "pw": pw,
            "verb": "Reset the password for" if info.get("updated") else "Onboarded"}
        st.session_state["onb_pw"] = _gen_password()   # fresh password for the next rep
        st.session_state["onb_name"] = ""
        st.session_state["onb_reset"] = False
    elif info.get("reason") == "rep_exists":
        st.session_state["onb_flash"] = {"error":
            f"A rep named '{name}' already exists. Turn ON 'Reset an existing "
            "rep's password' below if you meant to change their password."}
    else:
        st.session_state["onb_flash"] = {"error": f"Couldn't onboard: {info.get('error') or info}"}

st.set_page_config(page_title="No-Site Finder", page_icon="📞", layout="wide")

# When hosted online (Streamlit Cloud etc.), read config from the host's secret
# store. Locally there are no secrets, so this does nothing and .env is used.
try:
    for _k in ("GOOGLE_PLACES_API_KEY", "GOOGLE_SHEET_WEBHOOK_URL",
               "CALLER_NAME", "MOCK_MODE", "APP_PASSWORD"):
        if _k in st.secrets:
            os.environ[_k] = str(st.secrets[_k])
except Exception:
    pass

# Your goal photos as the background (no-op until images live in assets/).
goal_background(overlay=0.5, seconds_each=10)

# Shared-password gate — only active when APP_PASSWORD is set (i.e. when hosted).
_pw = os.environ.get("APP_PASSWORD", "")
if _pw and not st.session_state.get("_authed"):
    st.title("🔒 BT Venture Partners — No-Site Finder")
    _entry = st.text_input("Team password", type="password")
    if _entry == _pw:
        st.session_state["_authed"] = True
        st.rerun()
    elif _entry:
        st.error("Wrong password — try again.")
    st.stop()

cfg = load_config()
env = load_env()
mock = _places_mock(env)


@st.cache_data
def _state_list():
    from nichefinder.cities import load_cities
    return sorted(load_cities()["state_id"].unique().tolist())


@st.cache_data(ttl=30, show_spinner=False)
def _shared_calls(url):
    """Cached read of the shared Google Sheet (re-checks at most every 30s)."""
    from nichefinder.sheets import fetch_calls
    return fetch_calls(url)


@st.cache_data(ttl=30, show_spinner=False)
def _roster(url):
    """Cached list of onboarded (active) rep names for the assign dropdown."""
    return fetch_roster(url)


def _sheet_url():
    c = Cache()
    return (c.get_setting("sheet_url", "") or env.get("sheet_webhook", "")).strip()


st.title("📞 No-Site Finder")
st.caption("Find businesses that are on Google but have no real website — your cold-call list.")

if mock:
    st.info("**MOCK MODE** — realistic *fake* businesses so you can explore for free. "
            "Add a Google Places key to `.env` and set `MOCK_MODE=false` to pull real "
            "businesses (see SETUP.md). The first ~1,000 lookups/month are free.")
else:
    st.success("**LIVE MODE** — pulling real businesses from Google Places. This spends API credit.")
st.caption(f"Cities file: `{cities_source().name}`")

# ---------------- sidebar ----------------
with st.sidebar:
    st.header("Find leads")

    # Who's on the app right now — labels your calls in the shared sheet so you
    # both know who dialed whom. Session-only, so Tate and Ben never overwrite
    # each other even though they share one hosted app.
    _default_caller = (env.get("caller_name") or "").strip()
    _who_opts = ["Tate", "Ben"]
    if _default_caller and _default_caller not in _who_opts:
        _who_opts = [_default_caller] + _who_opts
    _who_idx = _who_opts.index(_default_caller) if _default_caller in _who_opts else 0
    st.session_state["caller"] = st.selectbox(
        "I'm calling as", _who_opts, index=_who_idx,
        help="Tags your calls in the shared sheet so you both know who called whom.")

    niche_keys = list(cfg["niches"].keys())
    niche = st.selectbox(
        "Trade / niche", niche_keys,
        format_func=lambda k: cfg["niches"][k]["display_name"],
        help="Sets what we search for and the job-value context. "
             "Override the exact search phrase below if you want.")

    default_q = ", ".join(category_queries(cfg["niches"][niche]))
    custom_q = st.text_input(
        "Or type any business type (optional)", value="",
        placeholder=default_q,
        help="Leave blank to use the trade's built-in search phrases. "
             "Or type anything, e.g. 'mobile detailing', 'taco shop'.")

    region_keys = ["(whole population band)"] + list(cfg["regions"].keys())
    region_choice = st.selectbox(
        "Region", region_keys,
        format_func=lambda k: k if k.startswith("(") else cfg["regions"][k]["label"])
    region_key = None if region_choice.startswith("(") else region_choice

    states = st.multiselect(
        "Filter to state(s) — optional", _state_list(),
        help="Leave empty for all states in the region.")

    limit = st.slider("Max towns to scan", 1, 100, 8, step=1)
    min_score = st.slider("Only show leads scoring at least", 0, 100, 0, step=5,
                          help="0 shows every no-website business. Raise it to see only the hottest.")
    run = st.button("📞  Find leads", type="primary", use_container_width=True)

    if not mock:   # show what a live run will roughly cost before they click
        nq = 1 if custom_q.strip() else len(category_queries(cfg["niches"][niche]))
        lo, hi = estimate_calls(limit, nq)
        st.caption(f"💳 Live run: ~{lo}–{hi} Google lookups "
                   f"(${call_cost(lo):.2f}–${call_cost(hi):.2f} gross). "
                   f"First 1,000/month are free, so this is likely **$0**.")

    n = cfg["niches"][niche]
    st.markdown("---")
    st.markdown(f"**{n['display_name']}**")
    lo = n.get("job_value_low", n["job_value"])
    hi = n.get("job_value_high", n["job_value"])
    st.metric("Typical job value", f"${lo:,}–${hi:,}")
    st.caption("Higher-ticket trades = each website you sell is worth more to the owner.")

    st.markdown("---")
    with st.expander("🔗 Shared Google Sheet (optional)"):
        _c = Cache()
        st.caption("Paste the same link on both computers to share ONE live call "
                   "log — so you and your partner never call the same business twice.")
        _url = st.text_input("Google Sheet link (ends in /exec)",
                             value=_c.get_setting("sheet_url", "") or env.get("sheet_webhook", ""))
        _name = st.text_input("Your name (shows in the sheet)",
                              value=_c.get_setting("caller_name", "") or env.get("caller_name", ""))
        if st.button("💾 Save sheet settings"):
            _c.set_setting("sheet_url", _url.strip())
            _c.set_setting("caller_name", _name.strip())
            st.success("Saved. Calls you log now go to the shared sheet.")

    with st.expander("👥 Onboard a rep"):
        _osu = _sheet_url()
        if not _osu:
            st.caption("Connect the shared Google Sheet above first, then you can "
                       "create rep logins here.")
        else:
            # Result of the last onboard (stashed by the callback so it survives
            # the rerun and stays on screen for the admin to copy).
            _of = st.session_state.get("onb_flash")
            if _of and _of.get("error"):
                st.error(_of["error"])
            elif _of:
                st.success(f"✅ {_of['verb']} **{_of['name']}**.")
                st.info("**Send them these 3 things:**\n\n"
                        "1. Your Rep Portal link\n"
                        f"2. Name: **{_of['name']}**\n"
                        f"3. Password: **{_of['pw']}**")
            st.caption("Create a login for a new salesperson. They use the **Rep "
                       "Portal** — they never see this finder. After onboarding, "
                       "pick them in the Assign panel to send leads.")
            if "onb_pw" not in st.session_state:
                st.session_state["onb_pw"] = _gen_password()
            st.text_input("Rep's name", key="onb_name", placeholder="e.g. Ben")
            st.text_input("Password (auto-filled — change it if you want)", key="onb_pw")
            st.checkbox("Reset an existing rep's password", key="onb_reset",
                        help="Leave OFF to add a NEW rep. Turn ON only to change "
                             "the password of a rep who already exists.")
            oc1, oc2 = st.columns(2)
            oc1.button("🔄 New password", on_click=_regen_pw, use_container_width=True)
            oc2.button("✅ Onboard rep", type="primary", on_click=_do_onboard,
                       use_container_width=True)

# ---------------- run ----------------
if run:
    bar = st.progress(0.0, text="Starting…")

    def cb(i, t, city):
        bar.progress(i / t, text=f"[{i}/{t}]  {city}")

    st.session_state["nsf"] = find_leads(
        query=(custom_q.strip() or None), niche_key=niche, region_key=region_key,
        limit=limit, states=states or None, progress=cb, min_score=min_score)
    bar.empty()

out = st.session_state.get("nsf")
if not out:
    st.warning("Pick a trade + region on the left, then click **Find leads**.")
    st.stop()

if out.get("fatal_error"):
    st.error("**Google API error — the live scan couldn't run.**\n\n"
             + f"`{out['fatal_error'][:400]}`\n\n"
             + "Most common fix: enable **Places API (New)** on your Google Cloud "
               "project, wait a few minutes, then click **Find leads** again.")
    st.stop()

# ---------------- summary ----------------
b = out["breakdown"]
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Businesses seen", out["scanned"])
c2.metric("📞 No-site leads", out["lead_count"])
c3.metric("🔥 HOT", b.get("HOT", 0))
c4.metric("⭐ WARM", b.get("WARM", 0))
c5.metric("▫️ COOL", b.get("COOL", 0))
st.caption(f"Searched: {', '.join(out['query'])}"
           + ("  ·  🧪 mock data" if out["mock"] else "  ·  live Google data"))
if not out["mock"]:
    used = out.get("api_calls", 0)
    within = used <= 1000
    st.caption(f"💳 This run made **{used}** Google lookups (${out.get('api_cost', 0):.2f} gross"
               + (" — within your free 1,000/month, so $0)." if within else ")."))

leads = out["leads"]
if not leads:
    st.warning("No no-website businesses came back. Try a broader trade, more towns, "
               "or lower the score filter.")
    st.stop()

# ---------------- call list (editable) ----------------
df = pd.DataFrame(leads)
saved = Cache().load_call_statuses()          # this computer's local log

# If a shared Google Sheet is set up, it's the source of truth: read it back so
# both partners see the same 'Called?' marks. Fail-safe — if it can't be reached
# we just show the local marks and say so.
sheet_url = _sheet_url()
shared, sync_err = ({}, "")
if sheet_url:
    shared, sync_err = _shared_calls(sheet_url)

def _state_for(pid):
    if pid in shared:                         # shared sheet wins when present
        return shared[pid]
    return saved.get(pid, {})

df["called"] = df["place_id"].map(lambda p: _state_for(p).get("called", False))
df["notes"] = df["place_id"].map(lambda p: _state_for(p).get("notes", ""))
if sheet_url:
    df["called_by"] = df["place_id"].map(lambda p: shared.get(p, {}).get("called_by", ""))
    df["assigned_to"] = df["place_id"].map(lambda p: shared.get(p, {}).get("assigned_to", ""))
    if sync_err:
        st.caption(f"⚠️ Couldn't reach the shared sheet just now — showing this "
                   f"computer's marks. ({sync_err})")
    else:
        _team = sum(1 for v in shared.values() if v.get("called"))
        cc, cr = st.columns([4, 1])
        cc.caption(f"🔗 Synced with your shared Google Sheet — {_team} call(s) "
                   f"logged by the team. Marks update for both of you.")
        if cr.button("🔄 Refresh", use_container_width=True):
            _shared_calls.clear()
            st.rerun()

pretty = {"HOT": "🔥 HOT", "WARM": "⭐ WARM", "COOL": "▫️ COOL"}
df["priority"] = df["priority"].map(lambda p: pretty.get(p, p))

st.subheader("Your call list — highest-priority first")
st.caption("Tick **Called?** and jot **Notes** as you dial, then hit 💾 Save. "
           "It remembers across runs (keyed to each Google listing).")

view_cols = ["called", "called_by", "assigned_to", "priority", "score", "name",
             "phone", "website_status", "rating", "reviews", "city", "state",
             "address", "maps_url", "notes", "place_id"]
view_cols = [c for c in view_cols if c in df.columns]

edited = st.data_editor(
    df[view_cols], hide_index=True, use_container_width=True, height=560,
    key="call_editor",
    column_config={
        "called": st.column_config.CheckboxColumn("Called?"),
        "called_by": st.column_config.TextColumn("By", width="small"),
        "assigned_to": st.column_config.TextColumn("Owner (rep)", width="small",
            help="If a rep owns this lead, it's theirs to call — don't cold-call it here."),
        "priority": st.column_config.TextColumn("Priority", width="small"),
        "score": st.column_config.NumberColumn("Score", format="%d", width="small"),
        "name": st.column_config.TextColumn("Business", width="large"),
        "phone": st.column_config.TextColumn("Phone"),
        "website_status": st.column_config.TextColumn("Website", width="medium"),
        "rating": st.column_config.NumberColumn("★", format="%.1f", width="small"),
        "reviews": st.column_config.NumberColumn("Reviews", format="%d", width="small"),
        "address": st.column_config.TextColumn("Address", width="medium"),
        "maps_url": st.column_config.LinkColumn("Map", display_text="open"),
        "notes": st.column_config.TextColumn("Notes", width="large"),
        "place_id": None,   # hide, but keep it for saving
    },
    disabled=["called_by", "assigned_to", "priority", "score", "name", "phone",
              "website_status", "rating", "reviews", "city", "state", "address",
              "maps_url"],
)

col_save, col_dl = st.columns([1, 3])
if col_save.button("💾  Save call log", use_container_width=True):
    cache = Cache()
    caller = st.session_state.get("caller", "") or env.get("caller_name", "")
    saved_n, sent_n, skipped_owned, errs = 0, 0, 0, []
    with st.spinner("Saving…"):
        for _, r in edited.iterrows():
            pid = r["place_id"]
            called = bool(r.get("called"))
            note = (r.get("notes") or "")
            if called or note.strip():
                cache.save_call_status(pid, called, note)
                saved_n += 1
            # Push to the shared sheet only when something changed vs. what's
            # already there (the sheet upserts by place_id, so no duplicates).
            if sheet_url and called:
                prev = shared.get(pid, {})
                # Don't stamp a call over a lead a rep already owns — that's how
                # a business gets called twice. Leave it to the rep. (The Apps
                # Script refuses this server-side too; this just avoids the call.)
                owner = prev.get("assigned_to", "")
                prev_by = prev.get("called_by", "")
                # Don't stamp a call over a lead someone else owns OR already
                # called — either way that's a double-call. Leave it as-is. (The
                # Apps Script refuses this server-side too; this avoids the call.)
                if (owner and not _same_name(owner, caller)) or \
                   (prev.get("called") and prev_by and not _same_name(prev_by, caller)):
                    skipped_owned += 1
                    continue
                unchanged = (prev.get("called") and prev.get("notes", "") == note
                             and _same_name(prev_by, caller))
                if unchanged:
                    continue
                payload = {
                    "business": r.get("name", ""), "phone": r.get("phone", ""),
                    "website_status": r.get("website_status", ""),
                    "rating": None if pd.isna(r.get("rating")) else r.get("rating"),
                    "reviews": int(r.get("reviews") or 0), "score": int(r.get("score") or 0),
                    "priority": r.get("priority", ""), "city": r.get("city", ""),
                    "state": r.get("state", ""), "address": r.get("address", ""),
                    "note": note, "maps_url": r.get("maps_url", ""),
                    "place_id": pid, "called_by": caller,
                }
                ok, msg = post_call(sheet_url, payload)
                if ok:
                    cache.mark_posted(pid)
                    sent_n += 1
                else:
                    errs.append(msg)
    done = f"Saved {saved_n} call-log entries."
    if sheet_url:
        done += f"  Sent {sent_n} update(s) to the shared Google Sheet."
        if skipped_owned:
            done += (f"  Skipped {skipped_owned} already worked by someone else "
                     "(left as-is, so nobody's called twice).")
        _shared_calls.clear()   # so the refreshed marks show for both of you
    st.success(done)
    if errs:
        st.warning("Some rows didn't reach the sheet (check the link): " + errs[0])

col_dl.download_button(
    "⬇️  Download call list (CSV)",
    df.drop(columns=["called", "notes"]).to_csv(index=False).encode(),
    file_name=f"leads_{(out['niche'] or 'search')}.csv", mime="text/csv",
    use_container_width=True)

# ---------------- assign to a rep ----------------
# Hand a batch of these leads to a salesperson. They land in the shared Sheet
# tagged to that rep, who then works them in the Rep Portal. Exclusivity is
# enforced two ways: we skip any place_id already in the Sheet (dedup below),
# and the Apps Script refuses to assign a lead that's already claimed/called —
# so no two reps ever get, or call, the same business.
st.markdown("---")
st.subheader("📤 Assign leads to a rep")
if not sheet_url:
    st.caption("Connect the shared Google Sheet first (sidebar → 🔗 Shared Google "
               "Sheet) to hand leads to a rep.")
else:
    # Prefer the live roster (reps onboarded via the sidebar); fall back to the
    # config list, then a free-typed name.
    reps = _roster(sheet_url) or [str(x) for x in (cfg.get("reps") or [])]
    ac1, ac2, ac3 = st.columns([2, 1, 1])
    with ac1:
        if reps:
            rep_pick = st.selectbox("Assign to", reps + ["➕ someone else…"])
            rep_name = (st.text_input("New rep's name (onboard them first, above)")
                        if rep_pick == "➕ someone else…" else rep_pick)
        else:
            rep_name = st.text_input(
                "Rep's name", placeholder="e.g. Ben",
                help="Onboard the rep first (sidebar → 👥 Onboard a rep) so they can log in.")
    with ac2:
        how_many = st.number_input("How many (top leads)", min_value=1,
                                   max_value=len(leads), value=min(40, len(leads)), step=1)
    with ac3:
        st.write("")
        st.write("")
        go_assign = st.button(f"📤 Assign top {int(how_many)}", use_container_width=True,
                              disabled=not (rep_name or "").strip())
    if go_assign and (rep_name or "").strip():
        worked, werr = fetch_worked_place_ids(sheet_url)
        today = date.today().isoformat()
        assigned, skipped, failed = 0, 0, []
        with st.spinner(f"Assigning to {rep_name}…"):
            for ld in leads:
                if assigned >= int(how_many):
                    break
                pid = ld.get("place_id", "")
                if pid in worked:          # already handed out / called by someone
                    skipped += 1
                    continue
                payload = {
                    "business": ld.get("name", ""), "phone": ld.get("phone", ""),
                    "website_status": ld.get("website_status", ""),
                    "rating": None if pd.isna(ld.get("rating")) else ld.get("rating"),
                    "reviews": int(ld.get("reviews") or 0), "score": int(ld.get("score") or 0),
                    "priority": ld.get("priority", ""), "city": ld.get("city", ""),
                    "state": ld.get("state", ""), "address": ld.get("address", ""),
                    "maps_url": ld.get("maps_url", ""), "place_id": pid,
                    "assigned_to": rep_name.strip(), "assigned_date": today,
                }
                ok, info = assign_lead(sheet_url, payload)
                if not ok:
                    failed.append(info.get("error", "?"))
                elif info.get("skipped"):     # server refused (already claimed)
                    skipped += 1
                else:
                    assigned += 1
        msg = f"✅ Assigned **{assigned}** lead(s) to **{rep_name.strip()}**."
        if skipped:
            msg += f"  Skipped {skipped} already-worked by the team (no double-calls)."
        st.success(msg)
        if werr:
            st.caption(f"⚠️ Couldn't pre-check the sheet for duplicates ({werr}) — "
                       "the server still blocked any that were already claimed.")
        if failed:
            st.warning(f"{len(failed)} didn't send (check the sheet link): {failed[0]}")

# ---------------- map ----------------
mp = df.dropna(subset=["lat", "lng"])[["lat", "lng"]] if {"lat", "lng"} <= set(df.columns) else None
if mp is not None and len(mp):
    st.subheader("Where these businesses are")
    st.map(mp, latitude="lat", longitude="lng", size=120)
