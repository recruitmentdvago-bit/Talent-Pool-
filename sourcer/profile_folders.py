"""Candidate Profiles folder: every extracted profile's PDF, filed in one folder per vacancy.

    Candidate Profiles/
        Data Analyst/        <- every profile found while searching for that vacancy
        Digital Marketing Manager/
        _Unsorted/           <- profiles that belong to no open vacancy
        _index.csv           <- name, vacancy, score, role, company, city, LinkedIn link, file

It is a plain copy for people: the database and the portal stay the source of truth. Running it
again only adds what is new.
"""
from __future__ import annotations

import csv
import os
import re
import shutil
from pathlib import Path

from .config import cfg
from .db import jload, session

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def default_folder() -> Path:
    v = os.environ.get("CANDIDATE_PROFILES_FOLDER", "").strip() or str(cfg().get("collector.profiles_folder", "Candidate Profiles"))
    p = Path(v).expanduser()
    return p if p.is_absolute() else cfg().root / p


def _safe(name: str, limit: int = 60) -> str:
    s = _BAD.sub("_", (name or "").strip()).strip(" .")
    return (s[:limit].rstrip(" .") or "_")


def export(folder: str | Path | None = None, log=print) -> dict:
    """Copy every candidate's PDF into <folder>/<vacancy title>/. Returns counts."""
    root = Path(folder) if folder else default_folder()
    root.mkdir(parents=True, exist_ok=True)
    out = {"folder": str(root), "copied": 0, "already": 0, "no_pdf": 0, "vacancies": {}}
    with session() as conn:
        titles = {r["code"]: r["title"] for r in conn.execute("SELECT code, title FROM jds ORDER BY active DESC, code")}
        dup = {t for t in titles.values() if list(titles.values()).count(t) > 1}
        names = {c: (f"{t} ({c})" if t in dup else t) for c, t in titles.items()}
        rows = conn.execute("""
            SELECT c.id, c.full_name, c.current_title, c.current_company, c.location_city, c.linkedin_url,
                   c.profile_pdf_path, c.raw_json,
                   (SELECT j.code FROM scores s JOIN jds j ON j.id=s.jd_id AND j.version=s.jd_version
                     WHERE s.candidate_id=c.id ORDER BY s.score DESC LIMIT 1) AS best_code,
                   (SELECT MAX(s.score) FROM scores s WHERE s.candidate_id=c.id) AS best_score
            FROM candidates c ORDER BY c.id""").fetchall()
    index = []
    for r in rows:
        code = (jload(r["raw_json"], {}) or {}).get("search_vacancy")
        if code not in titles:
            code = r["best_code"] if r["best_code"] in titles else None
        sub = _safe(names[code]) if code else "_Unsorted"
        src = Path(r["profile_pdf_path"] or "")
        if not src.is_file():
            out["no_pdf"] += 1
            continue
        dest_dir = root / sub
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / _safe(src.name, 120)
        if dest.is_file() and dest.stat().st_size == src.stat().st_size:
            out["already"] += 1
        else:
            shutil.copy2(src, dest)
            out["copied"] += 1
        out["vacancies"][sub] = out["vacancies"].get(sub, 0) + 1
        index.append([r["full_name"], sub, round(r["best_score"] or 0, 1), r["current_title"] or "", r["current_company"] or "",
                      r["location_city"] or "", r["linkedin_url"] or "", f"{sub}/{dest.name}"])
    with open(root / "_index.csv", "w", newline="", encoding="utf-8-sig") as f:        # utf-8-sig: opens cleanly in Excel
        w = csv.writer(f)
        w.writerow(["Name", "Vacancy folder", "Score (this PC)", "Current role", "Company", "City", "LinkedIn", "File"])
        w.writerows(sorted(index, key=lambda x: (x[1], -x[2])))
    log(f"Candidate Profiles folder: {out['copied']} new PDF(s) copied, {out['already']} already there"
        f"{', ' + str(out['no_pdf']) + ' without a PDF' if out['no_pdf'] else ''} -> {root}")
    return out
