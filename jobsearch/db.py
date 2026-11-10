from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Iterable, Optional

from . import config
from .normalize import norm_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,           -- domain, else cik:<n>, else yc:<slug>, else name:<normalized>
  name TEXT NOT NULL,
  name_norm TEXT NOT NULL,
  domain TEXT,
  website TEXT,
  cik TEXT,
  yc_slug TEXT,
  description TEXT,
  city TEXT,
  state TEXT,
  location_tag TEXT,                  -- a key of `locations` in search.yaml, remote, us, or NULL (elsewhere)
  team_size INTEGER,
  stage TEXT,
  industry TEXT,
  lanes TEXT NOT NULL DEFAULT '',     -- comma-separated
  sources TEXT NOT NULL DEFAULT '',   -- comma-separated
  ats_type TEXT,
  ats_slug TEXT,
  ats_via TEXT,                       -- page | probe | link
  ats_checked_at TEXT,
  tier INTEGER,
  tier_locked INTEGER NOT NULL DEFAULT 0,
  investors TEXT,                     -- VC/accelerator portfolios the company appears in
  last_raise_usd REAL,                -- latest Form D raise
  last_raise_date TEXT,
  last_raise_accession TEXT,
  formd_checked_at TEXT,              -- looked up on EDGAR by name for founder/officer names
  domain_via TEXT,                    -- guess (matched from the name) | none (looked, not found)
  contact_email TEXT,                 -- best shared inbox (founders@, careers@, hello@ ...)
  contact_email_source TEXT,          -- site | hn
  email_pattern TEXT,                 -- first | first.last | flast | ...
  email_pattern_source TEXT,          -- person (seen on a named person) | weak (seen on an unnamed address)
  mx_ok INTEGER,                      -- domain accepts email
  emails_checked_at TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS companies_name_norm ON companies(name_norm);

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  full_name TEXT NOT NULL,
  title TEXT,
  relationship TEXT,                  -- founder | Executive Officer, Director | ...
  email TEXT,
  linkedin_url TEXT,
  warmth TEXT NOT NULL DEFAULT 'cold',  -- warm = one of your LinkedIn connections
  priority INTEGER,                   -- 0 warm, 1 team leader or CEO, 2 founder, 3 exec, 5 board, 6 other
  role_kind TEXT,
  email_confidence TEXT,              -- published | given | pattern | guess
  source TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  UNIQUE(company_id, full_name)
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  department TEXT,
  location TEXT,
  location_tag TEXT,
  url TEXT,
  posted_at TEXT,
  family TEXT,                        -- matched role family
  reports_to TEXT,                    -- title the posting says the role reports to, when it says
  description TEXT,                   -- the posting's text
  years_min INTEGER,                  -- required years of experience the posting states (highest required minimum)
  years_label TEXT,                   -- "1–3 yrs", "2+ yrs"
  years_quote TEXT,                   -- the line it was read from
  background_quote TEXT,              -- a line naming one of your backgrounds as a fitting one
  priority_match INTEGER,             -- 1: matches every `priority_match` rule in search.yaml
  relevance REAL,
  is_open INTEGER NOT NULL DEFAULT 1,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Your own progress. The daily pipeline never writes to these two tables (except Gmail sync adding activities).
CREATE TABLE IF NOT EXISTS pipeline (
  company_id INTEGER PRIMARY KEY REFERENCES companies(id),
  stage TEXT NOT NULL,                -- see crm.STAGES
  max_stage TEXT NOT NULL,            -- furthest stage reached, for the funnel
  notes TEXT,
  updated_on TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  person_id INTEGER REFERENCES people(id),
  job_id INTEGER REFERENCES jobs(id),
  kind TEXT NOT NULL,                 -- sent | followup | reply | call | application | interview | note | bounce | stop
  channel TEXT,                       -- email | linkedin | application | call | other
  template TEXT,
  happened_on TEXT NOT NULL,
  note TEXT,
  source TEXT NOT NULL DEFAULT 'manual',  -- manual | gmail
  gmail_ref TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS activities_person ON activities(person_id);
CREATE INDEX IF NOT EXISTS activities_company ON activities(company_id);
CREATE INDEX IF NOT EXISTS activities_job ON activities(job_id);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company_id, is_open);  -- without it, Today took ~35s
"""


def today() -> str:
    return date.today().isoformat()


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)  # the dashboard and the daily job can overlap
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # the dashboard keeps reading while the daily run writes
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


_ADDED_COLUMNS = {
    "companies": {"investors": "TEXT", "last_raise_usd": "REAL", "last_raise_date": "TEXT",
                  "last_raise_accession": "TEXT", "formd_checked_at": "TEXT", "domain_via": "TEXT", "contact_email": "TEXT", "contact_email_source": "TEXT", "email_pattern": "TEXT",
                  "email_pattern_source": "TEXT", "mx_ok": "INTEGER", "emails_checked_at": "TEXT"},
    "people": {"priority": "INTEGER", "role_kind": "TEXT", "email_confidence": "TEXT"},
    "jobs": {"reports_to": "TEXT", "description": "TEXT", "years_min": "INTEGER", "years_label": "TEXT",
             "years_quote": "TEXT", "background_quote": "TEXT", "priority_match": "INTEGER"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was created."""
    if "fs_quote" in {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}:
        conn.execute("ALTER TABLE jobs RENAME COLUMN fs_quote TO background_quote")
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, kind in columns.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
    # Signals were removed: keep each company's latest raise, then drop the old tables and column.
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "signals" in tables:
        conn.execute("""UPDATE companies SET (last_raise_usd, last_raise_date, last_raise_accession) =
                          (SELECT amount_usd, occurred_on, source_ref FROM signals s
                           WHERE s.company_id = companies.id AND s.kind = 'funding' ORDER BY occurred_on DESC LIMIT 1)
                        WHERE last_raise_date IS NULL""")
        conn.execute("DROP TABLE signals")
    if "job_counts" in tables:
        conn.execute("DROP TABLE job_counts")
    if "is_leader" in {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}:
        conn.execute("DELETE FROM jobs WHERE family IS NULL")
        conn.execute("ALTER TABLE jobs DROP COLUMN is_leader")
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def _merge_csv(existing: str, new: Iterable[str]) -> str:
    items = [x for x in existing.split(",") if x]
    items += [x for x in new if x and x not in items]
    return ",".join(items)


def find_company_by_name(conn: sqlite3.Connection, name: str, location_tag: Optional[str] = None) -> Optional[int]:
    """Return the id of the single company with this normalized name (and location, if given), if exactly one."""
    sql, params = "SELECT id FROM companies WHERE name_norm = ?", [norm_name(name)]
    if location_tag:
        sql, params = sql + " AND location_tag = ?", params + [location_tag]
    rows = conn.execute(sql + " LIMIT 2", params).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def upsert_company(conn: sqlite3.Connection, *, key: str, name: str, source: str,
                   lanes: Iterable[str] = (), company_id: Optional[int] = None, **fields: Any) -> int:
    """Insert a company or enrich an existing one. Existing non-empty fields win; lanes and sources accumulate."""
    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    now = today()
    if company_id is None:
        row = conn.execute("SELECT id FROM companies WHERE key = ?", (key,)).fetchone()
        company_id = row["id"] if row else None
    if company_id is None:
        cols = {"key": key, "name": name, "name_norm": norm_name(name), "sources": source,
                "lanes": _merge_csv("", lanes), "first_seen": now, "last_seen": now, **fields}
        cur = conn.execute(f"INSERT INTO companies({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                           list(cols.values()))
        return cur.lastrowid
    row = conn.execute("SELECT sources, lanes FROM companies WHERE id = ?", (company_id,)).fetchone()
    sets = ["sources = ?", "lanes = ?", "last_seen = ?"] + [f"{k} = COALESCE(NULLIF({k}, ''), ?)" for k in fields]
    params = [_merge_csv(row["sources"], [source]), _merge_csv(row["lanes"], lanes), now, *fields.values()]
    conn.execute(f"UPDATE companies SET {', '.join(sets)} WHERE id = ?", [*params, company_id])
    return company_id


def upsert_person(conn: sqlite3.Connection, company_id: int, full_name: str, source: str, **fields: Any) -> None:
    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    cols = {"company_id": company_id, "full_name": full_name, "source": source, "first_seen": today(), **fields}
    updates = ", ".join(f"{k} = COALESCE(NULLIF({k}, ''), excluded.{k})" for k in fields) or "full_name = full_name"
    conn.execute(f"INSERT INTO people({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
                 f"ON CONFLICT(company_id, full_name) DO UPDATE SET {updates}", list(cols.values()))
