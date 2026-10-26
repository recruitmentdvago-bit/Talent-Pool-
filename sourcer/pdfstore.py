"""PDF in, PDF out.

in  - parse a LinkedIn "Save to PDF" export or an ordinary CV into a Candidate
out - render a clean, consistent one-page profile PDF for every candidate, so a
      hiring manager always gets the same document shape, and store it with a
      content hash so the same file is never kept twice.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from datetime import date
from pathlib import Path

import pdfplumber
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (HRFlowable, ListFlowable, ListItem, Paragraph,
                                SimpleDocTemplate, Spacer)

from .config import cfg
from .models import Candidate, Education, Experience
from .normalize import norm_text, seniority_of, skills_index, titles_index

# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
SECTION_WORDS = {
    "contact": "contact", "top skills": "skills", "skills": "skills",
    "key skills": "skills", "technical skills": "skills", "core competencies": "skills",
    "languages": "languages", "certifications": "certifications",
    "licenses & certifications": "certifications", "honors-awards": "awards",
    "honors & awards": "awards", "summary": "summary", "about": "summary",
    "profile": "summary", "objective": "summary", "experience": "experience",
    "work experience": "experience", "professional experience": "experience",
    "employment history": "experience", "education": "education",
    "projects": "projects", "publications": "publications", "interests": "interests",
    "volunteer experience": "volunteer", "references": "references",
}

MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_RANGE = (rf"(?:({MONTH})\s+)?((?:19|20)\d{{2}})\s*(?:[-–—]|\bto\b)+\s*"
          rf"(?:(?:({MONTH})\s+)?((?:19|20)\d{{2}})|present|current|to\s*date|till\s*date|now)")
DATE_RANGE = re.compile(_RANGE, re.I)            # anywhere in a line
DATE_LINE = re.compile(r"^\s*" + _RANGE, re.I)   # LinkedIn exports put it first
LOC_HINT = re.compile(r"(pakistan|karachi|lahore|islamabad|rawalpindi|faisalabad|multan|"
                      r"peshawar|quetta|hyderabad|remote|hybrid|on-?site|united arab|uae|"
                      r"dubai|saudi|qatar|,\s*[A-Z]{2}\b)", re.I)
DURATION_TAIL = re.compile(r"\(.*?(year|yr|month|mo).*?\)", re.I)
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RX = re.compile(r"(?:\+92|0092|92|0)?[\s-]?3\d{2}[\s-]?\d{7}|\+\d[\d\s().-]{7,}\d")
LI_RX = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w\-%.]+", re.I)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def pdf_to_text(path: str | Path) -> str:
    out = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            out.append(page.extract_text() or "")
    return "\n".join(out)


def _section_key(line: str) -> str | None:
    """Match a heading even when decorated: 'SKILLS (normalised)', 'Experience:'."""
    n = norm_text(re.sub(r"\(.*?\)", " ", line)).strip(" :-.")
    return SECTION_WORDS.get(n)


def _sectionise(lines: list[str]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {}
    cur = "header"
    for ln in lines:
        key = _section_key(ln)
        if key and len(ln.strip()) < 45:
            cur = key
            buckets.setdefault(cur, [])
            continue
        buckets.setdefault(cur, []).append(ln)
    return buckets


def _looks_like_title(line: str) -> bool:
    if titles_index().family_of(line):
        return True
    n = norm_text(line)
    return bool(re.search(
        r"\b(engineer|analyst|manager|developer|officer|lead|consultant|specialist|"
        r"scientist|architect|executive|associate|director|head|intern|administrator|"
        r"coordinator|supervisor|accountant|designer)\b", n))


def _date_match(line: str):
    """Line-start match first (LinkedIn exports), else anywhere in a short line
    (CV layouts that put the date after the company)."""
    t = line.strip()
    return DATE_LINE.match(t) or (DATE_RANGE.search(t) if len(t) < 140 else None)


def _span(m) -> tuple[str, str, bool]:
    start = f"{(m.group(1) or '').title()} {m.group(2)}".strip()
    if m.group(4):
        end = f"{(m.group(3) or '').title()} {m.group(4)}".strip()
        return start, end, False
    return start, "Present", True


def _is_header_line(line: str) -> bool:
    """Reject wrapped prose so a description line is not mistaken for a company."""
    t = line.strip()
    if not t or len(t) > 85:
        return False
    if DATE_RANGE.search(t):          # that is the previous role's date line
        return False
    if t[0].islower() or t.endswith((".", ";", ",", ":")):
        return False
    return not re.search(r"\b(and|with|the|for|using|to)\b.*\b(and|with|the|for|using|to)\b",
                         t, re.I)


def _parse_experience(lines: list[str]) -> list[Experience]:
    clean = [DURATION_TAIL.sub("", ln) for ln in lines]
    anchors = [i for i, ln in enumerate(clean) if DATE_LINE.match(ln.strip())]
    if not anchors:
        anchors = [i for i, ln in enumerate(clean) if _date_match(ln)]
    exps: list[Experience] = []

    for n, i in enumerate(anchors):
        line = clean[i].strip()
        m = _date_match(line)
        if not m:
            continue
        start, end, is_current = _span(m)

        # fragments on the anchor line before the date (CV layout:
        # "Habib Bank · Karachi, Pakistan · Mar 2021 - Present")
        inline = [x.strip() for x in re.split(r"\s*[·|]\s*", line[:m.start()]) if x.strip()]
        # fragments after the date are usually location or duration noise
        inline += [x.strip() for x in re.split(r"\s*[·|]\s*", line[m.end():])
                   if x.strip() and LOC_HINT.search(x)]

        above = [l.strip() for l in lines[max(0, i - 4):i]
                 if l.strip() and _is_header_line(l)][-2:]

        location = next((f for f in inline if LOC_HINT.search(f) and len(f) < 60), "")
        company_candidates = [f for f in inline if f != location]

        title = company = ""

        # CV layout: "Meezan Bank · Karachi, Pakistan · Feb 2019 - Mar 2022".
        # The company is right there on the anchor line, so prefer it and use the
        # lines above only for the title. Guessing from the lines above instead
        # picks up the previous role's title.
        if company_candidates:
            company = company_candidates[0]
            title = next((l for l in reversed(above) if _looks_like_title(l)), "")
            if title:
                above = []

        if not title and len(above) == 2:
            a, b = above
            if _looks_like_title(b) and not _looks_like_title(a):
                company, title = a, b
            elif _looks_like_title(a) and not _looks_like_title(b):
                title, company = a, b
            else:
                company, title = a, b
        elif not title and above:
            only = above[0]
            if re.search(r"\s+at\s+", only, re.I) or ("," in only and _looks_like_title(only)):
                parts = re.split(r"\s+at\s+|,", only, maxsplit=1)
                title = parts[0].strip()
                company = parts[1].strip() if len(parts) > 1 else ""
            elif _looks_like_title(only):
                title = only
            else:
                company = only

        if not company and company_candidates:
            company = company_candidates[0]

        # body = lines between this anchor and the next role's header lines
        nxt = anchors[n + 1] if n + 1 < len(anchors) else len(lines)
        header_lines_of_next = 2 if n + 1 < len(anchors) else 0
        body_end = max(i + 1, nxt - header_lines_of_next)
        body = [l.strip() for l in lines[i + 1:body_end] if l.strip()]
        if body and not location and len(body[0]) < 60 and LOC_HINT.search(body[0]):
            location = body.pop(0)

        exps.append(Experience(
            title=title, company=company, location=location,
            start_date=start, end_date="" if is_current else end,
            is_current=is_current, description=" ".join(body)[:4000]))
    return exps


def _edu_from_line(line: str) -> Education | None:
    """'BS Computer Science · NED University · 2018' on one line."""
    parts = [x.strip() for x in re.split(r"\s*[·|]\s*", line) if x.strip()]
    if len(parts) < 2:
        return None
    years = re.findall(r"(?:19|20)\d{2}", line)
    inst = next((x for x in parts
                 if re.search(r"(university|college|institute|school|academy|nust|lums|iba|ned)",
                              x, re.I)), None)
    deg_part = next((x for x in parts if x is not inst and not x.strip().isdigit()), parts[0])
    deg, fld = deg_part, ""
    m = re.match(r"^((?:b|m)[a-z.]*(?:\s+of\s+\w+)?|bachelor[s]?|master[s]?|phd|mba|bs|ms|bsc|msc|be|me)\b[\s,.-]*(.*)$",
                 deg_part, re.I)
    if m:
        deg, fld = m.group(1).strip(), m.group(2).strip()
    return Education(degree=deg, field_of_study=fld, institution=inst or "",
                     end_year=int(years[-1]) if years else None)


def _parse_education(lines: list[str]) -> list[Education]:
    joined = [l.strip() for l in lines if l.strip()]
    out: list[Education] = []
    i = 0
    while i < len(joined):
        line = joined[i]
        if "·" in line or "|" in line:
            e = _edu_from_line(line)
            if e:
                out.append(e)
            i += 1
            continue
        inst = line
        degree_line = joined[i + 1] if i + 1 < len(joined) else ""
        years = re.findall(r"((?:19|20)\d{2})", f"{inst} {degree_line}")
        deg, fld = degree_line, ""
        m = re.match(r"^([^,·()]+)[,·]\s*([^,·()]+)", degree_line)
        if m:
            deg, fld = m.group(1).strip(), m.group(2).strip()
        if norm_text(inst):
            out.append(Education(degree=re.sub(r"\s*\(.*?\)", "", deg).strip(),
                                 field_of_study=re.sub(r"\s*\(.*?\)", "", fld).strip(),
                                 institution=inst,
                                 end_year=int(years[-1]) if years else None))
        i += 2
    return out


NAME_STOP = re.compile(r"(linkedin|contact|skills|summary|experience|education|page \d|"
                       r"certification|language|honor|award|@|http|www\.)", re.I)


def _is_person_name(line: str) -> bool:
    t = line.strip().rstrip(",")
    if not t or NAME_STOP.search(t) or any(ch.isdigit() for ch in t):
        return False
    words = t.split()
    if not (2 <= len(words) <= 5) or len(t) > 45:
        return False
    if re.search(r"[|·/@]", t) or re.search(r"\bat\b", t, re.I):
        return False
    if LOC_HINT.search(t) or t.count(",") >= 2:   # a place, not a person
        return False
    if titles_index().family_of(t):
        return False
    letters = [w for w in words if w[:1].isalpha()]
    return len(letters) == len(words) and all(
        w[0].isupper() or w.isupper() for w in letters)


def _find_identity(lines: list[str]) -> tuple[str, str, str]:
    """-> (name, headline, location). Handles both layouts:
    CV            name at the very top
    LinkedIn PDF  sidebar first, then name / headline / location before Summary
    """
    nonempty = [(i, l.strip()) for i, l in enumerate(lines) if l.strip()]

    # anchor: the first Summary/Experience heading
    anchor = next((i for i, l in enumerate(lines)
                   if _section_key(l) in {"summary", "experience"}), None)

    candidates: list[tuple[int, int, str, list[str]]] = []   # (priority, idx, name, tail)
    if anchor is not None:
        before = [l for i, l in nonempty if i < anchor][-4:]
        for k, ln in enumerate(before):          # name comes first of name/headline/location
            if _is_person_name(ln):
                candidates.append((k, anchor, ln, before[k + 1:]))
                break
    for i, ln in nonempty[:8]:
        if _is_person_name(ln):
            tail = [l for j, l in nonempty if i < j <= i + 4]
            candidates.append((5, i, ln, tail))
            break

    if not candidates:
        return "", "", ""
    _, _, name, tail = min(candidates, key=lambda x: x[0])

    headline, location = "", ""
    for ln in tail[:3]:
        frags = [f.strip() for f in ln.split("·")] if "·" in ln else [ln.strip()]
        loc = next((f for f in frags if LOC_HINT.search(f) and len(f) < 70), None)
        if loc and not location:
            location = loc
            frags = [f for f in frags if f is not loc]
        rest = [f for f in frags if f and not EMAIL_RX.search(f) and not LI_RX.search(f)
                and not PHONE_RX.search(f) and not re.match(r"^[\d.]+\s*(yrs|years)", f, re.I)]
        if not headline and rest and len(rest) == len(frags):
            headline = rest[0]
    return name, headline, location


def parse_profile_pdf(path: str | Path, source: str = "pdf_inbox") -> Candidate:
    """Best-effort structured read of a LinkedIn PDF export or a CV."""
    p = Path(path)
    text = pdf_to_text(p)
    lines = [l.rstrip() for l in text.splitlines()]
    sec = _sectionise(lines)
    blob = "\n".join(lines)

    email = (EMAIL_RX.search(blob) or [None]) and (EMAIL_RX.search(blob).group(0)
                                                   if EMAIL_RX.search(blob) else "")
    phone_m = PHONE_RX.search(blob)
    li_m = LI_RX.search(blob)

    name, headline, location_raw = _find_identity(lines)
    if not name:
        header = [l.strip() for l in sec.get("header", []) if l.strip()]
        name = next((l for l in header if 2 <= len(l.split()) <= 6),
                    p.stem.replace("_", " ").title())

    skills_lines = [l.strip() for l in sec.get("skills", []) if l.strip()]
    skills_raw: list[str] = []
    for ln in skills_lines:
        skills_raw.extend([s.strip() for s in re.split(r"[,;•|]", ln) if 1 < len(s.strip()) < 40])

    cand = Candidate(
        full_name=name,
        headline=headline,
        summary=" ".join(l.strip() for l in sec.get("summary", []) if l.strip())[:6000],
        location_raw=location_raw,
        linkedin_url=("https://" + li_m.group(0).lstrip("htps:/")) if li_m else "",
        email=email or "",
        phone=phone_m.group(0).strip() if phone_m else "",
        skills_raw=skills_raw,
        experiences=_parse_experience(sec.get("experience", [])),
        educations=_parse_education(sec.get("education", [])),
        source=source,
        source_ref=p.name,
        profile_pdf_path=str(p),
        raw={"extracted_chars": len(text), "sections": sorted(sec.keys())},
    )
    if not cand.skills_raw:
        cand.skills_raw = sorted(skills_index().extract(blob, context="prose"))
    return cand


def parse_confidence(c: Candidate) -> tuple[float, list[str]]:
    """How much to trust an auto-parsed profile. Low scores need a human look."""
    issues = []
    score = 1.0
    if not c.full_name or len(c.full_name.split()) < 2:
        score -= 0.3; issues.append("name uncertain")
    if not c.experiences:
        score -= 0.35; issues.append("no experience parsed")
    elif not any(e.title for e in c.experiences):
        score -= 0.2; issues.append("role titles missing")
    if not c.skills_raw:
        score -= 0.15; issues.append("no skills found")
    if not c.location_raw:
        score -= 0.1; issues.append("no location")
    if not c.educations:
        score -= 0.1; issues.append("no education parsed")
    return max(0.0, round(score, 2)), issues


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def _styles():
    ss = getSampleStyleSheet()
    base = dict(fontName="Helvetica", leading=12.5, alignment=TA_LEFT)
    return {
        "name": ParagraphStyle("name", parent=ss["Title"], fontName="Helvetica-Bold",
                               fontSize=18, leading=21, spaceAfter=2, textColor=colors.HexColor("#111827")),
        "headline": ParagraphStyle("headline", fontSize=10.5, textColor=colors.HexColor("#374151"),
                                   spaceAfter=1, **base),
        "meta": ParagraphStyle("meta", fontSize=8.5, textColor=colors.HexColor("#6B7280"),
                               spaceAfter=6, **base),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10.5, leading=13,
                             spaceBefore=9, spaceAfter=3, textColor=colors.HexColor("#1F2937"),
                             alignment=TA_LEFT),
        "role": ParagraphStyle("role", fontName="Helvetica-Bold", fontSize=9.8, leading=12,
                               spaceAfter=0, alignment=TA_LEFT),
        "sub": ParagraphStyle("sub", fontSize=8.6, textColor=colors.HexColor("#6B7280"),
                              spaceAfter=2, **base),
        "body": ParagraphStyle("body", fontSize=9, **base),
        "foot": ParagraphStyle("foot", fontSize=7.3, textColor=colors.HexColor("#9CA3AF"), **base),
    }


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_profile_pdf(c: Candidate, dest: str | Path,
                       score_note: str = "", provenance: str = "") -> Path:
    """One consistent page per candidate. Includes a provenance footer."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    st = _styles()
    doc = SimpleDocTemplate(str(dest), pagesize=A4, title=f"{c.full_name} - profile",
                            author="hrbp-sourcer", leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=15 * mm, bottomMargin=14 * mm)
    fl = [Paragraph(_esc(c.full_name), st["name"])]
    if c.headline:
        fl.append(Paragraph(_esc(c.headline), st["headline"]))
    meta = " · ".join(filter(None, [
        c.location_raw, c.email, c.phone,
        f"{c.total_years():g} yrs experience" if c.total_years() else "",
        c.linkedin_url]))
    if meta:
        fl.append(Paragraph(_esc(meta), st["meta"]))
    fl.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#E5E7EB"),
                         spaceAfter=4))
    if score_note:
        fl.append(Paragraph(f"<b>Match:</b> {_esc(score_note)}", st["sub"]))

    if c.summary:
        fl += [Paragraph("SUMMARY", st["h2"]), Paragraph(_esc(c.summary[:1600]), st["body"])]

    if c.experiences:
        fl.append(Paragraph("EXPERIENCE", st["h2"]))
        for e in sorted(c.experiences, key=lambda x: (x.start or date.min), reverse=True):
            fl.append(Paragraph(_esc(e.title or "Role"), st["role"]))
            span = f"{e.start_date or '?'} – {'Present' if e.is_current else (e.end_date or '?')}"
            sub = " · ".join(filter(None, [e.company, e.location, span,
                                           f"{e.months()/12:.1f} yrs" if e.months() else ""]))
            fl.append(Paragraph(_esc(sub), st["sub"]))
            if e.description:
                fl.append(Paragraph(_esc(e.description[:900]), st["body"]))
            fl.append(Spacer(1, 3))

    if c.educations:
        fl.append(Paragraph("EDUCATION", st["h2"]))
        for ed in c.educations:
            line = " · ".join(filter(None, [
                " ".join(filter(None, [ed.degree, ed.field_of_study])),
                ed.institution, str(ed.end_year or "")]))
            fl.append(Paragraph(_esc(line), st["body"]))

    sk = sorted(c.skills())
    if sk:
        fl += [Paragraph("SKILLS (normalised)", st["h2"]),
               Paragraph(_esc(", ".join(s.replace("x:", "").replace("_", " ") for s in sk)),
                         st["body"])]

    fl += [Spacer(1, 10),
           HRFlowable(width="100%", thickness=0.4, color=colors.HexColor("#E5E7EB"), spaceAfter=3),
           Paragraph(_esc(provenance or
                          f"Source: {c.source or 'unknown'} ({c.source_ref or 'n/a'}) · "
                          f"compiled {date.today().isoformat()} · "
                          f"lawful basis: {cfg().get('retention.lawful_basis', 'n/a')}"),
                     st["foot"])]
    doc.build(fl)
    return dest


def store_pdf(c: Candidate, src: str | Path | None = None,
              candidate_key: str = "") -> tuple[Path, str, bool]:
    """Put a candidate's PDF in the library. -> (path, sha256, reused_existing).

    If `src` is given it is copied; otherwise a clean one is rendered.
    Identical content is never stored twice.
    """
    root = cfg().path_of("storage.profiles_dir")
    key = candidate_key or c.person_key()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{norm_text(c.full_name).replace(' ', '-')}_{key}")[:120]
    folder = root / date.today().strftime("%Y-%m")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{safe}.pdf"

    if src and Path(src).exists() and Path(src).suffix.lower() == ".pdf":
        sha = sha256_file(src)
        existing = next((p for p in root.rglob("*.pdf")
                         if p.name == dest.name and sha256_file(p) == sha), None)
        if existing:
            return existing, sha, True
        shutil.copy2(src, dest)
        return dest, sha, False

    render_profile_pdf(c, dest)
    return dest, sha256_file(dest), False
