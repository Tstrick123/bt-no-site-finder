"""No-Site Finder — the point-and-click cold-call list builder.

Run it with:   streamlit run nosite_dashboard.py
(or just double-click "Launch No-Site Finder.command")

Pick a trade + a town, click Find leads, and get a ranked list of businesses
that are on Google but have NO real website — the exact people to cold-call to
sell a website. Mark who you've called; download the list as a spreadsheet.
"""

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nichefinder.cache import Cache
from nichefinder.cities import cities_source
from nichefinder.config import load_config, load_env
from nichefinder.nosite import (_places_mock, call_cost, category_queries,
                                estimate_calls, find_leads)
from nichefinder.sheets import post_call
from nichefinder.theme import goal_background

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
saved = Cache().load_call_statuses()
df["called"] = df["place_id"].map(lambda p: saved.get(p, {}).get("called", False))
df["notes"] = df["place_id"].map(lambda p: saved.get(p, {}).get("notes", ""))

pretty = {"HOT": "🔥 HOT", "WARM": "⭐ WARM", "COOL": "▫️ COOL"}
df["priority"] = df["priority"].map(lambda p: pretty.get(p, p))

st.subheader("Your call list — highest-priority first")
st.caption("Tick **Called?** and jot **Notes** as you dial, then hit 💾 Save. "
           "It remembers across runs (keyed to each Google listing).")

view_cols = ["called", "priority", "score", "name", "phone", "website_status",
             "rating", "reviews", "city", "state", "address", "maps_url", "notes",
             "place_id"]
view_cols = [c for c in view_cols if c in df.columns]

edited = st.data_editor(
    df[view_cols], hide_index=True, use_container_width=True, height=560,
    key="call_editor",
    column_config={
        "called": st.column_config.CheckboxColumn("Called?"),
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
    disabled=["priority", "score", "name", "phone", "website_status", "rating",
              "reviews", "city", "state", "address", "maps_url"],
)

col_save, col_dl = st.columns([1, 3])
if col_save.button("💾  Save call log", use_container_width=True):
    cache = Cache()
    sheet_url = cache.get_setting("sheet_url", "") or env.get("sheet_webhook", "")
    caller = cache.get_setting("caller_name", "") or env.get("caller_name", "")
    prior = cache.load_call_statuses()          # to know what's already been sent
    saved_n, sent_n, errs = 0, 0, []
    with st.spinner("Saving…"):
        for _, r in edited.iterrows():
            pid = r["place_id"]
            called = bool(r.get("called"))
            note = (r.get("notes") or "")
            if called or note.strip():
                cache.save_call_status(pid, called, note)
                saved_n += 1
            # send to the shared Google Sheet: each called row once
            if sheet_url and called and not prior.get(pid, {}).get("posted", False):
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
        done += f"  Sent {sent_n} new call(s) to the shared Google Sheet."
    st.success(done)
    if errs:
        st.warning("Some rows didn't reach the sheet (check the link): " + errs[0])

col_dl.download_button(
    "⬇️  Download call list (CSV)",
    df.drop(columns=["called", "notes"]).to_csv(index=False).encode(),
    file_name=f"leads_{(out['niche'] or 'search')}.csv", mime="text/csv",
    use_container_width=True)

# ---------------- map ----------------
mp = df.dropna(subset=["lat", "lng"])[["lat", "lng"]] if {"lat", "lng"} <= set(df.columns) else None
if mp is not None and len(mp):
    st.subheader("Where these businesses are")
    st.map(mp, latitude="lat", longitude="lng", size=120)
