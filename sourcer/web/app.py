"""The local web app. Runs on this computer only (127.0.0.1) and talks to the
same database and files as the command line, so the two stay interchangeable.

    python -m sourcer ui
"""
from __future__ import annotations

import io
import re
import shutil
import tempfile
import threading
import time
import zipfile
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path

import base64
import hmac
import os
import secrets

import yaml
from flask import Flask, g, jsonify, make_response, redirect, request, send_file, send_from_directory, abort
from flask import session as flask_session
from werkzeug.security import check_password_hash, generate_password_hash

from .. import jd as jdmod
from .. import agent, events, report, rules, runner, sourcing, store
from ..ask import parse_ask
from .. import __version__
from ..config import cfg, data_dir, is_production
from ..collector import SHARED_LINKEDIN_KEYS
from ..models import Candidate
from ..db import jload, jdump, kv_get, kv_set, now, session
from ..normalize import norm_text
from ..jd import clean_field_values, field_for_label

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
HOD_POST_OK = {"/api/logout", "/api/ask"}      # everything else that writes is admin-only
ALLOWED_STATUS = {"new", "reviewed", "shortlisted", "contacted", "rejected", "hired"}


# --------------------------------------------------------------------------- #
# One background job at a time (a browser profile and the database both want it)
# --------------------------------------------------------------------------- #
class JobManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.kind = ""
        self.state = "idle"            # idle | running | stopping | done | error
        self.log: deque[str] = deque(maxlen=400)
        self.started = 0.0
        self.finished = 0.0
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None

    def say(self, msg: str, emit: bool = True):
        self.log.append(f"{datetime.now():%H:%M:%S}  {msg}")
        if emit:
            events.emit("error" if msg.startswith("ERROR") else "job", msg,
                        level="error" if msg.startswith("ERROR") else "info")

    def busy(self) -> bool:
        return self.state in {"running", "stopping"}

    def start(self, kind: str, fn) -> bool:
        with self.lock:
            if self.busy():
                return False
            self.reset()
            self.kind, self.state, self.started = kind, "running", time.time()
            self.stop_flag.clear()

            def target():
                events.emit("job", f"Job started: {kind}")
                try:
                    fn(self)
                    self.state = "done"
                    events.emit("job", f"Job finished: {kind}")
                except Exception as e:  # surfaced in the UI, not swallowed
                    self.say(f"ERROR: {type(e).__name__}: {e}")
                    self.state = "error"
                finally:
                    self.finished = time.time()

            self.thread = threading.Thread(target=target, daemon=True)
            self.thread.start()
            return True

    def stop(self):
        if self.busy():
            self.state = "stopping"
            self.stop_flag.set()
            self.say("stop requested - finishing the current step")

    def as_dict(self) -> dict:
        return {"kind": self.kind, "state": self.state, "log": list(self.log)[-120:],
                "started": self.started, "finished": self.finished}


JOB = JobManager()


def _run_pipeline(job: JobManager, sources: list[str], manual: bool, limit: int | None = None,
                  vacancy: str | None = None):
    note = f"starting: {', '.join(sources)}"
    if limit:
        note += f" (limit {limit})"
    if vacancy:
        note += f" [vacancy: {vacancy}]"
    job.say(note)
    s = runner.run(sources=sources, manual=manual, max_results=limit, log=job.say,
                   should_stop=job.stop_flag.is_set, vacancy=vacancy)
    lines = s.as_text().splitlines()
    for line in lines:
        job.say(line, emit=False)                     # the activity log gets one tidy summary instead
    events.emit("job", " · ".join(l.strip() for l in lines if not l.strip().startswith(("ERROR", "note"))),
                level="warn" if s.errors else "info")
    for l in lines:
        if l.strip().startswith(("ERROR", "note")):
            events.emit("error" if l.strip().startswith("ERROR") else "job", l.strip(),
                        level="error" if l.strip().startswith("ERROR") else "info")


def _run_login(job: JobManager):
    from ..linkedin_browser import login_window
    ok = login_window(log=job.say, should_stop=job.stop_flag.is_set)
    with session() as conn:
        kv_set(conn, "linkedin_logged_in", "1" if ok else "0")


# --------------------------------------------------------------------------- #
def _safe_url(u: str | None) -> str:
    """Profile links come from scraped/imported data, so they are untrusted:
    only plain web addresses may ever become a clickable link."""
    u = (u or "").strip()
    return u if re.match(r"^https?://[^\s\"'<>]+$", u, re.I) else ""


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")


def _jd_dict(r) -> dict:
    return {"code": r["code"], "title": r["title"], "family": r["title_family"],
            "seniority": r["seniority"], "location": r["location"],
            "min_years": r["min_years"], "max_years": r["max_years"],
            "hard_min_years": bool(r["hard_min_years"]), "education_min": r["education_min"],
            "openings": r["openings"], "industries": jload(r["industries"]),
            "must_have": jload(r["must_have"]), "nice_have": jload(r["nice_have"]),
            "hard_requirements": jload(r["hard_requirements"]),
            "preferred_employers": jload(r["preferred_employers"]),
            "extra_titles": jload(r["extra_titles"]),
            "search_skills_count": int(2 if r["search_skills_count"] is None else r["search_skills_count"]),
            "priority": int(r["priority"] or 0),
            "version": r["version"],
            "raw_text": r["raw_text"] or ""}


def _row_dict(r: report.Row, titles: dict | None = None) -> dict:
    return {"id": r.candidate_id, "name": r.name, "title": r.current_title,
            "company": r.current_company, "city": r.city, "years": round(r.total_years, 1),
            "vacancy": r.jd_code, "vacancy_title": r.jd_title, "score": r.score,
            "matched": r.matched, "missing": r.missing, "flags": r.flags,
            "status": r.status, "has_pdf": bool(r.pdf_path and Path(r.pdf_path).is_file()),
            "linkedin_url": _safe_url(r.linkedin_url),
            "found_for": r.found_for, "found_for_title": (titles or {}).get(r.found_for, "")}


def _role_matches(title: str, roles: list[str]) -> bool:
    return rules.role_matches(title, roles)


def _load_depts(conn) -> list[dict]:
    return rules.load_departments(conn)


def _jd_department(jd_row, depts: list[dict]) -> tuple[str, str]:
    """-> (department name, how) where how is 'assigned' (set on the vacancy),
    'role' (title matches the department's roles) or '' (unassigned)."""
    explicit = (jd_row["department"] or "").strip()
    if explicit:
        for d in depts:
            if d["name"].lower() == explicit.lower():
                return d["name"], "assigned"
        return explicit, "assigned"
    for d in depts:
        if _role_matches(jd_row["title"], d["roles"]):
            return d["name"], "role"
    return "", ""


def _visible_codes(conn, user: dict) -> set[str] | None:
    """Vacancy codes this user may see, or None for 'everything' (admin)."""
    if user["role"] == "admin":
        return None
    codes = set(user.get("vacancies") or [])
    mine = {d.lower() for d in (user.get("departments") or [])}
    if mine:
        depts = _load_depts(conn)
        for r in conn.execute("SELECT code, title, department FROM jds WHERE active=1"):
            if _jd_department(r, depts)[0].lower() in mine:
                codes.add(r["code"])
    return codes


def _clean_names(v) -> list[str]:
    if not isinstance(v, list):
        v = re.split(r"[,;\n]", str(v or ""))
    out: list[str] = []
    for x in v:
        x = str(x).strip()
        if x and x.lower() not in {o.lower() for o in out}:
            out.append(x)
    return out


def _set_front_department(code: str, dept: str) -> None:
    """Best effort: keep the vacancy file's front matter in step with the database."""
    jd_dir = cfg().path_of("jd_dir")
    for p in list(jd_dir.glob(f"{code}-*.md")) + list(jd_dir.glob(f"{code}.md")):
        text = p.read_text(encoding="utf-8")
        m = jdmod.FRONTMATTER.match(text)
        if not m:
            continue
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        if dept:
            meta["department"] = dept
        else:
            meta.pop("department", None)
        p.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) +
                     "---\n\n" + m.group(2).lstrip("\n"), encoding="utf-8")


def _secret_key() -> str:
    """The key that signs login cookies. From SOURCER_SECRET_KEY, else a random one that is
    created once and kept in the state folder (so logins survive restarts)."""
    v = os.environ.get("SOURCER_SECRET_KEY", "").strip()
    if v:
        return v
    dd = data_dir()
    f = (dd if dd else cfg().root / "storage") / ".secret_key"
    try:
        if f.is_file() and f.read_text().strip():
            return f.read_text().strip()
        f.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        f.write_text(key)
        try:
            f.chmod(0o600)
        except OSError:
            pass
        return key
    except OSError:
        return secrets.token_hex(32)


def _allowed_hosts() -> set[str] | None:
    """Host names this app answers to; None = any. Locally only loopback names (DNS-rebinding
    guard); in the cloud the public domain the host gave us plus SOURCER_ALLOWED_HOSTS."""
    if not is_production():
        return set(ALLOWED_HOSTS)
    hosts = {h.strip().lower() for h in os.environ.get("SOURCER_ALLOWED_HOSTS", "").split(",") if h.strip()}
    for k in ("RAILWAY_PUBLIC_DOMAIN", "RAILWAY_STATIC_URL"):
        v = os.environ.get(k, "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
        if v:
            hosts.add(v)
    return (hosts | {"127.0.0.1", "localhost"}) if hosts else None


def _reset_admin_if_asked() -> None:
    """Lock-out recovery: set RESET_ADMIN_PASSWORD=<new password> on the host, redeploy once,
    then delete the variable."""
    pw = os.environ.get("RESET_ADMIN_PASSWORD", "").strip()
    if not pw:
        return
    with session() as conn:
        conn.execute("UPDATE users SET password_hash=?, updated_at=? WHERE username='admin'",
                     (generate_password_hash(pw), now()))
    print("=== admin password was reset from RESET_ADMIN_PASSWORD - remove that variable now ===", flush=True)


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
    app.secret_key = _secret_key()
    prod = is_production()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=prod,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12 if prod else 24 * 7))
    if prod:                                            # behind the host's HTTPS proxy
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    allowed_hosts = _allowed_hosts()
    failed_logins: dict[str, list[float]] = {}
    with session() as conn:                              # create the database and the admin login at start-up
        events.purge(conn)
    _reset_admin_if_asked()

    # ---- local-only protection ---------------------------------------- #
    @app.before_request
    def guard():
        if request.path == "/healthz":
            return                                       # the host's health check (own hostname)
        host = (request.host or "").rsplit(":", 1)[0] if not request.host.startswith("[") \
            else request.host.split("]")[0] + "]"
        if allowed_hosts is not None and host not in allowed_hosts:
            abort(403)                                   # blocks DNS-rebinding
        if request.path.startswith("/api/ingest"):
            return                                       # token-protected, called by the collector, not a browser
        if request.method not in {"GET", "HEAD"} and request.headers.get("X-Sourcer-UI") != "1":
            abort(403)                                   # blocks drive-by posts from other sites

    @app.get("/healthz")
    def healthz():
        with session() as conn:
            conn.execute("SELECT 1")
        return jsonify({"ok": True})

    # ---- auth --------------------------------------------------------- #
    @app.before_request
    def require_auth():
        path = request.path
        if path in ("/login", "/api/login", "/healthz") or path.startswith("/static/") or path == "/favicon.ico" \
                or path.startswith("/api/ingest"):
            return
        uid = flask_session.get("user_id")
        if not uid:
            if path.startswith("/api/"):
                return jsonify({"error": "not logged in"}), 401
            return redirect("/login")
        with session() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            flask_session.clear()
            if path.startswith("/api/"):
                return jsonify({"error": "not logged in"}), 401
            return redirect("/login")
        g.user = {"id": row["id"], "username": row["username"], "role": row["role"],
                  "display_name": row["display_name"] or row["username"],
                  "vacancies": jload(row["vacancies"]),
                  "departments": jload(row["departments"]),
                  "default_pw": row["role"] == "admin" and check_password_hash(row["password_hash"], "admin123")}
        if g.user["role"] != "admin" and request.method not in ("GET", "HEAD") \
                and path not in HOD_POST_OK and not re.fullmatch(r"/api/candidates/\d+/status", path):
            return jsonify({"error": "admin only"}), 403

    @app.get("/login")
    def login_page():
        return send_from_directory(STATIC, "login.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC, name, max_age=3600)

    @app.post("/api/login")
    def login():
        d = request.get_json(silent=True) or {}
        username = (d.get("username") or "").strip()
        password = d.get("password") or ""
        if not username or not password:
            return jsonify({"error": "Username and password are required"}), 400
        who = request.remote_addr or "?"
        recent = [t for t in failed_logins.get(who, []) if time.time() - t < 600]
        if len(recent) >= 8:
            return jsonify({"error": "Too many wrong passwords - wait ten minutes and try again"}), 429
        with session() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], password):
            failed_logins[who] = recent + [time.time()]
            return jsonify({"error": "Invalid username or password"}), 401
        failed_logins.pop(who, None)
        flask_session.clear()
        flask_session["user_id"] = row["id"]
        flask_session.permanent = True
        return jsonify({"ok": True, "role": row["role"]})

    @app.post("/api/logout")
    def logout():
        flask_session.clear()
        return jsonify({"ok": True})

    @app.get("/api/me")
    def me():
        u = g.user
        return jsonify({"id": u["id"], "username": u["username"], "role": u["role"],
                        "display_name": u["display_name"], "vacancies": u["vacancies"],
                        "departments": u["departments"], "version": __version__, "cloud": prod,
                        "default_password": u["role"] == "admin" and u.get("default_pw", False)})

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        if prod:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-src 'self'; object-src 'self'")
        return resp

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(STATIC / "brand", "favicon.ico", max_age=86400)

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    # ---- overview ------------------------------------------------------ #
    @app.get("/api/overview")
    def overview():
        from ..pacing import (Pacer, cooldown_until, daily_cap, remaining_today, used_today)
        li = cfg().get("linkedin", {}) or {}
        try:
            import playwright  # noqa: F401
            has_pw = True
        except ImportError:
            has_pw = False
        with session() as conn:
            until = cooldown_until(conn)
            win_ok, win_why = Pacer(li).in_window()
            n_cand = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            n_jd = conn.execute("SELECT COUNT(*) FROM jds WHERE active=1").fetchone()[0]
            data = {
                "candidates": n_cand, "vacancies": n_jd,
                "threshold": float(cfg().get("scoring.default_shortlist_threshold", 75)),
                "linkedin": {
                    "playwright": has_pw,
                    "logged_in": kv_get(conn, "linkedin_logged_in") == "1",
                    "used": used_today(conn, "profile"), "cap": daily_cap(conn, li),
                    "configured_cap": int(li.get("daily_profile_cap", 40)),
                    "remaining": remaining_today(conn, li),
                    "searches": used_today(conn, "search"),
                    "cooldown_until": until.isoformat(timespec="minutes") if until else None,
                    "cooldown_reason": kv_get(conn, "linkedin_cooldown_reason") or "",
                    "in_window": win_ok, "window_note": win_why,
                },
                "inbox": {
                    "pdfs": len(list(cfg().path_of("storage.pdf_inbox").glob("*.pdf"))),
                    "sheets": len([p for p in (cfg().root / cfg().get("sources.csv.watch_dir", "storage/imports")).glob("*")
                                   if p.suffix.lower() in {".csv", ".xlsx", ".xls", ".tsv"}]),
                },
            }
        data["job"] = JOB.as_dict()
        if g.user["role"] != "admin":
            with session() as conn:
                vis = _visible_codes(conn, g.user)
                n_vis = conn.execute("SELECT COUNT(DISTINCT s.candidate_id) FROM scores s JOIN jds j ON "
                                     "j.id=s.jd_id AND j.version=s.jd_version WHERE j.active=1 AND j.code IN (%s)"
                                     % ",".join("?" * len(vis)), tuple(vis)).fetchone()[0] if vis else 0
            return jsonify({"candidates": n_vis, "vacancies": len(vis or ()), "threshold": data["threshold"],
                            "linkedin": None, "inbox": None, "job": {"state": data["job"]["state"], "log": []}})
        return jsonify(data)

    # ---- vacancies ----------------------------------------------------- #
    @app.get("/api/jds")
    def jds_list():
        thr = float(cfg().get("scoring.default_shortlist_threshold", 75))
        with session() as conn:
            allowed = _visible_codes(conn, g.user)
            rl = rules.Rules(conn)
            out = []
            for r in conn.execute("SELECT * FROM jds WHERE active=1 ORDER BY code"):
                if allowed is not None and r["code"] not in allowed:
                    continue
                d = _jd_dict(r)
                eff = rl.resolve(r["title"], r["department"] or "", d["preferred_employers"], d["extra_titles"])
                d["department"], d["department_how"] = eff["department"], eff["department_how"]
                d["effective_employers"], d["employers_from_rules"] = eff["employers"], eff["employers_from_rules"]
                d["employer_groups"] = eff["employer_groups"]
                d["effective_titles"], d["titles_from_rules"] = eff["titles"], eff["titles_from_rules"]
                row = conn.execute(
                    "SELECT COUNT(*) n, MAX(score) best FROM scores WHERE jd_id=? AND jd_version=?",
                    (r["id"], r["version"])).fetchone()
                d["scored"] = row["n"]
                d["best"] = row["best"]
                d["above_threshold"] = conn.execute(
                    "SELECT COUNT(*) FROM scores WHERE jd_id=? AND jd_version=? AND score>=?",
                    (r["id"], r["version"], thr)).fetchone()[0]
                out.append(d)
        return jsonify(out)

    @app.post("/api/jds/preview")
    def jd_preview():
        text = (request.get_json(silent=True) or {}).get("text", "")
        if not text.strip():
            return jsonify({"error": "paste the job description first"}), 400
        j = jdmod.parse_text(text)
        fields, desc = jdmod.labelled_fields(text)
        return jsonify({"title": j.title, "seniority": j.seniority, "min_years": j.min_years,
                        "max_years": j.max_years, "education_min": j.education_min,
                        "must_have": [s.replace("_", " ") for s in j.must_have],
                        "nice_have": [s.replace("_", " ") for s in j.nice_have],
                        "location": j.location, "family": j.title_family,
                        "department": j.department, "openings": j.openings, "priority": j.priority,
                        "hard_requirements": j.hard_requirements, "industries": j.industries,
                        "preferred_employers": j.preferred_employers, "extra_titles": j.extra_titles,
                        "code": fields.get("code", ""), "from_template": bool(fields),
                        "body": desc if fields else text})

    def _as_list(v) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return [p.strip() for p in re.split(r"[,;\n]", str(v or "")) if p.strip()]

    def _save_vacancy(d: dict) -> str:
        """Write one vacancy file from form-shaped data. -> its code. Raises ValueError."""
        title = (d.get("title") or "").strip()
        if not title:
            raise ValueError("the designation / job title is required")
        code = (d.get("code") or "").strip()
        jd_dir = cfg().path_of("jd_dir")
        jd_dir.mkdir(parents=True, exist_ok=True)
        if not code:
            n = 1
            with session() as conn:
                codes = {r["code"] for r in conn.execute("SELECT code FROM jds")}
            files = {"-".join(p.stem.split("-", 2)[:2]) for p in jd_dir.glob("*.md")}
            while f"V-{n:03d}" in codes or f"V-{n:03d}" in files:
                n += 1
            code = f"V-{n:03d}"
        code = re.sub(r"[^A-Za-z0-9_-]", "", code) or "V-001"

        front: dict = {"code": code, "title": title}
        if d.get("seniority"):
            front["seniority"] = d["seniority"]
        front["location"] = (d.get("location") or "").strip() or \
            f"{cfg().get('location.target_city', 'Karachi')}, {cfg().get('location.target_country', 'Pakistan')}"
        for k in ("min_years", "max_years"):
            if d.get(k) not in (None, ""):
                front[k] = float(d[k])
        if d.get("hard_min_years"):
            front["hard_min_years"] = True
        if d.get("education_min") and d["education_min"] != "none":
            front["education_min"] = d["education_min"]
        if d.get("openings"):
            front["openings"] = int(d["openings"])
        for k in ("industries", "must_have", "nice_have", "hard_requirements",
                  "preferred_employers", "extra_titles"):
            front[k] = _as_list(d.get(k))
        if d.get("search_skills_count") not in (None, ""):
            front["search_skills_count"] = max(0, min(2, int(d["search_skills_count"])))
        if d.get("priority") not in (None, ""):
            front["priority"] = max(0, min(10, int(d["priority"])))
        if (d.get("department") or "").strip():
            front["department"] = d["department"].strip()
        body = (d.get("text") or "").strip()
        text = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n\n" + \
               (body or f"# {title}\n")
        for old in list(jd_dir.glob(f"{code}-*.md")) + list(jd_dir.glob(f"{code}.md")):
            old.unlink()                                   # one file per code: an edit replaces it
        (jd_dir / f"{code}-{_slug(title).lower()[:60]}.md").write_text(text, encoding="utf-8")
        return code

    @app.post("/api/jds")
    def jd_save():
        d = request.get_json(silent=True) or {}
        try:
            code = _save_vacancy(d)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        with session() as conn:
            jdmod.sync_dir(conn)
            rules.refresh_auto_titles(conn)
            if "department" in d:                            # also lets the form clear a department
                conn.execute("UPDATE jds SET department=? WHERE code=?",
                             ((d["department"] or "").strip() or None, code))
            n = runner.score_all(conn)
        return jsonify({"ok": True, "code": code, "rescored": n})

    @app.post("/api/jds/search-preview")
    def jd_search_preview():
        """Show the LinkedIn search string that would be used for a vacancy."""
        d = request.get_json(silent=True) or {}
        title = (d.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title required"}), 400
        from ..sources.linkedin_browser import search_keywords
        from ..models import JD as JDModel
        jd = JDModel(title=title, must_have=_as_list(d.get("must_have")),
                     extra_titles=_as_list(d.get("extra_titles")),
                     search_skills_count=int(2 if d.get("search_skills_count") in (None, "") else d["search_skills_count"]))
        from ..normalize import titles_index
        jd.title_family = titles_index().family_of(title)
        return jsonify({"query": search_keywords(jd), "family": jd.title_family})

    @app.post("/api/jds/from-files")
    def jd_from_files():
        """mode=extract: return the text of ONE file so the form can be filled and reviewed.
        mode=create : make a vacancy from every file (reviewable afterwards)."""
        from ..jd import file_bytes_to_text
        mode = request.form.get("mode", "extract")
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "choose a file first"}), 400
        if mode == "extract":
            f = files[0]
            try:
                text = file_bytes_to_text(f.filename or "", f.read())
            except Exception as e:
                return jsonify({"error": f"could not read {f.filename}: {e}"}), 400
            if not text.strip():
                return jsonify({"error": "no text found in that file (a scanned image PDF cannot be read)"}), 400
            return jsonify({"text": text, "filename": f.filename})
        created, failed = [], []
        for f in files:
            try:
                text = file_bytes_to_text(f.filename or "", f.read())
                if not text.strip():
                    raise ValueError("no text found (a scanned image PDF cannot be read)")
                j = jdmod.parse_text(text, source_file=f.filename or "")
                fields, desc = jdmod.labelled_fields(text)
                code = _save_vacancy({
                    "title": j.title, "seniority": j.seniority, "min_years": j.min_years or None,
                    "max_years": j.max_years, "education_min": j.education_min, "openings": j.openings,
                    "must_have": j.must_have, "nice_have": j.nice_have, "text": desc if fields else text,
                    "department": j.department, "hard_requirements": j.hard_requirements,
                    "industries": j.industries, "preferred_employers": j.preferred_employers,
                    "extra_titles": j.extra_titles, "priority": j.priority,
                    "location": j.location if fields else "", "code": fields.get("code", "")})
                created.append({"code": code, "title": j.title, "file": f.filename,
                                "must_have": len(j.must_have)})
            except Exception as e:
                failed.append({"file": f.filename, "error": str(e)})
        if created:
            with session() as conn:
                jdmod.sync_dir(conn)
                rules.refresh_auto_titles(conn)
                runner.score_all(conn)
        return jsonify({"created": created, "failed": failed})

    @app.delete("/api/jds/<code>")
    def jd_archive(code):
        jd_dir = cfg().path_of("jd_dir")
        arch = jd_dir / "_archived"
        arch.mkdir(exist_ok=True)
        for p in list(jd_dir.glob(f"{code}-*.md")) + list(jd_dir.glob(f"{code}.md")):
            shutil.move(str(p), str(arch / p.name))
        with session() as conn:
            conn.execute("UPDATE jds SET active=0 WHERE code=?", (code,))
        return jsonify({"ok": True})

    def _can_see_candidate(conn, cid: int) -> bool:
        allowed = _visible_codes(conn, g.user)
        if allowed is None:
            return True
        return any(r["code"] in allowed for r in conn.execute(
            "SELECT DISTINCT j.code FROM scores s JOIN jds j ON j.id=s.jd_id AND j.version=s.jd_version "
            "WHERE s.candidate_id=? AND j.active=1", (cid,)))

    # ---- candidates ---------------------------------------------------- #
    @app.get("/api/candidates")
    def candidates():
        a = request.args
        min_score = float(a["min_score"]) if a.get("min_score") not in (None, "") else None
        found_for = a.get("found_for") or None
        with session() as conn:
            titles = {r["code"]: r["title"] for r in conn.execute("SELECT code, title FROM jds")}
            rows = report.query(conn, role=a.get("role") or found_for, min_score=min_score,   # scored for the vacancy it was found for
                                limit=int(a.get("limit", 300)), status=a.get("status") or None,
                                city=a.get("city") or None,
                                include_contacted=a.get("hide_done") != "1",
                                only_codes=_visible_codes(conn, g.user), found_for=found_for)
        return jsonify([_row_dict(r, titles) for r in rows])

    @app.post("/api/ask")
    def ask():
        body = request.get_json(silent=True) or {}
        text = body.get("text", "")
        q = parse_ask(text)
        try:
            slider = float(body["default_min"]) if body.get("default_min") not in (None, "") else None
        except (TypeError, ValueError):
            slider = None
        thr = q["min_score"] if q["min_score"] is not None else (
            slider if slider is not None else float(cfg().get("scoring.default_shortlist_threshold", 75)))
        with session() as conn:
            allowed = _visible_codes(conn, g.user)
            matched = report.resolve_jds(conn, q["role"]) if q["role"] else report.resolve_jds(conn, None)
            if allowed is not None:
                matched = [m for m in matched if m["code"] in allowed]
            if q["role"] and not matched:
                codes = [f'{r["code"]} {r["title"]}' for r in
                         conn.execute("SELECT code,title FROM jds WHERE active=1 ORDER BY code")
                         if allowed is None or r["code"] in allowed]
                return jsonify({"understood": q, "matched": [], "rows": [], "min_score": thr,
                                "message": f"No open vacancy matches \"{q['role']}\". "
                                           f"Open vacancies: {', '.join(codes) or 'none yet'}."})
            rows = report.query(conn, role=q["role"], min_score=thr, limit=300, only_codes=allowed)
            titles = {r["code"]: r["title"] for r in conn.execute("SELECT code, title FROM jds")}
        return jsonify({"understood": q, "min_score": thr,
                        "matched": [{"code": r["code"], "title": r["title"]} for r in matched],
                        "rows": [_row_dict(r, titles) for r in rows], "message": ""})

    @app.get("/api/candidates/<int:cid>")
    def candidate(cid):
        with session() as conn:
            c = store.load(conn, cid)
            if not c:
                abort(404)
            row = conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
            allowed = _visible_codes(conn, g.user)
            scores = []
            for r in conn.execute(
                    "SELECT s.*, j.code, j.title FROM scores s JOIN jds j ON j.id=s.jd_id AND "
                    "j.version=s.jd_version WHERE s.candidate_id=? AND j.active=1 ORDER BY s.score DESC", (cid,)):
                if allowed is not None and r["code"] not in allowed:
                    continue
                scores.append({"code": r["code"], "title": r["title"], "score": r["score"],
                               "breakdown": jload(r["breakdown"], {}), "matched": jload(r["matched"]),
                               "missing": jload(r["missing"]), "flags": jload(r["flags"]),
                               "rationale": r["rationale"], "llm_verdict": r["llm_verdict"],
                               "llm_score": r["llm_score"], "rubric_score": r["rubric_score"],
                               "llm_notes": jload(r["llm_notes"], {})})
            if allowed is not None and not scores:
                abort(404)                                   # not one of this HOD's candidates
            pdf = row["profile_pdf_path"]
            return jsonify({
                "id": cid, "name": c.full_name, "headline": c.headline, "summary": c.summary,
                "location": c.location_raw, "email": c.email, "phone": c.phone,
                "linkedin_url": _safe_url(c.linkedin_url), "status": row["status"], "notes": row["notes"] or "",
                "total_years": c.total_years(), "education": c.education_level(),
                "source": row["source"], "first_seen": row["first_seen_at"],
                "has_pdf": bool(pdf and Path(pdf).is_file()),
                "experiences": [{"title": e.title, "company": e.company, "location": e.location,
                                 "start": e.start_date, "end": "Present" if e.is_current else e.end_date,
                                 "years": round(e.months() / 12, 1), "description": e.description[:600]}
                                for e in c.experiences],
                "educations": [{"degree": f"{e.degree} {e.field_of_study}".strip(),
                                "institution": e.institution, "year": e.end_year} for e in c.educations],
                "skills": sorted(s.replace("x:", "").replace("_", " ") for s in c.skills()),
                "scores": scores})

    @app.post("/api/candidates/<int:cid>/status")
    def candidate_status(cid):
        d = request.get_json(silent=True) or {}
        st = d.get("status", "")
        if st not in ALLOWED_STATUS:
            return jsonify({"error": f"status must be one of {sorted(ALLOWED_STATUS)}"}), 400
        with session() as conn:
            if not store.load(conn, cid) or not _can_see_candidate(conn, cid):
                abort(404)
            store.set_status(conn, cid, st, d.get("note"))
        return jsonify({"ok": True})

    @app.get("/api/candidates/<int:cid>/pdf")
    def candidate_pdf(cid):
        with session() as conn:
            r = conn.execute("SELECT profile_pdf_path, full_name FROM candidates WHERE id=?", (cid,)).fetchone()
            visible = _can_see_candidate(conn, cid)
        if not r or not r["profile_pdf_path"] or not visible:
            abort(404)
        p = Path(r["profile_pdf_path"]).resolve()
        roots = [cfg().path_of("storage.profiles_dir").resolve(),
                 cfg().path_of("storage.pdf_inbox").resolve()]
        if not p.is_file() or not any(root in p.parents for root in roots):
            abort(404)
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", r["full_name"]) + ".pdf"
        return send_file(p, mimetype="application/pdf", download_name=name,
                         as_attachment=request.args.get("download") == "1")

    @app.get("/api/export")
    def export():
        a = request.args
        min_score = float(a["min_score"]) if a.get("min_score") not in (None, "") else None
        with session() as conn:
            rows = report.query(conn, role=a.get("role") or a.get("found_for") or None, min_score=min_score,
                                limit=2000, status=a.get("status") or None, city=a.get("city") or None,
                                include_contacted=a.get("hide_done") != "1",
                                only_codes=_visible_codes(conn, g.user), found_for=a.get("found_for") or None)
        if not rows:
            return jsonify({"error": "nothing to export for these filters"}), 404
        fmt = a.get("format", "xlsx")
        stamp = date.today().isoformat()
        tmp = Path(tempfile.mkdtemp(prefix="sourcer_export_"))
        if fmt == "csv":
            p = report.export_csv(rows, tmp / f"shortlist_{stamp}.csv")
            return send_file(p, as_attachment=True, download_name=p.name)
        if fmt == "zip":
            # every matching candidate, as a PDF: the stored one when there is one, otherwise a
            # fresh profile PDF drawn from what was read (LinkedIn profiles, spreadsheet rows)
            from ..pdfstore import render_profile_pdf
            with session() as conn:
                for r in rows:
                    if r.pdf_path and Path(r.pdf_path).is_file():
                        continue
                    c = store.load(conn, r.candidate_id)
                    if c:
                        r.pdf_path = str(render_profile_pdf(c, tmp / "generated" / f"{r.candidate_id}.pdf"))
                title = ""
                if a.get("role"):
                    m = conn.execute("SELECT code, title FROM jds WHERE active=1 AND (code=? OR lower(title)=lower(?))",
                                     (a["role"], a["role"])).fetchone()
                    title = f"{m['code']} {m['title']}" if m else a["role"]
            label = title or "All vacancies"
            folder = report.export_pdf_bundle(rows, tmp / "bundle", label=label)
            report.export_xlsx(rows, folder / "candidates.xlsx")
            base = _slug(label)[:60] or "resumes"
            zpath = shutil.make_archive(str(tmp / f"{base}_resumes_{stamp}"), "zip", folder)
            return send_file(zpath, as_attachment=True, download_name=Path(zpath).name)
        p = report.export_xlsx(rows, tmp / f"shortlist_{stamp}.xlsx")
        return send_file(p, as_attachment=True, download_name=p.name)

    # ---- intake -------------------------------------------------------- #
    @app.post("/api/upload")
    def upload():
        pdf_dir = cfg().path_of("storage.pdf_inbox")
        csv_dir = cfg().root / cfg().get("sources.csv.watch_dir", "storage/imports")
        pdf_dir.mkdir(parents=True, exist_ok=True)
        csv_dir.mkdir(parents=True, exist_ok=True)
        saved = {"pdfs": 0, "sheets": 0, "skipped": []}
        for f in request.files.getlist("files"):
            name = re.sub(r"[^A-Za-z0-9_. -]", "_", Path(f.filename or "file").name)
            ext = Path(name).suffix.lower()
            if ext == ".pdf":
                dest = pdf_dir / name
                saved["pdfs"] += 1
            elif ext in {".csv", ".xlsx", ".xls", ".tsv"}:
                dest = csv_dir / name
                saved["sheets"] += 1
            else:
                saved["skipped"].append(name)
                continue
            n = 1
            while dest.exists():
                dest = dest.with_name(f"{Path(name).stem}_{n}{ext}")
                n += 1
            f.save(dest)
        return jsonify(saved)

    @app.post("/api/run")
    def run_job():
        d = request.get_json(silent=True) or {}
        kind = d.get("kind", "files")
        if kind == "linkedin":
            ok, why = _linkedin_ready()
            if not ok:
                return jsonify({"error": why}), 400
            limit = d.get("limit")
            vacancy_code = d.get("vacancy")
            started = JOB.start("linkedin", lambda j: _run_pipeline(
                j, ["linkedin_browser"], True, int(limit) if limit else None,
                vacancy=vacancy_code))
        elif kind == "agent":
            vac, force = d.get("vacancy") or None, bool(d.get("force"))
            ok, why = agent.available()
            if not ok:
                return jsonify({"error": f"The AI agent is not ready: {why}"}), 400

            def run_agent(j):
                with session() as conn:
                    n, notes = runner.agent_review(conn, should_stop=j.stop_flag.is_set, vacancy=vac, force=force)
                    for line in notes:
                        j.say(line)
                    j.say(f"AI agent read {n} profile(s)")
            started = JOB.start("agent", run_agent)
        elif kind == "login":
            started = JOB.start("login", _run_login)
        elif kind == "rescore":
            def rescore(j):
                with session() as conn:
                    j.say(f"rescored {runner.score_all(conn, trace=True)} pairs")
            started = JOB.start("rescore", rescore)
        else:
            started = JOB.start("files", lambda j: _run_pipeline(j, ["pdf_inbox", "csv"], True))
        if not started:
            return jsonify({"error": "another job is still running"}), 409
        return jsonify({"ok": True})

    def _linkedin_ready() -> tuple[bool, str]:
        from ..sources.linkedin_browser import LinkedInBrowserSource
        return LinkedInBrowserSource().available()

    @app.post("/api/job/stop")
    def job_stop():
        JOB.stop()
        return jsonify({"ok": True})

    @app.post("/api/linkedin/clear-cooldown")
    def clear_cd():
        from ..pacing import clear_cooldown
        with session() as conn:
            clear_cooldown(conn)
        return jsonify({"ok": True})

    # ---- user management (admin only) ----------------------------------- #
    def _admin_only():
        if g.user["role"] != "admin":
            abort(make_response(jsonify({"error": "admin only"}), 403))

    def _valid_depts(conn, names) -> list[str]:
        known = {d["name"].lower(): d["name"] for d in _load_depts(conn)}
        return [known[n.lower()] for n in _clean_names(names) if n.lower() in known]

    @app.get("/api/users")
    def users_list():
        _admin_only()
        with session() as conn:
            rows = conn.execute("SELECT id, username, display_name, role, vacancies, departments, created_at "
                                "FROM users ORDER BY id").fetchall()
            out = []
            for r in rows:
                u = {"id": r["id"], "username": r["username"], "display_name": r["display_name"] or "",
                     "role": r["role"], "vacancies": jload(r["vacancies"]),
                     "departments": jload(r["departments"]), "created_at": r["created_at"]}
                vis = _visible_codes(conn, u)
                u["visible_vacancies"] = None if vis is None else len(vis)
                out.append(u)
        return jsonify(out)

    @app.post("/api/users")
    def user_create():
        _admin_only()
        d = request.get_json(silent=True) or {}
        username = re.sub(r"[^a-zA-Z0-9._-]", "", (d.get("username") or "").strip().lower())
        password = d.get("password") or ""
        if not username or len(username) < 3:
            return jsonify({"error": "Username must be at least 3 characters (letters, numbers, dots, dashes)"}), 400
        if len(password) < 4:
            return jsonify({"error": "Password must be at least 4 characters"}), 400
        role = d.get("role", "hod")
        if role not in ("admin", "hod"):
            role = "hod"
        vacancies = _clean_names(d.get("vacancies") or [])
        with session() as conn:
            if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                return jsonify({"error": f"Username '{username}' already exists"}), 409
            depts = _valid_depts(conn, d.get("departments") or [])
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, role, vacancies, departments, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (username, generate_password_hash(password), (d.get("display_name") or "").strip(),
                 role, jdump(vacancies), jdump(depts), now(), now()))
        return jsonify({"ok": True})

    @app.put("/api/users/<int:uid>")
    def user_update(uid):
        _admin_only()
        d = request.get_json(silent=True) or {}
        with session() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                abort(404)
            sets, vals = [], []
            if d.get("display_name") is not None:
                sets.append("display_name=?"); vals.append((d["display_name"] or "").strip())
            if d.get("role") in ("admin", "hod"):
                if row["role"] == "admin" and d["role"] != "admin" and \
                        conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
                    return jsonify({"error": "There must be at least one admin"}), 400
                sets.append("role=?"); vals.append(d["role"])
            if d.get("vacancies") is not None:
                sets.append("vacancies=?"); vals.append(jdump(_clean_names(d["vacancies"])))
            if d.get("departments") is not None:
                sets.append("departments=?"); vals.append(jdump(_valid_depts(conn, d["departments"])))
            if d.get("password") and len(d["password"]) >= 4:
                sets.append("password_hash=?"); vals.append(generate_password_hash(d["password"]))
            if sets:
                sets.append("updated_at=?"); vals.append(now())
                conn.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", (*vals, uid))
        return jsonify({"ok": True})

    @app.delete("/api/users/<int:uid>")
    def user_delete(uid):
        if g.user["role"] != "admin":
            return jsonify({"error": "admin only"}), 403
        if uid == g.user["id"]:
            return jsonify({"error": "Cannot delete your own account"}), 400
        with session() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
        return jsonify({"ok": True})

    # ---- org structure (admin only) ------------------------------------- #
    def _set_hods(conn, dept_name: str, hod_ids: list[int]) -> None:
        """Make exactly `hod_ids` the HODs of `dept_name` (users.departments is the link)."""
        want = {int(i) for i in hod_ids if str(i).isdigit()}
        for u in conn.execute("SELECT id, role, departments FROM users").fetchall():
            cur = jload(u["departments"])
            has = any(x.lower() == dept_name.lower() for x in cur)
            if u["id"] in want and u["role"] == "hod" and not has:
                cur.append(dept_name)
            elif u["id"] not in want and has:
                cur = [x for x in cur if x.lower() != dept_name.lower()]
            else:
                continue
            conn.execute("UPDATE users SET departments=?, updated_at=? WHERE id=?", (jdump(cur), now(), u["id"]))

    @app.get("/api/org")
    def org_get():
        _admin_only()
        with session() as conn:
            depts = _load_depts(conn)
            users = [dict(r) for r in conn.execute(
                "SELECT id, username, display_name, role, departments FROM users ORDER BY username")]
            jds = conn.execute("SELECT code, title, department FROM jds WHERE active=1 ORDER BY code").fetchall()
        for u in users:
            u["departments"] = jload(u["departments"])
        for d in depts:
            d["hods"] = [{"id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"]}
                         for u in users if u["role"] == "hod"
                         and any(x.lower() == d["name"].lower() for x in u["departments"])]
            d["vacancies"] = []
        by_name = {d["name"].lower(): d for d in depts}
        unassigned = []
        for r in jds:
            name, how = _jd_department(r, depts)
            item = {"code": r["code"], "title": r["title"], "how": how}
            if name.lower() in by_name:
                by_name[name.lower()]["vacancies"].append(item)
            else:
                unassigned.append({**item, "department": name})
        return jsonify({"departments": depts, "unassigned": unassigned,
                        "hods": [{"id": u["id"], "username": u["username"],
                                  "display_name": u["display_name"] or u["username"]}
                                 for u in users if u["role"] == "hod"]})

    def _dept_payload(d: dict) -> tuple[str, list[str]]:
        name = re.sub(r"\s+", " ", (d.get("name") or "")).strip()
        if not name or len(name) > 60:
            raise ValueError("Enter a department name (up to 60 characters)")
        return name, _clean_names(d.get("roles") or [])

    @app.post("/api/departments")
    def dept_create():
        _admin_only()
        d = request.get_json(silent=True) or {}
        try:
            name, roles = _dept_payload(d)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        with session() as conn:
            if conn.execute("SELECT 1 FROM departments WHERE lower(name)=lower(?)", (name,)).fetchone():
                return jsonify({"error": f"Department '{name}' already exists"}), 409
            conn.execute("INSERT INTO departments (name, roles, created_at, updated_at) VALUES (?,?,?,?)",
                         (name, jdump(roles), now(), now()))
            _set_hods(conn, name, d.get("hods") or [])
        return jsonify({"ok": True})

    @app.put("/api/departments/<int:did>")
    def dept_update(did):
        _admin_only()
        d = request.get_json(silent=True) or {}
        try:
            name, roles = _dept_payload(d)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        with session() as conn:
            row = conn.execute("SELECT * FROM departments WHERE id=?", (did,)).fetchone()
            if not row:
                abort(404)
            if conn.execute("SELECT 1 FROM departments WHERE lower(name)=lower(?) AND id<>?", (name, did)).fetchone():
                return jsonify({"error": f"Department '{name}' already exists"}), 409
            old = row["name"]
            if old != name:                                  # carry a rename through users and vacancies
                for u in conn.execute("SELECT id, departments FROM users").fetchall():
                    cur = jload(u["departments"])
                    if any(x.lower() == old.lower() for x in cur):
                        conn.execute("UPDATE users SET departments=? WHERE id=?",
                                     (jdump([name if x.lower() == old.lower() else x for x in cur]), u["id"]))
                conn.execute("UPDATE jds SET department=? WHERE lower(department)=lower(?)", (name, old))
                for gr in conn.execute("SELECT id, departments FROM employer_groups").fetchall():
                    cur = jload(gr["departments"])
                    if any(x.lower() == old.lower() for x in cur):
                        conn.execute("UPDATE employer_groups SET departments=? WHERE id=?",
                                     (jdump([name if x.lower() == old.lower() else x for x in cur]), gr["id"]))
            conn.execute("UPDATE departments SET name=?, roles=?, updated_at=? WHERE id=?",
                         (name, jdump(roles), now(), did))
            _set_hods(conn, name, d.get("hods") or [])
        return jsonify({"ok": True})

    @app.delete("/api/departments/<int:did>")
    def dept_delete(did):
        _admin_only()
        with session() as conn:
            row = conn.execute("SELECT name FROM departments WHERE id=?", (did,)).fetchone()
            if not row:
                abort(404)
            _set_hods(conn, row["name"], [])
            conn.execute("UPDATE jds SET department=NULL WHERE lower(department)=lower(?)", (row["name"],))
            for gr in conn.execute("SELECT id, departments FROM employer_groups").fetchall():
                cur = jload(gr["departments"])
                if any(x.lower() == row["name"].lower() for x in cur):
                    conn.execute("UPDATE employer_groups SET departments=? WHERE id=?",
                                 (jdump([x for x in cur if x.lower() != row["name"].lower()]), gr["id"]))
            conn.execute("DELETE FROM departments WHERE id=?", (did,))
        return jsonify({"ok": True})

    @app.post("/api/departments/import")
    def dept_import():
        """Paste lines like  `Technology: Data Analyst, Data Engineer, BI Developer`.
        Existing departments get the new roles added; nothing is removed."""
        _admin_only()
        text = (request.get_json(silent=True) or {}).get("text", "")
        added = merged = 0
        with session() as conn:
            for line in str(text).splitlines():
                if ":" not in line:
                    continue
                name, _, rest = line.partition(":")
                name = re.sub(r"\s+", " ", name).strip()
                roles = _clean_names(rest)
                if not name or len(name) > 60:
                    continue
                row = conn.execute("SELECT id, roles FROM departments WHERE lower(name)=lower(?)", (name,)).fetchone()
                if row:
                    have = jload(row["roles"])
                    mine = have + [r for r in roles if r.lower() not in {h.lower() for h in have}]
                    conn.execute("UPDATE departments SET roles=?, updated_at=? WHERE id=?", (jdump(mine), now(), row["id"]))
                    merged += 1
                else:
                    conn.execute("INSERT INTO departments (name, roles, created_at, updated_at) VALUES (?,?,?,?)",
                                 (name, jdump(roles), now(), now()))
                    added += 1
        if not (added or merged):
            return jsonify({"error": "No lines found. Write one department per line, like  Technology: Data Analyst, Data Engineer"}), 400
        return jsonify({"ok": True, "added": added, "merged": merged})

    @app.put("/api/jds/<code>/department")
    def jd_set_department(code):
        _admin_only()
        dept = ((request.get_json(silent=True) or {}).get("department") or "").strip()
        with session() as conn:
            if not conn.execute("SELECT 1 FROM jds WHERE code=?", (code,)).fetchone():
                abort(404)
            if dept:
                valid = _valid_depts(conn, [dept])
                if not valid:
                    return jsonify({"error": f"No department called '{dept}'"}), 400
                dept = valid[0]
            conn.execute("UPDATE jds SET department=? WHERE code=?", (dept or None, code))
        _set_front_department(code, dept)
        return jsonify({"ok": True})

    # ---- preferred companies (admin only) ------------------------------- #
    def _rescore() -> int:
        with session() as conn:
            return runner.score_all(conn)

    def _group_payload(d: dict) -> dict:
        name = re.sub(r"\s+", " ", (d.get("name") or "")).strip()
        companies = _clean_names(d.get("companies") or [])
        if not name or len(name) > 60:
            raise ValueError("Give the list a name (up to 60 characters)")
        if not companies:
            raise ValueError("Add at least one company")
        return {"name": name, "companies": companies,
                "departments": _clean_names(d.get("departments") or []),
                "roles": _clean_names(d.get("roles") or [])}

    @app.get("/api/employer-groups")
    def groups_get():
        _admin_only()
        with session() as conn:
            rl = rules.Rules(conn)
            vac = conn.execute("SELECT code, title, department FROM jds WHERE active=1 ORDER BY code").fetchall()
            out = []
            for gr in rl.groups:
                uses = []
                for r in vac:
                    dept = rl.department(r["title"], r["department"] or "")[0]
                    if rl.group_applies(gr, r["title"], dept):
                        uses.append({"code": r["code"], "title": r["title"]})
                out.append({**gr, "vacancies": uses, "scope": "all" if not gr["departments"] and not gr["roles"] else "some"})
            return jsonify({"groups": out, "departments": [d["name"] for d in rl.departments]})

    @app.post("/api/employer-groups")
    def group_create():
        _admin_only()
        try:
            p = _group_payload(request.get_json(silent=True) or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        with session() as conn:
            if conn.execute("SELECT 1 FROM employer_groups WHERE lower(name)=lower(?)", (p["name"],)).fetchone():
                return jsonify({"error": f"A list called '{p['name']}' already exists"}), 409
            conn.execute("INSERT INTO employer_groups (name, companies, departments, roles, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?)", (p["name"], jdump(p["companies"]), jdump(p["departments"]),
                                                  jdump(p["roles"]), now(), now()))
        return jsonify({"ok": True, "rescored": _rescore()})

    @app.put("/api/employer-groups/<int:gid>")
    def group_update(gid):
        _admin_only()
        try:
            p = _group_payload(request.get_json(silent=True) or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        with session() as conn:
            if not conn.execute("SELECT 1 FROM employer_groups WHERE id=?", (gid,)).fetchone():
                abort(404)
            if conn.execute("SELECT 1 FROM employer_groups WHERE lower(name)=lower(?) AND id<>?", (p["name"], gid)).fetchone():
                return jsonify({"error": f"A list called '{p['name']}' already exists"}), 409
            conn.execute("UPDATE employer_groups SET name=?, companies=?, departments=?, roles=?, updated_at=? WHERE id=?",
                         (p["name"], jdump(p["companies"]), jdump(p["departments"]), jdump(p["roles"]), now(), gid))
        return jsonify({"ok": True, "rescored": _rescore()})

    @app.delete("/api/employer-groups/<int:gid>")
    def group_delete(gid):
        _admin_only()
        with session() as conn:
            conn.execute("DELETE FROM employer_groups WHERE id=?", (gid,))
        return jsonify({"ok": True, "rescored": _rescore()})

    # ---- search titles (admin only) ------------------------------------- #
    @app.get("/api/title-rules")
    def title_rules_get():
        _admin_only()
        with session() as conn:
            rl = rules.Rules(conn)
            vac = conn.execute("SELECT code, title FROM jds WHERE active=1 ORDER BY code").fetchall()
        out = []
        for r in rl.title_rules:
            uses = [{"code": v["code"], "title": v["title"]} for v in vac if _role_matches(v["title"], [r["role"]])]
            out.append({**r, "vacancies": uses, "suggested": rules.suggest_titles(r["role"])})
        return jsonify({"rules": out})

    def _title_payload(d: dict) -> tuple[str, list[str]]:
        role = re.sub(r"\s+", " ", (d.get("role") or "")).strip()
        if not role or len(role) > 80:
            raise ValueError("Enter the role this applies to (e.g. Data Analyst)")
        return role, _clean_names(d.get("titles") or [])

    @app.post("/api/title-rules")
    def title_rule_create():
        _admin_only()
        try:
            role, titles = _title_payload(request.get_json(silent=True) or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        with session() as conn:
            if conn.execute("SELECT 1 FROM title_rules WHERE lower(role)=lower(?)", (role,)).fetchone():
                return jsonify({"error": f"'{role}' already has a rule - edit that one"}), 409
            conn.execute("INSERT INTO title_rules (role, titles, auto, created_at, updated_at) VALUES (?,?,0,?,?)",
                         (role, jdump(titles), now(), now()))
        return jsonify({"ok": True})

    @app.put("/api/title-rules/<int:rid>")
    def title_rule_update(rid):
        _admin_only()
        try:
            role, titles = _title_payload(request.get_json(silent=True) or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        with session() as conn:
            if not conn.execute("SELECT 1 FROM title_rules WHERE id=?", (rid,)).fetchone():
                abort(404)
            if conn.execute("SELECT 1 FROM title_rules WHERE lower(role)=lower(?) AND id<>?", (role, rid)).fetchone():
                return jsonify({"error": f"'{role}' already has a rule"}), 409
            conn.execute("UPDATE title_rules SET role=?, titles=?, auto=0, updated_at=? WHERE id=?",
                         (role, jdump(titles), now(), rid))
        return jsonify({"ok": True})

    @app.delete("/api/title-rules/<int:rid>")
    def title_rule_delete(rid):
        _admin_only()
        with session() as conn:
            conn.execute("DELETE FROM title_rules WHERE id=?", (rid,))
        return jsonify({"ok": True})

    @app.post("/api/title-rules/generate")
    def title_rules_generate():
        """A rule (with taxonomy suggestions) for every open vacancy that has none."""
        _admin_only()
        with session() as conn:
            n = rules.refresh_auto_titles(conn, force=bool((request.get_json(silent=True) or {}).get("force")))
        return jsonify({"ok": True, "created": n})

    @app.get("/api/title-rules/suggest")
    def title_rules_suggest():
        _admin_only()
        return jsonify({"titles": rules.suggest_titles(request.args.get("role", ""))})

    @app.post("/api/jds/effective")
    def jd_effective():
        """What the central rules add to a vacancy with this title/department (for the form)."""
        _admin_only()
        d = request.get_json(silent=True) or {}
        title = (d.get("title") or "").strip()
        with session() as conn:
            eff = rules.Rules(conn).resolve(title, d.get("department") or "")
        skills = _as_list(d.get("must_have")) + _as_list(d.get("nice_have"))
        sug, _ = rules.suggest_for_jd(title, d.get("text") or "", skills, use_ai=False)
        return jsonify({**eff, "suggested_titles": sug})

    # ---- vacancy templates and bulk upload (admin only) ------------------ #
    TEMPLATE_COLUMNS = [
        ("title", "Job title *"), ("department", "Department"), ("seniority", "Level"),
        ("min_years", "Min years"), ("max_years", "Max years"), ("education_min", "Minimum education"),
        ("openings", "Openings"), ("location", "Location"), ("must_have", "Must-have skills"),
        ("nice_have", "Nice-to-have skills"), ("hard_requirements", "Non-negotiable skills"),
        ("industries", "Industries"), ("preferred_employers", "Preferred employers"),
        ("priority", "Priority"), ("text", "Job description"),
    ]
    TEMPLATE_HELP = {
        "title": "Required. The designation, e.g. Data Analyst. Everything else is optional.",
        "department": "Pick from the list. Leave empty and the portal matches it by role (Org structure tab).",
        "seniority": "intern / junior / mid / senior / lead / manager / director / executive. Empty = worked out from the title.",
        "min_years": "Minimum years of experience, e.g. 3.", "max_years": "Optional upper limit, e.g. 7.",
        "education_min": "intermediate / diploma / bachelors / masters.", "openings": "How many people you are hiring.",
        "location": "Empty = Karachi, Pakistan.",
        "must_have": "Comma separated. Candidates are scored mostly on these. Empty = read from the Job description.",
        "nice_have": "Comma separated bonus skills.",
        "hard_requirements": "Comma separated. A candidate without these gets their score capped.",
        "industries": "Comma separated, e.g. banking, fintech.",
        "preferred_employers": "ONLY companies specific to this vacancy. Shared lists live in the Companies tab and are added automatically.",
        "priority": "0 (normal) to 3 (sourced first).", "text": "Paste the full JD. Skills are read from it when the skill columns are empty.",
    }

    @app.get("/api/templates/vacancies.xlsx")
    def template_xlsx():
        _admin_only()
        from openpyxl import Workbook
        from openpyxl.comments import Comment
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation
        with session() as conn:
            dept_names = [d["name"] for d in _load_depts(conn)]
        wb = Workbook()
        ws = wb.active
        ws.title = "Vacancies"
        head_fill = PatternFill("solid", fgColor="4CB003")
        for i, (key, label) in enumerate(TEMPLATE_COLUMNS, start=1):
            c = ws.cell(row=1, column=i, value=label)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
            c.comment = Comment(TEMPLATE_HELP[key], "Talent Pool")
            ws.column_dimensions[c.column_letter].width = 44 if key == "text" else 26 if key in (
                "must_have", "nice_have", "preferred_employers", "extra_titles", "hard_requirements") else 16
        ws.row_dimensions[1].height = 30
        ws.freeze_panes = "A2"
        col = {k: i for i, (k, _) in enumerate(TEMPLATE_COLUMNS, start=1)}

        def dv(key, formula):
            v = DataValidation(type="list", formula1=formula, allow_blank=True)
            ws.add_data_validation(v)
            letter = ws.cell(row=1, column=col[key]).column_letter
            v.add(f"{letter}2:{letter}500")
        dv("seniority", '"intern,junior,mid,senior,lead,manager,director,executive"')
        dv("education_min", '"intermediate,diploma,bachelors,masters"')
        dv("priority", '"0,1,2,3"')
        if dept_names and len(",".join(dept_names)) < 250:
            dv("department", '"' + ",".join(dept_names) + '"')
        ex = wb.create_sheet("Example")
        for i, (key, label) in enumerate(TEMPLATE_COLUMNS, start=1):
            c = ex.cell(row=1, column=i, value=label)
            c.font = Font(bold=True)
            ex.column_dimensions[c.column_letter].width = ws.column_dimensions[c.column_letter].width
        sample = [
            {"title": "Data Analyst", "department": dept_names[0] if dept_names else "Technology", "seniority": "mid",
             "min_years": 3, "max_years": 7, "education_min": "bachelors", "openings": 2, "location": "Karachi, Pakistan",
             "must_have": "SQL, Power BI, Excel", "nice_have": "Python, Tableau", "hard_requirements": "SQL",
             "industries": "banking, fintech", "priority": 1,
             "text": "Build dashboards and reports for the commercial team."},
            {"title": "Store Manager", "department": "Pharmacy Operations", "seniority": "manager", "min_years": 5,
             "education_min": "bachelors", "openings": 1, "must_have": "Inventory management, Team leadership",
             "text": "Run a pharmacy store end to end."}]
        for r, row in enumerate(sample, start=2):
            for key, v in row.items():
                ex.cell(row=r, column=col[key], value=v)
        how = wb.create_sheet("How to fill")
        how.column_dimensions["A"].width = 26
        how.column_dimensions["B"].width = 110
        how.append(["Column", "What to write"])
        for key, label in TEMPLATE_COLUMNS:
            how.append([label, TEMPLATE_HELP[key]])
        how.append([])
        how.append(["Then", "Fill rows on the Vacancies sheet (one vacancy per row), save, and upload the file with Bulk add in the Vacancies tab. The Example sheet is never read."])
        for c in how[1]:
            c.font = Font(bold=True)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="vacancies_template.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/templates/vacancy.docx")
    def template_docx():
        _admin_only()
        from xml.sax.saxutils import escape
        paras = [("Vacancy template", "title"), ("Type after each colon, save, then load this file in Vacancies. Every field fills itself in. Only Job title is required.", "hint")]
        hints = {"seniority": "intern / junior / mid / senior / lead / manager / director / executive",
                 "education_min": "intermediate / diploma / bachelors / masters",
                 "must_have": "comma separated", "nice_have": "comma separated", "hard_requirements": "comma separated - score is capped without them",
                 "industries": "comma separated", "preferred_employers": "only for this vacancy - shared lists are in the Companies tab",
                 "priority": "0 normal ... 3 highest",
                 "department": "empty = matched by role"}
        for key, label in TEMPLATE_COLUMNS:
            if key == "text":
                continue
            paras.append((label.replace(" *", "") + ": ", "field"))
            if key in hints:
                paras.append(("(" + hints[key] + ")", "hint"))
        paras += [("Job description:", "field"), ("(paste or type the full JD below this line)", "hint"), ("", "field")]

        def run(text, bold=False, grey=False, size=None):
            props = ("<w:b/>" if bold else "") + ('<w:i/><w:color w:val="7A8A70"/>' if grey else "") + \
                    (f'<w:sz w:val="{size}"/>' if size else "")
            return f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'
        body = []
        for text, kind in paras:
            if kind == "title":
                body.append(f"<w:p>{run(text, True, size=36)}</w:p>")
            elif kind == "hint":
                body.append(f"<w:p>{run(text, grey=True, size=18)}</w:p>")
            else:
                label, _, rest = text.partition(":")
                body.append(f"<w:p>{run(label + ':', True)}{run(' ' if rest else '')}</w:p>" if label else "<w:p/>")
        doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
               + "".join(body) + "</w:body></w:document>")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                       '<Default Extension="xml" ContentType="application/xml"/>'
                       '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
            z.writestr("word/document.xml", doc)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="vacancy_template.docx",
                         mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def _read_sheet(name: str, data: bytes) -> tuple[list[tuple[int, dict]], list[str]]:
        """-> ([(row number, {field: value})], headers that were not recognised)."""
        import csv
        ext = Path(name).suffix.lower()
        if ext in (".csv", ".tsv", ".txt"):
            text = data.decode("utf-8-sig", errors="replace")
            delim = "\t" if ext == ".tsv" or text.count("\t") > text.count(",") else ","
            grid = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
        elif ext in (".xlsx", ".xlsm"):
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb["Vacancies"] if "Vacancies" in wb.sheetnames else wb.worksheets[0]
            grid = [list(r) for r in ws.iter_rows(values_only=True)]
        else:
            raise ValueError("use the Excel template (.xlsx) or a .csv file")
        head_i = next((i for i, r in enumerate(grid[:6]) if any(field_for_label(str(c or "")) == "title" for c in r)), None)
        if head_i is None:
            raise ValueError("could not find a 'Job title' column - start from the template")
        headers = [field_for_label(str(c or "")) for c in grid[head_i]]
        unknown = [str(c) for c, h in zip(grid[head_i], headers) if c and not h]
        rows = []
        for n, r in enumerate(grid[head_i + 1:], start=head_i + 2):
            vals = {h: c for h, c in zip(headers, r) if h and c not in (None, "")}
            if vals:
                rows.append((n, vals))
        return rows, unknown

    @app.post("/api/jds/from-sheet")
    def jd_from_sheet():
        _admin_only()
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "choose the filled-in Excel or CSV file first"}), 400
        try:
            rows, unknown = _read_sheet(f.filename or "", f.read())
        except Exception as e:
            return jsonify({"error": f"could not read {f.filename}: {e}"}), 400
        if not rows:
            return jsonify({"error": "no vacancy rows found - fill at least the Job title column"}), 400
        created, failed, skipped = [], [], []
        with session() as conn:
            have = {norm_text(r["title"]): r["code"] for r in conn.execute("SELECT code, title FROM jds WHERE active=1")}
        for n, raw in rows:
            try:
                vals = clean_field_values(raw)
                if not vals.get("title"):
                    raise ValueError("no job title")
                key = norm_text(vals["title"])
                if key in have and request.form.get("allow_duplicates") != "1":
                    skipped.append({"row": n, "title": vals["title"], "code": have[key]})
                    continue
                code = _save_vacancy(vals)
                have[key] = code
                created.append({"row": n, "code": code, "title": vals["title"], "department": vals.get("department", "")})
            except Exception as e:
                failed.append({"row": n, "error": str(e)})
        if created:
            with session() as conn:
                jdmod.sync_dir(conn)
                rules.refresh_auto_titles(conn)
                runner.score_all(conn)
        return jsonify({"created": created, "failed": failed, "skipped": skipped, "unknown_columns": unknown})

    # ---- which vacancies are sourced now (admin only) --------------------- #
    @app.get("/api/sourcing")
    def sourcing_get():
        _admin_only()
        with session() as conn:
            sel = sourcing.selection(conn)
            eff = sourcing.effective_codes(conn)
            vac = [{"code": r["code"], "title": r["title"], "priority": int(r["priority"] or 0),
                    "sourced": r["code"] in eff}
                   for r in conn.execute("SELECT code, title, priority FROM jds WHERE active=1 ORDER BY code")]
        return jsonify({"all": not sel["active"], "until": sel["until"], "vacancies": vac,
                        "sourced": sum(v["sourced"] for v in vac)})

    @app.post("/api/sourcing")
    def sourcing_save():
        _admin_only()
        d = request.get_json(silent=True) or {}
        with session() as conn:
            open_codes = {r["code"] for r in conn.execute("SELECT code FROM jds WHERE active=1")}
            codes = [c for c in (d.get("codes") or []) if c in open_codes]
            everything = bool(d.get("all")) or d.get("codes") is None
            if not everything and not codes:
                return jsonify({"error": "tick at least one vacancy, or choose all vacancies"}), 400
            sel = sourcing.save(conn, None if (everything or len(codes) == len(open_codes)) else codes,
                                bool(d.get("today_only")))
        events.emit("job", "Sourcing: all open vacancies" if not sel["active"] else
                    f"Sourcing only {', '.join(sel['codes'])}" + (" (today only)" if sel["until"] else ""))
        return jsonify({"ok": True, "all": not sel["active"], "until": sel["until"]})

    # ---- collector ingest (token, not a browser login) ------------------- #
    def _ingest_auth():
        token = os.environ.get("INGEST_TOKEN", "").strip()
        if not token:
            abort(404)                                   # switched off unless a token is configured
        got = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(got.encode(), token.encode()):
            abort(make_response(jsonify({"error": "bad token"}), 401))

    @app.get("/api/ingest/vacancies")
    def ingest_vacancies():
        """The open vacancies, as files, for a collector PC to source against."""
        _ingest_auth()
        jd_dir = cfg().path_of("jd_dir")
        out = []
        with session() as conn:
            jdmod.sync_dir(conn)
            kv_set(conn, "collector_last_seen", now())
        with session() as conn:
            wanted = sourcing.effective_codes(conn)
            total = conn.execute("SELECT COUNT(*) FROM jds WHERE active=1").fetchone()[0]
        for p in sorted(jd_dir.glob("*.md")):
            code = "-".join(p.stem.split("-", 2)[:2])             # V-007-pharmacist -> V-007
            if code in wanted:                                    # only the vacancies picked for sourcing now
                out.append({"filename": p.name, "text": p.read_text(encoding="utf-8")})
        li = cfg().get("linkedin", {}) or {}
        return jsonify({"vacancies": out, "sourcing": {"selected": len(out), "total": total},
                        "settings": {"linkedin": {k: li.get(k) for k in SHARED_LINKEDIN_KEYS}}})

    @app.post("/api/ingest/candidates")
    def ingest_candidates():
        """Profiles read on a collector PC (LinkedIn needs a real, visible browser). They are filed,
        scored by the rubric, and the AI agent is started in the background."""
        _ingest_auth()
        items = (request.get_json(silent=True) or {}).get("candidates") or []
        if not isinstance(items, list) or len(items) > 100:
            return jsonify({"error": "send between 1 and 100 candidates per request"}), 400
        created = updated = failed = 0
        errors: list[str] = []
        events.emit("job", f"Collector is sending {len(items)} profile(s)")
        with session() as conn:
            for it in items:
                try:
                    cand = Candidate.from_dict((it or {}).get("candidate") or {})
                    if not cand.full_name:
                        raise ValueError("no name")
                    pdf = (it or {}).get("pdf_b64")
                    if pdf:
                        tmp = Path(tempfile.mkdtemp(prefix="ingest_")) / "profile.pdf"
                        tmp.write_bytes(base64.b64decode(pdf))
                        if tmp.read_bytes()[:4] == b"%PDF":
                            cand.profile_pdf_path = str(tmp)
                    cand.source = cand.source or "collector"
                    _, action, _ = runner.file_candidate(conn, cand)
                    created += action == "created"
                    updated += action == "updated"
                except Exception as e:
                    failed += 1
                    errors.append(f"{type(e).__name__}: {e}"[:200])
            conn.commit()
            scored = runner.score_all(conn, only_new=True, trace=True)
        with session() as conn:
            kv_set(conn, "collector_last_push", jdump({"at": now(), "created": created, "updated": updated,
                                                       "failed": failed}))
        started = False
        if (created or updated) and agent.available()[0] and not JOB.busy():
            def run_agent_after(j):
                with session() as conn:
                    n, notes = runner.agent_review(conn, should_stop=j.stop_flag.is_set)
                    for line in notes:
                        j.say(line)
            started = JOB.start("agent", run_agent_after)
        return jsonify({"ok": True, "created": created, "updated": updated, "failed": failed,
                        "errors": errors[:5], "scored": scored, "agent_started": started})

    # ---- settings (admin only) ----------------------------------------- #
    @app.get("/api/settings")
    def settings_get():
        if g.user["role"] != "admin":
            return jsonify({"error": "admin only"}), 403
        li = cfg().get("linkedin", {}) or {}
        keys = ("daily_profile_cap", "ramp_up", "window_start", "window_end", "weekdays_only",
                "session_minutes_max", "location_filter", "deep_experience", "geo_urn",
                "max_pages_per_search", "profiles_per_vacancy", "profiles_per_run", "max_rounds",
                "company_mode", "browser")
        ag = agent.settings()
        with session() as conn:
            seen, push = kv_get(conn, "collector_last_seen"), kv_get(conn, "collector_last_push")
        from ..linkedin_browser import find_browsers
        return jsonify({"linkedin": {k: li.get(k) for k in keys}, "browsers_found": sorted(find_browsers()),
                        "collector": {"last_seen": seen, "last_push": jload(push, {}) if push else {}},
                        "threshold": cfg().get("scoring.default_shortlist_threshold", 75),
                        "retention_days": cfg().get("retention.delete_after_days", 365),
                        "city": cfg().get("location.target_city", "Karachi"),
                        "weights": cfg().weights,
                        "agent": {
                            "provider": agent.provider(), "model": ag.get("model") or "",
                            "top_n": int(ag.get("top_n", 25)), "min_rubric": float(ag.get("min_rubric", 45)),
                            "weight": float(ag.get("weight", 0.35)),
                            "ready": agent.available()[0], "why": agent.available()[1],
                            "providers": [{"id": k, "label": v["label"], "env": v["env"], "default_model": v["model"],
                                           "has_key": agent.has_key(k)} for k, v in agent.PROVIDERS.items()],
                        }})

    @app.post("/api/settings")
    def settings_save():
        if g.user["role"] != "admin":
            return jsonify({"error": "admin only"}), 403
        d = request.get_json(silent=True) or {}
        li_in = d.get("linkedin", {}) or {}
        upd: dict = {"linkedin": {}}
        if "daily_profile_cap" in li_in:
            upd["linkedin"]["daily_profile_cap"] = max(1, min(int(li_in["daily_profile_cap"]), 150))
        for k in ("window_start", "window_end"):
            if k in li_in and re.match(r"^\d{1,2}:\d{2}$", str(li_in[k])):
                upd["linkedin"][k] = str(li_in[k])
        for k in ("ramp_up", "weekdays_only", "deep_experience"):
            if k in li_in:
                upd["linkedin"][k] = bool(li_in[k])
        if "session_minutes_max" in li_in:
            upd["linkedin"]["session_minutes_max"] = max(5, min(int(li_in["session_minutes_max"]), 90))
        if "max_pages_per_search" in li_in:
            upd["linkedin"]["max_pages_per_search"] = max(1, min(int(li_in["max_pages_per_search"]), 10))
        for k, hi in (("profiles_per_run", 500), ("max_rounds", 50)):
            if k in li_in:
                upd["linkedin"][k] = max(0, min(int(li_in[k] or 0), hi))
        if "profiles_per_vacancy" in li_in:
            upd["linkedin"]["profiles_per_vacancy"] = max(1, min(int(li_in["profiles_per_vacancy"]), 50))
        if li_in.get("browser") in {"auto", "chrome", "edge"}:
            upd["linkedin"]["browser"] = li_in["browser"]
        if li_in.get("company_mode") in {"priority_first", "only", "ignore"}:
            upd["linkedin"]["company_mode"] = li_in["company_mode"]
        if li_in.get("location_filter") in {"metro", "country", "any"}:
            upd["linkedin"]["location_filter"] = li_in["location_filter"]
        if "geo_urn" in li_in:
            raw = str(li_in["geo_urn"] or "").strip()
            m = re.search(r"geoUrn=([^&\s]+)", raw)
            upd["linkedin"]["geo_urn"] = m.group(1) if m else (raw if re.match(r"^[%\w\[\]\"',-]*$", raw) else "")
        if "threshold" in d:
            upd.setdefault("scoring", {})["default_shortlist_threshold"] = max(1, min(float(d["threshold"]), 100))
        if "retention_days" in d:
            upd.setdefault("retention", {})["delete_after_days"] = max(30, int(d["retention_days"]))
        if "weights" in d and isinstance(d["weights"], dict):
            valid_keys = {"must_have_skills", "nice_have_skills", "experience_years",
                          "title_relevance", "seniority_fit", "domain_industry",
                          "education", "location", "certifications", "projects"}
            w = {}
            for k, v in d["weights"].items():
                if k in valid_keys:
                    w[k] = max(0, min(100, float(v)))
            if w:
                upd.setdefault("scoring", {})["weights"] = w
        if "agent" in d:
            a_in = d["agent"] or {}
            ag = upd.setdefault("scoring", {}).setdefault("agent", {})
            if a_in.get("provider") in ("off", *agent.PROVIDERS):
                ag["provider"] = a_in["provider"]
                if a_in["provider"] == "off":                  # also switch off the legacy Claude flag
                    upd["scoring"].setdefault("llm_rerank", {})["enabled"] = False
            if "model" in a_in:
                m = str(a_in["model"] or "").strip()
                ag["model"] = m if re.fullmatch(r"[A-Za-z0-9._:/-]{0,80}", m) else ""
            if "top_n" in a_in:
                ag["top_n"] = max(1, min(int(a_in["top_n"]), 100))
            if "min_rubric" in a_in:
                ag["min_rubric"] = max(0.0, min(float(a_in["min_rubric"]), 100.0))
            if "weight" in a_in:
                ag["weight"] = max(0.0, min(float(a_in["weight"]), 0.5))
        cfg().save_ui(upd)
        return jsonify({"ok": True})

    # ---- agent activity (admin only) ------------------------------------- #
    @app.get("/api/events")
    def events_get():
        _admin_only()
        a = request.args
        with session() as conn:
            rows = events.list_events(conn, after=int(a.get("after") or 0), limit=int(a.get("limit") or 200),
                                      stage=a.get("stage", ""), level=a.get("level", ""), candidate=a.get("candidate", ""))
            st = events.status(conn, JOB.state)
            st["counts"] = events.counts(conn, 60)
        st["job"] = JOB.kind if JOB.busy() else ""
        st["agent"] = {"provider": agent.provider(), "model": agent.model(), "ready": agent.available()[0],
                       "why": agent.available()[1]}
        return jsonify({"events": rows, "status": st})

    @app.get("/api/backup")
    def backup():
        """Everything that is not code: the database (consistent copy), vacancies, UI settings and,
        with ?pdfs=1, the PDF library. Contains password hashes and personal data - keep it private."""
        _admin_only()
        import sqlite3 as _sq
        tmp = Path(tempfile.mkdtemp(prefix="backup_"))
        dst_db = tmp / "sourcer.db"
        from ..db import connect
        src = connect()
        out = _sq.connect(str(dst_db))
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
        zpath = tmp / f"talent-pool-backup-{date.today().isoformat()}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(dst_db, "sourcer.db")
            jd_dir = cfg().path_of("jd_dir")
            for p in sorted(jd_dir.glob("*.md")):
                z.write(p, f"jds/{p.name}")
            if cfg().ui_path.is_file():
                z.write(cfg().ui_path, "settings.ui.yaml")
            if request.args.get("pdfs") == "1":
                root = cfg().path_of("storage.profiles_dir")
                for p in sorted(root.rglob("*.pdf")):
                    z.write(p, f"profiles/{p.relative_to(root).as_posix()}")
        return send_file(zpath, as_attachment=True, download_name=zpath.name)

    @app.delete("/api/events")
    def events_clear():
        _admin_only()
        with session() as conn:
            conn.execute("DELETE FROM events")
        return jsonify({"ok": True})

    @app.get("/api/agent/models")
    def agent_models():
        _admin_only()
        if agent.provider() != "groq":
            return jsonify({"models": [], "note": "Live model lists are available for Groq."})
        ms = agent.groq_models()
        return jsonify({"models": ms, "current": agent.model(),
                        "note": "" if ms else "Could not fetch the list - check GROQ_API_KEY."})

    @app.post("/api/agent/test")
    def agent_test():
        _admin_only()
        events.emit("agent", f"Connection test with {agent.PROVIDERS.get(agent.provider(), {}).get('label', 'no provider')}")
        r = agent.test_connection()
        events.emit("agent" if r["ok"] else "error",
                    f"Connection test OK in {r.get('seconds')}s - the agent scored a made-up profile {r.get('score'):.0f}%"
                    if r["ok"] else f"Connection test failed: {r['error']}", level="info" if r["ok"] else "error")
        return jsonify(r), (200 if r["ok"] else 400)

    @app.get("/api/job")
    def job():
        return jsonify(JOB.as_dict())

    return app


def _port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        return sk.connect_ex(("127.0.0.1", port)) != 0


def serve(port: int = 8765, open_browser: bool = True) -> None:
    from .. import __version__
    app = create_app()
    # An older copy that is still running would keep answering on this port and the
    # browser would show the OLD screens. Never fall back to it silently.
    start = port
    while not _port_free(port) and port < start + 20:
        port += 1
    if port != start:
        print(f"\n  NOTE: port {start} is already in use - probably an older copy of this app is still open.\n"
              f"  Close that window (or restart the PC) to avoid confusion. Using port {port} for this one.\n")
    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    print(f"Dvago Talent Pool v{__version__} is running at http://127.0.0.1:{port}/   (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
