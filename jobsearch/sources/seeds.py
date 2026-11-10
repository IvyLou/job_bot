"""Hand-picked companies from seeds.yaml, grouped by category."""
from __future__ import annotations

import sqlite3

from .. import config, db


def ingest(conn: sqlite3.Connection) -> int:
    n, home = 0, config.home_location()
    for lane, entries in config.seeds().items():
        for e in entries:
            domain = e["domain"].lower()
            db.upsert_company(conn, key=domain, name=e["name"], source="seed", lanes=[lane.replace("_more", "")],
                              domain=domain, website=f"https://{domain}", location_tag=e.get("location", home))
            n += 1
    conn.commit()
    return n
