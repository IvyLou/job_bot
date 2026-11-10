"""Gmail sync over IMAP (read-only): log the outreach emails you send, the replies you get, and bounces.

Connect once with `python -m jobsearch gmail-connect`. It takes a Gmail app password (myaccount.google.com/apppasswords)
and keeps it in the macOS Keychain. The mailbox is opened read-only and messages are fetched with PEEK, so nothing in
Gmail changes and nothing gets marked as read.
"""
from __future__ import annotations

import email
import imaplib
import re
import sqlite3
import subprocess
import time
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.utils import getaddresses
from typing import Dict, List, Optional, Tuple

from . import crm, db
from .normalize import domain_of

KEYCHAIN_SERVICE = "jobsearch-gmail"
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "me.com", "aol.com",
             "proton.me", "protonmail.com", "live.com", "msn.com", "qq.com", "163.com"}
_AUTOMATED = re.compile(r"^(no-?reply|donotreply|do-not-reply|notifications?|mailer-daemon|postmaster|alerts?|"
                        r"newsletters?|news|updates?|marketing|calendar-notification)@", re.I)
# Customer-service inboxes: talking to them (an order, an account) isn't job outreach.
_SERVICE = re.compile(r"^(support|help|helpdesk|billing|accounts?|orders?|service|customerservice|customer-service|"
                      r"care|feedback|privacy|legal|security|abuse|payments?|returns?)@", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}")


def account() -> str:
    return crm.settings()["me"]["email"]


def _password() -> Optional[str]:
    r = subprocess.run(["security", "find-generic-password", "-a", account(), "-s", KEYCHAIN_SERVICE, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def connected() -> bool:
    return _password() is not None


def connect(password: str) -> str:
    """Check an app password against Gmail, then save it in the Keychain."""
    password = re.sub(r"\s+", "", password)  # Google shows it as four groups of four
    try:
        _Imap(account(), password).close()
    except imaplib.IMAP4.error as e:
        raise SystemExit(f"Gmail refused the app password ({e}). Create a new one and try again.")
    subprocess.run(["security", "add-generic-password", "-U", "-a", account(), "-s", KEYCHAIN_SERVICE,
                    "-l", "Job search: Gmail app password", "-w", password], check=True, capture_output=True)
    return f"connected {account()}"


def disconnect() -> str:
    subprocess.run(["security", "delete-generic-password", "-a", account(), "-s", KEYCHAIN_SERVICE],
                   capture_output=True)
    return "removed the app password from the Keychain (also revoke it at myaccount.google.com/apppasswords)"


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError):
        return value


class _Imap:
    """Gmail's All Mail folder, read-only, searched with Gmail's own search syntax (X-GM-RAW)."""

    def __init__(self, user: str, password: str):
        self.imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        self.imap.login(user, password)
        self.imap.select(f'"{self._all_mail()}"', readonly=True)

    def _all_mail(self) -> str:
        for line in self.imap.list()[1]:
            text = line.decode(errors="replace")
            if "\\All" in text:
                return text.rsplit(' "/" ', 1)[-1].strip().strip('"')
        return "[Gmail]/All Mail"

    def search(self, query: str) -> List[bytes]:
        typ, data = self.imap.uid("SEARCH", "X-GM-RAW", '"' + query.replace('"', "") + '"')
        return data[0].split() if typ == "OK" and data and data[0] else []

    def message(self, uid: bytes) -> Tuple[Dict[str, str], str, str, str]:
        """(headers, day as YYYY-MM-DD, start of the body text, Gmail message id)"""
        typ, data = self.imap.uid("FETCH", uid, "(X-GM-MSGID INTERNALDATE BODY.PEEK[HEADER.FIELDS "
                                                 "(FROM TO CC SUBJECT X-FAILED-RECIPIENTS)] BODY.PEEK[TEXT]<0.3000>)")
        meta = b" ".join(part[0] if isinstance(part, tuple) else part for part in data if part)
        # Gmail doesn't return the parts in the order asked for, so match each literal by its label.
        header_block = next((part[1] for part in data if isinstance(part, tuple) and b"HEADER.FIELDS" in part[0]), b"")
        body_block = next((part[1] for part in data if isinstance(part, tuple) and b"BODY[TEXT]" in part[0]), b"")
        headers = {k: _decode(v) for k, v in email.message_from_bytes(header_block).items()}
        when = imaplib.Internaldate2tuple(meta)
        day = time.strftime("%Y-%m-%d", when) if when else db.today()
        msgid = (re.search(rb"X-GM-MSGID (\d+)", meta) or [b"", uid])[1].decode()
        text = body_block.decode(errors="replace")
        return headers, day, text, msgid

    def close(self) -> None:
        try:
            self.imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def _person(conn: sqlite3.Connection, company_id: int, display: str, addr: str) -> int:
    """The person behind an address at a known company, adding them if they're new."""
    name = display.strip().strip('"') or " ".join(p.capitalize() for p in re.split(r"[._-]", addr.split("@")[0]) if p)
    row = conn.execute("SELECT id FROM people WHERE company_id = ? AND full_name = ?", (company_id, name)).fetchone()
    if row:  # Gmail shows the address that actually works, so it beats any guess
        conn.execute("UPDATE people SET email = ?, email_confidence = 'given' WHERE id = ?", (addr, row["id"]))
        return row["id"]
    db.upsert_person(conn, company_id, name, "gmail", email=addr, email_confidence="given", priority=2,
                     role_kind="contact")
    return conn.execute("SELECT id FROM people WHERE company_id = ? AND full_name = ?", (company_id, name)).fetchone()["id"]


def _log_outbound(conn: sqlite3.Connection, company_id: int, person_id: int, day: str, subject: str, ref: str) -> bool:
    # If you already logged this email by hand, attach the Gmail message to that entry instead of counting it twice.
    manual = conn.execute("""SELECT id FROM activities WHERE person_id = ? AND happened_on = ? AND channel = 'email'
                             AND kind IN ('sent', 'followup') AND gmail_ref IS NULL""", (person_id, day)).fetchone()
    if manual:
        conn.execute("UPDATE activities SET gmail_ref = ? WHERE id = ?", (ref, manual["id"]))
        conn.commit()
        return False
    return crm.log(conn, company_id, crm.next_outbound_kind(conn, person_id), person_id=person_id, channel="email",
                   happened_on=day, note=subject, source="gmail", gmail_ref=ref)


def sync(conn: sqlite3.Connection, days_back: int = 30) -> Dict[str, int]:
    password = _password()
    if not password:
        return {"connected": 0}
    gmail = _Imap(account(), password)
    since = db.get_meta(conn, "gmail_synced_through") or (date.today() - timedelta(days=days_back)).isoformat()
    after = (date.fromisoformat(since) - timedelta(days=1)).strftime("%Y/%m/%d")  # overlap a day; refs dedupe
    companies = {r["domain"]: r["id"] for r in conn.execute("SELECT id, domain FROM companies WHERE domain IS NOT NULL")}
    people = {r["email"].lower(): (r["id"], r["company_id"])
              for r in conn.execute("SELECT id, company_id, email FROM people WHERE email IS NOT NULL")}
    stats = {"connected": 1, "sent": 0, "replies": 0, "bounces": 0}

    def company_for(addr: str) -> Optional[int]:
        domain = addr.split("@")[-1]
        return None if domain in FREE_MAIL else companies.get(domain) or companies.get(domain_of(domain) or "")

    # 1. Outreach you sent to anyone at a company in the universe.
    for uid in gmail.search(f"in:sent after:{after}"):
        headers, day, _, mid = gmail.message(uid)
        for display, addr in getaddresses([headers.get("To", ""), headers.get("Cc", "")]):
            addr = addr.lower()
            known = people.get(addr)
            cid = known[1] if known else company_for(addr)
            if not cid or _SERVICE.match(addr):
                continue
            pid = known[0] if known else _person(conn, cid, display, addr)
            people[addr] = (pid, cid)
            stats["sent"] += _log_outbound(conn, cid, pid, day, headers.get("Subject", ""), f"{mid}:{addr}")

    # 2. Replies from companies you've contacted.
    contacted: List[str] = [r["domain"] for r in conn.execute(
        """SELECT DISTINCT c.domain FROM activities a JOIN companies c ON c.id = a.company_id
           WHERE a.kind IN ('sent', 'followup') AND c.domain IS NOT NULL""")]
    for i in range(0, len(contacted), 25):
        senders = " ".join(f"from:{d}" for d in contacted[i:i + 25])
        for uid in gmail.search(f"-in:sent -in:drafts after:{after} {{{senders}}}"):
            headers, day, _, mid = gmail.message(uid)
            for display, addr in getaddresses([headers.get("From", "")]):
                addr = addr.lower()
                cid = (people.get(addr) or (None, company_for(addr)))[1]
                if not cid or _AUTOMATED.match(addr) or _SERVICE.match(addr):
                    continue
                pid = people[addr][0] if addr in people else _person(conn, cid, display, addr)
                # They wrote from this address, so it works: upgrade a guessed address to confirmed.
                conn.execute("""UPDATE people SET email = ?, email_confidence = 'given'
                                WHERE id = ? AND COALESCE(email_confidence, '') IN ('', 'guess', 'pattern', 'published')""",
                             (addr, pid))
                stats["replies"] += crm.log(conn, cid, "reply", person_id=pid, channel="email", happened_on=day,
                                            note=headers.get("Subject", ""), source="gmail", gmail_ref=mid)

    # 3. Bounces: a guessed address that doesn't exist.
    for uid in gmail.search(f"from:mailer-daemon after:{after}"):
        headers, day, text, mid = gmail.message(uid)
        failed = headers.get("X-Failed-Recipients") or text  # Gmail names the bad address in a header
        for addr in {a.lower() for a in _EMAIL.findall(failed)}:
            if addr in people:
                pid, cid = people[addr]
                conn.execute("UPDATE people SET email_confidence = 'bounced' WHERE id = ?", (pid,))
                stats["bounces"] += crm.log(conn, cid, "bounce", person_id=pid, channel="email", happened_on=day,
                                            note=f"bounced: {addr}", source="gmail", gmail_ref=f"{mid}:bounce:{addr}")
    gmail.close()
    db.set_meta(conn, "gmail_synced_through", db.today())
    conn.commit()
    return stats
