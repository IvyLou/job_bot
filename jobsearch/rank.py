"""Role matching, relevance scores for open roles, and company tiers."""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from functools import lru_cache
from typing import List, Optional, Pattern, Tuple

from . import config, requirements


def _rx(patterns: List[str]) -> Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I)


@lru_cache(maxsize=None)
def _rules() -> Tuple[List[Tuple[str, float, Pattern[str]]], Pattern[str], Pattern[str]]:
    t = config.search()["titles"]
    families = [(name, f["weight"], _rx(f["patterns"])) for name, f in t["families"].items()]
    return families, _rx(t["exclude"]), _rx(t["too_senior"])


def title_family(title: str) -> Optional[str]:
    families, exclude, too_senior = _rules()
    if exclude.search(title) or too_senior.search(title):
        return None
    return next((name for name, _, rx in families if rx.search(title)), None)


_JUNIOR = re.compile(r"\b(analyst|associate|specialist|coordinator|fellow|rotational)\b", re.I)
_MID = re.compile(r"\b(manager|lead)\b", re.I)


def seniority_fit(title: str) -> float:
    if _JUNIOR.search(title):
        return 1.0
    return 0.75 if _MID.search(title) else 0.85


def relevance(title: str, family: Optional[str], location_tag: Optional[str], lanes: str,
              posted_at: Optional[str]) -> float:
    if not family:
        return 0.0
    cfg = config.search()
    weights = {name: w for name, w, _ in _rules()[0]}
    weight = config.locations().get(location_tag or "", {}).get("weight", 0)
    score = 100 * weights[family] * weight * seniority_fit(title)
    if set(lanes.split(",")) & set(cfg["priority_lanes"]):
        score *= 1.1
    if posted_at and posted_at[:10] >= (date.today() - timedelta(days=7)).isoformat():
        score *= 1.05
    return round(min(score, 100.0), 1)


def rank_all(conn: sqlite3.Connection) -> None:
    # Re-apply title rules too, so edits to config/search.yaml take effect without refetching.
    for j in conn.execute("""SELECT j.id, j.title, j.location_tag, j.posted_at, j.description, c.lanes, c.name
                             FROM jobs j JOIN companies c ON c.id = j.company_id WHERE j.is_open = 1""").fetchall():
        family = title_family(j["title"])
        a = requirements.assess(j["title"], family, j["location_tag"], j["description"], j["name"])
        conn.execute("""UPDATE jobs SET family = ?, relevance = ?, years_min = ?, years_label = ?, years_quote = ?,
                          background_quote = ?, priority_match = ? WHERE id = ?""",
                     (family, relevance(j["title"], family, j["location_tag"], j["lanes"], j["posted_at"]), a.years_min,
                      a.years_label, a.years_quote, a.background_quote, a.priority_match, j["id"]))
    _assign_tiers(conn)
    conn.commit()


def _assign_tiers(conn: sqlite3.Connection) -> None:
    """Tiers rank fit, not timing: everyone gets contacted, Tier 1 first.

    1: a priority-match role (see requirements.py), a strong open role (85+), or a company in your home location
       and a priority category where we know a founder or leader to write to.
    2: anywhere in your locations, or any matching open role.
    3: everything else.
    """
    priority = set(config.search()["priority_lanes"])
    home, targets = config.home_location(), config.target_locations()
    rows = conn.execute("""
        SELECT c.id, c.location_tag, c.lanes,
               (SELECT MAX(relevance) FROM jobs j WHERE j.company_id = c.id AND j.is_open = 1) AS best_role,
               EXISTS (SELECT 1 FROM people p WHERE p.company_id = c.id AND p.priority <= 2) AS has_contact,
               EXISTS (SELECT 1 FROM jobs j WHERE j.company_id = c.id AND j.is_open = 1 AND j.priority_match = 1)
                 AS priority_role
        FROM companies c WHERE c.tier_locked = 0""").fetchall()
    for r in rows:
        best = r["best_role"] or 0
        if r["priority_role"] or best >= 85 or (r["location_tag"] == home and r["has_contact"]
                                                and set(r["lanes"].split(",")) & priority):
            tier = 1
        elif r["location_tag"] in targets or best > 0:
            tier = 2
        else:
            tier = 3
        conn.execute("UPDATE companies SET tier = ? WHERE id = ?", (tier, r["id"]))
