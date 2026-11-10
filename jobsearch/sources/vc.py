"""VC and accelerator portfolios, via the Getro-hosted job boards many funds run (jobs.<fund>.com and similar).

Each board's API lists every portfolio company with its domain, locations, stage and industry. Boards are found by
probing the usual subdomains of each fund in config/vcs.yaml; funds on other platforms are reported as not found.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

import httpx

from .. import config, db
from ..ats import _get_text
from ..http import bounded, web_client
from ..normalize import domain_of, lanes_for, location_tag

GETRO_SEARCH = "https://api.getro.com/api/v2/collections/{id}/search/companies"
SUBDOMAINS = ("jobs", "careers", "talent")
# Getro's head_count codes follow LinkedIn's size bands (1-10, 11-50, 51-200, 201-500, 501-1k, 1k-5k, 5k-10k, 10k+);
# store a representative headcount for each.
HEADCOUNT = {1: 5, 2: 30, 3: 100, 4: 350, 5: 750, 6: 3000, 7: 7500, 8: 15000}


def funds() -> List[Tuple[str, str, str]]:
    """(category, fund name, domain) for every fund and accelerator in config/vcs.yaml."""
    return [(cat, f["name"], f["domain"]) for cat, entries in config.load("vcs").items() for f in entries]


async def _find_board(client: httpx.AsyncClient, domain: str) -> Optional[str]:
    """The Getro network id behind a fund's job board, if it has one."""
    for sub in SUBDOMAINS:
        body, _ = await _get_text(client, f"https://{sub}.{domain}")
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', body, re.S)
        if m and "getro" in body.lower():
            network = json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("network") or {}
            if network.get("id"):
                return str(network["id"])
    return None


async def _portfolio(client: httpx.AsyncClient, network_id: str) -> List[dict]:
    companies: List[dict] = []
    for page in range(100):
        try:
            r = await client.post(GETRO_SEARCH.format(id=network_id), headers={"Accept": "application/json"},
                                  json={"hits_per_page": 100, "page": page, "query": "", "filters": ""})
            results = r.json().get("results", {}) if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            break
        batch = results.get("companies") or []
        companies += batch
        if not batch or len(companies) >= (results.get("count") or 0):
            break
    return companies


async def ingest(conn: sqlite3.Connection, rediscover: bool = False) -> Dict[str, int]:
    lane_keywords = config.search()["lanes"]
    stats = {"funds": 0, "boards": 0, "portfolio": 0, "kept": 0}
    all_funds = funds()
    async with web_client() as client:
        todo = [f for f in all_funds if rediscover or db.get_meta(conn, f"vc_board:{f[2]}") is None]
        async for (_, _, domain), board in bounded(todo, lambda f: _find_board(client, f[2]), limit=16):
            db.set_meta(conn, f"vc_board:{domain}", f"getro:{board}" if board else "none")
        conn.commit()
        for category, fund, domain in all_funds:
            stats["funds"] += 1
            board = db.get_meta(conn, f"vc_board:{domain}") or "none"
            if board == "none":
                continue
            stats["boards"] += 1
            for c in await _portfolio(client, board.split(":", 1)[1]):
                stats["portfolio"] += 1
                site = domain_of(c.get("domain"))
                tag = location_tag("; ".join(c.get("locations") or []))
                if not site or tag not in config.target_locations():
                    continue
                text = " ".join([*(c.get("industry_tags") or []), (c.get("description") or "")[:400]])
                cid = db.upsert_company(conn, key=site, name=c.get("name") or site, source="vc",
                                        lanes=lanes_for(text, lane_keywords), domain=site, website=f"https://{site}",
                                        description=(c.get("description") or "")[:500], location_tag=tag,
                                        city=(c.get("locations") or [""])[0], stage=(c.get("stage") or "").replace("_", " "),
                                        team_size=HEADCOUNT.get(c.get("head_count")))
                row = conn.execute("SELECT investors FROM companies WHERE id = ?", (cid,)).fetchone()
                conn.execute("UPDATE companies SET investors = ? WHERE id = ?",
                             (db._merge_csv(row["investors"] or "", [fund]), cid))
                stats["kept"] += 1
            conn.commit()
    return stats
