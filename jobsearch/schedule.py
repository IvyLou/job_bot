"""macOS launchd agents: the daily pipeline run, and the dashboard kept running at http://localhost:8501."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from . import config, crm

AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
DAILY = "com.jobbot.daily"
DASHBOARD = "com.jobbot.dashboard"
PORT = 8501


def _plists() -> Dict[str, dict]:
    python = str(Path(sys.executable))
    logs = config.DATA_DIR / "logs"
    hour, minute = (int(x) for x in str(crm.settings()["daily_refresh"]).split(":"))
    return {
        DAILY: {"Label": DAILY, "ProgramArguments": [python, "-m", "jobsearch", "daily"],
                "WorkingDirectory": str(config.ROOT), "StartCalendarInterval": {"Hour": hour, "Minute": minute},
                "StandardOutPath": str(logs / "daily.log"), "StandardErrorPath": str(logs / "daily.log")},
        DASHBOARD: {"Label": DASHBOARD,
                    "ProgramArguments": [python, "-m", "streamlit", "run", str(config.ROOT / "dashboard.py"),
                                         "--server.port", str(PORT)],
                    "WorkingDirectory": str(config.ROOT), "RunAtLoad": True, "KeepAlive": True,
                    "StandardOutPath": str(logs / "dashboard.log"), "StandardErrorPath": str(logs / "dashboard.log")},
    }


def _launchctl(*args: str) -> None:
    subprocess.run(["launchctl", *args], check=False, capture_output=True)


def install() -> List[str]:
    (config.DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    written = []
    for label, plist in _plists().items():
        path = AGENTS_DIR / f"{label}.plist"
        _launchctl("bootout", domain, str(path))  # reload cleanly if already installed
        with open(path, "wb") as f:
            plistlib.dump(plist, f)
        _launchctl("bootstrap", domain, str(path))
        written.append(str(path))
    return written


def uninstall() -> List[str]:
    domain = f"gui/{os.getuid()}"
    removed = []
    for label in (DAILY, DASHBOARD):
        path = AGENTS_DIR / f"{label}.plist"
        if path.exists():
            _launchctl("bootout", domain, str(path))
            path.unlink()
            removed.append(str(path))
    return removed
