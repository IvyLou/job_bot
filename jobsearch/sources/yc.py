"""Y Combinator: the full company directory, plus each company's public page (founders and job posts)."""
from __future__ import annotations

import html
import json
import re
import sqlite3
from typing import List, Optional, Tuple

import httpx

from .. import config, db, jobs
from ..ats import SUPPORTED, Posting
from ..http import bounded, web_client
from ..normalize import domain_of, lanes_for, location_tag

DIRECTORY_URL = "https://yc-oss.github.io/api/companies/all.json"
PAGE_URL = "https://www.ycombinator.com/companies/{slug}/jobs"


def _tag(c: dict) -> Optional[str]:
    # "San Francisco; Remote" is an SF company, so only trust real cities here and use the region for remote.
    tag = location_tag(re.sub(r"\bremote\b", "", c.get("all_locations") or "", flags=re.I))
    regions = set(c.get("regions") or [])
    if tag is None and "Fully Remote" in regions and "United States of America" in regions:
        tag = "remote"
    return tag


def ingest(conn: sqlite3.Connection) -> int:
    data = httpx.get(DIRECTORY_URL, timeout=60).json()
    lane_keywords = config.search()["lanes"]
    n = 0
    for c in data:
        if c.get("status") not in ("Active", "Public"):
            continue
        tag = _tag(c)
        if tag not in config.target_locations():
            continue
        domain = domain_of(c.get("website"))
        text = " ".join([c.get("subindustry") or "", " ".join(c.get("industries") or []),
                         " ".join(c.get("tags") or []), c.get("one_liner") or ""])
        stage = " · ".join(x for x in (c.get("stage"), c.get("batch")) if x)
        db.upsert_company(conn, key=domain or f"yc:{c['slug']}", name=c["name"], source="yc",
                          lanes=lanes_for(text, lane_keywords), domain=domain, website=c.get("website"),
                          yc_slug=c["slug"], description=c.get("one_liner"),
                          city=(c.get("all_locations") or "").split(";")[0].strip(), location_tag=tag,
                          team_size=c.get("team_size"), stage=stage, industry=c.get("subindustry"))
        n += 1
    conn.commit()
    return n


async def _page(client: httpx.AsyncClient, slug: str) -> Optional[Tuple[List[dict], List[Posting]]]:
    try:
        r = await client.get(PAGE_URL.format(slug=slug))
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    m = re.search(r'data-page="([^"]+)"', r.text)
    if not m:
        return None
    props = json.loads(html.unescape(m.group(1))).get("props", {})
    founders = (props.get("company") or {}).get("founders") or []
    postings = [Posting(external_id=f"yc:{slug}:{j['id']}", title=j.get("title") or "", location=j.get("location") or "",
                        url="https://www.ycombinator.com" + (j.get("url") or ""), department=j.get("prettyRole") or "")
                for j in props.get("jobPostings") or []]
    return founders, postings


async def crawl_pages(conn: sqlite3.Connection, limit: Optional[int] = None) -> Tuple[int, int]:
    """Fetch founders for YC companies we haven't crawled, and YC-hosted jobs for companies without another ATS."""
    rows = conn.execute(
        f"""SELECT c.id, c.yc_slug, c.ats_type FROM companies c
            WHERE c.yc_slug IS NOT NULL
              AND (NOT EXISTS (SELECT 1 FROM people p WHERE p.company_id = c.id AND p.source = 'yc')
                   OR COALESCE(c.ats_type, '') NOT IN ({','.join('?' * len(SUPPORTED))}))
            ORDER BY c.id""", SUPPORTED).fetchall()
    rows = rows[:limit] if limit else rows
    n_people = n_companies = 0
    async with web_client() as client:
        async for row, result in bounded(rows, lambda r: _page(client, r["yc_slug"]), limit=4):
            if result is None:
                continue
            founders, postings = result
            for f in founders:
                if f.get("full_name"):
                    db.upsert_person(conn, row["id"], f["full_name"], "yc", title=f.get("title"),
                                     relationship="founder", linkedin_url=f.get("linkedin_url"))
                    n_people += 1
            if row["ats_type"] not in SUPPORTED:
                jobs.store_postings(conn, row["id"], "yc", postings)
            n_companies += 1
            if n_companies % 100 == 0:
                conn.commit()
                print(f"  yc pages: {n_companies}/{len(rows)}", flush=True)
    conn.commit()
    return n_companies, n_people
