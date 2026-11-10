"""Who to contact for a company (and role), by company size.

- Early stage (up to ~50 people): the CEO or a founder. They usually are the hiring manager.
- Growth stage (up to ~300): the head of the team the role sits in (`teams` in crm.yaml).
- Big companies: both the hiring manager and the recruiter, after applying.
Someone you know always comes first.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional
from urllib.parse import quote

from . import crm

_CEO = re.compile(r"\b(ceo|chief executive|president|found)", re.I)
_RECRUITER = re.compile(r"recruit|talent acquisition|talent partner|sourcer|campus", re.I)


@lru_cache(maxsize=None)
def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


def team(family: Optional[str], lanes: str, has_role: bool) -> dict:
    """The team a role sits in, by its family; with no role in view, by the company's categories (crm.yaml)."""
    s = crm.settings()
    teams = s["teams"]
    if has_role:
        key = next((k for k, t in teams.items() if family in (t.get("role_families") or [])), s["default_team"])
    else:
        key = next((k for k, t in teams.items() if set(lanes.split(",")) & set(t.get("company_categories") or [])),
                   s["default_team"])
    return teams[key]


@dataclass
class Target:
    kind: str                 # warm | ceo | leader | hiring_manager | recruiter | other
    label: str
    look_for: str
    search: str               # LinkedIn people-search keywords
    template: str
    people: List[dict] = field(default_factory=list)

    @property
    def linkedin_search(self) -> str:
        return f"https://www.linkedin.com/search/results/people/?keywords={quote(self.search)}"


def size_band(team_size: Optional[int], sources: str) -> str:
    limits = crm.settings()["company_size"]
    if team_size:
        return "early" if team_size <= limits["early_max"] else "growth" if team_size <= limits["growth_max"] else "big"
    # Unknown size: hand-picked companies are established firms; everything else came from startup sources.
    return "big" if "seed" in sources.split(",") else "early"


BAND_ADVICE = {
    "early": "Early-stage startup: write to the CEO or a founder.",
    "growth": "Growth-stage company: write to the head of the team this role sits in.",
    "big": "Big company: after you apply, message both the hiring manager and the recruiter.",
}


def linkedin_job_search(title: str, company: str) -> str:
    """LinkedIn's copy of the posting usually shows "Meet the hiring team", i.e. who posted it."""
    return f"https://www.linkedin.com/jobs/search/?keywords={quote(title + ' ' + company)}"


def targets(conn: sqlite3.Connection, company_id: int, job_id: Optional[int] = None) -> List[Target]:
    co = conn.execute("SELECT name, team_size, sources, lanes FROM companies WHERE id = ?", (company_id,)).fetchone()
    job = conn.execute("SELECT title, family, department, reports_to FROM jobs WHERE id = ?", (job_id,)).fetchone() if job_id else None
    people = [dict(p) for p in conn.execute(
        """SELECT id, full_name, title, relationship, role_kind, email, email_confidence, linkedin_url, warmth
           FROM people WHERE company_id = ? ORDER BY priority, full_name""", (company_id,))]
    name, band = co["name"], size_band(co["team_size"], co["sources"])
    tm = team(job["family"] if job else None, co["lanes"], bool(job))
    leader, leader_label, manager_label = _rx(tm["leader"]), tm["leader_label"], tm["manager_label"]
    applying = "apply_and_reach_out" if job else None

    def matching(kind: str, rx: Optional[re.Pattern]) -> List[dict]:
        return [p for p in people if p["role_kind"] == kind
                or (rx is not None and rx.search(f"{p['title'] or ''} {p['relationship'] or ''}"))]

    out: List[Target] = []
    talking = {r[0] for r in conn.execute("""SELECT DISTINCT person_id FROM activities WHERE company_id = ?
                                              AND person_id IS NOT NULL""", (company_id,))}
    if talking:
        out.append(Target("active", "Already in touch", "someone you've already contacted", name,
                          "follow_up_1", [p for p in people if p["id"] in talking]))
    warm = [p for p in people if p["warmth"] == "warm" and p["id"] not in talking]
    if warm:
        out.append(Target("warm", "Someone you know", "a warm connection", f"{name}", "warm_intro", warm))
    if band == "early":
        out.append(Target("ceo", "Founder / CEO", "the CEO or a co-founder", f"founder {name}",
                          applying or "founder_outreach", matching("ceo", _CEO)))
    elif band == "growth":
        out.append(Target("leader", leader_label, leader_label, f"{tm['leader_search']} {name}",
                          applying or "founder_outreach", matching("leader", leader)))
    else:
        reports_to = job["reports_to"] if job else None
        look_for = f"{reports_to} (named in the posting)" if reports_to else manager_label
        search = reports_to or (f"{job['department']} director" if job and job["department"] else tm["manager_search"])
        out.append(Target("hiring_manager", "Hiring manager", look_for, f"{search} {name}",
                          applying or "big_company_networking",
                          matching("hiring_manager", leader)
                          + matching("hiring_manager", _rx(crm.settings()["hiring_manager"]))))
        out.append(Target("recruiter", "Recruiter", "the recruiter for this role (often the person who posted it)",
                          f"recruiter {name}", "recruiter_after_applying" if job else "big_company_networking",
                          matching("recruiter", _RECRUITER)))
    for t in out:  # de-duplicate within a target, keep order
        seen = set()
        t.people = [p for p in t.people if not (p["id"] in seen or seen.add(p["id"]))]
    used = {p["id"] for t in out for p in t.people}
    rest = [p for p in people if p["id"] not in used and p["role_kind"] != "board"]
    if rest:
        fallback = "founder_outreach" if band == "early" else "big_company_networking"
        out.append(Target("other", "Someone else", "another contact", name, applying or fallback, rest))
    return out
