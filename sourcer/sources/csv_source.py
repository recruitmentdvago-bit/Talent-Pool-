"""CSV / Excel intake.

The widest-compatibility route: anything you can get into a spreadsheet becomes
a scored candidate. Column names are matched loosely, so exports from different
tools usually just work.

Compact sub-formats inside a cell:
  skills       SQL; Power BI; Python                (';' or ',' separated)
  experiences  Title | Company | Start | End | Description      (' ;; ' between roles)
  educations   Degree | Field | Institution | EndYear           (' ;; ' between entries)
A cell starting with '[' is parsed as JSON instead.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd

from .. import events
from ..config import cfg
from ..models import Candidate, Education, Experience
from ..normalize import norm_text
from .base import BaseSource, FetchContext, FetchResult

ALIASES = {
    "full_name": ["full_name", "name", "candidate", "candidate_name", "person", "fullname"],
    "headline": ["headline", "title", "current_title", "job_title", "position", "designation"],
    "summary": ["summary", "about", "bio", "profile_summary", "overview"],
    "location_raw": ["location", "location_raw", "city", "address", "geo", "region"],
    "linkedin_url": ["linkedin_url", "linkedin", "profile_url", "url", "link", "public_profile_url"],
    "email": ["email", "email_address", "e-mail", "mail"],
    "phone": ["phone", "mobile", "contact", "phone_number", "cell"],
    "skills": ["skills", "skill", "key_skills", "skillset", "technologies", "tech_stack"],
    "experiences": ["experiences", "experience", "work_history", "roles", "employment"],
    "educations": ["educations", "education", "qualifications", "degrees"],
    "company": ["company", "current_company", "employer", "organisation", "organization"],
    "years": ["years", "total_years", "experience_years", "yoe", "years_of_experience"],
    "pdf_path": ["pdf_path", "profile_pdf", "resume_path", "cv_path", "attachment"],
}


def _colmap(columns) -> dict[str, str]:
    lower = {norm_text(c).replace(" ", "_"): c for c in columns}
    out = {}
    for canon, names in ALIASES.items():
        for n in names:
            if n in lower:
                out[canon] = lower[n]
                break
    return out


def _cell(row, colmap, key, default="") -> str:
    col = colmap.get(key)
    if not col:
        return default
    v = row.get(col)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    return str(v).strip()


def _split_multi(cell: str) -> list[str]:
    if not cell:
        return []
    return [p.strip() for p in cell.replace(";;", "\x00").split("\x00") if p.strip()] \
        if ";;" in cell else [cell.strip()]


def parse_experiences(cell: str) -> list[Experience]:
    if not cell:
        return []
    if cell.lstrip().startswith("["):
        try:
            return [Experience(
                title=d.get("title", ""), company=d.get("company", ""),
                location=d.get("location", ""), start_date=str(d.get("start_date", "")),
                end_date=str(d.get("end_date", "")),
                is_current=bool(d.get("is_current")) or norm_text(d.get("end_date")) in {"", "present", "current"},
                description=d.get("description", "")) for d in json.loads(cell)]
        except (ValueError, AttributeError):
            return []
    out = []
    for chunk in _split_multi(cell):
        f = [p.strip() for p in chunk.split("|")]
        f += [""] * (5 - len(f))
        end = f[3]
        out.append(Experience(title=f[0], company=f[1], start_date=f[2], end_date=end,
                              is_current=norm_text(end) in {"", "present", "current", "now"},
                              description=f[4]))
    return out


def parse_educations(cell: str) -> list[Education]:
    if not cell:
        return []
    if cell.lstrip().startswith("["):
        try:
            return [Education(degree=d.get("degree", ""), field_of_study=d.get("field", ""),
                              institution=d.get("institution", ""),
                              end_year=d.get("end_year")) for d in json.loads(cell)]
        except (ValueError, AttributeError):
            return []
    out = []
    for chunk in _split_multi(cell):
        f = [p.strip() for p in chunk.split("|")]
        f += [""] * (4 - len(f))
        year = None
        if f[3].isdigit():
            year = int(f[3])
        out.append(Education(degree=f[0], field_of_study=f[1], institution=f[2], end_year=year))
    return out


def rows_to_candidates(df: pd.DataFrame, source_name: str, ref: str = "") -> list[Candidate]:
    cm = _colmap(df.columns)
    out: list[Candidate] = []
    for i, row in df.iterrows():
        name = _cell(row, cm, "full_name")
        if not name:
            continue
        skills_cell = _cell(row, cm, "skills")
        skills = [s.strip() for s in skills_cell.replace(";", ",").split(",") if s.strip()]
        exps = parse_experiences(_cell(row, cm, "experiences"))
        if not exps:
            company = _cell(row, cm, "company")
            headline = _cell(row, cm, "headline")
            if company or headline:
                exps = [Experience(title=headline, company=company, start_date="", is_current=True)]
        out.append(Candidate(
            full_name=name,
            headline=_cell(row, cm, "headline"),
            summary=_cell(row, cm, "summary"),
            location_raw=_cell(row, cm, "location_raw"),
            linkedin_url=_cell(row, cm, "linkedin_url"),
            email=_cell(row, cm, "email"),
            phone=_cell(row, cm, "phone"),
            skills_raw=skills,
            experiences=exps,
            educations=parse_educations(_cell(row, cm, "educations")),
            source=source_name,
            source_ref=ref or f"row:{i}",
            profile_pdf_path=_cell(row, cm, "pdf_path"),
            raw={str(k): (None if pd.isna(v) else str(v)) for k, v in row.items()},
        ))
    return out


class CsvSource(BaseSource):
    """Watches a folder for .csv/.xlsx drops, then archives what it read."""
    name = "csv"

    def __init__(self, watch_dir: str | Path | None = None):
        self.watch_dir = Path(watch_dir) if watch_dir else \
            cfg().root / cfg().get("sources.csv.watch_dir", "storage/imports")

    def available(self) -> tuple[bool, str]:
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        files = self._files()
        return (bool(files), f"{len(files)} file(s) waiting in {self.watch_dir}")

    def _files(self) -> list[Path]:
        return sorted(p for p in self.watch_dir.glob("*")
                      if p.suffix.lower() in {".csv", ".xlsx", ".xls", ".tsv"}
                      and not p.name.startswith("."))

    def fetch(self, ctx: FetchContext) -> FetchResult:
        res = FetchResult()
        done = self.watch_dir / "processed"
        for path in self._files():
            try:
                events.emit("extract", f"Reading the spreadsheet {path.name}")
                if path.suffix.lower() in {".xlsx", ".xls"}:
                    df = pd.read_excel(path, dtype=str)
                else:
                    df = pd.read_csv(path, dtype=str,
                                     sep="\t" if path.suffix.lower() == ".tsv" else ",")
                cands = rows_to_candidates(df, self.name, ref=path.name)
                res.candidates.extend(cands[: ctx.max_results])
                res.notes.append(f"{path.name}: {len(cands)} rows")
                events.emit("parse", f"{path.name}: {len(cands)} row(s) turned into candidate profiles")
                if not ctx.dry_run:
                    done.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(done / path.name))
            except Exception as e:
                res.errors.append(f"{path.name}: {type(e).__name__}: {e}")
                events.emit("error", f"Failed to read {path.name}: {type(e).__name__}: {e}", level="error")
        return res
