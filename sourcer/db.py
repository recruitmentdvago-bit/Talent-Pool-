"""SQLite storage layer. One file, no server, safe to back up by copying."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import cfg

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    person_key      TEXT NOT NULL UNIQUE,
    full_name       TEXT NOT NULL,
    headline        TEXT,
    summary         TEXT,
    location_raw    TEXT,
    location_city   TEXT,
    location_country TEXT,
    linkedin_url    TEXT,
    email           TEXT,
    phone           TEXT,
    current_title   TEXT,
    current_company TEXT,
    total_years     REAL DEFAULT 0,
    source          TEXT,
    source_ref      TEXT,
    profile_pdf_path TEXT,
    pdf_sha256      TEXT,
    raw_json        TEXT,
    certifications_json TEXT,
    projects_json   TEXT,
    featured_text   TEXT,
    status          TEXT DEFAULT 'new',
    notes           TEXT,
    lawful_basis    TEXT,
    delete_after    TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cand_city   ON candidates(location_city);
CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_cand_name   ON candidates(full_name);

CREATE TABLE IF NOT EXISTS experiences (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    title        TEXT,
    company      TEXT,
    location     TEXT,
    start_date   TEXT,
    end_date     TEXT,
    is_current   INTEGER DEFAULT 0,
    description  TEXT,
    ord          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_exp_cand ON experiences(candidate_id);

CREATE TABLE IF NOT EXISTS educations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    degree       TEXT,
    field        TEXT,
    institution  TEXT,
    start_year   INTEGER,
    end_year     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_edu_cand ON educations(candidate_id);

CREATE TABLE IF NOT EXISTS candidate_skills (
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    skill        TEXT NOT NULL,
    weight       REAL DEFAULT 1.0,
    evidence     TEXT,
    PRIMARY KEY (candidate_id, skill)
);

CREATE TABLE IF NOT EXISTS jds (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT NOT NULL UNIQUE,
    title            TEXT NOT NULL,
    title_family     TEXT,
    seniority        TEXT,
    location         TEXT,
    min_years        REAL DEFAULT 0,
    max_years        REAL,
    hard_min_years   INTEGER DEFAULT 0,
    education_min    TEXT,
    industries       TEXT,
    must_have        TEXT,
    nice_have        TEXT,
    hard_requirements TEXT,
    preferred_employers TEXT,
    extra_titles     TEXT,
    search_skills_count INTEGER DEFAULT 2,
    priority         INTEGER DEFAULT 0,
    department       TEXT,
    raw_text         TEXT,
    source_file      TEXT,
    content_hash     TEXT,
    version          INTEGER DEFAULT 1,
    openings         INTEGER DEFAULT 1,
    active           INTEGER DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    jd_id        INTEGER NOT NULL REFERENCES jds(id) ON DELETE CASCADE,
    jd_version   INTEGER NOT NULL,
    score        REAL NOT NULL,
    rubric_score REAL,
    semantic     REAL,
    breakdown    TEXT,
    matched      TEXT,
    missing      TEXT,
    flags        TEXT,
    rationale    TEXT,
    llm_score    REAL,
    llm_verdict  TEXT,
    llm_notes    TEXT,
    scored_at    TEXT NOT NULL,
    UNIQUE (candidate_id, jd_id, jd_version)
);
CREATE INDEX IF NOT EXISTS idx_score_jd    ON scores(jd_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_score_cand  ON scores(candidate_id);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    sources     TEXT,
    fetched     INTEGER DEFAULT 0,
    created     INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    scored      INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    log         TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    action    TEXT NOT NULL,
    entity    TEXT,
    entity_id TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS linkedin_activity (
    day   TEXT NOT NULL,
    kind  TEXT NOT NULL,
    n     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind)
);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS source_cursor (
    source TEXT PRIMARY KEY,
    cursor TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    role          TEXT NOT NULL DEFAULT 'hod',
    vacancies     TEXT,
    departments   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Org structure: which designations belong to which department. HODs are linked
-- to departments through users.departments; a vacancy reaches a HOD either by
-- its explicit jds.department or because its title matches one of `roles`.
-- Preferred companies are managed centrally: a group of companies plus the
-- departments / roles it applies to (empty scope = every vacancy).
CREATE TABLE IF NOT EXISTS employer_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    companies   TEXT,
    departments TEXT,
    roles       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- "Also search for these titles" per role, applied to every vacancy whose
-- title contains the role. auto=1 rows were generated from the title taxonomy.
CREATE TABLE IF NOT EXISTS title_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL UNIQUE,
    titles     TEXT,
    auto       INTEGER DEFAULT 0,
    source     TEXT,
    basis      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- What the pipeline and the AI agent are doing, step by step (the "Agent activity" tab).
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL DEFAULT 'info',
    stage     TEXT NOT NULL,
    candidate TEXT,
    vacancy   TEXT,
    message   TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_id_stage ON events(stage, id);

CREATE TABLE IF NOT EXISTS departments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    roles      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else cfg().path_of("storage.db_path")
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after the first release. Each entry is applied only if missing,
# so an existing database upgrades in place without losing data.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("jds", "hard_min_years", "INTEGER DEFAULT 0"),
    ("scores", "llm_score", "REAL"),
    ("scores", "llm_verdict", "TEXT"),
    ("scores", "llm_notes", "TEXT"),
    ("jds", "preferred_employers", "TEXT"),
    ("jds", "extra_titles", "TEXT"),
    ("jds", "search_skills_count", "INTEGER DEFAULT 2"),
    ("jds", "priority", "INTEGER DEFAULT 0"),
    ("candidates", "certifications_json", "TEXT"),
    ("candidates", "projects_json", "TEXT"),
    ("candidates", "featured_text", "TEXT"),
    ("jds", "department", "TEXT"),
    ("users", "departments", "TEXT"),
    ("title_rules", "source", "TEXT"),
    ("title_rules", "basis", "TEXT"),
]


def migrate(conn: sqlite3.Connection) -> list[str]:
    applied = []
    for table, column, decl in MIGRATIONS:
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"{table}.{column}")
    return applied


def ensure_admin(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
    except sqlite3.OperationalError:
        return
    if row["c"] == 0:
        import os
        import secrets
        from werkzeug.security import generate_password_hash
        pw = (os.environ.get("ADMIN_PASSWORD") or "").strip()
        if not pw and os.environ.get("SOURCER_ENV", "").lower() == "production":
            pw = secrets.token_urlsafe(12)           # never ship a known password to the internet
            print(f"\n=== FIRST START: the admin login is  admin / {pw}  (set ADMIN_PASSWORD to choose your own) ===\n",
                  flush=True)
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, role, vacancies, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("admin", generate_password_hash(pw or "admin123"), "Administrator", "admin", "[]", now(), now()))


_ready: set[str] = set()            # databases already migrated in this process


def init(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else cfg().path_of("storage.db_path")
    key = str(p.resolve())
    if key in _ready and p.exists():          # the schema work is done once per process, not per request
        return connect(path)
    conn = connect(path)
    # Migrate first: SCHEMA creates indexes over columns that older databases
    # may not have yet, and an index on a missing column aborts the script.
    migrate(conn)
    conn.executescript(SCHEMA)
    ensure_admin(conn)
    conn.commit()
    _ready.add(key)
    return conn


@contextmanager
def session(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = init(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def audit(conn: sqlite3.Connection, action: str, entity: str = "", entity_id: Any = "",
          detail: Any = None) -> None:
    conn.execute(
        "INSERT INTO audit (ts, action, entity, entity_id, detail) VALUES (?,?,?,?,?)",
        (now(), action, entity, str(entity_id),
         detail if isinstance(detail, str) or detail is None else json.dumps(detail, default=str)),
    )


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def jload(v: Any, default=None):
    if not v:
        return default if default is not None else []
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default if default is not None else []


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def kv_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT INTO kv (key, value, updated_at) VALUES (?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (key, value, now()))
