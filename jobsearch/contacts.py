"""Contacts: the right person at every company, and a way to reach them.

Every company in the universe is a lead whether or not it posts jobs. This module
1. finds a website for companies we only know by name (Form D filers),
2. collects the email addresses a company publishes on its own site,
3. ranks the people we know at each company by who to contact first,
4. infers the company's email format and fills in a best-guess address for its top contacts,
5. marks warm paths from a LinkedIn connections export.
"""
from __future__ import annotations

import asyncio
import csv
import html
import re
import sqlite3
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import dns.asyncresolver
import httpx

from . import crm, db
from .ats import _get_text
from .http import bounded, web_client
from .normalize import compact, domain_of, norm_name

# --- who to contact first ---------------------------------------------------------------------------------

# (priority, kind, pattern) checked in order against "title relationship"; lower priority = contact first.
# `first_contacts` in crm.yaml (the leaders of the teams you'd join) come before these.
_ROLES = [
    (1, "ceo", re.compile(r"\b(ceo|chief executive|president)\b", re.I)),
    (2, "founder", re.compile(r"found", re.I)),
    (3, "exec", re.compile(r"\b(chief|cto|cpo|cmo|cro|head of|vp|vice president|general manager)\b|executive officer", re.I)),
    (5, "board", re.compile(r"\bdirector\b", re.I)),  # Form D "Director" = board seat, usually an investor
]


@lru_cache(maxsize=None)
def _first_contacts(items: Tuple[Tuple[str, str], ...]) -> List[Tuple[int, str, re.Pattern]]:
    return [(1, kind, re.compile(pattern, re.I)) for kind, pattern in items]


def _role(title: Optional[str], relationship: Optional[str], team_size: Optional[int]) -> Tuple[int, str]:
    text = f"{title or ''} {relationship or ''}"
    first = _first_contacts(tuple((crm.settings().get("first_contacts") or {}).items()))
    priority, kind = next(((p, k) for p, k, rx in first + _ROLES if rx.search(text)), (6, "other"))
    if team_size and team_size > 150 and kind in ("ceo", "founder"):
        priority += 2  # at larger companies the leader of your team is the better first contact
    return priority, kind


# --- email formats ---------------------------------------------------------------------------------------

FORMATS = {
    "first": lambda f, l: f,
    "first.last": lambda f, l: f"{f}.{l}",
    "flast": lambda f, l: f"{f[0]}{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "first_last": lambda f, l: f"{f}_{l}",
    "f.last": lambda f, l: f"{f[0]}.{l}",
    "firstl": lambda f, l: f"{f}{l[0]}",
    "last": lambda f, l: l,
}
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "cfa", "cpa", "mba", "esq"}


def name_parts(full_name: str) -> Optional[Tuple[str, str]]:
    ascii_name = unicodedata.normalize("NFKD", full_name).encode("ascii", "ignore").decode().lower()
    tokens = [t for t in re.sub(r"[^a-z\s-]", " ", ascii_name).replace("-", "").split() if t not in _NAME_SUFFIXES]
    if len(tokens) < 2 or len(tokens[0]) < 2:
        return None
    return tokens[0], tokens[-1]


def format_of(local: str, first: str, last: str) -> Optional[str]:
    return next((name for name, fn in FORMATS.items() if fn(first, last) == local), None)


# --- published emails ------------------------------------------------------------------------------------

_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,4})")
_CLOUDFLARE = re.compile(r'data-cfemail="([0-9a-fA-F]{4,200})"')
# Shared inboxes worth writing to, best first. Anything else generic (support@, sales@, privacy@ ...) is skipped.
INBOXES = ["founders", "founder", "careers", "jobs", "hiring", "talent", "recruiting", "people", "hello", "hi", "team",
           "contact", "info"]
_SKIP_LOCAL = re.compile(r"^(support|help|sales|press|media|pr|privacy|legal|security|abuse|no-?reply|donotreply|"
                         r"billing|accounts?|invoices?|ap|ar|compliance|dpo|gdpr|unsubscribe|webmaster|admin|partners?|"
                         r"partnerships|investors?|ir|marketing|example|name|email|you|your|user|test)$", re.I)
_DEPARTMENTS = {"customer", "client", "service", "services", "media", "news", "events", "office", "hr", "finance",
                "ops", "operations", "recruit", "recruiting", "loans", "rewards", "portal", "requests", "inquiries",
                "relations", "desk", "general", "global", "us", "uk", "emea", "apac", "group", "the", "and"}
PAGES = ("", "/about", "/team", "/contact", "/careers")


def _cloudflare_decode(hexstr: str) -> str:
    key = int(hexstr[:2], 16)
    return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr) - 1, 2))


def emails_in(text: str, domain: str) -> Set[str]:
    """Emails at the company's own domain found in page text, including Cloudflare-obfuscated ones."""
    text = html.unescape(text) + " " + " ".join(_cloudflare_decode(h) for h in _CLOUDFLARE.findall(text))
    found = set()
    for local, host in _EMAIL.findall(text):
        host = host.lower()
        if (host == domain or host.endswith("." + domain)) and not _SKIP_LOCAL.match(local):
            found.add(f"{local.lower()}@{domain}")
    return found


def record_emails(conn: sqlite3.Connection, company_id: int, emails: Set[str], source: str) -> None:
    """Attach published emails to known people, learn the company's email format, and keep the best shared inbox."""
    people = conn.execute("SELECT id, full_name FROM people WHERE company_id = ?", (company_id,)).fetchall()
    company = conn.execute("SELECT contact_email, email_pattern_source FROM companies WHERE id = ?",
                           (company_id,)).fetchone()
    inbox, pattern, pattern_source = None, None, None
    for email in sorted(emails):
        local = email.split("@")[0]
        if local in INBOXES:
            if inbox is None or INBOXES.index(local) < INBOXES.index(inbox.split("@")[0]):
                inbox = email
            continue
        for p in people:
            parts = name_parts(p["full_name"])
            fmt = parts and format_of(local, *parts)
            if fmt:
                conn.execute("UPDATE people SET email = ?, email_confidence = 'published' WHERE id = ?", (email, p["id"]))
                pattern, pattern_source = fmt, "person"
                break
        else:
            # Unnamed addresses only count when they look like "jane.doe": single words are as often departments
            # (connect@, loans@) or initials+surname (jdoe@) as first names, so they say nothing about the format.
            m = re.fullmatch(r"([a-z]{2,15})\.([a-z]{2,20})", local)
            if pattern is None and m and not any(_SKIP_LOCAL.match(t) or t in INBOXES or t in _DEPARTMENTS
                                                 for t in m.groups()):
                pattern, pattern_source = "first.last", "weak"
    if inbox and not company["contact_email"]:
        conn.execute("UPDATE companies SET contact_email = ?, contact_email_source = ? WHERE id = ?",
                     (inbox, source, company_id))
    if pattern and (pattern_source == "person" or company["email_pattern_source"] != "person"):
        conn.execute("UPDATE companies SET email_pattern = ?, email_pattern_source = ? WHERE id = ?",
                     (pattern, pattern_source, company_id))


async def _has_mx(domain: str) -> bool:
    try:
        return bool(await dns.asyncresolver.resolve(domain, "MX", lifetime=6))
    except Exception:  # NXDOMAIN, no answer, timeout: treat as "can't receive mail"
        return False


async def harvest_all(conn: sqlite3.Connection, recheck: bool = False, limit: Optional[int] = None) -> Dict[str, int]:
    rows = conn.execute(f"""SELECT id, domain, website FROM companies WHERE domain IS NOT NULL
                            {'' if recheck else 'AND emails_checked_at IS NULL'} ORDER BY COALESCE(tier, 9), id""").fetchall()
    rows = rows[:limit] if limit else rows
    stats = {"companies": 0, "with_emails": 0, "mx_ok": 0}

    async def visit(client: httpx.AsyncClient, row: sqlite3.Row) -> Tuple[Set[str], bool]:
        base = (row["website"] or f"https://{row['domain']}").rstrip("/")
        pages = await asyncio.gather(*(_get_text(client, base + path) for path in PAGES))
        found: Set[str] = set()
        for body, _ in pages:
            found |= emails_in(body, row["domain"])
        return found, await _has_mx(row["domain"])

    async with web_client() as client:
        async for row, (found, mx) in bounded(rows, lambda r: visit(client, r), limit=24):
            if found:
                record_emails(conn, row["id"], found, "site")
                stats["with_emails"] += 1
            stats["mx_ok"] += mx
            conn.execute("UPDATE companies SET mx_ok = ?, emails_checked_at = ? WHERE id = ?",
                         (int(mx), db.today(), row["id"]))
            stats["companies"] += 1
            if stats["companies"] % 100 == 0:
                conn.commit()
                print(f"  harvest: {stats['companies']}/{len(rows)}  {stats}", flush=True)
    conn.commit()
    return stats


# --- websites for companies known only by name -----------------------------------------------------------

_PARKED = re.compile(r"domain (is )?for sale|buy this domain|this domain|parked|hugedomains|afternic|sedo|dan\.com|"
                     r"godaddy|domain name", re.I)
_LEGAL_WORDS = {"inc", "llc", "corp", "corporation", "co", "ltd", "limited", "incorporated", "pbc", "plc", "de"}


def _domain_candidates(name: str) -> List[str]:
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", html.unescape(name).lower()).split() if w not in _LEGAL_WORDS]
    base, joined = compact(name), "".join(words)
    if len(base) < 3:
        return []
    out = [f"{base}.com", f"{joined}.com", f"{base}.ai", f"{base}.io", f"{base}.co", f"get{base}.com",
           f"try{base}.com", f"{base}hq.com", f"use{base}.com", f"{joined}.ai"]
    return list(dict.fromkeys(out))


async def _resolve_domain(client: httpx.AsyncClient, name: str) -> Optional[str]:
    tokens = norm_name(name).split()
    for cand in _domain_candidates(name):
        body, final = await _get_text(client, f"https://{cand}")
        if not body or domain_of(final) != cand:
            continue
        title = html.unescape((re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I) or [None, ""])[1]).lower()
        if _PARKED.search(title) or _PARKED.search(body[:4000]):
            continue
        if tokens and all(t in title for t in tokens):
            return cand
    return None


def _merge_into(conn: sqlite3.Connection, src: int, dst: int) -> None:
    """Fold a duplicate company record (e.g. a Form D filer we just matched to a YC company) into the original."""
    for table in ("people", "jobs"):
        conn.execute(f"UPDATE OR IGNORE {table} SET company_id = ? WHERE company_id = ?", (dst, src))
        conn.execute(f"DELETE FROM {table} WHERE company_id = ?", (src,))
    s = conn.execute("SELECT * FROM companies WHERE id = ?", (src,)).fetchone()
    d = conn.execute("SELECT sources, lanes FROM companies WHERE id = ?", (dst,)).fetchone()
    conn.execute("""UPDATE companies SET sources = ?, lanes = ?, cik = COALESCE(cik, ?),
                    last_raise_usd = COALESCE(last_raise_usd, ?), last_raise_date = COALESCE(last_raise_date, ?),
                    last_raise_accession = COALESCE(last_raise_accession, ?) WHERE id = ?""",
                 (db._merge_csv(d["sources"], s["sources"].split(",")), db._merge_csv(d["lanes"], s["lanes"].split(",")),
                  s["cik"], s["last_raise_usd"], s["last_raise_date"], s["last_raise_accession"], dst))
    conn.execute("DELETE FROM companies WHERE id = ?", (src,))


async def resolve_domains(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute("""SELECT id, name FROM companies WHERE domain IS NULL AND website IS NULL
                           AND domain_via IS NULL""").fetchall()
    stats = {"checked": 0, "found": 0, "merged": 0}
    async with web_client() as client:
        async for row, found in bounded(rows, lambda r: _resolve_domain(client, r["name"]), limit=16):
            stats["checked"] += 1
            if not found:
                conn.execute("UPDATE companies SET domain_via = 'none' WHERE id = ?", (row["id"],))
                continue
            stats["found"] += 1
            twin = conn.execute("SELECT id FROM companies WHERE (key = ? OR domain = ?) AND id != ?",
                                (found, found, row["id"])).fetchone()
            if twin:
                _merge_into(conn, row["id"], twin["id"])
                stats["merged"] += 1
            else:
                conn.execute("UPDATE companies SET domain = ?, website = ?, domain_via = 'guess' WHERE id = ?",
                             (found, f"https://{found}", row["id"]))
    conn.commit()
    return stats


# --- ranking and best-guess emails -----------------------------------------------------------------------

def assign(conn: sqlite3.Connection) -> Dict[str, int]:
    """Rank everyone we know at each company and fill in best-guess emails for the top contacts."""
    stats = {"people": 0, "guessed": 0}
    for p in conn.execute("""SELECT p.id, p.full_name, p.title, p.relationship, p.warmth, p.source, p.email,
                                    p.role_kind AS kind_before, p.priority AS priority_before,
                                    p.email_confidence,
                                    c.team_size, c.sources, c.domain, c.email_pattern, c.email_pattern_source, c.mx_ok
                             FROM people p JOIN companies c ON c.id = p.company_id""").fetchall():
        priority, kind = _role(p["title"], p["relationship"], p["team_size"])
        if p["warmth"] == "warm":
            priority = 0
        elif p["source"] == "gmail":
            priority = min(priority, 2)  # someone you've already emailed
        if p["source"] in ("manual", "gmail", "site") and p["kind_before"]:
            kind = p["kind_before"]  # hiring_manager / recruiter / contact / inbox: set on purpose, keep it
            priority = min(priority, p["priority_before"] if p["priority_before"] is not None else priority)
        email, confidence = p["email"], p["email_confidence"] or ("given" if p["email"] else None)
        if confidence in ("guess", "pattern"):  # recompute: the company's format may have been learned since
            email, confidence = None, None
        parts = name_parts(p["full_name"])
        if not email and parts and p["domain"] and p["mx_ok"] and priority <= 3:
            # Startups mostly use first@; bigger or unknown-size hand-picked companies mostly use first.last@.
            small = p["team_size"] <= 100 if p["team_size"] else "seed" not in p["sources"].split(",")
            fmt = p["email_pattern"] or ("first" if small else "first.last")
            email = f"{FORMATS[fmt](*parts)}@{p['domain']}"
            confidence = "pattern" if p["email_pattern_source"] == "person" else "guess"
            stats["guessed"] += 1
        conn.execute("UPDATE people SET priority = ?, role_kind = ?, email = ?, email_confidence = ? WHERE id = ?",
                     (priority, kind, email, confidence, p["id"]))
        stats["people"] += 1
    conn.commit()
    return stats


# --- warm paths ----------------------------------------------------------------------------------------------

def import_linkedin(conn: sqlite3.Connection, path: str) -> Dict[str, int]:
    """Load LinkedIn's Connections.csv export (Settings > Data privacy > Get a copy of your data)."""
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("First Name"))
    stats = {"connections": 0, "matched": 0}
    for r in csv.DictReader(lines[start:]):
        stats["connections"] += 1
        name = f"{r.get('First Name', '').strip()} {r.get('Last Name', '').strip()}".strip()
        cid = r.get("Company") and db.find_company_by_name(conn, r["Company"])
        if not (cid and name):
            continue
        db.upsert_person(conn, cid, name, "linkedin", title=r.get("Position"), linkedin_url=r.get("URL"),
                         email=r.get("Email Address"))
        conn.execute("UPDATE people SET warmth = 'warm' WHERE company_id = ? AND full_name = ?", (cid, name))
        stats["matched"] += 1
    conn.commit()
    return stats
