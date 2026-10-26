"""Asking the bot for people, and getting them out in a usable shape."""
from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import cfg
from .db import jload
from .normalize import norm_text, titles_index
from .pdfstore import _styles, _esc
from .scoring import ScoreResult


@dataclass
class Row:
    candidate_id: int
    name: str
    current_title: str
    current_company: str
    city: str
    total_years: float
    jd_code: str
    jd_title: str
    score: float
    rubric: float
    semantic: float
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    rationale: str = ""
    breakdown: dict = field(default_factory=dict)
    pdf_path: str = ""
    linkedin_url: str = ""
    email: str = ""
    phone: str = ""
    status: str = "new"
    last_seen: str = ""
    found_for: str = ""            # the vacancy code the bot was searching for when it found this person

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        for k in ("matched", "missing", "flags"):
            d[k] = ", ".join(x.replace("x:", "").replace("_", " ") for x in d[k])
        d.pop("breakdown", None)
        return d


def resolve_jds(conn: sqlite3.Connection, role: str | None) -> list[sqlite3.Row]:
    """Match a free-text role ('data analyst/data engineer', 'DA-001') to vacancies."""
    rows = conn.execute("SELECT * FROM jds WHERE active=1").fetchall()
    if not role or norm_text(role) in {"all", "any", "*"}:
        return rows
    wanted = [w.strip() for w in re.split(r"[/,;|]| or ", role, flags=re.I) if w.strip()]
    ti = titles_index()
    hits: list[sqlite3.Row] = []
    for w in wanted:
        # accept "data-analyst" and "data_analyst" as well as "data analyst";
        # people type the slug or the family name as often as the title
        loose = re.sub(r"[-_]+", " ", w)
        nw = norm_text(loose)
        fam = ti.family_of(loose) or ti.family_of(w)
        for r in rows:
            if r in hits:
                continue
            code_n = norm_text(r["code"])
            title_n = norm_text(r["title"])
            if (nw in {code_n, re.sub(r"[-_]+", " ", code_n)}
                    or nw in title_n or title_n in nw
                    or (fam and fam == (r["title_family"] or ""))):
                hits.append(r)
    return hits


def query(conn: sqlite3.Connection, role: str | None = None, min_score: float | None = None,
          limit: int = 50, status: str | None = None, city: str | None = None,
          max_score: float | None = None, include_contacted: bool = True,
          only_codes: set[str] | None = None, found_for: str | None = None) -> list[Row]:
    """Best match per candidate across the vacancies that `role` resolves to."""
    jds = resolve_jds(conn, role)
    if only_codes is not None:                       # a HOD: best match among *their* vacancies only
        jds = [r for r in jds if r["code"] in only_codes]
    if not jds:
        return []
    if min_score is None:
        min_score = float(cfg().get("scoring.default_shortlist_threshold", 75))
    jd_ids = [r["id"] for r in jds]
    jd_by_id = {r["id"]: r for r in jds}
    ph = ",".join("?" * len(jd_ids))

    sql = f"""
        SELECT s.*, c.full_name, c.current_title, c.current_company, c.location_city,
               c.total_years, c.profile_pdf_path, c.linkedin_url, c.email, c.phone,
               c.status, c.last_seen_at, c.raw_json
        FROM scores s
        JOIN candidates c ON c.id = s.candidate_id
        JOIN jds j ON j.id = s.jd_id AND j.version = s.jd_version
        WHERE s.jd_id IN ({ph}) AND s.score >= ?
    """
    params: list = [*jd_ids, min_score]
    if max_score is not None:
        sql += " AND s.score <= ?"
        params.append(max_score)
    if status:
        sql += " AND c.status = ?"
        params.append(status)
    elif not include_contacted:
        sql += " AND c.status NOT IN ('contacted','rejected','hired')"
    if city:
        sql += " AND LOWER(c.location_city) LIKE ?"
        params.append(f"%{city.lower()}%")
    sql += " ORDER BY s.score DESC, s.semantic DESC"

    best: dict[int, Row] = {}
    for r in conn.execute(sql, params):
        cid = r["candidate_id"]
        found = (jload(r["raw_json"], {}) or {}).get("search_vacancy", "") if r["raw_json"] else ""
        if found_for and found != found_for:
            continue
        if cid in best and best[cid].score >= r["score"]:
            continue
        j = jd_by_id[r["jd_id"]]
        best[cid] = Row(
            candidate_id=cid, name=r["full_name"],
            current_title=r["current_title"] or "", current_company=r["current_company"] or "",
            city=r["location_city"] or "", total_years=r["total_years"] or 0.0,
            jd_code=j["code"], jd_title=j["title"],
            score=r["score"], rubric=r["rubric_score"] or 0.0, semantic=r["semantic"] or 0.0,
            matched=jload(r["matched"]), missing=jload(r["missing"]), flags=jload(r["flags"]),
            rationale=r["rationale"] or "", breakdown=jload(r["breakdown"], {}),
            pdf_path=r["profile_pdf_path"] or "", linkedin_url=r["linkedin_url"] or "",
            email=r["email"] or "", phone=r["phone"] or "",
            status=r["status"] or "new", last_seen=r["last_seen_at"] or "", found_for=found)
    return sorted(best.values(), key=lambda x: (-x.score, -x.semantic))[:limit]


def as_table(rows: list[Row], width: int = 150) -> str:
    if not rows:
        return "no candidates matched."
    head = (f"{'#':<4}{'id':<6}{'score':<7}{'name':<24}{'current role':<30}"
            f"{'company':<22}{'yrs':<6}{'city':<12}{'vacancy':<10}{'status':<10}")
    out = [head, "-" * min(width, len(head))]
    for i, r in enumerate(rows, 1):
        out.append(f"{i:<4}{r.candidate_id:<6}{r.score:<7.1f}{r.name[:23]:<24}"
                   f"{(r.current_title or '-')[:29]:<30}{(r.current_company or '-')[:21]:<22}"
                   f"{r.total_years:<6.1f}{(r.city or '-')[:11]:<12}{r.jd_code[:9]:<10}"
                   f"{r.status:<10}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #
def _out_dir() -> Path:
    d = cfg().path_of("storage.exports_dir")
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_csv(rows: list[Row], path: str | Path | None = None) -> Path:
    import pandas as pd
    p = Path(path) if path else _out_dir() / f"shortlist_{date.today().isoformat()}.csv"
    pd.DataFrame([r.as_dict() for r in rows]).to_csv(p, index=False)
    return p


def export_xlsx(rows: list[Row], path: str | Path | None = None) -> Path:
    """Shortlist sheet plus a per-dimension score sheet, auto-sized and frozen."""
    import pandas as pd
    p = Path(path) if path else _out_dir() / f"shortlist_{date.today().isoformat()}.xlsx"
    main = pd.DataFrame([r.as_dict() for r in rows])
    dims: list[dict] = []
    for r in rows:
        row = {"candidate_id": r.candidate_id, "name": r.name,
               "vacancy": r.jd_code, "score": r.score}
        for dim, v in (r.breakdown or {}).items():
            row[dim] = round((v or {}).get("raw", 0) * 100, 1)
        dims.append(row)
    with pd.ExcelWriter(p, engine="openpyxl") as xl:
        main.to_excel(xl, sheet_name="shortlist", index=False)
        pd.DataFrame(dims).to_excel(xl, sheet_name="score detail", index=False)
        for sheet, df in (("shortlist", main), ("score detail", pd.DataFrame(dims))):
            ws = xl.sheets[sheet]
            ws.freeze_panes = "A2"
            for i, col in enumerate(df.columns, 1):
                vals = [str(col)] + [str(x) for x in df[col].head(200).tolist()]
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = \
                    min(46, max(10, max(len(v) for v in vals) + 2))
    return p


def export_pdf_bundle(rows: list[Row], out: str | Path | None = None,
                      label: str = "") -> Path:
    """A folder with every shortlisted PDF plus a cover sheet - what you hand
    to a hiring manager."""
    folder = Path(out) if out else _out_dir() / f"bundle_{label or 'shortlist'}_{date.today().isoformat()}"
    folder.mkdir(parents=True, exist_ok=True)
    copied = 0
    missing = 0
    for i, r in enumerate(rows, 1):
        if not r.pdf_path:
            missing += 1
            continue
        src = Path(r.pdf_path)
        if src.is_file():
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", r.name)[:60]
            shutil.copy2(src, folder / f"{i:02d}_{int(r.score)}pct_{safe}.pdf")
            copied += 1
        else:
            missing += 1
    _cover_sheet(rows, folder / "00_cover_sheet.pdf", label=label, copied=copied)
    if missing:
        (folder / "MISSING_PDFS.txt").write_text(
            f"{missing} of {len(rows)} shortlisted candidates have no stored PDF.\n"
            "Run 'sourcer run' to file one for every candidate.\n")
    return folder


def _cover_sheet(rows: list[Row], dest: Path, label: str = "", copied: int = 0) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    st = _styles()
    doc = SimpleDocTemplate(str(dest), pagesize=landscape(A4), title="Shortlist cover sheet",
                            leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm)
    fl = [Paragraph(f"Shortlist{' - ' + _esc(label) if label else ''}", st["name"]),
          Paragraph(f"{len(rows)} candidate(s), {copied} profile PDF(s) attached · "
                    f"generated {date.today().isoformat()} by hrbp-sourcer", st["meta"]),
          Spacer(1, 4)]
    data = [["#", "Score", "Name", "Current role", "Company", "Yrs", "City",
             "Vacancy", "Key gaps"]]
    for i, r in enumerate(rows, 1):
        data.append([str(i), f"{r.score:.0f}", r.name[:26], (r.current_title or "-")[:28],
                     (r.current_company or "-")[:20], f"{r.total_years:.1f}",
                     (r.city or "-")[:12], r.jd_code,
                     ", ".join(m.replace("_", " ") for m in r.missing[:3]) or "-"])
    t = Table(data, repeatRows=1, colWidths=[9 * mm, 14 * mm, 44 * mm, 50 * mm, 36 * mm,
                                             11 * mm, 22 * mm, 20 * mm, 52 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.6),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111827")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#E5E7EB")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFAFA")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    fl += [t, Spacer(1, 6),
           Paragraph("Scores are produced by a documented rubric (skills, relevant years, "
                     "title fit, seniority, domain, education, location) blended with text "
                     "similarity. Every score is explainable - run "
                     "<b>sourcer show &lt;id&gt;</b> for the full breakdown.", st["foot"])]
    doc.build(fl)
    return dest
