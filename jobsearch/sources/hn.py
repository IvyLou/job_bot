"""Hacker News "Who is hiring?": companies hiring in your locations this month."""
from __future__ import annotations

import html
import re
import sqlite3
from typing import Optional, Tuple

import httpx

from .. import config, contacts, db, jobs, rank
from ..ats import SUPPORTED, Posting, find_ats
from ..normalize import domain_of, html_text, lanes_for, location_tag, norm_name, strip_tags

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
# Links in posts that point at a job board or a generic site rather than the company's own domain.
_NOT_COMPANY = ("ycombinator.com", "lever.co", "greenhouse.io", "ashbyhq.com", "workable.com", "smartrecruiters.com",
                "recruitee.com", "rippling.com", "gem.com", "dover.com", "dover.io", "workatastartup.com", "wellfound.com",
                "angel.co", "linkedin.com", "github.com", "google.com", "forms.gle", "bit.ly", "calendly.com",
                "notion.site", "notion.so", "breezy.hr", "bamboohr.com", "typeform.com", "airtable.com", "x.com",
                "twitter.com", "youtube.com", "medium.com", "substack.com", "docs.google.com", "teamtailor.com")


def latest_story() -> Tuple[str, str]:
    hits = httpx.get(SEARCH_URL, params={"tags": "story,author_whoishiring", "hitsPerPage": 10}, timeout=30).json()["hits"]
    story = next(h for h in hits if h["title"].lower().startswith("ask hn: who is hiring"))
    return story["objectID"], story["title"]


def _company_domain(text: str) -> Optional[str]:
    for url in re.findall(r'href="([^"]+)"', text):
        d = domain_of(html.unescape(url))
        if d and not d.endswith(_NOT_COMPANY):
            return d
    return None


def ingest(conn: sqlite3.Connection) -> Tuple[str, int]:
    story_id, title = latest_story()
    item = httpx.get(ITEM_URL.format(id=story_id), timeout=60).json()
    lane_keywords = config.search()["lanes"]
    n = 0
    for c in item.get("children") or []:
        text = c.get("text") or ""
        head = strip_tags(re.split(r"<p>", text, maxsplit=1)[0])
        tag = location_tag(head)
        if "|" not in head or tag not in config.target_locations():  # real posts follow "Company | Role | Location | ..." 
            continue
        name = re.split(r"\s[-–—:]\s|\|", re.sub(r"\s*\(.*?\)", "", head))[0].strip()[:80]
        if not name:
            continue
        domain = _company_domain(text)
        cid = None if domain else db.find_company_by_name(conn, name)
        cid = db.upsert_company(conn, key=domain or f"name:{norm_name(name)}", name=name, source="hn",
                                lanes=lanes_for(strip_tags(text), lane_keywords), company_id=cid, domain=domain,
                                website=f"https://{domain}" if domain else None, location_tag=tag)
        ats = find_ats(html.unescape(text))
        if ats and ats[0] in SUPPORTED:
            conn.execute("UPDATE companies SET ats_type = ?, ats_slug = ?, ats_via = 'link', ats_checked_at = ? "
                         "WHERE id = ? AND ats_type IS NULL", (*ats, db.today(), cid))
        if domain:
            contacts.record_emails(conn, cid, contacts.emails_in(text, domain), "hn")
        roles = [p.strip() for p in head.split("|") if rank.title_family(p.strip())]
        if roles:
            url = f"https://news.ycombinator.com/item?id={c['id']}"
            jobs.store_postings(conn, cid, "hn", [Posting(f"hn:{c['id']}:{i}", r, head, url, description=html_text(text))
                                                  for i, r in enumerate(roles)])
        n += 1
    db.set_meta(conn, "hn_story", story_id)
    conn.commit()
    return title, n
