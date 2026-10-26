"""Turn the visible text of LinkedIn pages into structured data.

Works from page TEXT (what a person reads), not CSS classes. LinkedIn changes
its markup constantly and obfuscates class names, but the reading order and the
section headings ("About", "Experience", "Education", "Skills") move far less.
That makes this the part least likely to break - and when it does, the
`debug_dir` dump of the raw text is exactly what is needed to fix it.

Pure functions, no browser: everything here is testable on saved text.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .models import Candidate, Education, Experience
from .normalize import norm_text

MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
RANGE = re.compile(
    rf"^\s*(?:({MONTH})\s+)?((?:19|20)\d{{2}})\s*[-–—]\s*"
    rf"(?:(?:({MONTH})\s+)?((?:19|20)\d{{2}})|present)\b", re.I)
DURATION_ONLY = re.compile(
    r"^(?:(?:full[- ]time|part[- ]time|contract|internship|self[- ]employed|freelance|"
    r"apprenticeship|seasonal|permanent|temporary)\s*·\s*)?"
    r"\d+\s+(?:yrs?|years?|mos?|months?)(?:\s+\d+\s+(?:mos?|months?))?$", re.I)
EMPLOYMENT_TYPES = r"(?:full[- ]time|part[- ]time|contract|internship|self[- ]employed|freelance|apprenticeship|seasonal|permanent|temporary)"
LOC_HINT = re.compile(
    r"(pakistan|karachi|lahore|islamabad|rawalpindi|faisalabad|multan|peshawar|quetta|"
    r"hyderabad|sialkot|remote|hybrid|united arab|uae|dubai|saudi|riyadh|qatar|doha|"
    r"united kingdom|london|united states|canada|germany|singapore|sindh|punjab|"
    r"khyber|balochistan|area$|metropolitan)", re.I)

SECTION_HEADS = {
    "about": "about", "experience": "experience", "education": "education",
    "skills": "skills", "licenses & certifications": "certs", "certifications": "certs",
    "projects": "projects", "languages": "languages", "volunteering": "other",
    "volunteer experience": "other", "courses": "other", "honors & awards": "other",
    "publications": "other", "recommendations": "other", "interests": "other",
    "activity": "activity", "featured": "featured", "highlights": "other",
    "analytics": "other", "resources": "other", "people also viewed": "other",
    "people you may know": "other", "more profiles for you": "other",
    "explore premium profiles": "other", "you might like": "other",
    "test scores": "other", "organizations": "other", "patents": "other",
}
NOISE = re.compile(
    r"^(connect|message|follow|more|pending|save in sales navigator|book an appointment|"
    r"visit my website|contact info|show all|see more|…see more|see less|show more|"
    r"\d+ followers?|\d+\+? connections?|•|·|\(she/her\)|\(he/him\)|\(they/them\)|"
    r"open to|premium|try premium|1st|2nd|3rd\+?|you|mutual connections?)$", re.I)


def clean_lines(text: str) -> list[str]:
    """Strip blanks, drop accessibility duplicates (LinkedIn prints many lines twice)."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        t = re.sub(r"\s+", " ", raw.replace("​", "")).strip()
        if not t:
            continue
        if out and out[-1] == t:
            continue
        out.append(t)
    return out


def slug_of(url: str) -> str:
    path = urlparse(url or "").path
    m = re.search(r"/in/([^/]+)", path)
    return m.group(1) if m else ""


def canonical_profile_url(url: str) -> str:
    """https://<host>/in/<slug>/ - tracking parameters dropped. Regional LinkedIn
    hosts (pk.linkedin.com) are folded into www; any other host is left alone."""
    slug = slug_of(url)
    if not slug:
        return url
    u = urlparse(url)
    if not u.netloc:
        return f"https://www.linkedin.com/in/{slug}/"
    host = "www.linkedin.com" if u.netloc.lower().endswith("linkedin.com") else u.netloc
    scheme = "https" if host == "www.linkedin.com" else (u.scheme or "https")
    return f"{scheme}://{host}/in/{slug}/"


# --------------------------------------------------------------------------- #
# Search result cards
# --------------------------------------------------------------------------- #
CARD_NOISE = re.compile(
    r"^(connect|message|follow|view .*profile|status is .*|•.*|· ?(1st|2nd|3rd\+?)|"
    r"1st|2nd|3rd\+?|\d+ mutual connections?.*|.* is a mutual connection|"
    r"provides services.*|current:.*|past:.*|skills:.*|summary:.*|"
    r"mutual connections?|pending|save|more)$", re.I)


def parse_card(card: dict) -> dict | None:
    """card = {"href": str, "lines": [str]} -> {url, slug, name, headline, location}"""
    slug = slug_of(card.get("href", ""))
    if not slug:
        return None
    lines = [l for l in clean_lines("\n".join(card.get("lines") or []))
             if not CARD_NOISE.match(l)]
    lines = [re.sub(r"\s*[•·]\s*(1st|2nd|3rd\+?)\s*$", "", l).strip() for l in lines]
    lines = [l for l in lines if l and not re.match(r"^[•·]?\s*(1st|2nd|3rd\+?)$", l, re.I)]
    if not lines:
        return None
    name = lines[0]
    if name.lower() in {"linkedin member"}:
        return None
    rest = lines[1:]
    headline = rest[0] if rest else ""
    location = ""
    for l in rest[1:4] if len(rest) > 1 else []:
        if LOC_HINT.search(l) and len(l) < 80:
            location = l
            break
    if not location:
        for l in rest:
            if LOC_HINT.search(l) and len(l) < 80 and l != headline:
                location = l
                break
    return {"slug": slug, "url": canonical_profile_url(card.get("href", "")),
            "name": name, "headline": headline, "location": location}


def dedupe_cards(cards: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in cards:
        p = parse_card(c)
        if p and p["slug"] not in seen:
            seen.add(p["slug"])
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Profile text
# --------------------------------------------------------------------------- #
def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    secs: dict[str, list[str]] = {"top": []}
    cur = "top"
    for l in lines:
        key = SECTION_HEADS.get(norm_text(l).replace("&", "&"))
        if key is None and len(l) < 40:
            key = SECTION_HEADS.get(l.lower().strip())
        if key is not None and len(l) < 40:
            cur = key if key != "other" else f"other:{l.lower()}"
            secs.setdefault(cur, [])
            continue
        secs.setdefault(cur, []).append(l)
    return secs


def _span(m) -> tuple[str, str, bool]:
    start = f"{(m.group(1) or '').title().rstrip('.')} {m.group(2)}".strip()
    if m.group(4):
        return start, f"{(m.group(3) or '').title().rstrip('.')} {m.group(4)}".strip(), False
    return start, "", True


def parse_experience_lines(lines: list[str]) -> list[Experience]:
    """Handles single roles and a company with several roles grouped under it:

        Senior Data Engineer            |  Systems Limited
        Systems Limited · Full-time     |  Full-time · 5 yrs 2 mos
        Feb 2022 - Present · 3 yrs      |  Senior Data Engineer
        Karachi, Pakistan · On-site     |  Feb 2022 - Present · 3 yrs
    """
    lines = clean_lines("\n".join(lines))
    anchors = [i for i, l in enumerate(lines) if RANGE.match(l)]
    exps: list[Experience] = []
    group_company = ""
    last_anchor = -1
    for n, i in enumerate(anchors):
        m = RANGE.match(lines[i])
        start, end, current = _span(m)

        # a group header sits between the previous role and this one:
        # "<Company>" followed directly by a duration-only line
        for j in range(last_anchor + 1, i):
            if j + 1 < i and DURATION_ONLY.match(lines[j + 1]) and not DURATION_ONLY.match(lines[j]) \
                    and not RANGE.match(lines[j]):
                group_company = lines[j]

        p1 = lines[i - 1] if i - 1 > last_anchor else ""
        p2 = lines[i - 2] if i - 2 > last_anchor else ""
        title = company = ""
        if p1 and " · " in p1 and not DURATION_ONLY.match(p1):
            company = p1.split(" · ")[0].strip()
            title = p2
            group_company = ""             # a standalone role ends any group
        elif p1 and DURATION_ONLY.match(p1):
            title = p2                      # "<title> / <type · duration>" shape
            company = group_company
        else:
            title = p1
            company = group_company

        # The body runs to the start of the NEXT role's header lines. Only trim when
        # a next role exists - for the last role the body runs to the section end.
        if n + 1 < len(anchors):
            nxt = anchors[n + 1]
            h = 1
            prev = lines[nxt - 1] if nxt - 1 > i else ""
            if prev and ((" · " in prev and not DURATION_ONLY.match(prev))
                         or DURATION_ONLY.match(prev)):
                h = 2                                   # "<company> · <type>" above the date
            elif nxt - 2 > i and DURATION_ONLY.match(lines[nxt - 2]):
                h = 3                                   # grouped: company / duration / title
            body_end = max(i + 1, nxt - h)
        else:
            body_end = len(lines)
        body = [b for b in lines[i + 1:body_end]
                if not re.match(r"^show all \d+", b, re.I)]
        location = ""
        if body and LOC_HINT.search(body[0]) and len(body[0]) < 90:
            location = body.pop(0).split(" · ")[0]
        desc = " ".join(b for b in body if not re.match(r"^(skills:|show all|see more)", b, re.I))
        exps.append(Experience(title=title.strip(), company=company.strip(), location=location,
                               start_date=start, end_date=end, is_current=current,
                               description=desc[:3000]))
        last_anchor = i
    return exps


YEAR_RANGE = re.compile(r"^((?:19|20)\d{2})\s*(?:[-–—]\s*((?:19|20)\d{2}|present))?$", re.I)


def parse_education_lines(lines: list[str]) -> list[Education]:
    lines = [l for l in clean_lines("\n".join(lines)) if not NOISE.match(l)]
    out: list[Education] = []
    i = 0
    while i < len(lines):
        inst = lines[i]
        deg = ""
        end_year = None
        j = i + 1
        if j < len(lines) and not YEAR_RANGE.match(lines[j]) and not _looks_like_institution(lines[j]):
            deg = lines[j]
            j += 1
        if j < len(lines) and YEAR_RANGE.match(lines[j]):
            m = YEAR_RANGE.match(lines[j])
            end_year = int(m.group(2)) if (m.group(2) or "").isdigit() else int(m.group(1))
            j += 1
        # skip trailing detail lines (grade, activities) until the next institution
        while j < len(lines) and re.match(r"^(grade|activities|societies|skills:|description)", lines[j], re.I):
            j += 1
        degree, field = deg, ""
        m = re.match(r"^([^,]+?)\s*,\s*(.+)$", deg)
        if m:
            degree, field = m.group(1).strip(), m.group(2).strip()
        out.append(Education(degree=degree, field_of_study=field, institution=inst,
                             end_year=end_year))
        i = max(j, i + 1)
    return out


def _looks_like_institution(line: str) -> bool:
    return bool(re.search(r"\b(university|college|institute|school|academy|nust|lums|iba|fast|ned)\b",
                          line, re.I)) and not re.search(r"\b(bachelor|master|bs|ms|mba|phd|diploma)\b", line, re.I)


def parse_skills_lines(lines: list[str]) -> list[str]:
    out = []
    for l in clean_lines("\n".join(lines)):
        if NOISE.match(l) or re.search(r"(endorsement|^used at|^show all|^and \+|passed linkedin|"
                                       r"^\d+ (experience|school)|·)", l, re.I):
            continue
        if 1 < len(l) <= 40 and not re.match(r"^\d", l):
            out.append(l)
    seen, uniq = set(), []
    for s in out:
        if s.lower() not in seen:
            seen.add(s.lower())
            uniq.append(s)
    return uniq[:60]


def parse_certifications_lines(lines: list[str]) -> list[str]:
    out = []
    for l in clean_lines("\n".join(lines)):
        if NOISE.match(l) or re.search(r"(show all|see more|credential|issued|expires|^see credential$)", l, re.I):
            continue
        if RANGE.match(l) or re.match(r"^\w{3,9}\s+\d{4}$", l, re.I):
            continue
        if 2 < len(l) <= 120 and not re.match(r"^\d", l):
            out.append(l)
    seen, uniq = set(), []
    for s in out:
        if s.lower() not in seen:
            seen.add(s.lower())
            uniq.append(s)
    return uniq[:30]


def parse_projects_lines(lines: list[str]) -> list[dict]:
    raw = clean_lines("\n".join(lines))
    raw = [l for l in raw if not NOISE.match(l)
           and not re.search(r"(show all|see more|^show \d+)", l, re.I)]
    projects: list[dict] = []
    i = 0
    while i < len(raw):
        title = raw[i]
        desc_parts = []
        i += 1
        while i < len(raw):
            if RANGE.match(raw[i]) or re.match(r"^\w{3,9}\s+\d{4}$", raw[i], re.I):
                i += 1
                continue
            if len(raw[i]) > 60 or re.search(r"[.;,]", raw[i]):
                desc_parts.append(raw[i])
                i += 1
            else:
                break
        projects.append({"title": title, "description": " ".join(desc_parts)[:2000]})
        if len(projects) >= 20:
            break
    return projects


def _collect_other_text(secs: dict[str, list[str]], prefix: str) -> str:
    parts = []
    for key, lines in secs.items():
        if key.startswith(f"{prefix}:") or key == prefix:
            parts.extend(l for l in lines if not NOISE.match(l) and len(l) > 5)
    return " ".join(parts)[:4000]


def parse_profile_text(text: str, url: str = "", source: str = "linkedin_browser") -> Candidate | None:
    """The visible text of a profile page -> Candidate. None if it is not a usable profile."""
    lines = clean_lines(text)
    if not lines:
        return None
    secs = _split_sections(lines)
    top = [l for l in secs.get("top", []) if not NOISE.match(l)]
    top = [re.sub(r"\s*[·•]\s*(1st|2nd|3rd\+?)\s*$", "", l).strip() for l in top]
    top = [l for l in top if l]
    if not top:
        return None
    name = top[0]
    if name.lower() in {"linkedin member", "profile", "home"}:
        return None

    headline, location = "", ""
    for l in top[1:12]:
        l2 = re.sub(r"\s*·?\s*contact info\s*$", "", l, flags=re.I).strip()
        if not location and LOC_HINT.search(l2) and len(l2) < 90 and l2 != headline:
            location = l2
            if headline:
                break
            continue
        if not headline and l2 and not LOC_HINT.fullmatch(l2) and len(l2) < 260 \
                and not re.match(r"^(\d|·)", l2):
            headline = l2

    about = " ".join(l for l in secs.get("about", []) if not NOISE.match(l))[:6000]
    exps = parse_experience_lines(secs.get("experience", []))
    edus = parse_education_lines(secs.get("education", []))
    skills = parse_skills_lines(secs.get("skills", []))
    certs = parse_certifications_lines(secs.get("certs", []))
    projects = parse_projects_lines(secs.get("projects", []))
    featured = _collect_other_text(secs, "featured")
    activity = _collect_other_text(secs, "activity")
    featured_text = " ".join(filter(None, [featured, activity]))[:6000]

    c = Candidate(
        full_name=name, headline=headline, summary=about, location_raw=location,
        linkedin_url=canonical_profile_url(url) if url else "",
        skills_raw=skills, experiences=exps, educations=edus,
        certifications=certs, projects=projects, featured_text=featured_text,
        source=source, source_ref=slug_of(url) or url,
        raw={"parsed_from": "linkedin page text", "chars": len(text or "")})
    return c


def has_truncated_experience(text: str) -> bool:
    """'Show all 7 experiences' means the page only showed the first few roles."""
    return bool(re.search(r"show all \d+ experiences?", text or "", re.I))


def has_truncated_education(text: str) -> bool:
    return bool(re.search(r"show all \d+ educations?", text or "", re.I))
