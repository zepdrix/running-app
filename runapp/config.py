"""Filesystem locations. Everything personal lives under data/ (gitignored)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("RUNAPP_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "runapp.db"
RAW_DIR = DATA_DIR / "raw"  # untouched copies of everything pulled from Garmin
TOKEN_PATH = DATA_DIR / "garmin_tokens.json"

# Distances (meters) we search every run for best efforts.
BEST_EFFORT_DISTANCES = {
    "400m": 400,
    "1k": 1000,
    "1 mile": 1609.344,
    "5k": 5000,
    "10k": 10000,
    "Half marathon": 21097.5,
    "Marathon": 42195,
}
DEMO = False  # set by `serve --demo`; disables syncing into the demo database

# Host names the app answers to (any *.ts.net Tailscale name is also accepted; see server.py).
# Add others, comma-separated, in RUNAPP_ALLOWED_HOSTS.
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"} | {h.strip() for h in os.getenv("RUNAPP_ALLOWED_HOSTS", "").split(",") if h.strip()}
