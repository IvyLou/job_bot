"""Company-name, domain and location normalization shared by every source."""
from __future__ import annotations

import html
import re
from functools import lru_cache
from typing import Iterable, List, Optional, Pattern, Tuple
from urllib.parse import urlsplit

from . import config

_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|co|company|ltd|limited|lp|l\.p|pbc|plc|gmbh|technologies|"
    r"technology|labs|holdings?|group)\b\.?", re.I)


def norm_name(name: str) -> str:
    s = html.unescape(name or "").lower().replace("&", " and ")
    s = _SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def compact(name: str) -> str:
    return norm_name(name).replace(" ", "")


def names_match(a: str, b: str) -> bool:
    x, y = compact(a), compact(b)
    return min(len(x), len(y)) >= 3 and (x == y or x in y or y in x)


def display_name(name: str) -> str:
    name = html.unescape(name or "").strip()
    return name.title() if name.isupper() else name


_TWO_LEVEL_TLDS = {"co.uk", "com.au", "co.jp", "com.br", "co.in", "com.cn", "com.sg", "co.il", "com.hk", "co.nz"}
_HOSTED = ("vercel.app", "webflow.io", "github.io", "notion.site", "netlify.app", "framer.website", "framer.ai",
           "herokuapp.com", "wixsite.com", "carrd.co", "substack.com")


def domain_of(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    u = url.strip().lower()
    if "://" not in u:
        u = "http://" + u
    try:
        host = urlsplit(u).hostname or ""
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    if host.endswith(_HOSTED):
        return host
    parts = host.split(".")
    keep = 3 if ".".join(parts[-2:]) in _TWO_LEVEL_TLDS else 2
    return ".".join(parts[-keep:])


def domain_root(domain: str) -> str:
    return domain.split(".")[0] if domain else ""


_REMOTE = re.compile(r"\bremote\b|work from home|\bwfh\b|anywhere|distributed", re.I)
_NON_US = re.compile(
    r"canada|united kingdom|\buk\b|europe|\bemea\b|\bapac\b|asia|india|latam|latin america|mexico|brazil|germany|"
    r"france|spain|poland|portugal|netherlands|ireland|israel|australia|singapore|philippines|argentina|colombia|"
    r"nigeria|kenya|japan|china|korea|london|berlin|toronto|vancouver|bangalore|bengaluru|tel aviv|dublin|"
    r"amsterdam|paris|sydney|taipei|manila", re.I)
_US = re.compile(r"\bus\b|\bu\.s\.|\busa\b|united states|north america|americas|\bus-", re.I)


@lru_cache(maxsize=None)
def _place_patterns() -> List[Tuple[str, Pattern[str]]]:
    return [(tag, re.compile(loc["match"], re.I)) for tag, loc in config.locations().items() if loc.get("match")]


def location_tag(text: Optional[str]) -> Optional[str]:
    """Classify free-text location: the first of your locations (config/search.yaml) whose pattern matches, else
    remote (in the US), else us (a bare "United States"), else None."""
    if not text:
        return None
    for tag, rx in _place_patterns():
        if rx.search(text):
            return tag
    if _REMOTE.search(text) and (not _NON_US.search(text) or _US.search(text)):
        return "remote"
    if re.fullmatch(r"\s*(united states( of america)?|usa|us|u\.s\.)\s*", text, re.I):
        return "us"
    return None


def lanes_for(text: str, lane_keywords: dict) -> List[str]:
    text = text.lower()
    return [lane for lane, words in lane_keywords.items()
            if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)]


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


_BLOCK_TAG = re.compile(r"<\s*(?:br|/p|/li|li|/div|/h\d|/tr|/ul|/ol)\b[^>]*>", re.I)


def html_text(s: str) -> str:
    """Readable text from a posting's HTML, one paragraph or bullet per line."""
    s = s or ""
    if "&lt;" in s and "<" not in s:  # Greenhouse sends its HTML entity-escaped
        s = html.unescape(s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", _BLOCK_TAG.sub("\n", s)))
    return "\n".join(" ".join(line.split()) for line in s.splitlines() if line.strip())


def first(items: Iterable[Optional[str]]) -> Optional[str]:
    return next((x for x in items if x), None)
