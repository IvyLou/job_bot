"""Console summary and an Excel workbook of the pipeline."""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Sequence

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from . import config


def _counts(conn: sqlite3.Connection, sql: str, *params) -> str:
    return ", ".join(f"{r[0] or 'none'} {r[1]}" for r in conn.execute(sql, params).fetchall())


def summary(conn: sqlite3.Connection) -> str:
    q = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731
    by_source = lambda s: q("SELECT COUNT(*) FROM companies WHERE ',' || sources || ',' LIKE ?", f"%,{s},%")  # noqa: E731
    lines = [
        f"Companies: {q('SELECT COUNT(*) FROM companies')}",
        "  by source:   " + ", ".join(f"{s} {by_source(s)}" for s in ("yc", "vc", "formd", "seed", "hn")),
        "  by location: " + _counts(conn, "SELECT location_tag, COUNT(*) FROM companies GROUP BY 1 ORDER BY 2 DESC"),
        "  by tier:     " + _counts(conn, "SELECT tier, COUNT(*) FROM companies GROUP BY 1 ORDER BY 1"),
        "  job system:  " + _counts(conn, "SELECT ats_type, COUNT(*) FROM companies WHERE ats_checked_at IS NOT NULL "
                                          "GROUP BY 1 ORDER BY 2 DESC LIMIT 10"),
        f"  with founders/execs named: {q('SELECT COUNT(DISTINCT company_id) FROM people')} "
        f"({q('SELECT COUNT(*) FROM people')} people)",
        f"Open matching roles: {q('SELECT COUNT(*) FROM jobs WHERE is_open = 1 AND relevance > 0')}",
        "  by location: " + _counts(conn, "SELECT location_tag, COUNT(*) FROM jobs WHERE is_open = 1 AND relevance > 0 "
                                          "GROUP BY 1 ORDER BY 2 DESC"),
        "  by family:   " + _counts(conn, "SELECT family, COUNT(*) FROM jobs WHERE is_open = 1 AND relevance > 0 "
                                          "GROUP BY 1 ORDER BY 2 DESC"),
        f"Contacts: {q('SELECT COUNT(DISTINCT company_id) FROM people WHERE priority <= 3')} companies with a "
        f"founder/leader to contact, {q('SELECT COUNT(DISTINCT company_id) FROM people WHERE warmth = ?', 'warm')} "
        f"with a warm connection",
        "  emails:      " + _counts(conn, "SELECT email_confidence, COUNT(*) FROM people WHERE email IS NOT NULL "
                                          "GROUP BY 1 ORDER BY 2 DESC")
        + f"; shared inboxes {q('SELECT COUNT(*) FROM companies WHERE contact_email IS NOT NULL')}"
        + f"; linkedin profiles {q('SELECT COUNT(*) FROM people WHERE linkedin_url IS NOT NULL')}",
        "",
        "Top roles:",
    ]
    for r in conn.execute("""SELECT j.relevance, c.name, j.title, j.location FROM jobs j
                             JOIN companies c ON c.id = j.company_id WHERE j.is_open = 1 AND j.relevance > 0
                             ORDER BY j.relevance DESC, j.first_seen DESC LIMIT 20"""):
        lines.append(f"  {r[0]:5.1f}  {r[1][:28]:28}  {r[2][:55]:55}  {(r[3] or '')[:40]}")
    return "\n".join(lines)


def _sheet(wb: Workbook, title: str, headers: Sequence[str], rows: List[Sequence], widths: Sequence[int],
           link_col: int = -1) -> None:
    ws = wb.create_sheet(title)
    ws.append(list(headers))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append(list(row))
        if link_col >= 0 and row[link_col]:
            cell = ws.cell(row=ws.max_row, column=link_col + 1)
            cell.hyperlink, cell.style = row[link_col], "Hyperlink"
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def _outreach_rows(conn: sqlite3.Connection) -> List[Sequence]:
    """One row per company: who to contact, how to reach them, and what to know about the company."""
    people: Dict[int, List[sqlite3.Row]] = defaultdict(list)
    for p in conn.execute("""SELECT company_id, full_name, COALESCE(title, role_kind) AS role, linkedin_url, email,
                                     email_confidence, warmth FROM people WHERE priority <= 3
                              ORDER BY company_id, priority, linkedin_url IS NULL, email IS NULL"""):
        people[p["company_id"]].append(p)
    roles = {r["company_id"]: r for r in conn.execute("""
        SELECT company_id, COUNT(*) AS n, title FROM (SELECT company_id, title FROM jobs
            WHERE is_open = 1 AND relevance > 0 ORDER BY relevance DESC) GROUP BY company_id""")}
    rows = []
    for c in conn.execute("""SELECT id, tier, name, location_tag, sources, contact_email, website, description,
                                    investors, last_raise_usd, last_raise_date FROM companies
                             ORDER BY tier, location_tag = ? DESC, name""", (config.home_location(),)):
        role = roles.get(c["id"])
        open_roles = f"{role['n']}: {role['title']}" if role else ""
        raise_ = f"${c['last_raise_usd'] / 1e6:.1f}M ({c['last_raise_date']})" if c["last_raise_usd"] else ""
        startup = any(src in c["sources"].split(",") for src in ("yc", "vc", "formd", "hn"))
        approach = "apply + reach out" if role else ("founder outreach" if startup else "network in")
        top = people[c["id"]][:2] + [None, None]
        cells = []
        for p in top[:2]:
            cells += [p["full_name"], p["role"], p["linkedin_url"], p["email"],
                      p["email_confidence"], "warm" if p["warmth"] == "warm" else ""] if p else [None] * 6
        rows.append([c["tier"], c["name"], c["location_tag"], approach, open_roles, (c["description"] or "")[:200],
                     c["investors"], raise_, *cells, c["contact_email"], c["website"]])
    return rows


def export(conn: sqlite3.Connection) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    contact_cols = [f"{h} {i}" for i in (1, 2) for h in ("Contact", "Role", "LinkedIn", "Email", "Email basis", "Warm")]
    _sheet(wb, "Outreach", ["Tier", "Company", "Loc", "Approach", "Open roles", "About", "Investors", "Last raise",
                            *contact_cols, "Shared inbox", "Website"], _outreach_rows(conn),
           [6, 28, 8, 17, 40, 50, 24, 18, 22, 14, 36, 30, 11, 6, 22, 14, 36, 30, 11, 6, 28, 30])
    roles = conn.execute("""
        SELECT CASE WHEN j.priority_match = 1 THEN 'yes' ELSE '' END, j.relevance, c.tier, c.name, j.title, j.family,
               j.years_label, j.location, j.location_tag, j.posted_at, j.first_seen, c.lanes, c.team_size, c.stage,
               j.years_quote, j.background_quote, j.url
        FROM jobs j JOIN companies c ON c.id = j.company_id
        WHERE j.is_open = 1 AND j.relevance > 0
        ORDER BY j.priority_match DESC, j.relevance DESC, j.first_seen DESC""").fetchall()
    _sheet(wb, "Roles", ["Priority match", "Relevance", "Tier", "Company", "Title", "Family", "Experience", "Location",
                         "Loc", "Posted", "First seen", "Lanes", "Team size", "Stage", "Experience line",
                         "Background line", "URL"], roles,
           [9, 10, 6, 28, 48, 16, 11, 32, 8, 11, 11, 22, 9, 18, 50, 50, 50], link_col=16)
    companies = conn.execute("""
        SELECT c.tier, c.name, c.location_tag, c.city, c.lanes, c.sources, c.team_size, c.stage, c.industry,
               c.ats_type,
               (SELECT COUNT(*) FROM jobs j WHERE j.company_id = c.id AND j.is_open = 1 AND j.relevance > 0),
               c.last_raise_date, c.last_raise_usd, c.investors,
               (SELECT GROUP_CONCAT(full_name || COALESCE(' (' || title || ')', ''), '; ')
                FROM people p WHERE p.company_id = c.id),
               c.description, c.website
        FROM companies c ORDER BY c.tier, c.location_tag = ? DESC, c.name""", (config.home_location(),)).fetchall()
    _sheet(wb, "Companies", ["Tier", "Company", "Loc", "City", "Lanes", "Sources", "Team size", "Stage", "Industry",
                             "Job system", "Open roles", "Last raise filed", "Raise amount ($)", "Investors",
                             "Founders / execs", "Description", "Website"], companies,
           [6, 28, 8, 16, 22, 14, 9, 18, 22, 14, 10, 14, 14, 24, 50, 50, 30], link_col=16)
    people = conn.execute("""
        SELECT c.tier, c.name, p.full_name, p.title, p.relationship, p.priority, p.linkedin_url, p.email,
               p.email_confidence, p.warmth, c.location_tag, p.source
        FROM people p JOIN companies c ON c.id = p.company_id ORDER BY c.tier, c.name, p.priority""").fetchall()
    _sheet(wb, "People", ["Tier", "Company", "Name", "Title", "Relationship", "Contact priority", "LinkedIn", "Email",
                          "Email basis", "Warmth", "Loc", "Source"], people,
           [6, 28, 26, 24, 28, 10, 40, 30, 11, 8, 8, 8], link_col=6)
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.EXPORT_DIR / f"pipeline_{date.today().isoformat()}.xlsx"
    wb.save(path)
    return path
