from __future__ import annotations

import argparse
import asyncio
import socket
import sqlite3
import time
import traceback
from datetime import date, timedelta
from typing import List, Optional

from . import ats, config, contacts, db, gmail, rank, report, schedule
from .sources import formd, hn, seeds, vc, yc


_failed: Optional[List[str]] = None  # set by daily(): a failing step is logged and skipped instead of ending the run


def _step(label: str, fn, *args):
    start = time.monotonic()
    print(f"→ {label} ...", flush=True)
    try:
        result = fn(*args)
    except Exception:
        if _failed is None:
            raise
        traceback.print_exc()
        print(f"  {label}: FAILED, skipped  ({time.monotonic() - start:.0f}s)", flush=True)
        _failed.append(label)
        return None
    print(f"  {label}: {result}  ({time.monotonic() - start:.0f}s)", flush=True)
    return result


def _wait_for_network(host: str = "imap.gmail.com", tries: int = 18, pause: int = 10) -> None:
    """The 7:00 run often starts as the Mac wakes, before the network is back (up to 3 minutes)."""
    for _ in range(tries):
        try:
            socket.getaddrinfo(host, 443)
            return
        except OSError:
            time.sleep(pause)


def ingest(conn: sqlite3.Connection, which: str) -> None:
    if which in ("seeds", "all"):
        _step("seeds", seeds.ingest, conn)
    if which in ("yc", "all"):
        _step("yc directory", yc.ingest, conn)
        db.set_meta(conn, "yc_directory", db.today())
    if which in ("vc", "all"):
        _step("vc portfolios", lambda: asyncio.run(vc.ingest(conn)))
    if which in ("formd", "all"):
        _step("form d bulk", formd.ingest_bulk, conn)
        _step("form d daily", formd.ingest_daily, conn)
    if which in ("hn", "all"):
        _step("hn who's hiring", hn.ingest, conn)
    conn.commit()


def backup(conn: sqlite3.Connection, keep: int = 14) -> str:
    """Copy the database to data/backups/ (your stages, notes and activity log live there too)."""
    folder = config.DATA_DIR / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"jobsearch_{db.today()}.db"
    with sqlite3.connect(target) as dest:
        conn.backup(dest)
    for old in sorted(folder.glob("jobsearch_*.db"))[:-keep]:
        old.unlink()
    return str(target)


def daily(conn: sqlite3.Connection) -> None:
    """Everything a morning run needs: new sources, new companies' job systems, fresh postings, ranks, export."""
    print(f"=== daily run {time.strftime('%Y-%m-%d %H:%M')} ===", flush=True)
    global _failed
    _failed = []
    _wait_for_network()
    _step("backup", backup, conn)
    if gmail.connected():
        _step("gmail sync", gmail.sync, conn)
    ingest(conn, "seeds")
    last_yc = db.get_meta(conn, "yc_directory")
    if not last_yc or last_yc <= (date.today() - timedelta(days=7)).isoformat():
        ingest(conn, "yc")
        ingest(conn, "vc")
        db.set_meta(conn, "yc_directory", db.today())
    _step("form d daily", formd.ingest_daily, conn)
    _step("hn who's hiring", hn.ingest, conn)
    _step("form d founders (new companies)", formd.lookup_founders, conn)
    db.set_meta(conn, "last_daily_run", time.strftime("%Y-%m-%d %H:%M"))
    _step("find websites (new companies)", lambda: asyncio.run(contacts.resolve_domains(conn)))
    _step("detect job systems (new companies)", lambda: asyncio.run(ats.detect_all(conn)))
    _step("yc pages", lambda: asyncio.run(yc.crawl_pages(conn)))
    _step("fetch jobs", lambda: asyncio.run(ats.fetch_all(conn)))
    _step("fetch descriptions", lambda: asyncio.run(ats.fetch_descriptions(conn)))
    _step("published emails (new companies)", lambda: asyncio.run(contacts.harvest_all(conn)))
    _step("rank contacts", contacts.assign, conn)
    _step("rank", rank.rank_all, conn)
    print(f"  exported {report.export(conn)}")
    print(report.summary(conn))
    if _failed:
        print(f"Skipped after errors: {', '.join(_failed)}. They run again tomorrow.", flush=True)
    _failed = None


def main() -> None:
    p = argparse.ArgumentParser(prog="jobsearch", description="Job-search sourcing engine and CRM")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest", help="pull companies from a source")
    s.add_argument("source", choices=["seeds", "yc", "vc", "formd", "hn", "all"])
    s = sub.add_parser("detect-ats", help="find each company's job system")
    s.add_argument("--recheck", action="store_true")
    s.add_argument("--limit", type=int)
    s = sub.add_parser("crawl-yc", help="founders and YC-hosted jobs from YC company pages")
    s.add_argument("--limit", type=int)
    s = sub.add_parser("fetch-jobs", help="pull open roles from every supported job system")
    s.add_argument("--limit", type=int)
    s = sub.add_parser("fetch-descriptions", help="pull the full text of open roles whose listing doesn't include it")
    s.add_argument("--limit", type=int)
    s = sub.add_parser("formd-founders", help="look up officers on EDGAR for startups with no named people")
    s.add_argument("--limit", type=int)
    sub.add_parser("find-domains", help="find websites for companies known only by name (Form D)")
    s = sub.add_parser("harvest-emails", help="collect emails companies publish on their own sites")
    s.add_argument("--recheck", action="store_true")
    s.add_argument("--limit", type=int)
    sub.add_parser("contacts", help="rank who to contact at each company and fill in best-guess emails")
    s = sub.add_parser("import-linkedin", help="mark warm paths from LinkedIn's Connections.csv export")
    s.add_argument("path")
    sub.add_parser("rank", help="score roles and assign company tiers")
    sub.add_parser("report", help="print a summary")
    sub.add_parser("export", help="write data/exports/pipeline_<date>.xlsx")
    sub.add_parser("daily", help="run the whole pipeline")
    sub.add_parser("backup", help="copy the database to data/backups/")
    s = sub.add_parser("gmail-connect", help="save a Gmail app password in the Keychain and test it")
    s.add_argument("--password-file", help="read the app password from this file instead of prompting")
    sub.add_parser("gmail-disconnect", help="remove the Gmail app password from the Keychain")
    sub.add_parser("gmail-sync", help="log sent emails, replies and bounces from Gmail")
    sub.add_parser("install-schedule", help="run `daily` every morning and keep the dashboard running (launchd)")
    sub.add_parser("uninstall-schedule", help="remove the launchd agents")
    sub.add_parser("dashboard", help="open the dashboard in this terminal session")
    args = p.parse_args()

    conn = db.connect()
    if args.cmd == "ingest":
        ingest(conn, args.source)
    elif args.cmd == "detect-ats":
        _step("detect job systems", lambda: asyncio.run(ats.detect_all(conn, args.recheck, args.limit)))
    elif args.cmd == "crawl-yc":
        _step("yc pages", lambda: asyncio.run(yc.crawl_pages(conn, args.limit)))
    elif args.cmd == "fetch-jobs":
        _step("fetch jobs", lambda: asyncio.run(ats.fetch_all(conn, args.limit)))
    elif args.cmd == "fetch-descriptions":
        _step("fetch descriptions", lambda: asyncio.run(ats.fetch_descriptions(conn, args.limit)))
    elif args.cmd == "formd-founders":
        _step("form d founders", formd.lookup_founders, conn, args.limit)
        _step("rank contacts", contacts.assign, conn)
    elif args.cmd == "find-domains":
        _step("find websites", lambda: asyncio.run(contacts.resolve_domains(conn)))
    elif args.cmd == "harvest-emails":
        _step("published emails", lambda: asyncio.run(contacts.harvest_all(conn, args.recheck, args.limit)))
    elif args.cmd == "contacts":
        _step("rank contacts", contacts.assign, conn)
    elif args.cmd == "import-linkedin":
        _step("linkedin connections", contacts.import_linkedin, conn, args.path)
        _step("rank contacts", contacts.assign, conn)
    elif args.cmd == "rank":
        _step("rank", rank.rank_all, conn)
    elif args.cmd == "report":
        print(report.summary(conn))
    elif args.cmd == "export":
        print(report.export(conn))
    elif args.cmd == "daily":
        daily(conn)
    elif args.cmd == "backup":
        print(backup(conn))
    elif args.cmd == "gmail-connect":
        if args.password_file:
            with open(args.password_file) as f:
                password = f.read()
        else:
            import getpass
            password = getpass.getpass("Gmail app password: ")
        print(gmail.connect(password))
        _step("gmail sync", gmail.sync, conn)
    elif args.cmd == "gmail-disconnect":
        print(gmail.disconnect())
    elif args.cmd == "gmail-sync":
        _step("gmail sync", gmail.sync, conn)
    elif args.cmd == "install-schedule":
        for path in schedule.install():
            print(f"installed {path}")
        print(f"dashboard: http://localhost:{schedule.PORT}")
    elif args.cmd == "uninstall-schedule":
        for path in schedule.uninstall():
            print(f"removed {path}")
    elif args.cmd == "dashboard":
        import subprocess
        import sys
        subprocess.run([sys.executable, "-m", "streamlit", "run", str(config.ROOT / "dashboard.py")])
