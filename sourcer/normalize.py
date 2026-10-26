"""Normalisation: skills, titles, seniority, dates, locations.

Everything downstream compares canonical forms produced here, so this is the
one place to teach the bot new vocabulary (edit sourcer/data/*.yaml).
"""
from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

import yaml

from .config import DATA_DIR

PUNCT = re.compile(r"[^a-z0-9+#/&.\- ]+")
WS = re.compile(r"\s+")


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("’", "'").replace("–", "-").replace("—", "-")
    s = PUNCT.sub(" ", s)
    return WS.sub(" ", s).strip()


def tokens(s: str | None) -> list[str]:
    return [t for t in norm_text(s).split() if len(t) > 1 or t.isdigit()]


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #
@dataclass
class SkillIndex:
    canonical: dict[str, dict] = field(default_factory=dict)
    _phrases: list[tuple[str, str, re.Pattern]] = field(default_factory=list)
    _alias_exact: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path=None) -> "SkillIndex":
        raw = yaml.safe_load((path or DATA_DIR / "skills.yaml").read_text()) or {}
        idx = cls(canonical=raw)
        phrases: list[tuple[str, str]] = []
        for canon, meta in raw.items():
            meta = meta or {}
            names = [canon.replace("_", " ")] + list(meta.get("aliases") or [])
            for n in names:
                n = norm_text(n)
                if not n:
                    continue
                phrases.append((n, canon))
                idx._alias_exact[n] = canon
                idx._alias_exact[n.replace(" ", "")] = canon
        phrases.sort(key=lambda x: -len(x[0]))
        idx._phrases = [
            (p, c, re.compile(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])"))
            for p, c in phrases
        ]
        return idx

    def canonicalize(self, raw: str) -> str | None:
        """Map one free-text skill label to a canonical skill."""
        n = norm_text(raw)
        if not n:
            return None
        if n in self._alias_exact:
            return self._alias_exact[n]
        if n.replace(" ", "") in self._alias_exact:
            return self._alias_exact[n.replace(" ", "")]
        for phrase, canon, rx in self._phrases:
            if len(phrase) >= 3 and rx.search(n):
                return canon
        return None

    def extract(self, text: str, context: str = "prose") -> set[str]:
        """Find canonical skills in a blob of text.

        context='skills' allows very short aliases (r, js, ml) because the text
        is a curated skills list; context='prose' skips them to avoid matching
        stray words inside sentences.
        """
        n = norm_text(text)
        if not n:
            return set()
        found: set[str] = set()
        min_len = 1 if context == "skills" else 3
        for phrase, canon, rx in self._phrases:
            if len(phrase) < min_len or canon in found:
                continue
            if rx.search(n):
                found.add(canon)
        return found

    def expand(self, skills) -> set[str]:
        """Add skills implied by the ones present (pytorch -> deep_learning -> ...)."""
        out = set(skills)
        changed = True
        while changed:
            changed = False
            for s in list(out):
                for imp in (self.canonical.get(s) or {}).get("implies") or []:
                    if imp not in out:
                        out.add(imp)
                        changed = True
        return out

    def group_of(self, skill: str) -> str:
        return (self.canonical.get(skill) or {}).get("group") or "other"

    def unknown_label(self, raw: str) -> str:
        """Stable pseudo-canonical name for a skill not in the taxonomy."""
        return "x:" + norm_text(raw).replace(" ", "_")[:40]


# --------------------------------------------------------------------------- #
# Titles & seniority
# --------------------------------------------------------------------------- #
SENIORITY_ORDER = ["intern", "junior", "mid", "senior", "lead", "manager", "director", "executive"]
SENIORITY_PATTERNS = [
    ("intern", r"\b(intern|internship|trainee|apprentice|fresh graduate)\b"),
    ("executive", r"\b(chief|cto|ceo|cfo|coo|vp|vice president|head of|president)\b"),
    ("director", r"\b(director|general manager|gm)\b"),
    ("manager", r"\b(manager|mgr)\b"),
    ("lead", r"\b(lead|principal|staff|architect|team lead|tl)\b"),
    ("senior", r"\b(senior|snr|sr|specialist iii|iii|iv)\b"),
    ("junior", r"\b(junior|jr|associate|assistant|entry level|graduate|officer i|i)\b"),
]


def seniority_of(title: str | None) -> str:
    n = norm_text(title)
    if not n:
        return "mid"
    for level, pat in SENIORITY_PATTERNS:
        if re.search(pat, n):
            return level
    return "mid"


def seniority_distance(a: str, b: str) -> int:
    try:
        return abs(SENIORITY_ORDER.index(a) - SENIORITY_ORDER.index(b))
    except ValueError:
        return 2


@dataclass
class TitleIndex:
    families: dict[str, dict] = field(default_factory=dict)
    _alias: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path=None) -> "TitleIndex":
        raw = yaml.safe_load((path or DATA_DIR / "titles.yaml").read_text()) or {}
        idx = cls(families=raw)
        for fam, meta in raw.items():
            meta = meta or {}
            for a in [fam.replace("_", " ")] + list(meta.get("aliases") or []):
                idx._alias[norm_text(a)] = fam
        return idx

    def family_of(self, title: str | None) -> str | None:
        n = norm_text(title)
        if not n:
            return None
        if n in self._alias:
            return self._alias[n]
        # longest alias contained in the title wins
        best, best_len = None, 0
        for alias, fam in self._alias.items():
            if len(alias) > best_len and re.search(
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", n
            ):
                best, best_len = fam, len(alias)
        return best

    def core_skills(self, family: str | None) -> list[str]:
        return list((self.families.get(family or "") or {}).get("core_skills") or [])

    def relatedness(self, jd_family: str | None, cand_title: str | None) -> float:
        """0..1 how close a candidate's title is to the JD's title family."""
        if not jd_family:
            return jaccard(tokens(cand_title), [])
        cand_family = self.family_of(cand_title)
        if cand_family == jd_family:
            return 1.0
        adjacent = (self.families.get(jd_family) or {}).get("adjacent") or []
        if cand_family and cand_family in adjacent:
            return 0.65
        jd_tokens = tokens(jd_family.replace("_", " ")) + [
            t for a in (self.families.get(jd_family) or {}).get("aliases", [])[:3] for t in tokens(a)
        ]
        sim = jaccard(set(jd_tokens), set(tokens(cand_title)))
        return min(0.55, sim)


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --------------------------------------------------------------------------- #
# Dates & tenure
# --------------------------------------------------------------------------- #
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
PRESENT = {"present", "current", "now", "to date", "till date", "ongoing", ""}


def parse_date(s: str | None, default_to_today: bool = False) -> date | None:
    """Parse the date shapes LinkedIn/CV exports actually use."""
    if s is None:
        return date.today() if default_to_today else None
    raw = norm_text(s)
    if raw in PRESENT:
        return date.today() if default_to_today else None
    m = re.search(r"\b(19|20)\d{2}\b", raw)
    year = int(m.group(0)) if m else None
    if year is None:
        return date.today() if default_to_today else None
    month = 1
    for name, num in MONTHS.items():
        if re.search(rf"\b{name}", raw):
            month = num
            break
    else:
        m2 = re.match(r"^(\d{1,2})[/\-](19|20)\d{2}$", raw) or re.match(
            r"^(19|20)\d{2}[/\-](\d{1,2})$", raw)
        if m2:
            cand = [int(g) for g in m2.groups() if g and len(g) <= 2]
            if cand and 1 <= cand[0] <= 12:
                month = cand[0]
    try:
        return date(year, month, 1)
    except ValueError:
        return date(year, 1, 1)


def months_between(a: date, b: date) -> int:
    return max(0, (b.year - a.year) * 12 + (b.month - a.month))


def merge_months(ranges: list[tuple[date, date]]) -> float:
    """Total months covered by a set of (start, end) ranges, overlaps counted once.

    Without this, two concurrent jobs would inflate a candidate's experience.
    """
    spans = sorted([(s, e) for s, e in ranges if s and e and e >= s])
    if not spans:
        return 0.0
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return float(sum(months_between(s, e) for s, e in merged))


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #
@dataclass
class GeoIndex:
    target: str = "Karachi, Pakistan"
    metro: set[str] = field(default_factory=set)
    country: str = "pakistan"
    country_cities: set[str] = field(default_factory=set)
    remote_tokens: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path=None) -> "GeoIndex":
        raw = yaml.safe_load((path or DATA_DIR / "locations.yaml").read_text()) or {}
        return cls(
            target=raw.get("target", "Karachi, Pakistan"),
            metro={norm_text(x) for x in raw.get("metro", [])},
            country=norm_text(raw.get("country", "pakistan")),
            country_cities={norm_text(x) for x in raw.get("country_cities", [])},
            remote_tokens={norm_text(x) for x in raw.get("remote_tokens", [])},
        )

    def classify(self, raw_location: str | None) -> tuple[str, str, str]:
        """-> (city, country, tier) where tier is metro|country|remote|other|unknown."""
        n = norm_text(raw_location)
        if not n:
            return "", "", "unknown"
        # split before normalising - norm_text strips the separators
        parts = [norm_text(p) for p in re.split(r"[,/|]", str(raw_location)) if norm_text(p)]
        city = parts[0] if parts else ""
        country = parts[-1] if len(parts) > 1 else ""
        if any(t and t in n for t in self.remote_tokens):
            tier = "remote"
        elif any(m in n for m in self.metro):
            tier = "metro"
        elif self.country in n or any(c in n for c in self.country_cities):
            tier = "country"
        else:
            tier = "other"
        return city.title(), (country or "").title(), tier


@functools.lru_cache(maxsize=1)
def skills_index() -> SkillIndex:
    return SkillIndex.load()


@functools.lru_cache(maxsize=1)
def titles_index() -> TitleIndex:
    return TitleIndex.load()


@functools.lru_cache(maxsize=1)
def geo_index() -> GeoIndex:
    return GeoIndex.load()
