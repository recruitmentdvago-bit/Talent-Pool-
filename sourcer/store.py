"""Candidate persistence with identity resolution.

Re-seeing the same person updates the existing record rather than duplicating,
keyed on linkedin slug > email > (name + city + company).
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .config import cfg
from .db import audit, jdump, jload, now
from .models import Candidate, Education, Experience
from .normalize import geo_index

CAND_FIELDS = ("person_key", "full_name", "headline", "summary", "location_raw",
               "location_city", "location_country", "linkedin_url", "email", "phone",
               "current_title", "current_company", "total_years", "source", "source_ref",
               "profile_pdf_path", "pdf_sha256", "raw_json",
               "certifications_json", "projects_json", "featured_text")


def _row_values(c: Candidate) -> dict:
    city, country, _ = geo_index().classify(c.location_raw)
    return {
        "person_key": c.person_key(),
        "full_name": c.full_name.strip(),
        "headline": c.headline,
        "summary": c.summary,
        "location_raw": c.location_raw,
        "location_city": city,
        "location_country": country or cfg().get("location.target_country", ""),
        "linkedin_url": c.linkedin_url,
        "email": c.email,
        "phone": c.phone,
        "current_title": c.current_title(),
        "current_company": c.current_company(),
        "total_years": c.total_years(),
        "source": c.source,
        "source_ref": c.source_ref,
        "profile_pdf_path": c.profile_pdf_path,
        "pdf_sha256": "",
        "raw_json": jdump(c.raw or {}),
        "certifications_json": jdump(c.certifications or []),
        "projects_json": jdump(c.projects or []),
        "featured_text": c.featured_text or "",
    }


def _replace_children(conn: sqlite3.Connection, cid: int, c: Candidate) -> None:
    conn.execute("DELETE FROM experiences WHERE candidate_id=?", (cid,))
    conn.execute("DELETE FROM educations WHERE candidate_id=?", (cid,))
    conn.execute("DELETE FROM candidate_skills WHERE candidate_id=?", (cid,))
    for i, e in enumerate(c.experiences):
        conn.execute(
            "INSERT INTO experiences (candidate_id,title,company,location,start_date,"
            "end_date,is_current,description,ord) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, e.title, e.company, e.location, e.start_date, e.end_date,
             int(e.is_current), e.description, i))
    for ed in c.educations:
        conn.execute(
            "INSERT INTO educations (candidate_id,degree,field,institution,start_year,end_year) "
            "VALUES (?,?,?,?,?,?)",
            (cid, ed.degree, ed.field_of_study, ed.institution, ed.start_year, ed.end_year))
    for s in sorted(c.skills()):
        conn.execute(
            "INSERT OR REPLACE INTO candidate_skills (candidate_id,skill,weight,evidence) "
            "VALUES (?,?,?,?)", (cid, s, 1.0, "derived"))


def upsert(conn: sqlite3.Connection, c: Candidate) -> tuple[int, str]:
    """-> (candidate_id, 'created' | 'updated')."""
    vals = _row_values(c)
    key = vals["person_key"]
    retention = int(cfg().get("retention.delete_after_days", 365))
    row = conn.execute("SELECT id, profile_pdf_path FROM candidates WHERE person_key=?",
                       (key,)).fetchone()
    if row is None:
        cols = ",".join(vals)
        conn.execute(
            f"INSERT INTO candidates ({cols}, lawful_basis, delete_after, "
            f"first_seen_at, last_seen_at, updated_at) "
            f"VALUES ({','.join('?' * len(vals))},?,?,?,?,?)",
            (*vals.values(), cfg().get("retention.lawful_basis", ""),
             (date.today() + timedelta(days=retention)).isoformat(), now(), now(), now()))
        cid = conn.execute("SELECT id FROM candidates WHERE person_key=?", (key,)).fetchone()[0]
        _replace_children(conn, cid, c)
        audit(conn, "candidate.create", "candidate", cid, {"key": key, "source": c.source})
        return cid, "created"

    cid = row["id"]
    # never blank out an existing PDF path with an empty one
    if not vals["profile_pdf_path"] and row["profile_pdf_path"]:
        vals["profile_pdf_path"] = row["profile_pdf_path"]
    sets = ",".join(f"{k}=?" for k in vals)
    conn.execute(
        f"UPDATE candidates SET {sets}, last_seen_at=?, updated_at=?, delete_after=? WHERE id=?",
        (*vals.values(), now(), now(),
         (date.today() + timedelta(days=retention)).isoformat(), cid))
    _replace_children(conn, cid, c)
    audit(conn, "candidate.update", "candidate", cid, {"key": key, "source": c.source})
    return cid, "updated"


def load(conn: sqlite3.Connection, candidate_id: int) -> Candidate | None:
    r = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    return _hydrate(conn, r) if r else None


def _hydrate(conn: sqlite3.Connection, r: sqlite3.Row) -> Candidate:
    cid = r["id"]
    exps = [Experience(title=x["title"] or "", company=x["company"] or "",
                       location=x["location"] or "", start_date=x["start_date"] or "",
                       end_date=x["end_date"] or "", is_current=bool(x["is_current"]),
                       description=x["description"] or "")
            for x in conn.execute(
                "SELECT * FROM experiences WHERE candidate_id=? ORDER BY ord", (cid,))]
    edus = [Education(degree=x["degree"] or "", field_of_study=x["field"] or "",
                      institution=x["institution"] or "", start_year=x["start_year"],
                      end_year=x["end_year"])
            for x in conn.execute("SELECT * FROM educations WHERE candidate_id=?", (cid,))]
    skills = [x["skill"] for x in conn.execute(
        "SELECT skill FROM candidate_skills WHERE candidate_id=?", (cid,))]
    def _col(name, default=""):
        try:
            return r[name]
        except (IndexError, KeyError):
            return default
    c = Candidate(
        full_name=r["full_name"], headline=r["headline"] or "", summary=r["summary"] or "",
        location_raw=r["location_raw"] or "", linkedin_url=r["linkedin_url"] or "",
        email=r["email"] or "", phone=r["phone"] or "", skills_raw=skills,
        experiences=exps, educations=edus,
        certifications=jload(_col("certifications_json"), []),
        projects=jload(_col("projects_json"), []),
        featured_text=_col("featured_text") or "",
        source=r["source"] or "",
        source_ref=r["source_ref"] or "", profile_pdf_path=r["profile_pdf_path"] or "")
    c.db_id = cid           # type: ignore[attr-defined]
    c.status = r["status"]  # type: ignore[attr-defined]
    return c


def load_all(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[Candidate]:
    q = "SELECT * FROM candidates" + (f" WHERE {where}" if where else "") + " ORDER BY id"
    return [_hydrate(conn, r) for r in conn.execute(q, params).fetchall()]


def set_status(conn: sqlite3.Connection, candidate_id: int, status: str,
               note: str | None = None) -> None:
    conn.execute("UPDATE candidates SET status=?, notes=COALESCE(?, notes), updated_at=? "
                 "WHERE id=?", (status, note, now(), candidate_id))
    audit(conn, "candidate.status", "candidate", candidate_id, {"status": status, "note": note})


def purge_expired(conn: sqlite3.Connection, keep_statuses=("shortlisted", "contacted", "hired")
                  ) -> int:
    """Delete personal data past its retention date unless the candidate is in process."""
    placeholders = ",".join("?" * len(keep_statuses))
    rows = conn.execute(
        f"SELECT id FROM candidates WHERE delete_after < ? AND status NOT IN ({placeholders})",
        (date.today().isoformat(), *keep_statuses)).fetchall()
    for r in rows:
        audit(conn, "candidate.purge", "candidate", r["id"], "retention expiry")
        conn.execute("DELETE FROM candidates WHERE id=?", (r["id"],))
    return len(rows)
