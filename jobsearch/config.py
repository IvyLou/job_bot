from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"   # examples, committed
LOCAL_DIR = ROOT / "local"     # your own settings, never committed: a file here replaces the one in config/
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "jobsearch.db"


_loaded: Dict[str, Tuple[Path, float, dict]] = {}


def path(name: str) -> Path:
    """local/<name>.yaml if you have one, else the example in config/."""
    local = LOCAL_DIR / f"{name}.yaml"
    return local if local.exists() else CONFIG_DIR / f"{name}.yaml"


def load(name: str = "search") -> dict:
    """A config file, parsed once and re-read when it changes, so edits show up without restarting the dashboard."""
    file = path(name)
    mtime = file.stat().st_mtime
    if name not in _loaded or _loaded[name][:2] != (file, mtime):
        with open(file) as f:
            _loaded[name] = (file, mtime, yaml.safe_load(f))
    return _loaded[name][2]


def search() -> dict:
    return load("search")


def seeds() -> dict:
    return load("seeds")


def locations() -> Dict[str, dict]:
    """Your locations from search.yaml, in order. The first is home: listed first, and the one where a company in a
    priority category can reach Tier 1. `remote` (in the US) and `us` (a bare "United States") are recognized
    without a pattern."""
    return search()["locations"]


def target_locations() -> List[str]:
    """Where companies are sourced from: every location except `us`."""
    return [tag for tag in locations() if tag != "us"]


def home_location() -> str:
    return next(iter(locations()))
