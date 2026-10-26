"""Plain dataclasses passed between sources, store and scorer."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any

from .normalize import (geo_index, merge_months, norm_text, parse_date,
                        seniority_of, skills_index, titles_index)

EDU_LADDER = ["none", "matric", "intermediate", "diploma", "bachelors", "masters", "mphil", "phd"]
EDU_ALIASES = {
    "matric": "matric", "o level": "matric", "ssc": "matric",
    "intermediate": "intermediate", "a level": "intermediate", "hssc": "intermediate", "fsc": "intermediate",
    "diploma": "diploma", "associate": "diploma",
    "bachelor": "bachelors", "bachelors": "bachelors", "bs": "bachelors", "bsc": "bachelors",
    "be": "bachelors", "bba": "bachelors", "bcs": "bachelors", "bcom": "bachelors", "beng": "bachelors",
    "master": "masters", "masters": "masters", "ms": "masters", "msc": "masters", "mba": "masters",
    "mcs": "masters", "mcom": "masters", "meng": "masters", "mphil": "mphil",
    "phd": "phd", "doctorate": "phd", "dphil": "phd",
}


def edu_level(text: str | None) -> str:
    n = norm_text(text)
    best = "none"
    for alias, level in EDU_ALIASES.items():
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", n):
            if EDU_LADDER.index(level) > EDU_LADDER.index(best):
                best = level
    return best


@dataclass
class Experience:
    title: str = ""
    company: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    is_current: bool = False
    description: str = ""

    @property
    def start(self) -> date | None:
        return parse_date(self.start_date)

    @property
    def end(self) -> date | None:
        if self.is_current or norm_text(self.end_date) in {"", "present", "current"}:
            return date.today()
        return parse_date(self.end_date, default_to_today=True)

    def months(self) -> float:
        s, e = self.start, self.end
        if not s or not e or e < s:
            return 0.0
        return float((e.year - s.year) * 12 + (e.month - s.month))

    def text(self) -> str:
        return " ".join(filter(None, [self.title, self.company, self.description]))

    def evidence_skills(self) -> set[str]:
        cached = getattr(self, "_ev_cache", None)
        if cached is None:
            si = skills_index()
            cached = self._ev_cache = si.expand(si.extract(self.text()))  # type: ignore[attr-defined]
        return cached


@dataclass
class Education:
    degree: str = ""
    field_of_study: str = ""
    institution: str = ""
    start_year: int | None = None
    end_year: int | None = None

    def level(self) -> str:
        return edu_level(f"{self.degree} {self.field_of_study}")


@dataclass
class Candidate:
    full_name: str = ""
    headline: str = ""
    summary: str = ""
    location_raw: str = ""
    linkedin_url: str = ""
    email: str = ""
    phone: str = ""
    skills_raw: list[str] = field(default_factory=list)
    experiences: list[Experience] = field(default_factory=list)
    educations: list[Education] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)
    featured_text: str = ""
    source: str = ""
    source_ref: str = ""
    profile_pdf_path: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    # ---- derived -------------------------------------------------------- #
    def person_key(self) -> str:
        """Stable identity for dedup, best signal first."""
        if self.linkedin_url:
            slug = re.sub(r"[?#].*$", "", self.linkedin_url.rstrip("/")).rsplit("/", 1)[-1]
            if slug and slug not in {"in", "www.linkedin.com"}:
                return f"li:{norm_text(slug).replace(' ', '-')}"
        if self.email:
            # lowercase only - norm_text would strip the '@', which can merge
            # two different addresses (ab@c.com and a@bc.com) into one person
            return "em:" + re.sub(r"\s+", "", self.email).lower()
        city, _, _ = geo_index().classify(self.location_raw)
        basis = f"{norm_text(self.full_name)}|{norm_text(city)}|{norm_text(self.current_company())}"
        return "nm:" + hashlib.sha1(basis.encode()).hexdigest()[:16]

    def current(self) -> Experience | None:
        cur = [e for e in self.experiences if e.is_current]
        if cur:
            return max(cur, key=lambda e: e.start or date.min)
        return max(self.experiences, key=lambda e: e.end or date.min, default=None)

    def current_title(self) -> str:
        c = self.current()
        return (c.title if c else "") or self.headline

    def current_company(self) -> str:
        c = self.current()
        return c.company if c else ""

    def total_years(self) -> float:
        spans = [(e.start, e.end) for e in self.experiences if e.start and e.end]
        return round(merge_months(spans) / 12.0, 2)

    def relevant_years(self, jd_family: str | None, jd_skills: set[str],
                       threshold: float = 0.45) -> float:
        """Years in roles that actually look like the JD.

        A role counts if its title is close to the JD's title family OR its text
        evidences a decent share of the JD's required skills.
        """
        cache = getattr(self, "_rel_cache", None)
        if cache is None:
            cache = self._rel_cache = {}   # type: ignore[attr-defined]
        ck = (jd_family, frozenset(jd_skills), threshold)
        if ck in cache:
            return cache[ck]
        ti, si = titles_index(), skills_index()
        declared = si.expand({si.canonicalize(s) or si.unknown_label(s) for s in self.skills_raw})
        spans = []
        for e in self.experiences:
            if not (e.start and e.end):
                continue
            title_rel = ti.relatedness(jd_family, e.title)
            ev = e.evidence_skills()
            # A role with no written description carries no skill evidence of its own.
            # Absence of a description is not evidence of irrelevance, so fall back to
            # the profile's declared skills rather than scoring the role at zero.
            if len(e.description or "") < 40:
                ev = ev | declared
            skill_rel = (len(ev & jd_skills) / len(jd_skills)) if jd_skills else 0.0
            if max(title_rel, skill_rel) >= threshold:
                spans.append((e.start, e.end))
        out = round(merge_months(spans) / 12.0, 2)
        cache[ck] = out
        return out

    def skills(self) -> set[str]:
        """Canonical skills from the explicit list plus evidence in role text."""
        cached = getattr(self, "_skills_cache", None)
        if cached is not None:
            return cached
        si = skills_index()
        found: set[str] = set()
        for raw in self.skills_raw:
            c = si.canonicalize(raw)
            found.add(c if c else si.unknown_label(raw))
        found |= si.extract(" ".join(self.skills_raw), context="skills")
        blob = " ".join([self.headline, self.summary] + [e.text() for e in self.experiences])
        found |= si.extract(blob, context="prose")
        if self.certifications:
            found |= si.extract(" ".join(self.certifications), context="prose")
        if self.projects:
            proj_text = " ".join(f"{p.get('title','')} {p.get('description','')}" for p in self.projects)
            found |= si.extract(proj_text, context="prose")
        out = si.expand({f for f in found if f})
        self._skills_cache = out       # type: ignore[attr-defined]
        return out

    def education_level(self) -> str:
        levels = [e.level() for e in self.educations] or ["none"]
        return max(levels, key=lambda l: EDU_LADDER.index(l))

    def seniority(self) -> str:
        return seniority_of(self.current_title())

    def profile_text(self) -> str:
        """Single blob used for semantic similarity."""
        cached = getattr(self, "_text_cache", None)
        if cached is not None:
            return cached
        parts = [self.full_name, self.headline, self.summary, self.location_raw,
                 " ".join(self.skills_raw)]
        for e in self.experiences:
            parts.append(f"{e.title} at {e.company}. {e.description}")
        for ed in self.educations:
            parts.append(f"{ed.degree} {ed.field_of_study} {ed.institution}")
        if self.certifications:
            parts.append("Certifications: " + ", ".join(self.certifications))
        for p in self.projects:
            parts.append(f"Project: {p.get('title', '')}. {p.get('description', '')}")
        if self.featured_text:
            parts.append(self.featured_text)
        out = "\n".join(p for p in parts if p)
        self._text_cache = out        # type: ignore[attr-defined]
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        """The inverse of to_dict (profiles travel from the collector PC to the cloud as JSON)."""
        d = dict(d or {})
        exps = [Experience(**{k: v for k, v in e.items() if k in Experience.__dataclass_fields__})
                for e in (d.pop("experiences", None) or [])]
        edus = [Education(**{k: v for k, v in e.items() if k in Education.__dataclass_fields__})
                for e in (d.pop("educations", None) or [])]
        c = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        c.experiences, c.educations = exps, edus
        return c


@dataclass
class JD:
    code: str = ""
    title: str = ""
    title_family: str | None = None
    seniority: str = "mid"
    location: str = "Karachi, Pakistan"
    min_years: float = 0.0
    max_years: float | None = None
    hard_min_years: bool = False   # True -> below min_years caps the score
    education_min: str = "none"
    openings: int = 1
    industries: list[str] = field(default_factory=list)
    must_have: list[str] = field(default_factory=list)
    nice_have: list[str] = field(default_factory=list)
    hard_requirements: list[str] = field(default_factory=list)
    preferred_employers: list[str] = field(default_factory=list)
    extra_titles: list[str] = field(default_factory=list)
    search_skills_count: int = 2   # how many must-have skills to AND in the search (0-2)
    priority: int = 0              # higher = sourced first; 0 = normal rotation
    department: str = ""           # owning department (org structure); not part of the content hash
    raw_text: str = ""
    source_file: str = ""
    version: int = 1
    active: bool = True

    def content_hash(self) -> str:
        basis = "|".join([
            self.title, str(self.min_years), str(self.max_years), str(self.hard_min_years),
            self.seniority,
            self.education_min, self.location,
            ",".join(sorted(self.must_have)), ",".join(sorted(self.nice_have)),
            ",".join(sorted(self.hard_requirements)), ",".join(sorted(self.industries)),
            ",".join(sorted(self.preferred_employers)), ",".join(sorted(self.extra_titles)),
            str(self.search_skills_count), str(self.priority),
        ])
        return hashlib.sha256(basis.encode()).hexdigest()[:16]

    def must_skills(self) -> set[str]:
        si = skills_index()
        return si.expand({si.canonicalize(s) or si.unknown_label(s) for s in self.must_have})

    def nice_skills(self) -> set[str]:
        si = skills_index()
        return {si.canonicalize(s) or si.unknown_label(s) for s in self.nice_have}

    def hard_skills(self) -> set[str]:
        si = skills_index()
        return {si.canonicalize(s) or si.unknown_label(s) for s in self.hard_requirements}

    def jd_text(self) -> str:
        return "\n".join([
            self.title, self.seniority, self.location,
            f"{self.min_years}+ years experience",
            "Required: " + ", ".join(self.must_have),
            "Preferred: " + ", ".join(self.nice_have),
            self.raw_text or "",
        ])
