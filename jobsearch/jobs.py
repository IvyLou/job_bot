"""Store fetched postings: keep only roles we care about and close the ones that disappeared."""
from __future__ import annotations

import re
import sqlite3
from typing import Iterable, Optional

from . import db, rank
from .ats import Posting
from .normalize import location_tag


_REPORTS_TO = re.compile(r"\breport(?:s|ing)?\s+(?:directly\s+)?(?:in)?to\s+(?:the\s+|our\s+|a\s+|an\s+)?"
                         r"([^.;:\n()]{3,70}?)(?=[.;:\n(]|,\s|\s+(?:and|who|within|in|on|at|for|with|as)\b|$)", re.I)
_TITLE_WORDS = re.compile(r"\b(head|director|vp|vice president|chief|cfo|coo|ceo|cro|manager|lead|founder|president|"
                          r"controller|treasurer|partner|officer)\b", re.I)


_REPORTS_TO_HEADING = re.compile(r"\breports?\s+to\s*:\s*([^\n.;]{3,70})", re.I)


def reports_to(description: str) -> Optional[str]:
    """The title a posting says the role reports to ("reports to the VP of Finance"), if it says."""
    for m in [*_REPORTS_TO_HEADING.finditer(description or ""), *_REPORTS_TO.finditer(description or "")]:
        title = m.group(1).strip(" ,-")
        if _TITLE_WORDS.search(title) and len(title.split()) <= 8:
            return title[0].upper() + title[1:]
    return None


def store_postings(conn: sqlite3.Connection, company_id: int, source: str, postings: Iterable[Posting]) -> int:
    """Upsert relevant postings for one company from one source. Returns how many relevant roles are open."""
    today = db.today()
    kept = []
    for p in postings:
        family = rank.title_family(p.title)
        if not family:
            continue
        fields = dict(title=p.title, department=p.department, location=p.location, location_tag=location_tag(p.location),
                      url=p.url, posted_at=p.posted_at, family=family, is_open=1, last_seen=today)
        if p.description:
            fields.update(description=p.description, reports_to=reports_to(p.description))
        existing = conn.execute("SELECT id FROM jobs WHERE source = ? AND external_id = ?",
                                (source, p.external_id)).fetchone()
        if existing:
            conn.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         [*fields.values(), existing["id"]])
        else:
            cols = dict(company_id=company_id, source=source, external_id=p.external_id, first_seen=today, **fields)
            conn.execute(f"INSERT INTO jobs({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", list(cols.values()))
        kept.append(p.external_id)
    conn.execute(f"""UPDATE jobs SET is_open = 0 WHERE company_id = ? AND source = ? AND is_open = 1
                     AND external_id NOT IN ({', '.join('?' * len(kept))})""", [company_id, source, *kept])
    return len(kept)
