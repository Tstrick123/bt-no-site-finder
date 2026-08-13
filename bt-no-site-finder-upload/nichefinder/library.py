"""Add a new niche to config.yaml without wrecking the file's comments.

We append the new niche as a text block at the end of the file (the `niches:`
section is the last thing in config.yaml), so all the existing comments and
formatting stay intact.
"""

import re
from pathlib import Path

from .config import load_config

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return s or "niche"


def unique_key(base):
    existing = set(load_config().get("niches", {}))
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def _clean(text):
    """Make a string safe to sit inside a double-quoted YAML scalar."""
    return str(text).replace('"', "'").replace("\n", " ").strip()


def add_niche(key, display_name, keywords, national_cpc, job_value,
              category="custom", verdict="Added via keyword discovery."):
    """Append a niche block to config.yaml. Returns the key used."""
    if not keywords:
        raise ValueError("A niche needs at least one keyword.")
    kw_str = ", ".join(f'"{_clean(k)}"' for k in keywords)
    block = (
        f"\n  {key}:\n"
        f'    display_name: "{_clean(display_name)}"\n'
        f"    category: {slugify(category)}\n"
        f"    keywords: [{kw_str}]\n"
        f"    national_cpc: {float(national_cpc)}\n"
        f"    job_value: {int(job_value)}\n"
        f"    recommended: true\n"
        f'    verdict: "{_clean(verdict)}"\n'
    )
    text = CONFIG.read_text()
    if not text.endswith("\n"):
        text += "\n"
    CONFIG.write_text(text + block)
    return key
