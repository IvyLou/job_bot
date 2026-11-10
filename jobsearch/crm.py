"""Outreach tracking: company stages, an activity log, follow-up timing, templates and hit rates."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from . import config, db

STAGES = ["Not started", "Reached out", "In conversation", "Applied / Referred", "Recruiter screen", "Interviewing",
          "Offer", "Closed"]
OUTBOUND = ("sent", "followup")
# Logging one of these moves the company at least this far along.
_ADVANCES = {"sent": "Reached out", "followup": "Reached out", "reply": "In conversation", "call": "In conversation",
             "application": "Applied / Referred", "interview": "Interviewing"}


def settings() -> dict:
    return config.load("crm")


def templates() -> dict:
    """config/templates.yaml (edits show up on the next page load)."""
    return config.load("templates")


def pitches() -> dict:
    """The resume numbers each message leads with, one version per kind of role (`versions` in pitches.yaml)."""
    return config.load("pitches")["versions"]


# --- stages ----------------------------------------------------------------------------------------------------

def stage_of(conn: sqlite3.Connection, company_id: int) -> str:
    row = conn.execute("SELECT stage FROM pipeline WHERE company_id = ?", (company_id,)).fetchone()
    return row["stage"] if row else STAGES[0]


def set_stage(conn: sqlite3.Connection, company_id: int, stage: str, notes: Optional[str] = None) -> None:
    row = conn.execute("SELECT max_stage, notes FROM pipeline WHERE company_id = ?", (company_id,)).fetchone()
    furthest = row["max_stage"] if row else STAGES[0]
    if stage != "Closed" and STAGES.index(stage) > STAGES.index(furthest):
        furthest = stage
    conn.execute("""INSERT INTO pipeline(company_id, stage, max_stage, notes, updated_on) VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(company_id) DO UPDATE SET stage = excluded.stage, max_stage = excluded.max_stage,
                      notes = excluded.notes, updated_on = excluded.updated_on""",
                 (company_id, stage, furthest, notes if notes is not None else (row["notes"] if row else None), db.today()))


def _advance(conn: sqlite3.Connection, company_id: int, to: str) -> None:
    current = stage_of(conn, company_id)
    if current != "Closed" and STAGES.index(to) > STAGES.index(current):
        set_stage(conn, company_id, to)


# --- activity log ----------------------------------------------------------------------------------------------

def next_outbound_kind(conn: sqlite3.Connection, person_id: int) -> str:
    n = conn.execute(f"SELECT COUNT(*) FROM activities WHERE person_id = ? AND kind IN {OUTBOUND}", (person_id,)).fetchone()[0]
    return "followup" if n else "sent"


def log(conn: sqlite3.Connection, company_id: int, kind: str, *, person_id: Optional[int] = None,
        channel: Optional[str] = None, job_id: Optional[int] = None, template: Optional[str] = None,
        note: Optional[str] = None, happened_on: Optional[str] = None, source: str = "manual",
        gmail_ref: Optional[str] = None) -> bool:
    """Record an activity and move the company's stage forward if it implies progress. False if already logged."""
    cur = conn.execute("""INSERT OR IGNORE INTO activities(company_id, person_id, job_id, kind, channel, template,
                          happened_on, note, source, gmail_ref) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (company_id, person_id, job_id, kind, channel, template, happened_on or db.today(), note,
                        source, gmail_ref))
    if cur.rowcount and kind in _ADVANCES:
        _advance(conn, company_id, _ADVANCES[kind])
    conn.commit()
    return cur.rowcount > 0


# --- what to do today ------------------------------------------------------------------------------------------

def followups_due(conn: sqlite3.Connection, on: Optional[date] = None) -> List[dict]:
    """People who got outreach, haven't replied, and are due their next follow-up."""
    on = on or date.today()
    gaps = settings()["follow_up_after_days"]
    rows = conn.execute(f"""
        SELECT p.id AS person_id, p.full_name, p.email, p.email_confidence, p.linkedin_url, c.id AS company_id,
               c.name AS company, c.tier,
               COUNT(*) AS sent, MAX(a.happened_on) AS last_on,
               (SELECT channel FROM activities x WHERE x.person_id = p.id AND x.kind IN {OUTBOUND}
                ORDER BY happened_on DESC, id DESC LIMIT 1) AS channel
        FROM activities a JOIN people p ON p.id = a.person_id JOIN companies c ON c.id = a.company_id
        WHERE a.kind IN {OUTBOUND}
          AND NOT EXISTS (SELECT 1 FROM activities r WHERE r.person_id = p.id AND r.kind IN ('reply', 'call', 'stop'))
          AND COALESCE((SELECT stage FROM pipeline pl WHERE pl.company_id = c.id), '') != 'Closed'
        GROUP BY p.id""").fetchall()
    due = []
    for r in rows:
        if r["sent"] > len(gaps):
            continue  # all follow-ups done
        due_on = date.fromisoformat(r["last_on"]) + timedelta(days=gaps[r["sent"] - 1])
        if due_on <= on:
            due.append({**dict(r), "due_on": due_on.isoformat(), "template": f"follow_up_{r['sent']}"})
    return sorted(due, key=lambda d: (d["due_on"], d["tier"] or 9))


def today_counts(conn: sqlite3.Connection, on: Optional[str] = None) -> Dict[str, int]:
    on = on or db.today()
    counts = dict(conn.execute("SELECT kind, COUNT(*) FROM activities WHERE happened_on = ? GROUP BY kind",
                               (on,)).fetchall())
    return {"new_outreach": counts.get("sent", 0), "follow_ups": counts.get("followup", 0),
            "applications": counts.get("application", 0)}


def next_up(conn: sqlite3.Connection, limit: int = 50) -> List[sqlite3.Row]:
    """Companies not reached out to yet, best fit first, each with its best contact.

    Applying doesn't count as reaching out: a company stays here after "Log application" until a message is logged.
    """
    return conn.execute(f"""
        SELECT c.id, c.tier, c.name, c.location_tag, c.lanes, c.sources, c.description, c.investors, c.website,
               c.contact_email, p.id AS person_id, p.full_name, COALESCE(p.title, p.role_kind) AS role, p.email,
               p.email_confidence, p.linkedin_url, p.warmth,
               (SELECT title FROM jobs j WHERE j.company_id = c.id AND j.is_open = 1 AND j.relevance > 0
                ORDER BY relevance DESC LIMIT 1) AS top_role,
               (SELECT MAX(relevance) FROM jobs j WHERE j.company_id = c.id AND j.is_open = 1) AS best_role
        FROM companies c
        LEFT JOIN people p ON p.id = (SELECT id FROM people x WHERE x.company_id = c.id AND x.priority <= 3
                                      ORDER BY priority, linkedin_url IS NULL, email IS NULL LIMIT 1)
        WHERE COALESCE((SELECT stage FROM pipeline pl WHERE pl.company_id = c.id), 'Not started')
                IN ('Not started', 'Applied / Referred')
          AND NOT EXISTS (SELECT 1 FROM activities a WHERE a.company_id = c.id AND a.kind IN {OUTBOUND})
          AND NOT EXISTS (SELECT 1 FROM jobs pj WHERE pj.company_id = c.id AND pj.is_open = 1 AND pj.priority_match = 1)
        ORDER BY c.tier, p.id IS NULL, c.location_tag = ? DESC, COALESCE(best_role, 0) DESC, c.name
        LIMIT ?""", (config.home_location(), limit)).fetchall()


def priority_matches(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Companies with a priority-match role (see requirements.py) still to act on, one row each with its best such role.

    A company stays until every priority role there has an application logged and you've reached out to someone.
    These companies are left out of next_up so they don't show twice.
    """
    priority = "FROM jobs k WHERE k.company_id = c.id AND k.is_open = 1 AND k.priority_match = 1"
    return conn.execute(f"""
        SELECT c.id, c.name, c.location_tag, c.contact_email, j.id AS job_id, j.title, j.years_label,
               (SELECT COUNT(*) {priority}) AS n_roles
        FROM companies c JOIN jobs j ON j.id = (SELECT k.id {priority} ORDER BY k.relevance DESC, k.id LIMIT 1)
        WHERE COALESCE((SELECT stage FROM pipeline pl WHERE pl.company_id = c.id), '') != 'Closed'
          AND (EXISTS (SELECT 1 {priority} AND NOT EXISTS (
                         SELECT 1 FROM activities a WHERE a.job_id = k.id AND a.kind = 'application'))
               OR NOT EXISTS (SELECT 1 FROM activities a WHERE a.company_id = c.id AND a.kind IN {OUTBOUND}))
        ORDER BY c.location_tag = ? DESC, j.relevance DESC, c.name""", (config.home_location(),)).fetchall()


# --- templates -------------------------------------------------------------------------------------------------

class _Fill(dict):
    def __missing__(self, key: str) -> str:
        return f"[{key}]"


def default_pitch(conn: sqlite3.Connection, company_id: int, job_id: Optional[int] = None) -> str:
    """Which version a message leads with (`pick` in pitches.yaml): by the role's family, else by the company's
    categories (the first listed match wins), else the default."""
    pick = config.load("pitches")["pick"]
    if job_id:
        row = conn.execute("SELECT family FROM jobs WHERE id = ?", (job_id,)).fetchone()
        families = pick.get("role_families") or {}
        if row and row["family"] in families:
            return families[row["family"]]
    lanes = set(conn.execute("SELECT lanes FROM companies WHERE id = ?", (company_id,)).fetchone()["lanes"].split(","))
    by_category = pick.get("company_categories") or {}
    return next((pitch for pitch, group in by_category.items() if lanes & set(group)), pick["default"])


def render(key: str, *, person_name: Optional[str], company: str, role: Optional[str],
           pitch: Optional[str] = None) -> Tuple[str, str, str]:
    """(subject, email body, LinkedIn note) for a template, with names and the pitch's resume numbers filled in."""
    t = templates()[key]
    p = pitches()[pitch or config.load("pitches")["pick"]["default"]]
    me = settings()["me"]
    fields = _Fill(company=company, my_name=me["name"], my_linkedin=me["linkedin"], pitch=p["email"].strip(),
                   pitch_short=p["linkedin"].strip(), focus=p["focus"], skills=p["skills"])
    # Shared inboxes ("Careers inbox") and unnamed contacts get "Hi there".
    fields["first_name"] = person_name.split()[0] if person_name and not person_name.endswith(" inbox") else "there"
    if role:
        fields["role"] = role
    return tuple(t[part].format_map(fields).strip() for part in ("subject", "email", "linkedin"))  # type: ignore


def suggested_template(company_sources: str, has_role: bool, warm: bool) -> str:
    if warm:
        return "warm_intro"
    if has_role:
        return "apply_and_reach_out"
    startup = any(s in company_sources.split(",") for s in ("yc", "vc", "formd", "hn"))
    return "founder_outreach" if startup else "big_company_networking"


# --- hit rates -------------------------------------------------------------------------------------------------

def funnel(conn: sqlite3.Connection) -> List[Tuple[str, int]]:
    """How many companies reached at least each stage."""
    reached = [r["max_stage"] for r in conn.execute("SELECT max_stage FROM pipeline")]
    return [(stage, sum(STAGES.index(m) >= i for m in reached)) for i, stage in enumerate(STAGES[1:-1], start=1)]


def reply_rates(conn: sqlite3.Connection, by: str) -> List[sqlite3.Row]:
    """People contacted vs people who replied, grouped by channel, tier, template, source or lane."""
    group = {"channel": "first.channel", "template": "COALESCE(first.template, 'none')", "tier": "c.tier",
             "source": "c.sources", "lane": "COALESCE(NULLIF(c.lanes, ''), 'other')"}[by]
    return conn.execute(f"""
        WITH first AS (SELECT person_id, company_id, channel, template, MIN(id) FROM activities
                       WHERE kind = 'sent' AND person_id IS NOT NULL GROUP BY person_id)
        SELECT {group} AS grp, COUNT(*) AS contacted,
               SUM(EXISTS (SELECT 1 FROM activities r WHERE r.person_id = first.person_id AND r.kind = 'reply')) AS replied
        FROM first JOIN companies c ON c.id = first.company_id
        GROUP BY grp ORDER BY contacted DESC""").fetchall()


def weekly_activity(conn: sqlite3.Connection, weeks: int = 8) -> List[sqlite3.Row]:
    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    return conn.execute("""SELECT date(happened_on, '-6 days', 'weekday 1') AS week, kind, COUNT(*) AS n
                           FROM activities WHERE happened_on >= ? AND kind IN ('sent', 'followup', 'reply', 'application',
                                                                                'call', 'interview')
                           GROUP BY week, kind ORDER BY week""", (since,)).fetchall()
