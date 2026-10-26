"""Turn a JD into the search strings and links you actually run.

The bot cannot log into LinkedIn and harvest profiles for you (see README -
that is an account ban and a terms breach). What it can do is build the exact
boolean queries, LinkedIn Recruiter/Sales-Navigator-ready filters and Google
x-ray links for each vacancy, so the human step is one click plus "Save to PDF".
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from .config import cfg
from .models import JD
from .normalize import titles_index

SENIORITY_WORDS = {
    "intern": ["intern", "trainee"],
    "junior": ["junior", "associate", "jr"],
    "mid": [],
    "senior": ["senior", "sr", "specialist"],
    "lead": ["lead", "principal", "staff"],
    "manager": ["manager", "head"],
    "director": ["director", "general manager"],
    "executive": ["chief", "vp", "head of"],
}


def _quote(term: str) -> str:
    t = term.replace("_", " ").strip()
    return f'"{t}"' if " " in t else t


@dataclass
class SearchPlan:
    jd_code: str
    jd_title: str
    titles: list[str] = field(default_factory=list)
    must_terms: list[str] = field(default_factory=list)
    nice_terms: list[str] = field(default_factory=list)
    location: str = ""
    boolean: str = ""
    linkedin_people_url: str = ""
    linkedin_recruiter_hint: dict = field(default_factory=dict)
    google_xray: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        lines = [f"### {self.jd_code} - {self.jd_title}",
                 f"Location filter : {self.location}",
                 f"Target titles   : {', '.join(self.titles)}",
                 "",
                 "Boolean (LinkedIn Recruiter / Sales Navigator keywords box):",
                 f"  {self.boolean}",
                 "",
                 f"LinkedIn people search: {self.linkedin_people_url}",
                 "",
                 "Google x-ray (paste in Google, open results, Save to PDF):"]
        lines += [f"  {u}" for u in self.google_xray]
        if self.linkedin_recruiter_hint:
            lines += ["", "Recruiter filter settings:"]
            lines += [f"  {k:<16} {v}" for k, v in self.linkedin_recruiter_hint.items()]
        if self.notes:
            lines += [""] + [f"note: {n}" for n in self.notes]
        return "\n".join(lines)


def build(jd: JD, city: str | None = None, country: str | None = None) -> SearchPlan:
    c = cfg()
    city = city or c.get("location.target_city", "Karachi")
    country = country or c.get("location.target_country", "Pakistan")
    ti = titles_index()

    aliases = (ti.families.get(jd.title_family or "") or {}).get("aliases") or []
    seen: set[str] = set()
    titles = []
    for t in [jd.title] + aliases[:8]:
        if t.lower() not in seen:
            seen.add(t.lower())
            titles.append(t)
    titles = titles[:7]
    sen = SENIORITY_WORDS.get(jd.seniority or "mid", [])

    must = [t for t in jd.must_have[:6]]
    nice = [t for t in jd.nice_have[:5]]

    title_clause = "(" + " OR ".join(_quote(t) for t in titles) + ")"
    must_clause = " AND ".join(f"({_quote(m)})" for m in must[:4]) if must else ""
    nice_clause = "(" + " OR ".join(_quote(n) for n in nice) + ")" if nice else ""
    sen_clause = "(" + " OR ".join(_quote(w) for w in sen) + ")" if sen else ""
    boolean = " AND ".join(x for x in [title_clause, sen_clause, must_clause] if x)
    if nice_clause:
        boolean += f" AND {nice_clause}"

    kw = urllib.parse.quote_plus(boolean)
    people_url = (f"https://www.linkedin.com/search/results/people/"
                  f"?keywords={kw}&origin=GLOBAL_SEARCH_HEADER")

    xray = []
    for t in titles[:3]:
        q = (f'site:linkedin.com/in "{t}" "{city}" '
             + " ".join(f'"{m}"' for m in must[:3]))
        xray.append("https://www.google.com/search?q=" + urllib.parse.quote_plus(q))

    return SearchPlan(
        jd_code=jd.code, jd_title=jd.title, titles=titles,
        must_terms=must, nice_terms=nice, location=f"{city}, {country}",
        boolean=boolean, linkedin_people_url=people_url,
        linkedin_recruiter_hint={
            "Locations": f"{city}, {country}",
            "Years in current company": "any",
            "Years of experience": f"{jd.min_years:g}+"
                                   + (f" to {jd.max_years:g}" if jd.max_years else ""),
            "Seniority": jd.seniority,
            "Skills": ", ".join(m.replace("_", " ") for m in must[:6]),
            "Industries": ", ".join(jd.industries) or "any",
            "Spotlights": "Open to work, Engaged with talent brand",
        },
        google_xray=xray,
        notes=[f"{jd.openings} opening(s); shortlist threshold "
               f"{c.get('scoring.default_shortlist_threshold', 75)}%",
               "Save each promising profile as PDF into storage/pdf_inbox, then run "
               "`sourcer run` - the bot parses, dedupes, scores and files it."],
    )


def build_all(jds: list[JD]) -> list[SearchPlan]:
    return [build(j) for j in jds]
