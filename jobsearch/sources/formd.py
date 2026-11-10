"""SEC Form D: companies in our metros that just raised private money, with the founders/officers named on the filing.

Backfill comes from the SEC's quarterly bulk data sets; recent days come from EDGAR full-text search plus each
filing's primary_doc.xml.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterator, List, Optional, Set, Tuple

import httpx

from .. import config, db
from ..http import Throttle, sec_client
from ..normalize import display_name, location_tag, norm_name

BULK_URLS = ("https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/{q}_d.zip",
             "https://www.sec.gov/files/structureddata/data/form-d-data-sets/{q}_d.zip")
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/primary_doc.xml"

# Funds and SPVs that slip through under an operating industry.
_FUND_NAME = re.compile(r"\b(fund|feeder|spv|co-?invest\w*|a series of|series [a-z0-9-]+ of|topco|holdco|aggregator|"
                        r"acquisition corp\w*|securities|film|productions|pictures)\b|\bl\.?p\.?$", re.I)


def _states() -> Dict[str, Set[str]]:
    """Your locations that list Form D states, each with its states (config/search.yaml)."""
    return {tag: set(loc["states"]) for tag, loc in config.locations().items() if loc.get("states")}


@dataclass
class Person:
    name: str
    relationship: str
    title: Optional[str]


@dataclass
class Filing:
    accession: str
    cik: str
    name: str
    city: str
    state: str
    entity_type: str
    industry: str
    is_amendment: bool
    filed: str
    first_sale: Optional[str]
    offering_amount: Optional[float]
    amount_sold: Optional[float]
    people: List[Person] = field(default_factory=list)


def _amount(s: Optional[str]) -> Optional[float]:
    try:
        return float(s) if s else None
    except ValueError:
        return None  # "Indefinite"


def _person(first: Optional[str], last: Optional[str], rels: List[str], clarification: Optional[str]) -> Optional[Person]:
    first, last = (first or "").strip(), (last or "").strip()
    if not last or first.lower() in ("", "n/a", "na", "-", "none"):
        return None
    return Person(display_name(f"{first} {last}"), ", ".join(r for r in rels if r), (clarification or "").strip() or None)


def _accept(f: Filing) -> Optional[str]:
    """Return the location tag if this filing is a real operating company raise we care about."""
    cfg = config.search()["formd"]
    if f.is_amendment or f.state not in cfg["states"] or f.industry in cfg["exclude_industries"]:
        return None
    if f.entity_type not in cfg["entity_types"]:
        return None
    if _FUND_NAME.search(f.name):
        return None
    if (f.amount_sold or 0) < cfg["min_raise_usd"]:
        return None
    tag = location_tag(f"{f.city}, {f.state}")
    return tag if tag in _states() else None


def _store(conn: sqlite3.Connection, f: Filing, tag: str) -> None:
    row = conn.execute("SELECT id FROM companies WHERE cik = ?", (f.cik,)).fetchone()
    cid = row["id"] if row else db.find_company_by_name(conn, f.name, tag)
    industry_lanes = config.search()["formd"].get("industry_lanes") or {}
    cid = db.upsert_company(conn, key=f"cik:{f.cik}", name=display_name(f.name), source="formd",
                            lanes=industry_lanes.get(f.industry, []), company_id=cid, cik=f.cik,
                            city=display_name(f.city), state=f.state, location_tag=tag, industry=f.industry)
    for p in f.people:
        db.upsert_person(conn, cid, p.name, "formd", title=p.title, relationship=p.relationship)
    conn.execute("""UPDATE companies SET last_raise_usd = ?, last_raise_date = ?, last_raise_accession = ?
                    WHERE id = ? AND (last_raise_date IS NULL OR last_raise_date <= ?)""",
                 (f.amount_sold, f.filed, f.accession, cid, f.filed))


# --- quarterly bulk data sets -------------------------------------------------------------------------------

def _download_bulk(client: httpx.Client, quarter: str) -> zipfile.ZipFile:
    path = config.RAW_DIR / "formd" / f"{quarter}_d.zip"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        for url in BULK_URLS:
            r = client.get(url.format(q=quarter))
            if r.status_code == 200:
                path.write_bytes(r.content)
                break
        else:
            raise RuntimeError(f"No SEC Form D data set found for {quarter}")
    return zipfile.ZipFile(path)


def _bulk_filings(z: zipfile.ZipFile) -> Iterator[Filing]:
    csv.field_size_limit(sys.maxsize)

    def rows(name: str) -> Iterator[Dict[str, str]]:
        member = next(m for m in z.namelist() if m.endswith(name))
        with z.open(member) as fh:
            yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"), delimiter="\t")

    filed = {r["ACCESSIONNUMBER"]: datetime.strptime(r["FILING_DATE"], "%d-%b-%Y").date().isoformat()
             for r in rows("FORMDSUBMISSION.tsv")}
    offerings = {r["ACCESSIONNUMBER"]: r for r in rows("OFFERING.tsv")}
    people: Dict[str, List[Person]] = defaultdict(list)
    for r in rows("RELATEDPERSONS.tsv"):
        p = _person(r["FIRSTNAME"], r["LASTNAME"], [r["RELATIONSHIP_1"], r["RELATIONSHIP_2"], r["RELATIONSHIP_3"]],
                    r["RELATIONSHIPCLARIFICATION"])
        if p:
            people[r["ACCESSIONNUMBER"]].append(p)
    for r in rows("ISSUERS.tsv"):
        if r["IS_PRIMARYISSUER_FLAG"] != "YES":
            continue
        acc = r["ACCESSIONNUMBER"]
        o = offerings.get(acc, {})
        yield Filing(accession=acc, cik=r["CIK"].lstrip("0"), name=r["ENTITYNAME"], city=r["CITY"],
                     state=r["STATEORCOUNTRY"], entity_type=r["ENTITYTYPE"], industry=o.get("INDUSTRYGROUPTYPE", ""),
                     is_amendment=o.get("ISAMENDMENT") == "true", filed=filed.get(acc, ""),
                     first_sale=o.get("SALE_DATE") or None, offering_amount=_amount(o.get("TOTALOFFERINGAMOUNT")),
                     amount_sold=_amount(o.get("TOTALAMOUNTSOLD")), people=people.get(acc, []))


def _has_contact() -> bool:
    # sec.gov rejects downloads unless the User-Agent includes a contact email.
    if "@" in config.search()["http"]["sec_user_agent"]:
        return True
    print("  ! sec.gov needs a contact email: set http.sec_user_agent in config/search.yaml (e.g. 'Your Name you@x.com')")
    return False


def ingest_bulk(conn: sqlite3.Connection, quarters: Optional[List[str]] = None) -> int:
    n = 0
    with sec_client() as client:
        for q in quarters or config.search()["formd"]["quarters"]:
            if db.get_meta(conn, f"formd_bulk:{q}"):
                continue
            if not (config.RAW_DIR / "formd" / f"{q}_d.zip").exists() and not _has_contact():
                continue
            for f in _bulk_filings(_download_bulk(client, q)):
                tag = _accept(f)
                if tag:
                    _store(conn, f, tag)
                    n += 1
            db.set_meta(conn, f"formd_bulk:{q}", db.today())
            conn.commit()
    return n


# --- daily filings via EDGAR full-text search ---------------------------------------------------------------

def _efts_hits(client: httpx.Client, throttle: Throttle, state: str, start: str, end: str) -> Iterator[dict]:
    offset = 0
    while True:
        throttle.wait()
        r = client.get(EFTS_URL, params={"forms": "D", "dateRange": "custom", "startdt": start, "enddt": end,
                                         "locationCodes": state, "from": offset})
        r.raise_for_status()
        hits = r.json()["hits"]
        yield from hits["hits"]
        offset += len(hits["hits"])
        if not hits["hits"] or offset >= hits["total"]["value"]:
            return


def _parse_doc(xml_text: str, accession: str, filed: str) -> Filing:
    root = ET.fromstring(xml_text)
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]

    def g(path: str) -> str:
        return (root.findtext(path) or "").strip()

    people = [_person(p.findtext("relatedPersonName/firstName"), p.findtext("relatedPersonName/lastName"),
                      [x.text or "" for x in p.findall("relatedPersonRelationshipList/relationship")],
                      p.findtext("relationshipClarification"))
              for p in root.findall(".//relatedPersonInfo")]
    sales = "offeringData/offeringSalesAmounts/"
    return Filing(accession=accession, cik=g("primaryIssuer/cik").lstrip("0"), name=g("primaryIssuer/entityName"),
                  city=g("primaryIssuer/issuerAddress/city"), state=g("primaryIssuer/issuerAddress/stateOrCountry"),
                  entity_type=g("primaryIssuer/entityType"),
                  industry=g("offeringData/industryGroup/industryGroupType"),
                  is_amendment=g("offeringData/typeOfFiling/newOrAmendment/isAmendment") == "true", filed=filed,
                  first_sale=g("offeringData/typeOfFiling/dateOfFirstSale/value") or None,
                  offering_amount=_amount(g(sales + "totalOfferingAmount")),
                  amount_sold=_amount(g(sales + "totalAmountSold")), people=[p for p in people if p])


def ingest_daily(conn: sqlite3.Connection, since: Optional[str] = None) -> Tuple[int, int]:
    """Pull new Form D filings since the last run. Returns (filings fetched, companies stored)."""
    cfg = config.search()["formd"]
    last = db.get_meta(conn, "formd_daily_through")
    start = since or ((date.fromisoformat(last) + timedelta(days=1)).isoformat() if last else str(cfg["daily_since"]))
    end = db.today()
    if start > end or not _has_contact():
        return 0, 0
    throttle = Throttle(per_second=8)
    fetched = stored = 0
    with sec_client() as client:
        for state in cfg["states"]:
            for hit in _efts_hits(client, throttle, state, start, end):
                s = hit["_source"]
                name = re.sub(r"\s*\(CIK \d+\)\s*$", "", (s.get("display_names") or [""])[0])
                if s.get("form") != "D" or _FUND_NAME.search(name):
                    continue
                if location_tag((s.get("biz_locations") or [""])[0]) not in _states():
                    continue
                adsh = s["adsh"]
                if conn.execute("SELECT 1 FROM companies WHERE last_raise_accession = ?", (adsh,)).fetchone():
                    continue
                throttle.wait()
                r = client.get(DOC_URL.format(cik=int(s["ciks"][0]), adsh=adsh.replace("-", "")))
                if r.status_code != 200:
                    continue
                fetched += 1
                try:
                    f = _parse_doc(r.text, adsh, s.get("file_date") or end)
                except ET.ParseError:
                    continue
                tag = _accept(f)
                if tag:
                    _store(conn, f, tag)
                    stored += 1
            conn.commit()
    db.set_meta(conn, "formd_daily_through", end)
    conn.commit()
    return fetched, stored


# --- officers for companies found elsewhere (VC portfolios, HN) -------------------------------------------------

def lookup_founders(conn: sqlite3.Connection, limit: Optional[int] = None) -> Dict[str, int]:
    """Find each startup's Form D by company name and take the officers listed on its latest filing.

    Only issuers whose normalized name matches exactly and whose business address is in the company's state count,
    so a common name like "Flex" doesn't pick up an unrelated filer.
    """
    stats = {"checked": 0, "matched": 0, "people": 0}
    if not _has_contact():
        return stats
    states = _states()
    rows = conn.execute(f"""SELECT id, name, location_tag FROM companies c
                           WHERE formd_checked_at IS NULL AND location_tag IN ({','.join('?' * len(states))})
                             AND sources != 'seed' AND NOT EXISTS (SELECT 1 FROM people p WHERE p.company_id = c.id)
                           ORDER BY COALESCE(tier, 9), id""", list(states)).fetchall()
    rows = rows[:limit] if limit else rows
    throttle = Throttle(per_second=8)
    with sec_client() as client:
        for row in rows:
            stats["checked"] += 1
            target = norm_name(row["name"])
            throttle.wait()
            try:
                r = client.get(EFTS_URL, params={"entityName": row["name"], "forms": "D"})
                hits = r.json()["hits"]["hits"] if r.status_code == 200 else []
            except (httpx.HTTPError, ValueError, KeyError):
                hits = []
            matches = [h["_source"] for h in hits
                       if norm_name(re.sub(r"\s*\(CIK \d+\)\s*$", "", (h["_source"].get("display_names") or [""])[0])) == target
                       and set(h["_source"].get("biz_states") or []) & states[row["location_tag"]]]
            if matches:
                latest = max(matches, key=lambda src: src.get("file_date") or "")
                throttle.wait()
                doc = client.get(DOC_URL.format(cik=int(latest["ciks"][0]), adsh=latest["adsh"].replace("-", "")))
                if doc.status_code == 200:
                    try:
                        f = _parse_doc(doc.text, latest["adsh"], latest.get("file_date") or "")
                    except ET.ParseError:
                        f = None
                    if f:
                        for p in f.people:
                            db.upsert_person(conn, row["id"], p.name, "formd", title=p.title, relationship=p.relationship)
                        stats["people"] += len(f.people)
                        conn.execute("""UPDATE companies SET cik = COALESCE(cik, ?), last_raise_usd = COALESCE(last_raise_usd, ?),
                                        last_raise_date = COALESCE(last_raise_date, ?),
                                        last_raise_accession = COALESCE(last_raise_accession, ?) WHERE id = ?""",
                                     (f.cik, f.amount_sold, f.filed, f.accession, row["id"]))
                        stats["matched"] += 1
            conn.execute("UPDATE companies SET formd_checked_at = ? WHERE id = ?", (db.today(), row["id"]))
            if stats["checked"] % 100 == 0:
                conn.commit()
                print(f"  form d founders: {stats['checked']}/{len(rows)}  {stats}", flush=True)
    conn.commit()
    return stats
