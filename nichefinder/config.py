"""Loads config.yaml and your API keys (.env). Nothing clever here."""

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config():
    """Return the parsed config.yaml as a dict."""
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def load_env():
    """Read API keys from .env (if present) plus the environment.

    python-dotenv is optional — if it isn't installed we just read os.environ,
    so the tool still runs in mock mode out of the box.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass

    dfs_login = os.getenv("DATAFORSEO_LOGIN", "").strip()
    dfs_pw = os.getenv("DATAFORSEO_PASSWORD", "").strip()
    places_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    sheet_webhook = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
    caller_name = os.getenv("CALLER_NAME", "").strip()
    # MOCK_MODE=false only counts if it's literally "false"
    mock_flag = os.getenv("MOCK_MODE", "true").strip().lower() != "false"

    placeholders = {"", "your_api_password_here", "your_email@example.com",
                    "your_google_maps_api_key_here"}
    have_dfs = dfs_login not in placeholders and dfs_pw not in placeholders
    have_places = places_key not in placeholders

    return {
        "dataforseo_login": dfs_login,
        "dataforseo_password": dfs_pw,
        "google_places_key": places_key,
        "mock_requested": mock_flag,
        "have_dataforseo": have_dfs,
        "have_places": have_places,
        "sheet_webhook": sheet_webhook,
        "caller_name": caller_name,
    }
