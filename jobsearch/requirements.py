"""Read a posting's required experience and whether it names a background you have as one that fits.

Rule-based (no AI). Each finding keeps the line it came from, so the dashboard can quote it and a wrong read is easy
to spot. Rules live under `priority_match` in config/search.yaml.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Pattern, Tuple

from . import config

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_N = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
# "2+ years", "1-3 years", "two (2) years", "3 to 5 yrs"; not "4-year degree" or "5 years ago".
_YEARS = re.compile(rf"\b{_N}\s*(?:\(\d{{1,2}}\)\s*)?(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*{_N}\s*(?:\(\d{{1,2}}\)\s*)?)?"
                    r"\+?\s*(?:years?|yrs?)\b(?!\s+(?:ago|old|of age))", re.I)
_EXPERIENCE = re.compile(r"experience|\bexp\b|background|\bwork", re.I)
_YEARS_IN = re.compile(r"\s+(?:in|at|doing|working|post)\b", re.I)  # "2+ years in analytics"
_NOT_TENURE = re.compile(r"\b(?:within|next|past|last|first|every|after|over the)\s+$", re.I)  # "within 2 years"
_PREFERRED = re.compile(r"prefer|ideal(ly)?\b|nice[- ]to[- ]have|bonus|\ba plus\b|\badvantage", re.I)
_CUE = re.compile(r"experience|background|\bformer|\bex-|\bprior\b|previous|careers? (?:in|at)|exposure|come from|"
                  r"coming from|worked (?:in|at)", re.I)
_BENEFITS = re.compile(r"401\(?k|\bpto\b|paid time off|parental leave|medical|dental|vision|benefits", re.I)
_NEAR = 120  # characters between a background term and its cue


def sentences(text: str) -> List[str]:
    parts = re.split(r"\n+|(?<=[.!?;])\s+(?=[A-Z0-9•\-–])|\s[•·▪]\s", text or "")
    return [s for s in (" ".join(p.split()).strip(" •·▪-–") for p in parts) if s]


def _num(s: str) -> int:
    return int(s) if s.isdigit() else _WORDS[s.lower()]


def years_required(text: str) -> Optional[Tuple[int, Optional[int], str]]:
    """(minimum, maximum or None, line) of required experience. With several required lines, the highest minimum
    wins: "5+ years in finance, 2+ managing people" needs 5. Lines marked preferred / a plus don't count."""
    best: Optional[Tuple[int, Optional[int], str]] = None
    for s in sentences(text):
        if _PREFERRED.search(s):
            continue
        for m in _YEARS.finditer(s):
            if _NOT_TENURE.search(s[max(0, m.start() - 20):m.start()]):
                continue
            if not (_EXPERIENCE.search(s) or _YEARS_IN.match(s, m.end())):
                continue
            lo, hi = _num(m.group(1)), _num(m.group(2)) if m.group(2) else None
            if lo > 20 or (hi is not None and hi < lo):  # "50+ years of combined experience", typos
                continue
            if best is None or lo > best[0]:
                best = (lo, hi, s)
    return best


@lru_cache(maxsize=None)
def _rules() -> Tuple[set, Pattern[str], Tuple[int, int], set, Pattern[str], Pattern[str]]:
    r = config.search()["priority_match"]
    rx = lambda items: re.compile("|".join(f"(?:{p})" for p in items), re.I)  # noqa: E731
    return (set(r["families"]), rx(r["titles"]), tuple(r["years"]), set(r["locations"]), rx(r["backgrounds"]),
            rx(r["exclude_titles"]))


def background_quote(text: str, company: str = "") -> Optional[str]:
    """The first line naming one of your backgrounds (`priority_match.backgrounds`) near "experience", "background",
    "former" and the like. The company's own name doesn't count ("Bank of America is committed to career
    development")."""
    terms = _rules()[4]
    own_name = re.compile(re.escape(company), re.I) if company else None
    for line in sentences(text):
        s = own_name.sub(lambda m: "_" * len(m.group(0)), line) if own_name else line
        if _BENEFITS.search(s):
            continue
        cues = [m.start() for m in _CUE.finditer(s)]
        if any(abs(m.start() - c) <= _NEAR for m in terms.finditer(s) for c in cues):
            return line[:400]
    return None


@dataclass
class Assessment:
    years_min: Optional[int] = None
    years_label: Optional[str] = None
    years_quote: Optional[str] = None
    background_quote: Optional[str] = None
    priority_match: int = 0


def assess(title: str, family: Optional[str], location_tag: Optional[str], description: Optional[str],
           company: str = "") -> Assessment:
    if not description or not family:
        return Assessment()
    families, titles, (lo, hi), locations, _, not_titles = _rules()
    years = years_required(description)
    a = Assessment(background_quote=background_quote(description, company))
    if years:
        a.years_min, a.years_quote = years[0], years[2][:400]
        a.years_label = f"{years[0]}–{years[1]} yrs" if years[1] else f"{years[0]}+ yrs"
    right_role = (family in families or bool(titles.search(title))) and not not_titles.search(title)
    # The posting's range must overlap the target band: "0–3" and "3–5" overlap 1–3, "4–6" and "5+" don't.
    in_band = years is not None and years[0] <= hi and (years[1] is None or years[1] >= lo)
    a.priority_match = int(right_role and location_tag in locations and in_band and a.background_quote is not None)
    return a
