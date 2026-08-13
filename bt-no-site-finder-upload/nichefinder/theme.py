"""Optional 'goal photos' background for the dashboards.

Drop 1–3 images in niche_finder/assets/ named goal1.*, goal2.* … (jpg/png/webp)
and the dashboards use them as a gently-dimmed, slowly-crossfading background —
so you see your goals every time you open the tool. No images in assets/ = the
normal dark UI, unchanged.

Tune the look from the dashboard call:
    goal_background(overlay=0.5, seconds_each=10)
  overlay      0 = photos at full brightness (harder to read text)
               1 = solid black (photos invisible). ~0.45–0.6 is the sweet spot.
  seconds_each how long each photo holds before crossfading to the next.
"""

import base64
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp"}


def _goal_images():
    if not ASSETS.exists():
        return []
    return [p for p in sorted(ASSETS.glob("goal*")) if p.suffix.lower() in MIME]


def _data_uri(path):
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:{MIME[path.suffix.lower()]};base64,{b64}"


def goal_background(overlay=0.5, seconds_each=10):
    """Inject the goal-photo background CSS. No-op if assets/ has no goal images."""
    imgs = _goal_images()
    if not imgs:
        return
    uris = [_data_uri(p) for p in imgs]
    n = len(uris)

    layers = "".join(
        f'<div class="goalbg gb{i}" style="background-image:url({u})"></div>'
        for i, u in enumerate(uris)
    )

    if n == 1:
        anim_css = ".goalbg{opacity:1}"
    else:
        total = seconds_each * n
        vis = 100 / n
        fin, hold, out = round(vis * 0.12, 2), round(vis * 0.85, 2), round(vis, 2)
        delays = "".join(
            f".gb{i}{{animation-delay:{i * seconds_each}s}}\n" for i in range(n)
        )
        anim_css = (
            f".goalbg{{opacity:0;animation:gbcycle {total}s infinite}}\n"
            f"{delays}"
            f"@keyframes gbcycle{{0%{{opacity:0}}{fin}%{{opacity:1}}"
            f"{hold}%{{opacity:1}}{out}%{{opacity:0}}100%{{opacity:0}}}}"
        )

    st.markdown(
        f"""
        {layers}
        <div class="goal-overlay"></div>
        <style>
        .goalbg {{
            position: fixed; inset: 0; z-index: -2;
            background-size: cover; background-position: center;
        }}
        .goal-overlay {{
            position: fixed; inset: 0; z-index: -1;
            background: rgba(8, 10, 18, {overlay});
        }}
        /* let the photos show through Streamlit's dark chrome */
        [data-testid="stAppViewContainer"], .stApp,
        [data-testid="stHeader"] {{ background: transparent !important; }}
        /* keep the sidebar + text panels legible over the photo */
        [data-testid="stSidebar"] {{ background: rgba(14, 17, 23, 0.86) !important; }}
        h1, h2, h3, .stMarkdown p, [data-testid="stMetricLabel"],
        [data-testid="stCaptionContainer"] {{ text-shadow: 0 1px 6px rgba(0,0,0,0.75); }}
        {anim_css}
        </style>
        """,
        unsafe_allow_html=True,
    )
