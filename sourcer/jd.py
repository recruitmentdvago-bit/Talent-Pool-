"""Turn a job description file into a structured JD.

Two paths:
  1. YAML front-matter (recommended) - you state the requirements explicitly.
  2. Free text - the parser infers title, years, education and skills.
Front-matter always wins over inference.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .config import cfg
from .db import audit, jdump, now
from .models import JD, edu_level
from .normalize import norm_text, seniority_of, skills_index, titles_index

FRONTMATTER = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

YEARS_PATTERNS = [
    re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:to|-|–|and)\s*(\d{1,2})\s*(?:\+)?\s*year", re.I),
    re.compile(r"minimum\s+(?:of\s+)?(\d{1,2})\s*(?:\+)?\s*year", re.I),
    re.compile(r"at\s+least\s+(\d{1,2})\s*(?:\+)?\s*year", re.I),
    re.compile(r"(\d{1,2})\s*\+\s*year", re.I),
    re.compile(r"(\d{1,2})\s*years?\s+(?:of\s+)?(?:relevant\s+|professional\s+|hands[- ]on\s+)?experience", re.I),
]

SECTION_HEADS = {
    "must": re.compile(r"(must[- ]?have|requirements?|required (?:skills|qualifications)|"
                       r"qualifications|essential|you (?:will )?(?:need|have)|eligibility)", re.I),
    "nice": re.compile(r"(nice[- ]?to[- ]?have|preferred|desirable|bonus|plus points|good to have|"
                       r"advantageous)", re.I),
    "resp": re.compile(r"(responsibilit|duties|what you.ll do|role overview|job purpose|about the role)", re.I),
}


# --------------------------------------------------------------------------- #
# Template fields: "Job title: Data Analyst" lines (Word template) and the same
# names as spreadsheet headers (Excel template) map onto the same vacancy fields.
# --------------------------------------------------------------------------- #
FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "title": ("job title", "title", "designation", "position", "role"),
    "code": ("code", "vacancy code", "job code"),
    "department": ("department", "dept"),
    "seniority": ("level", "seniority"),
    "min_years": ("min years", "minimum years", "min experience", "minimum experience", "min exp"),
    "max_years": ("max years", "maximum years", "max experience", "max exp"),
    "education_min": ("minimum education", "min education", "education"),
    "openings": ("openings", "positions", "headcount", "no of positions"),
    "location": ("location", "city"),
    "must_have": ("must have skills", "must have", "required skills", "must haves"),
    "nice_have": ("nice to have skills", "nice to have", "nice have skills", "good to have"),
    "hard_requirements": ("non negotiable skills", "non negotiable", "hard requirements", "mandatory skills"),
    "industries": ("industries", "industries preferred", "industry"),
    "preferred_employers": ("preferred employers", "preferred companies", "extra preferred employers"),
    "extra_titles": ("extra titles", "also search titles", "extra search titles", "alternate titles"),
    "priority": ("priority",),
    "text": ("job description", "description", "jd", "about the role"),
}
def _label_key(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", norm_text(s)))


_LABEL_TO_FIELD = {_label_key(a): f for f, aliases in FIELD_LABELS.items() for a in aliases}
_SENIORITY_VALUES = {"intern", "junior", "mid", "senior", "lead", "manager", "director", "executive"}


def field_for_label(label: str) -> str | None:
    return _LABEL_TO_FIELD.get(_label_key(label.replace("(optional)", "")))


def clean_field_values(raw: dict) -> dict:
    """Normalise values typed into a template (either kind) into _save_vacancy form data."""
    out: dict = {}
    for k, v in raw.items():
        if v is None or (isinstance(v, float) and v != v):
            continue
        v = str(v).strip() if not isinstance(v, (int, float)) else v
        if v == "":
            continue
        out[k] = v
    for k in ("min_years", "max_years"):
        if k in out:
            m = re.search(r"\d+(?:\.\d+)?", str(out[k]))
            if m:
                out[k] = float(m.group())
            else:
                out.pop(k)
    for k in ("openings", "priority"):
        if k in out:
            m = re.search(r"\d+", str(out[k]))
            out[k] = int(m.group()) if m else out.pop(k) and None
            if out.get(k) is None:
                out.pop(k, None)
    if "seniority" in out:
        sv = norm_text(str(out["seniority"]))
        if sv in _SENIORITY_VALUES:
            out["seniority"] = sv
        else:
            out.pop("seniority")
    if "education_min" in out:
        ev = norm_text(str(out["education_min"]))
        if ev in {"intermediate", "diploma", "bachelors", "masters"}:
            out["education_min"] = ev
        else:
            lvl = edu_level(str(out["education_min"]))
            if lvl and lvl != "none":
                out["education_min"] = lvl
            else:
                out.pop("education_min")
    return out


def labelled_fields(text: str) -> tuple[dict, str]:
    """Pick `Label: value` lines out of a filled-in template. Only trusted when at
    least two known labels appear near the top - an ordinary JD that happens to have
    a "Location:" line is left to the normal reader. Returns (fields, description)."""
    lines = text.splitlines()
    found: dict[str, str] = {}
    body_from = None
    for i, line in enumerate(lines[:60]):
        m = re.match(r"^\s*[-*\u2022]?\s*([A-Za-z][A-Za-z /&()*-]{1,40}?)\s*:\s*(.*)$", line)
        if not m:
            continue
        f = field_for_label(m.group(1))
        if not f:
            continue
        if f == "text":
            body_from = i
            first = m.group(2).strip()
            if first:
                lines[i] = first
                body_from = i
            else:
                body_from = i + 1
            break
        found.setdefault(f, m.group(2).strip())
    if len(found) < 2:
        return {}, text
    body = "\n".join(lines[body_from:]).strip() if body_from is not None else ""
    return clean_field_values(found), body


def _is_heading_shape(line: str) -> bool:
    """Does this line look like a heading at all (as opposed to body text)?"""
    t = line.strip()
    if not t:
        return False
    stripped = t.lstrip("#*-\u2022 ").rstrip(":").strip()
    if not stripped or len(stripped) > 60:
        return False
    if t.startswith("#") or t.endswith(":") or stripped.isupper():
        return True
    if t.lstrip().startswith(("-", "*", "\u2022")):
        return False
    # Sentence-final punctuation means body text, however short the line is
    if stripped.endswith((".", "!", "?", ",", ";")):
        return False
    return len(stripped.split()) <= 7


def _split_sections(text: str) -> dict[str, str]:
    """Bucket the JD body into must / nice / resp / other by heading.

    A heading is recognised by matching one of SECTION_HEADS first and only
    then by its shape - checking the shape first wrongly rejects real headings
    whose capitalisation is not Title Case ("Nice to have", "nice to have"),
    which silently files nice-to-have skills as must-haves.
    """
    buckets: dict[str, list[str]] = {"must": [], "nice": [], "resp": [], "other": []}
    cur = "other"
    for line in text.splitlines():
        stripped = line.strip().lstrip("#*-\u2022 ").rstrip(":").strip()
        if stripped and len(stripped) <= 60:
            matched = next((k for k, rx in SECTION_HEADS.items() if rx.search(stripped)), None)
            if matched and not line.lstrip().startswith(("-", "*", "\u2022")):
                cur = matched
                continue
            if matched is None and _is_heading_shape(line):
                cur = "other"
                buckets[cur].append(line)
                continue
        buckets[cur].append(line)
    return {k: "\n".join(v) for k, v in buckets.items()}


def _years(text: str) -> tuple[float, float | None]:
    for rx in YEARS_PATTERNS:
        m = rx.search(text)
        if m:
            groups = [g for g in m.groups() if g]
            lo = float(groups[0])
            hi = float(groups[1]) if len(groups) > 1 else None
            if hi is not None and hi < lo:
                lo, hi = hi, lo
            return lo, hi
    return 0.0, None


def parse_text(text: str, source_file: str = "", code: str = "") -> JD:
    body, meta = text, {}
    m = FRONTMATTER.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        body = m.group(2)
    else:
        fields, desc = labelled_fields(text)
        if fields:
            meta = {k: v for k, v in fields.items() if k != "text"}
            body = desc or ""

    si, ti = skills_index(), titles_index()
    sections = _split_sections(body)

    # ---- title ---------------------------------------------------------- #
    title = meta.get("title")
    if not title:
        for line in body.splitlines():
            s = line.strip().lstrip("# ").strip()
            if s and len(s) < 90:
                title = s
                break
    title = (title or Path(source_file).stem.replace("_", " ").replace("-", " ").title()).strip()

    # ---- skills --------------------------------------------------------- #
    def listify(v) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [p.strip() for p in re.split(r"[,;\n]", v) if p.strip()]
        return [str(x).strip() for x in v if str(x).strip()]

    must = listify(meta.get("must_have"))
    nice = listify(meta.get("nice_have"))
    if not must:
        inferred = si.extract(sections["must"] or body, context="prose")
        # responsibilities are weaker evidence - treat as nice-to-have only
        must = sorted(inferred)
    if not nice:
        nice = sorted(
            si.extract(sections["nice"], context="prose")
            | (si.extract(sections["resp"], context="prose") - set(must))
        )
    nice = [n for n in nice if n not in set(must)]

    # ---- years / seniority / education ---------------------------------- #
    min_y, max_y = _years(body)
    if meta.get("min_years") is not None:
        min_y = float(meta["min_years"])
    if meta.get("max_years") is not None:
        max_y = float(meta["max_years"])

    seniority = meta.get("seniority") or seniority_of(title)
    family = meta.get("title_family") or ti.family_of(title)
    edu_min = meta.get("education_min") or edu_level(sections["must"] or body)

    jd = JD(
        code=str(meta.get("code") or code or norm_text(title).replace(" ", "-")[:40]),
        title=title,
        title_family=family,
        seniority=seniority,
        location=meta.get("location") or cfg().get("location.target_city", "Karachi") + ", "
                 + cfg().get("location.target_country", "Pakistan"),
        min_years=min_y,
        max_years=max_y,
        hard_min_years=bool(meta.get("hard_min_years", False)),
        education_min=edu_min,
        openings=int(meta.get("openings") or 1),
        industries=listify(meta.get("industries")),
        must_have=must,
        nice_have=nice,
        hard_requirements=listify(meta.get("hard_requirements")),
        preferred_employers=listify(meta.get("preferred_employers")),
        extra_titles=listify(meta.get("extra_titles")),
        search_skills_count=int(meta.get("search_skills_count", 2)),
        priority=int(meta.get("priority", 0)),
        department=str(meta.get("department") or "").strip(),
        raw_text=body.strip(),
        source_file=source_file,
        active=bool(meta.get("active", True)),
    )
    return jd


def docx_to_text(data: bytes) -> str:
    """Plain text from a .docx, with no extra dependency. A .docx is a zip with
    word/document.xml; paragraphs and table cells are read in order."""
    import io
    import xml.etree.ElementTree as ET
    import zipfile
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("that Word file is too large")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "word/document.xml" not in z.namelist():
            raise ValueError("not a Word (.docx) file")
        info = z.getinfo("word/document.xml")
        if info.file_size > 40 * 1024 * 1024:
            raise ValueError("that Word file is too large")
        xml = z.read("word/document.xml")
    if b"<!DOCTYPE" in xml[:2000] or b"<!ENTITY" in xml[:20000]:
        raise ValueError("that Word file looks unsafe and was not opened")
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(xml)
    lines = []
    for para in root.iter(ns + "p"):
        parts = []
        for node in para.iter():
            if node.tag == ns + "t" and node.text:
                parts.append(node.text)
            elif node.tag == ns + "tab":
                parts.append("\t")
            elif node.tag == ns + "br":
                parts.append("\n")
        text = "".join(parts).strip()
        # bulleted/numbered paragraphs carry numPr; keep them recognisable as list items
        if text and para.find(f"{ns}pPr/{ns}numPr") is not None:
            text = "- " + text
        lines.append(text)
    return "\n".join(lines)


def file_bytes_to_text(name: str, data: bytes) -> str:
    """Text of an uploaded JD: .pdf, .docx, .txt, .md. (Old binary .doc is not supported.)"""
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((pg.extract_text() or "") for pg in pdf.pages)
    if ext == ".docx":
        return docx_to_text(data)
    if ext == ".doc":
        raise ValueError("old .doc files cannot be read - in Word use Save As > .docx (or PDF)")
    if ext in {".txt", ".md", ".markdown", ""}:
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"unsupported file type {ext} (use PDF, Word .docx, or text)")


def parse_file(path: str | Path) -> JD:
    p = Path(path)
    text = file_bytes_to_text(p.name, p.read_bytes())
    return parse_text(text, source_file=str(p))


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
JD_COLUMNS = ("code", "title", "title_family", "seniority", "location", "min_years",
              "max_years", "hard_min_years", "education_min", "industries", "must_have", "nice_have",
              "hard_requirements", "preferred_employers", "extra_titles", "search_skills_count",
              "priority", "department", "raw_text", "source_file", "content_hash",
              "version", "openings", "active")


def upsert(conn, jd: JD) -> tuple[int, bool]:
    """Insert or version-bump a JD. Returns (jd_id, changed)."""
    h = jd.content_hash()
    row = conn.execute("SELECT id, content_hash, version FROM jds WHERE code=?",
                       (jd.code,)).fetchone()
    vals = {
        "code": jd.code, "title": jd.title, "title_family": jd.title_family,
        "seniority": jd.seniority, "location": jd.location, "min_years": jd.min_years,
        "max_years": jd.max_years, "hard_min_years": int(jd.hard_min_years),
        "education_min": jd.education_min,
        "industries": jdump(jd.industries), "must_have": jdump(jd.must_have),
        "nice_have": jdump(jd.nice_have), "hard_requirements": jdump(jd.hard_requirements),
        "preferred_employers": jdump(jd.preferred_employers),
        "extra_titles": jdump(jd.extra_titles),
        "search_skills_count": jd.search_skills_count,
        "priority": jd.priority,
        "department": jd.department,
        "raw_text": jd.raw_text, "source_file": jd.source_file, "content_hash": h,
        "openings": jd.openings, "active": int(jd.active),
    }
    if row is None:
        vals["version"] = 1
        cols = ",".join(vals)
        conn.execute(
            f"INSERT INTO jds ({cols}, created_at, updated_at) "
            f"VALUES ({','.join('?' * len(vals))}, ?, ?)",
            (*vals.values(), now(), now()))
        jd_id = conn.execute("SELECT id FROM jds WHERE code=?", (jd.code,)).fetchone()[0]
        audit(conn, "jd.create", "jd", jd_id, {"code": jd.code, "version": 1})
        jd.version = 1
        return jd_id, True

    if row["content_hash"] == h:
        conn.execute("UPDATE jds SET updated_at=?, active=? WHERE id=?",
                     (now(), int(jd.active), row["id"]))
        conn.execute("UPDATE jds SET raw_text=? WHERE id=?", (jd.raw_text, row["id"]))   # wording-only edits
        if jd.department:                       # a file with no department never clears one set in the UI
            conn.execute("UPDATE jds SET department=? WHERE id=?", (jd.department, row["id"]))
        jd.version = row["version"]
        return row["id"], False

    if not jd.department:
        vals.pop("department")                  # keep whatever was assigned in the UI
    vals["version"] = row["version"] + 1
    sets = ",".join(f"{k}=?" for k in vals)
    conn.execute(f"UPDATE jds SET {sets}, updated_at=? WHERE id=?",
                 (*vals.values(), now(), row["id"]))
    audit(conn, "jd.version", "jd", row["id"], {"code": jd.code, "version": vals["version"]})
    jd.version = vals["version"]
    return row["id"], True


def load_all(conn, active_only: bool = True, effective: bool = True) -> list[tuple[int, JD]]:
    """Every vacancy. With `effective`, preferred employers and extra search titles
    include what the central rules (Companies / Search titles tabs) add to each one."""
    from .db import jload
    from .rules import Rules
    rules = Rules(conn) if effective else None
    q = "SELECT * FROM jds" + (" WHERE active=1" if active_only else "") + " ORDER BY code"
    out = []
    for r in conn.execute(q).fetchall():
        eff = rules.resolve(r["title"], r["department"] or "", jload(r["preferred_employers"]),
                            jload(r["extra_titles"])) if rules else None
        out.append((r["id"], JD(
            code=r["code"], title=r["title"], title_family=r["title_family"],
            seniority=r["seniority"] or "mid", location=r["location"] or "",
            min_years=r["min_years"] or 0.0, max_years=r["max_years"],
            hard_min_years=bool(r["hard_min_years"]),
            education_min=r["education_min"] or "none", openings=r["openings"] or 1,
            industries=jload(r["industries"]), must_have=jload(r["must_have"]),
            nice_have=jload(r["nice_have"]), hard_requirements=jload(r["hard_requirements"]),
            preferred_employers=eff["employers"] if eff else jload(r["preferred_employers"]),
            extra_titles=eff["titles"] if eff else jload(r["extra_titles"]),
            search_skills_count=int(2 if r["search_skills_count"] is None else r["search_skills_count"]),
            priority=int(r["priority"] or 0),
            department=(eff["department"] if eff else "") or r["department"] or "",
            raw_text=r["raw_text"] or "", source_file=r["source_file"] or "",
            version=r["version"] or 1, active=bool(r["active"]),
        )))
    return out


def sync_dir(conn, directory: str | Path | None = None) -> list[tuple[int, JD, bool]]:
    """Read every JD file in config/jds and upsert it."""
    d = Path(directory) if directory else cfg().path_of("jd_dir")
    d.mkdir(parents=True, exist_ok=True)
    results = []
    for p in sorted(d.iterdir()):
        if p.suffix.lower() not in {".md", ".txt", ".pdf", ".markdown", ".docx"} or p.name.startswith("."):
            continue
        jd = parse_file(p)
        jd_id, changed = upsert(conn, jd)
        results.append((jd_id, jd, changed))
    try:                                       # keep each vacancy's search titles in step with its JD
        from .rules import refresh_auto_titles
        refresh_auto_titles(conn)
    except Exception:
        pass
    return results
