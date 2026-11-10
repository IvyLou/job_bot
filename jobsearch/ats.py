"""Find which applicant tracking system (ATS) each company uses, then pull open roles from its public posting API."""
from __future__ import annotations

import asyncio
import html
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from . import config, db
from .http import bounded, web_client
from .normalize import compact, domain_of, domain_root, html_text, names_match, norm_name

# Order matters: when a page mentions several systems we keep the first supported one.
SUPPORTED = ("greenhouse", "ashby", "lever", "workable", "smartrecruiters", "recruitee", "workday")
PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.recruitee\.com", re.I)),
    ("workday", re.compile(r"(?<![a-z0-9-])([a-z0-9-]{1,63}\.wd\d+\.myworkdayjobs\.com(?:/[a-z]{2}-[A-Z]{2})?/[A-Za-z0-9_-]+)", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/([A-Za-z0-9_-]+)", re.I)),
    ("bamboohr", re.compile(r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.bamboohr\.com/(?:careers|jobs)", re.I)),
    ("gem", re.compile(r"jobs\.gem\.com/([A-Za-z0-9_-]+)", re.I)),
    ("dover", re.compile(r"app\.dover\.(?:com|io)/(?:jobs|apply)/([A-Za-z0-9_-]+)", re.I)),
    ("teamtailor", re.compile(r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.teamtailor\.com", re.I)),
    ("jazzhr", re.compile(r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.applytojob\.com", re.I)),
    ("breezy", re.compile(r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.breezy\.hr", re.I)),
    ("icims", re.compile(r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.icims\.com", re.I)),
    ("taleo", re.compile(r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.taleo\.net", re.I)),
    ("oracle", re.compile(r"(?<![a-z0-9-])([a-z0-9-]{1,63}\.fa\.[a-z0-9-]{1,63}\.oraclecloud\.com)", re.I)),
    ("successfactors", re.compile(r"(career\d*\.successfactors\.com|jobs\.sap\.com)", re.I)),
    ("yc", re.compile(r"ycombinator\.com/companies/([A-Za-z0-9_-]+)/jobs", re.I)),
]
_BAD_SLUGS = {"embed", "js", "v1", "api", "jobs", "job_board", "careers", "www", "app", "static", "assets", "cdn",
              "widget", "boards", "job-boards", "search", "images", "css", "careers-page", "api-docs"}
_CAREER_HREF = re.compile(r"career|jobs|join|hiring|work-with-us|workwithus|open-roles|positions|opportunities", re.I)


@dataclass
class Posting:
    external_id: str
    title: str
    location: str = ""
    url: str = ""
    department: str = ""
    posted_at: Optional[str] = None
    description: str = ""      # where the listing includes it (Lever, Ashby, Recruitee); others: fetch_descriptions


def find_ats(text: str) -> Optional[Tuple[str, str]]:
    found = []
    for kind, rx in PATTERNS:
        for m in rx.finditer(text):
            slug = m.group(1).strip("/")
            if slug.lower() not in _BAD_SLUGS:
                found.append((kind, slug))
                break
    if not found:
        return None
    return min(found, key=lambda f: SUPPORTED.index(f[0]) if f[0] in SUPPORTED else len(SUPPORTED))


async def _get_text(client: httpx.AsyncClient, url: str) -> Tuple[str, str]:
    try:
        r = await client.get(url)
    except Exception:  # crawling arbitrary sites: DNS, TLS, bad URLs, decode errors all mean "nothing here"
        return "", url
    if r.status_code >= 400 or not any(t in r.headers.get("content-type", "") for t in ("html", "json", "text")):
        return "", str(r.url)
    return r.text[:1_500_000], str(r.url)


async def _get_json(client: httpx.AsyncClient, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    try:
        r = await client.get(url, params=params)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _career_links(body: str, base: str) -> List[str]:
    host = domain_of(base)
    links = []
    for href in re.findall(r"""href=["']([^"'#\s]+)""", body):
        if not _CAREER_HREF.search(href):
            continue
        url = urljoin(base, href)
        if domain_of(url) == host and url not in links:
            links.append(url)
    return sorted(links, key=len)


def _mentions(company: sqlite3.Row, text: str) -> bool:
    name = compact(company["name"])
    return (len(name) >= 4 and name in compact(text)) or bool(company["domain"] and company["domain"] in text.lower())


async def _probe(client: httpx.AsyncClient, company: sqlite3.Row) -> Optional[Tuple[str, str, str]]:
    """Guess board slugs from the domain/name and accept only boards that clearly belong to this company."""
    root, name = domain_root(company["domain"] or ""), company["name"]
    candidates = []
    for s in (company["yc_slug"], root, compact(name), norm_name(name).replace(" ", "-")):
        if s and s not in candidates:
            candidates.append(s)
    strict = [s for s in candidates if s in (root, compact(name))]

    async def greenhouse(slug: str) -> Optional[str]:
        board = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        return slug if board and names_match(board.get("name", ""), name) else None

    async def ashby(slug: str) -> Optional[str]:
        board = await _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        return slug if board and board.get("jobs") and _mentions(company, json.dumps(board["jobs"][:5])) else None

    async def lever(slug: str) -> Optional[str]:
        board = await _get_json(client, f"https://api.lever.co/v0/postings/{slug}", {"mode": "json", "limit": 5})
        return slug if isinstance(board, list) and board and _mentions(company, json.dumps(board)) else None

    checks = [("greenhouse", greenhouse(s)) for s in candidates]
    checks += [(kind, fn(s)) for s in strict for kind, fn in (("ashby", ashby), ("lever", lever))]
    results = await asyncio.gather(*(c for _, c in checks))
    return next(((kind, slug, "probe") for (kind, _), slug in zip(checks, results) if slug), None)


async def _from_site(client: httpx.AsyncClient, website: str) -> Optional[Tuple[str, str]]:
    body, final = await _get_text(client, website)
    hit = find_ats(final + " " + body)
    if hit and hit[0] in SUPPORTED:
        return hit
    links = _career_links(body, final)[:3] or [urljoin(final, "/careers"), urljoin(final, "/jobs")]
    for b, f in await asyncio.gather(*(_get_text(client, link) for link in links)):
        h = find_ats(f + " " + b)
        if h and (h[0] in SUPPORTED or not hit):
            hit = h
    return hit


async def detect(client: httpx.AsyncClient, company: sqlite3.Row) -> Optional[Tuple[str, str, str]]:
    """The company's own site is authoritative; slug probes run alongside and fill in when the site says nothing."""
    website = company["website"] or (f"https://{company['domain']}" if company["domain"] else None)
    probe = asyncio.ensure_future(_probe(client, company))
    hit = await _from_site(client, website) if website else None
    if hit and hit[0] in SUPPORTED:
        probe.cancel()
        return hit[0], hit[1], "page"
    return await probe or ((hit[0], hit[1], "page") if hit else None)


async def detect_all(conn: sqlite3.Connection, recheck: bool = False, limit: Optional[int] = None) -> Dict[str, int]:
    rows = conn.execute(f"""SELECT id, name, domain, website, yc_slug FROM companies
                            WHERE (domain IS NOT NULL OR website IS NOT NULL)
                              AND {'1=1' if recheck else 'ats_checked_at IS NULL'}
                            ORDER BY COALESCE(tier, 9), id""").fetchall()
    rows = rows[:limit] if limit else rows
    counts: Dict[str, int] = {}
    done = 0
    async with web_client() as client:
        async for row, hit in bounded(rows, lambda r: detect(client, r), limit=32):
            kind, slug, via = hit or (None, None, None)
            conn.execute("UPDATE companies SET ats_type = ?, ats_slug = ?, ats_via = ?, ats_checked_at = ? WHERE id = ?",
                         (kind, slug, via, db.today(), row["id"]))
            counts[kind or "none"] = counts.get(kind or "none", 0) + 1
            done += 1
            if done % 100 == 0:
                conn.commit()
                print(f"  detect-ats: {done}/{len(rows)}  {counts}", flush=True)
    conn.commit()
    return counts


# --- posting fetchers ---------------------------------------------------------------------------------------

def _iso(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # Lever uses epoch milliseconds
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    return str(value)[:10]


def _join(*parts: Optional[str]) -> str:
    return ", ".join(p for p in parts if p)


async def _greenhouse(client: httpx.AsyncClient, slug: str) -> Optional[List[Posting]]:
    d = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if d is None:
        return None
    return [Posting(f"greenhouse:{slug}:{j['id']}", j.get("title", ""), (j.get("location") or {}).get("name", ""),
                    j.get("absolute_url", ""), "", _iso(j.get("first_published") or j.get("updated_at")))
            for j in d.get("jobs", [])]


async def _lever(client: httpx.AsyncClient, slug: str) -> Optional[List[Posting]]:
    d = await _get_json(client, f"https://api.lever.co/v0/postings/{slug}", {"mode": "json"})
    if not isinstance(d, list):
        return None
    out = []
    for j in d:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location")]
        if j.get("workplaceType") == "remote":
            locs = [*locs, "Remote"]
        description = "\n".join([j.get("descriptionPlain") or "",
                                 *(f"{l.get('text') or ''}\n{html_text(l.get('content') or '')}" for l in j.get("lists") or []),
                                 j.get("additionalPlain") or ""])
        out.append(Posting(f"lever:{slug}:{j['id']}", j.get("text", ""), "; ".join(l for l in locs if l),
                           j.get("hostedUrl", ""), cat.get("team") or cat.get("department") or "", _iso(j.get("createdAt")),
                           description))
    return out


async def _ashby(client: httpx.AsyncClient, slug: str) -> Optional[List[Posting]]:
    d = await _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if d is None:
        return None
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote") or str(j.get("workplaceType", "")).lower() == "remote":
            locs.append("Remote")
        out.append(Posting(f"ashby:{slug}:{j['id']}", j.get("title", ""), "; ".join(l for l in locs if l),
                           j.get("jobUrl", ""), j.get("department") or j.get("team") or "", _iso(j.get("publishedAt")),
                           j.get("descriptionPlain") or ""))
    return out


async def _workable(client: httpx.AsyncClient, slug: str) -> Optional[List[Posting]]:
    d = await _get_json(client, f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if d is None:
        return None
    return [Posting(f"workable:{slug}:{j.get('shortcode')}", j.get("title", ""),
                    _join(j.get("city"), j.get("state"), j.get("country")) + ("; Remote" if j.get("telecommuting") else ""),
                    j.get("url") or j.get("shortlink") or "", j.get("department") or "", _iso(j.get("published_on")))
            for j in d.get("jobs", [])]


async def _smartrecruiters(client: httpx.AsyncClient, slug: str) -> Optional[List[Posting]]:
    out: List[Posting] = []
    for offset in range(0, 1000, 100):
        d = await _get_json(client, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                            {"limit": 100, "offset": offset})
        if d is None:
            return out if out else None
        content = d.get("content", [])
        for j in content:
            loc = j.get("location") or {}
            out.append(Posting(f"smartrecruiters:{slug}:{j['id']}", j.get("name", ""),
                               _join(loc.get("city"), loc.get("region"), loc.get("country")) + ("; Remote" if loc.get("remote") else ""),
                               f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                               (j.get("department") or {}).get("label", ""), _iso(j.get("releasedDate"))))
        if len(content) < 100:
            break
    return out


async def _recruitee(client: httpx.AsyncClient, slug: str) -> Optional[List[Posting]]:
    d = await _get_json(client, f"https://{slug}.recruitee.com/api/offers/")
    if d is None:
        return None
    return [Posting(f"recruitee:{slug}:{j['id']}", j.get("title", ""),
                    (j.get("location") or _join(j.get("city"), j.get("country"))) + ("; Remote" if j.get("remote") else ""),
                    j.get("careers_url", ""), j.get("department") or "", _iso(j.get("published_at")),
                    html_text(f"{j.get('description') or ''}<br>{j.get('requirements') or ''}"))
            for j in d.get("offers", [])]


# Workday boards (most banks and large companies) are big, so we search them by keyword (`workday_queries` in
# config/search.yaml).
_WORKDAY_PAGES = 5  # x 20 results per query
_WORKDAY_POSTED = re.compile(r"(\d+)\+? days? ago", re.I)


def _workday_posted(text: str) -> Optional[str]:
    text = (text or "").lower()
    if "today" in text:
        days = 0
    elif "yesterday" in text:
        days = 1
    else:
        m = _WORKDAY_POSTED.search(text)
        if not m:
            return None
        days = int(m.group(1))
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


async def _workday(client: httpx.AsyncClient, slug: str) -> Optional[List[Posting]]:
    host, _, path = slug.partition("/")
    site = path.split("/")[-1]
    api = f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}"

    async def search(text: str, offset: int, limit: int = 20) -> Any:
        try:
            r = await client.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": limit, "offset": offset,
                                                       "searchText": text})
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    if await search("", 0, 1) is None:
        return None
    seen: Dict[str, Posting] = {}
    for query in config.search()["workday_queries"]:
        for page in range(_WORKDAY_PAGES):
            d = await search(query, page * 20)
            hits = (d or {}).get("jobPostings") or []
            for j in hits:
                ext = j.get("externalPath") or ""
                if not ext or ext in seen:
                    continue
                seen[ext] = Posting(f"workday:{host}:{(j.get('bulletFields') or [ext])[0]}", j.get("title", ""),
                                    j.get("locationsText", ""), f"https://{host}/{site}{ext}", "",
                                    _workday_posted(j.get("postedOn", "")))
            if len(hits) < 20:
                break
    postings = list(seen.values())
    # "3 Locations" hides the cities; look those up only for roles we'd actually keep.
    from . import rank
    for p, ext in zip(postings, seen):
        if re.match(r"\d+ locations", p.location, re.I) and rank.title_family(p.title):
            detail = await _get_json(client, f"{api}{ext}")
            info = (detail or {}).get("jobPostingInfo") or {}
            p.location = "; ".join(l for l in [info.get("location"), *(info.get("additionalLocations") or [])] if l) or p.location
    return postings


# --- full posting text, for listings that don't include it ---------------------------------------------------

async def _description(client: httpx.AsyncClient, source: str, external_id: str, url: str) -> Optional[str]:
    """The posting's text from its job system's detail endpoint, or None if it couldn't be fetched."""
    _, slug, job = external_id.split(":", 2)
    if source == "greenhouse":
        d = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job}")
        return html_text(d.get("content") or "") if d else None
    if source == "workable":
        d = await _get_json(client, f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{job}")
        return html_text("<br>".join(d.get(k) or "" for k in ("description", "requirements", "benefits"))) if d else None
    if source == "smartrecruiters":
        d = await _get_json(client, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job}")
        sections = ((d or {}).get("jobAd") or {}).get("sections") or {}
        return html_text("<br>".join((v or {}).get("text") or "" for v in sections.values())) if d else None
    if source == "workday":  # url is https://<host>/<site>/job/...; the CXS API serves the same path
        u = urlparse(url)
        site, _, path = u.path.lstrip("/").partition("/")
        d = await _get_json(client, f"https://{u.netloc}/wday/cxs/{u.netloc.split('.')[0]}/{site}/{path}")
        return html_text(((d or {}).get("jobPostingInfo") or {}).get("jobDescription") or "") if d else None
    if source == "yc":
        body, _ = await _get_text(client, url)
        m = re.search(r'data-page="([^"]+)"', body)
        if not m:
            return None
        job_ = json.loads(html.unescape(m.group(1))).get("props", {}).get("job") or {}
        experience = f"\nExperience: {job_['minExperience']}" if job_.get("minExperience") else ""
        return html_text(job_.get("description") or "") + experience
    return None


async def fetch_descriptions(conn: sqlite3.Connection, limit: Optional[int] = None) -> Tuple[int, int]:
    """Fill in the text of open roles whose listing didn't include it. Returns (fetched, failed)."""
    from .jobs import reports_to

    rows = conn.execute("""SELECT id, source, external_id, url FROM jobs WHERE is_open = 1 AND family IS NOT NULL
                           AND description IS NULL AND source IN ('greenhouse', 'workable', 'smartrecruiters',
                                                                  'workday', 'yc') ORDER BY id""").fetchall()
    rows = rows[:limit] if limit else rows
    ok = failed = 0
    async with web_client() as client:
        async for row, text in bounded(rows, lambda r: _description(client, r["source"], r["external_id"], r["url"]),
                                       limit=16):
            if not text:
                failed += 1
                continue
            conn.execute("UPDATE jobs SET description = ?, reports_to = COALESCE(reports_to, ?) WHERE id = ?",
                         (text, reports_to(text), row["id"]))
            ok += 1
            if ok % 200 == 0:
                conn.commit()
                print(f"  fetch-descriptions: {ok}/{len(rows)}", flush=True)
    conn.commit()
    return ok, failed


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workable": _workable,
            "smartrecruiters": _smartrecruiters, "recruitee": _recruitee, "workday": _workday}


async def fetch_all(conn: sqlite3.Connection, limit: Optional[int] = None) -> Tuple[int, int, int]:
    """Pull postings for every company on a supported ATS. Returns (companies fetched, failed, relevant open roles)."""
    from .jobs import store_postings  # jobs imports Posting from here

    rows = conn.execute(f"""SELECT id, ats_type, ats_slug FROM companies
                            WHERE ats_type IN ({','.join('?' * len(SUPPORTED))}) ORDER BY id""", SUPPORTED).fetchall()
    rows = rows[:limit] if limit else rows
    ok = failed = relevant = 0
    async with web_client() as client:
        async for row, postings in bounded(rows, lambda r: FETCHERS[r["ats_type"]](client, r["ats_slug"]), limit=16):
            if postings is None:
                failed += 1
                continue
            relevant += store_postings(conn, row["id"], row["ats_type"], postings)
            ok += 1
            if ok % 100 == 0:
                conn.commit()
                print(f"  fetch-jobs: {ok}/{len(rows)}", flush=True)
    conn.commit()
    return ok, failed, relevant
