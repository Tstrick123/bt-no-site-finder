"""💬 Social DM list: the No-Site Finder's second tab.

Same search as the call list (sidebar -> Find leads), but instead of a phone
list it lines up each no-website business's Facebook page and Instagram
profile, with one-click links that open a Messenger chat / Instagram DM. You
send every message yourself; this tab finds the pages and keeps the log.

Every DM is logged to the shared sheet's "DMs" tab (Tate + Ben), and the first
person to message a business owns that conversation, so nobody messages the
same owner twice. The engine is nichefinder/social.py; this file is the UI.
"""

import pandas as pd
import streamlit as st

from nichefinder.cache import Cache
from nichefinder.sheets import DM_STATUSES, fetch_dms, post_dm
from nichefinder.social import (MANUAL, SERPER_FREE, TAVILY_FREE_MONTHLY,
                                SocialFinder, fb_message_url,
                                fb_page, fb_search_url, find_socials, ig_dm_url,
                                ig_handle, ig_profile_url, ig_search_url,
                                match_label, needs_lookup, quick_links)

VIA = ["Facebook", "Instagram", "Both"]
PRETTY = {"HOT": "🔥 HOT", "WARM": "⭐ WARM", "COOL": "▫️ COOL"}
FOLLOW_UP = {"Sent", "Replied", "Interested"}
VIEWS = ["📨 Ready to message", "🔁 Follow-ups", "🔎 Find by hand", "All"]
EDITABLE = ["dm_status", "via", "facebook", "instagram", "note"]


def _same(a, b):
    return (a or "").strip().lower() == (b or "").strip().lower()


def _val(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    return str(x).strip()


@st.cache_data(ttl=30, show_spinner=False)
def _shared_dms(url):
    """Cached read of the sheet's DMs tab (re-checks at most every 30s)."""
    return fetch_dms(url)


def load_dm_state(sheet_url):
    """(dms by place_id, sync problem or '', shared_ok).

    The shared sheet wins over this computer's log. shared_ok=False means DMs
    only save locally (no sheet, or the sheet still runs the pre-DM script).
    """
    local = Cache().load_dms()
    if not sheet_url:
        return local, "", False
    shared, err, ok = _shared_dms(sheet_url)
    if not ok:
        return local, err, False
    merged = dict(local)
    merged.update(shared)
    return merged, "", True


def render(leads, mock, env, sheet_url, caller, shared_calls, dm_state):
    dms, dm_err, dm_shared = dm_state
    finder = SocialFinder(env, Cache(), mock=mock)
    by_id = {ld["place_id"]: ld for ld in leads}

    # Links found so far this session (Google's own link + anything cached).
    store = st.session_state.setdefault("dm_social", {})
    for ld in leads:
        if ld["place_id"] not in store:
            store[ld["place_id"]] = quick_links(ld, finder)

    def links(pid):
        """(facebook page url, instagram handle) — a saved/hand-pasted link wins."""
        s, d = store[pid], dms.get(pid, {})
        return (d.get("facebook") or s.get("facebook", ""),
                ig_handle(d.get("instagram", "")) or s.get("instagram", ""))

    def how(pid, fb, ig):
        s = store[pid]
        return match_label({
            "facebook": fb, "fb_match": s.get("fb_match") if fb == s.get("facebook") else MANUAL,
            "instagram": ig, "ig_match": s.get("ig_match") if ig == s.get("instagram") else MANUAL})

    st.subheader("💬 Message them on Facebook or Instagram")
    st.caption("The same no-website businesses, lined up with their Facebook page and "
               "Instagram profile. Click 💬 to open the chat, send your message, then set "
               "**DM status** and hit 💾 Save so your partner never messages them too.")

    # ---------------- sync status ----------------
    if not sheet_url:
        st.caption("Not connected to the shared Google Sheet, so DMs save on this computer only.")
    elif not dm_shared:
        st.warning("**The shared Google Sheet needs its one-time DM update** (DM_LIST.md, "
                   "step 2) before DMs can sync between you and Ben. Until then DMs save on "
                   f"this computer only, and the hosted app forgets them on restart. ({dm_err})")
    else:
        sc1, sc2 = st.columns([4, 1])
        n_dm = sum(1 for d in dms.values() if d.get("dm_status"))
        sc1.caption(f"🔗 Synced with the shared sheet's DMs tab: {n_dm} business(es) "
                    "messaged by the team.")
        if sc2.button("🔄 Refresh", use_container_width=True, key="dm_refresh"):
            _shared_dms.clear()
            st.rerun()

    # ---------------- what we have ----------------
    have = [links(ld["place_id"]) for ld in leads]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("No-site businesses", len(leads))
    m2.metric("Facebook pages", sum(1 for fb, _ in have if fb))
    m3.metric("Instagram profiles", sum(1 for _, ig in have if ig))
    m4.metric("💬 Reachable by DM", sum(1 for fb, ig in have if fb or ig))

    # ---------------- look up the rest ----------------
    pending = [ld for ld, (fb, ig) in zip(leads, have)
               if not (fb and ig) and needs_lookup(ld, store[ld["place_id"]], finder)]
    if finder.backend is None:
        st.info("**Auto-search is off.** Add a free Tavily key (`TAVILY_API_KEY`, "
                "1,000 free searches a month at tavily.com) to turn it on (DM_LIST.md, "
                "step 1). Until then you get the links Google already has, plus 🔎 "
                "search buttons on every other row.")
    if pending:
        lc1, lc2 = st.columns([1, 2])
        cap = int(lc1.number_input("How many to look up (best leads first)", min_value=1,
                                   max_value=len(pending), value=min(len(pending), 100),
                                   step=10, key="dm_cap"))
        n_search = sum(1 for ld in pending[:cap]
                       if finder.backend and not store[ld["place_id"]].get("searched"))
        if finder.backend == "mock":
            cost_txt = "🧪 Mock mode: fake pages, free."
        elif finder.backend == "tavily":
            cost_txt = (f"💳 ~{n_search} searches on Tavily: free for the first "
                        f"{TAVILY_FREE_MONTHLY:,} each month (then about \\$0.008 each). "
                        "Each business is only ever searched once a month.")
        elif finder.backend == "serper":
            cost_txt = (f"💳 ~{n_search} searches ≈ \\${finder.cost(n_search):.2f} on Serper "
                        f"(the first {SERPER_FREE:,} are free, then about \\$1 per 1,000). "
                        "Each business is only ever searched once a month.")
        elif finder.backend == "dataforseo":
            cost_txt = (f"💳 ~{n_search} searches ≈ \\${finder.cost(n_search):.2f} on DataForSEO. "
                        "Each business is only ever searched once a month.")
        else:
            cost_txt = "Free: checks their own Wix / GoDaddy / Linktree pages for links."
        go = lc2.button(f"🔎 Find Facebook + Instagram for {cap} business(es)",
                        type="primary", use_container_width=True, key="dm_go")
        lc2.caption(cost_txt)
        if go:
            bar = st.progress(0.0, text="Looking up…")

            def cb(i, t, name):
                bar.progress(min(1.0, i / max(t, 1)), text=f"[{i}/{t}]  {name}")

            res, info = find_socials(pending[:cap], finder, progress=cb)
            bar.empty()
            store.update(res)
            st.session_state["dm_info"] = info
            st.session_state["dm_ver"] = st.session_state.get("dm_ver", 0) + 1
            st.rerun()

    info = st.session_state.get("dm_info")
    if info:
        if info.get("fatal"):
            st.error("**The search service refused the lookups** (usually a wrong key or "
                     f"used-up credit):\n\n`{info['fatal']}`")
        msg = []
        if info.get("searches"):
            msg.append(f"ran {info['searches']} searches (\\${info['cost']:.2f})")
        if info.get("pages"):
            msg.append(f"checked {info['pages']} of their own pages (free)")
        if msg:
            st.caption("Last lookup: " + ", ".join(msg) + ".")

    flash = st.session_state.pop("dm_flash", None)
    if flash:
        st.success(flash["ok"])
        for w in flash.get("warn", []):
            st.warning(w)

    # ---------------- the list ----------------
    vc1, vc2 = st.columns([3, 2])
    view = vc1.radio("Show", VIEWS, horizontal=True, key="dm_view")
    skip_called = vc2.checkbox("Skip businesses the team already called or gave to a rep",
                               value=True, key="dm_skip_called")

    rows = []
    for ld, (fb, ig) in zip(leads, have):
        pid = ld["place_id"]
        d = dms.get(pid, {})
        status = d.get("dm_status", "")
        c = shared_calls.get(pid, {})
        worked = (f"📞 {c.get('called_by') or 'called'}" if c.get("called")
                  else (f"📤 {c['assigned_to']}" if c.get("assigned_to") else ""))
        if view == VIEWS[0] and (status or not (fb or ig)):
            continue
        if view == VIEWS[1] and status not in FOLLOW_UP:
            continue
        if view == VIEWS[2] and (status or fb or ig):
            continue
        if skip_called and worked and view in (VIEWS[0], VIEWS[2]):
            continue
        rows.append({
            "dm_status": status or None,
            "via": d.get("via") or None,
            "messaged_by": d.get("messaged_by", ""),
            "priority": PRETTY.get(ld.get("priority"), ld.get("priority")),
            "name": ld.get("name", ""),
            "city": ld.get("city", ""),
            "fb_msg": fb_message_url(fb) or None,
            "ig_dm": ig_dm_url(ig) or None,
            "facebook": fb or None,
            "instagram": ig_profile_url(ig) or None,
            "how": how(pid, fb, ig),
            "find_fb": None if fb else fb_search_url(ld.get("name", ""), ld.get("city", "")),
            "find_ig": None if ig else ig_search_url(ld.get("name", ""), ld.get("city", ""),
                                                     ld.get("state", "")),
            "worked": worked,
            "note": d.get("note", ""),
            "phone": ld.get("phone", ""),
            "rating": ld.get("rating"),
            "reviews": ld.get("reviews"),
            "website_status": ld.get("website_status", ""),
            "place_id": pid,
        })

    if not rows:
        st.info({VIEWS[0]: "Nobody left to message here. Run 🔎 Find Facebook + Instagram "
                           "above, try 🔎 Find by hand, or search more towns.",
                 VIEWS[1]: "No conversations to follow up on yet.",
                 VIEWS[2]: "Every business here already has a link or a DM."}.get(view, "No businesses."))
        return

    df = pd.DataFrame(rows).reset_index(drop=True)
    st.caption("Send each message yourself and keep it personal. Meta restricts accounts "
               "that blast lots of identical cold DMs, especially newer accounts. Wrong page? "
               "Paste the right link into the Facebook / Instagram cell.")
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, height=560,
        key=f"dm_editor_{VIEWS.index(view)}_{st.session_state.get('dm_ver', 0)}",
        column_config={
            "dm_status": st.column_config.SelectboxColumn(
                "DM status", options=DM_STATUSES, width="small",
                help="Set it once you've sent the DM. Clear it to undo."),
            "via": st.column_config.SelectboxColumn("Via", options=VIA, width="small"),
            "messaged_by": st.column_config.TextColumn("By", width="small"),
            "priority": st.column_config.TextColumn("Priority", width="small"),
            "name": st.column_config.TextColumn("Business", width="medium"),
            "city": st.column_config.TextColumn("City", width="small"),
            "fb_msg": st.column_config.LinkColumn("Messenger", display_text="💬 Message"),
            "ig_dm": st.column_config.LinkColumn("IG DM", display_text="💬 DM"),
            "facebook": st.column_config.LinkColumn(
                "Facebook page", display_text="open page",
                help="Empty or wrong? Paste the page's facebook.com link here."),
            "instagram": st.column_config.LinkColumn(
                "Instagram", display_text="open profile",
                help="Empty or wrong? Paste the instagram.com/<name> link here."),
            "how": st.column_config.TextColumn(
                "How we found it", width="medium",
                help="✅ = on their Google listing / own page / a strong name match. "
                     "⚠️ check it = a likely match: open the page and make sure it's them."),
            "find_fb": st.column_config.LinkColumn("Find on FB", display_text="🔎 search"),
            "find_ig": st.column_config.LinkColumn("Find on IG", display_text="🔎 search"),
            "worked": st.column_config.TextColumn(
                "Call side", width="small",
                help="📞 already called by the team · 📤 handed to a rep"),
            "note": st.column_config.TextColumn("Notes", width="large"),
            "phone": st.column_config.TextColumn("Phone"),
            "rating": st.column_config.NumberColumn("★", format="%.1f", width="small"),
            "reviews": st.column_config.NumberColumn("Reviews", format="%d", width="small"),
            "website_status": st.column_config.TextColumn("Website", width="small"),
            "place_id": None,
        },
        disabled=[c for c in df.columns if c not in EDITABLE],
    )

    bc1, bc2 = st.columns([1, 3])
    if bc1.button("💾  Save DM log", use_container_width=True, key="dm_save"):
        _save(df, edited, by_id, store, dms, dm_shared, sheet_url, caller)
        st.rerun()
    bc2.download_button(
        "⬇️  Download this DM list (CSV)",
        df.drop(columns=["fb_msg", "ig_dm", "find_fb", "find_ig"]).to_csv(index=False).encode(),
        file_name="dm_list.csv", mime="text/csv", use_container_width=True)


def _save(df, edited, by_id, store, dms, dm_shared, sheet_url, caller):
    """Write every changed row to the shared sheet (or this computer's log)."""
    cache = Cache()
    saved, skipped, bad, errs = 0, [], [], []
    with st.spinner("Saving…"):
        for i, r in edited.iterrows():
            o = df.loc[i]
            if all(_val(r[c]) == _val(o[c]) for c in EDITABLE):
                continue                                   # untouched row
            pid, name = r["place_id"], r["name"]
            status, via, note = _val(r["dm_status"]), _val(r["via"]), _val(r["note"])
            fb_in, ig_in = _val(r["facebook"]), _val(r["instagram"])
            fb = fb_page(fb_in) if fb_in else ""
            ig = ig_handle(ig_in) if ig_in else ""
            if (fb_in and not fb) or (ig_in and not ig):
                bad.append(name)
            s = store[pid]
            if fb_in != _val(o["facebook"]):               # pasted by hand
                s["facebook"], s["fb_match"] = fb, (MANUAL if fb else "")
            if ig_in != _val(o["instagram"]):
                s["instagram"], s["ig_match"] = ig, (MANUAL if ig else "")
            if status and not via:
                via = "Both" if fb and ig else ("Facebook" if fb else ("Instagram" if ig else ""))
            prev = dms.get(pid, {})
            if prev.get("dm_status") and prev.get("messaged_by") \
                    and not _same(prev["messaged_by"], caller):
                skipped.append(f"{name} ({prev['messaged_by']})")
                continue
            rec = {"dm_status": status, "via": via, "note": note, "facebook": fb,
                   "instagram": ig_profile_url(ig),
                   "match": match_label({"facebook": fb, "fb_match": s.get("fb_match"),
                                         "instagram": ig, "ig_match": s.get("ig_match")}),
                   "messaged_by": (prev.get("messaged_by") if prev.get("dm_status")
                                   else (caller if status else ""))}
            cache.save_dm(pid, rec)
            if dm_shared:
                ld = by_id[pid]
                payload = {
                    "business": ld.get("name", ""), "phone": ld.get("phone", ""),
                    "website_status": ld.get("website_status", ""),
                    "rating": None if pd.isna(ld.get("rating")) else ld.get("rating"),
                    "reviews": int(ld.get("reviews") or 0), "score": int(ld.get("score") or 0),
                    "priority": ld.get("priority", ""), "city": ld.get("city", ""),
                    "state": ld.get("state", ""), "address": ld.get("address", ""),
                    "maps_url": ld.get("maps_url", ""), "place_id": pid,
                    **rec, "messaged_by": caller,
                }
                ok, info = post_dm(sheet_url, payload)
                if not ok:
                    errs.append(info.get("error", "?"))
                    continue
                if info.get("skipped"):
                    skipped.append(f"{name} ({info.get('worked_by', 'someone else')})")
                    continue
            saved += 1
    _shared_dms.clear()
    st.session_state["dm_ver"] = st.session_state.get("dm_ver", 0) + 1
    where = "the shared sheet" if dm_shared else "this computer"
    warn = []
    if skipped:
        warn.append("Already being messaged by your partner, left alone: " + ", ".join(skipped[:8]))
    if bad:
        warn.append("Couldn't read the pasted link for: " + ", ".join(bad[:8]) + ". Paste the "
                    "full facebook.com/... or instagram.com/... address of their page.")
    if errs:
        warn.append(f"{len(errs)} row(s) didn't reach the sheet: {errs[0]}")
    st.session_state["dm_flash"] = {"ok": f"Saved {saved} DM update(s) to {where}.", "warn": warn}
