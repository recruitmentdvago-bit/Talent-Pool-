"""Command line interface.

    sourcer init                      one-time setup
    sourcer jds                       list vacancies the bot knows about
    sourcer plan                      search strings / links per vacancy
    sourcer run                       the daily job: intake, dedupe, file, score
    sourcer query "data analyst" --min-score 75
    sourcer show 42                   why candidate 42 scored what they scored
    sourcer status 42 shortlisted
    sourcer stats | sources | purge | rescore
"""
from __future__ import annotations

import argparse
import shutil
import sys
import textwrap
from pathlib import Path

from . import jd as jdmod
from . import report, runner, store
from .config import ROOT, cfg
from .db import jload, session
from .searchplan import build as build_plan


def _p(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- #
def cmd_init(args) -> int:
    c = cfg()
    for key in ("storage.profiles_dir", "storage.pdf_inbox", "storage.exports_dir", "jd_dir"):
        c.path_of(key).mkdir(parents=True, exist_ok=True)
    (c.root / c.get("sources.csv.watch_dir", "storage/imports")).mkdir(parents=True, exist_ok=True)
    settings = c.root / "config" / "settings.yaml"
    if not settings.exists():
        sample = c.root / "config" / "settings.example.yaml"
        if sample.exists():
            shutil.copy2(sample, settings)
            _p(f"wrote {settings}")
    with session() as conn:
        jdmod.sync_dir(conn)
        n = conn.execute("SELECT COUNT(*) FROM jds").fetchone()[0]
    _p(f"database ready at {c.path_of('storage.db_path')}")
    _p(f"vacancies loaded: {n}")
    _p(f"\nnext: drop profile PDFs into {c.path_of('storage.pdf_inbox')} then run 'sourcer run'")
    return 0


def cmd_jds(args) -> int:
    with session() as conn:
        synced = jdmod.sync_dir(conn)
        if not synced:
            _p(f"no JD files in {cfg().path_of('jd_dir')} - add a .md or .txt file.")
            return 1
        _p(f"{'code':<12}{'title':<32}{'fam':<16}{'sen':<9}{'yrs':<9}{'open':<6}{'must-have'}")
        _p("-" * 128)
        for _, j, changed in synced:
            yrs = f"{j.min_years:g}+" + (f"-{j.max_years:g}" if j.max_years else "")
            _p(f"{j.code:<12}{j.title[:31]:<32}{(j.title_family or '-')[:15]:<16}"
               f"{j.seniority:<9}{yrs:<9}{j.openings:<6}"
               f"{', '.join(j.must_have[:6])}{'  [updated]' if changed else ''}")
    return 0


def cmd_plan(args) -> int:
    with session() as conn:
        jds = [j for _, j in jdmod.load_all(conn)]
    if args.jd:
        jds = [j for j in jds if j.code.lower() == args.jd.lower()]
        if not jds:
            _p(f"no active vacancy with code '{args.jd}'")
            return 1
    text = "\n\n".join(build_plan(j).as_text() for j in jds)
    if args.out:
        Path(args.out).write_text(text)
        _p(f"wrote {args.out}")
    else:
        _p(text)
    return 0


def cmd_run(args) -> int:
    s = runner.run(sources=args.source or None, dry_run=args.dry_run,
                   only_new_scores=args.only_new, max_results=args.max, log=print)
    _p(s.as_text())
    if not args.dry_run and s.created + s.updated:
        thr = float(cfg().get("scoring.default_shortlist_threshold", 75))
        with session() as conn:
            rows = report.query(conn, role=None, min_score=thr, limit=10)
        if rows:
            _p(f"\ntop matches at or above {thr:g}%:")
            _p(report.as_table(rows))
    return 1 if s.errors else 0


def cmd_query(args) -> int:
    with session() as conn:
        if args.role:
            matched = report.resolve_jds(conn, args.role)
            if not matched:
                codes = [r["code"] for r in conn.execute(
                    "SELECT code FROM jds WHERE active=1 ORDER BY code")]
                _p(f"'{args.role}' does not match any open vacancy.")
                _p(f"open vacancies: {', '.join(codes) or 'none - add a JD first'}")
                _p("match on a code (DA-001), a title (\"data analyst\"), or several "
                   "with a slash.")
                return 2
            _p(f"vacancies: {', '.join(r['code'] + ' ' + r['title'] for r in matched)}")
        rows = report.query(conn, role=args.role, min_score=args.min_score,
                            max_score=args.max_score, limit=args.limit,
                            status=args.status, city=args.city,
                            include_contacted=not args.new_only)
        if not rows:
            thr = args.min_score if args.min_score is not None else \
                cfg().get("scoring.default_shortlist_threshold", 75)
            _p(f"no candidates for '{args.role or 'any vacancy'}' at or above {thr}%.")
            _p("try a lower --min-score, or run 'sourcer run' after adding profiles.")
            return 1
        _p(report.as_table(rows))
        _p(f"\n{len(rows)} candidate(s). "
           f"'sourcer show <id>' for a full breakdown.")

        want = set((args.export or "").split(",")) - {""}
        if "all" in want:
            want = {"csv", "xlsx", "pdf"}
        label = (args.role or "all").replace("/", "-").replace(" ", "-")[:30]
        if "csv" in want:
            _p(f"csv   : {report.export_csv(rows, args.out)}")
        if "xlsx" in want:
            _p(f"xlsx  : {report.export_xlsx(rows, args.out)}")
        if "pdf" in want:
            _p(f"bundle: {report.export_pdf_bundle(rows, args.out, label=label)}")
    return 0


def cmd_show(args) -> int:
    with session() as conn:
        c = store.load(conn, args.candidate_id)
        if not c:
            _p(f"no candidate with id {args.candidate_id}")
            return 1
        _p(f"{c.full_name}   ({getattr(c, 'status', 'new')})")
        _p(f"  {c.headline or '-'}")
        _p(f"  {c.location_raw or '-'} · {c.email or 'no email'} · {c.phone or 'no phone'}")
        if c.linkedin_url:
            _p(f"  {c.linkedin_url}")
        _p(f"  {c.total_years():g} yrs total · {c.education_level()} · {c.seniority()}")
        row = conn.execute("SELECT profile_pdf_path FROM candidates WHERE id=?",
                           (args.candidate_id,)).fetchone()
        if row and row["profile_pdf_path"]:
            _p(f"  pdf: {row['profile_pdf_path']}")
        _p("\n  experience")
        for e in c.experiences:
            span = f"{e.start_date or '?'} - {'Present' if e.is_current else (e.end_date or '?')}"
            _p(f"    {e.title or '?'} · {e.company or '?'} · {span} ({e.months()/12:.1f}y)")
        if c.educations:
            _p("  education")
            for ed in c.educations:
                _p(f"    {ed.degree} {ed.field_of_study} · {ed.institution} {ed.end_year or ''}")
        _p("  skills: " + ", ".join(sorted(s.replace('x:', '').replace('_', ' ')
                                           for s in c.skills())))

        q = ("SELECT s.*, j.code, j.title FROM scores s JOIN jds j "
             "ON j.id=s.jd_id AND j.version=s.jd_version WHERE s.candidate_id=? ")
        params = [args.candidate_id]
        if args.jd:
            q += "AND LOWER(j.code)=? "
            params.append(args.jd.lower())
        for r in conn.execute(q + "ORDER BY s.score DESC", params):
            _p(f"\n  === {r['code']} · {r['title']} ===")
            _p(f"  score {r['score']:.1f}/100  (rubric {r['rubric_score']:.1f}, "
               f"semantic {r['semantic']:.2f})")
            for dim, v in (jload(r["breakdown"], {}) or {}).items():
                raw = (v or {}).get("raw", 0)
                bar = "#" * int(round(raw * 20))
                _p(f"    {dim:<20}{raw*100:5.1f}%  {bar:<20} "
                   f"{(v or {}).get('points', 0):5.1f}/{(v or {}).get('weight', 0):g} pts"
                   f"   {(v or {}).get('note', '')}")
            for lbl, key in (("matched", "matched"), ("missing", "missing"), ("flags", "flags")):
                vals = jload(r[key])
                if vals:
                    _p(f"    {lbl:<9}: " + ", ".join(str(v).replace('x:', '').replace('_', ' ')
                                                     for v in vals))
    return 0


def cmd_status(args) -> int:
    allowed = {"new", "reviewed", "shortlisted", "contacted", "rejected", "hired"}
    if args.status not in allowed:
        _p(f"status must be one of: {', '.join(sorted(allowed))}")
        return 2
    with session() as conn:
        if not store.load(conn, args.candidate_id):
            _p(f"no candidate with id {args.candidate_id}")
            return 1
        store.set_status(conn, args.candidate_id, args.status, args.note)
    _p(f"candidate {args.candidate_id} -> {args.status}")
    return 0


def cmd_stats(args) -> int:
    with session() as conn:
        q = lambda s, *p: conn.execute(s, p).fetchone()
        total = q("SELECT COUNT(*) FROM candidates")[0]
        _p(f"candidates      : {total}")
        _p(f"vacancies active: {q('SELECT COUNT(*) FROM jds WHERE active=1')[0]}")
        _p(f"scores stored   : {q('SELECT COUNT(*) FROM scores')[0]}")
        pdfs = q("SELECT COUNT(*) FROM candidates WHERE IFNULL(pdf_sha256,'') <> ''")[0]
        _p(f"pdfs filed      : {pdfs}")
        if total:
            _p("\nby status")
            for r in conn.execute("SELECT status, COUNT(*) n FROM candidates "
                                  "GROUP BY status ORDER BY n DESC"):
                _p(f"  {r['status'] or 'new':<14}{r['n']}")
            _p("\nby city")
            for r in conn.execute("SELECT location_city, COUNT(*) n FROM candidates "
                                  "GROUP BY location_city ORDER BY n DESC LIMIT 8"):
                _p(f"  {(r['location_city'] or 'unknown')[:20]:<22}{r['n']}")
            _p("\npipeline per vacancy (candidates at or above threshold)")
            thr = float(cfg().get("scoring.default_shortlist_threshold", 75))
            for r in conn.execute(
                    "SELECT j.code, j.title, j.openings, "
                    "SUM(CASE WHEN s.score >= ? THEN 1 ELSE 0 END) hits, "
                    "MAX(s.score) best, COUNT(*) scored "
                    "FROM jds j LEFT JOIN scores s ON s.jd_id=j.id AND s.jd_version=j.version "
                    "WHERE j.active=1 GROUP BY j.id ORDER BY j.code", (thr,)):
                _p(f"  {r['code']:<10}{(r['title'] or '')[:26]:<28}"
                   f"{r['hits'] or 0:>4} / {r['openings']} needed"
                   f"   best {r['best'] or 0:.0f}%   ({r['scored'] or 0} scored)")
        _p("\nrecent runs")
        for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5"):
            _p(f"  #{r['id']:<4}{(r['started_at'] or '')[:19]:<22}"
               f"fetched {r['fetched']:<5}new {r['created']:<5}"
               f"scored {r['scored']:<7}errors {r['errors']}")
    return 0


def cmd_sources(args) -> int:
    from .sources import REGISTRY, get
    enabled = set(cfg().get("sources.enabled", []))
    _p(f"{'source':<18}{'enabled':<10}{'ready':<8}detail")
    _p("-" * 100)
    for name in sorted(REGISTRY):
        try:
            ok, why = get(name).available()
        except Exception as e:
            ok, why = False, f"{type(e).__name__}: {e}"
        _p(f"{name:<18}{'yes' if name in enabled else 'no':<10}"
           f"{'yes' if ok else 'no':<8}{why}")
    return 0


def cmd_purge(args) -> int:
    if not args.yes:
        _p("this permanently deletes candidate records past their retention date.")
        _p("re-run with --yes to confirm.")
        return 1
    with session() as conn:
        n = store.purge_expired(conn)
    _p(f"purged {n} record(s)")
    return 0


def cmd_ui(args) -> int:
    from .web.app import serve
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_collect(args) -> int:
    import os
    from .collector import collect
    url = args.url or os.environ.get("PORTAL_URL", "")
    token = args.token or os.environ.get("INGEST_TOKEN", "")
    if not url or not token:
        _p("need --url and --token (or PORTAL_URL and INGEST_TOKEN in the environment)")
        return 2
    return collect(url, token, limit=args.limit, push_only=args.push_only, log=_p, now=args.now)


def cmd_rescore(args) -> int:
    with session() as conn:
        n = runner.score_all(conn, only_new=False)
    _p(f"rescored {n} candidate-vacancy pairs")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sourcer", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Candidate sourcing, scoring and shortlisting for HR teams.",
        epilog=textwrap.dedent("""\
            typical day
              sourcer plan                      # get today's search strings
              ... save promising profiles as PDF into storage/pdf_inbox ...
              sourcer run                       # parse, dedupe, file, score
              sourcer query "data analyst" --min-score 75 --export xlsx,pdf
        """))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create folders, database and load JDs").set_defaults(f=cmd_init)
    sub.add_parser("jds", help="list vacancies (re-reads config/jds)").set_defaults(f=cmd_jds)

    p = sub.add_parser("plan", help="boolean searches and links per vacancy")
    p.add_argument("--jd", help="single vacancy code, e.g. DA-001")
    p.add_argument("--out", help="write to a file instead of stdout")
    p.set_defaults(f=cmd_plan)

    p = sub.add_parser("run", help="the daily job")
    p.add_argument("--source", action="append", help="limit to one source (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    p.add_argument("--only-new", action="store_true",
                   help="skip pairs already scored at the current JD version")
    p.add_argument("--max", type=int, help="cap profiles taken in this run")
    p.set_defaults(f=cmd_run)

    p = sub.add_parser("query", help="ask for candidates")
    p.add_argument("role", nargs="?", help='e.g. "data analyst/data engineer" or DA-001')
    p.add_argument("--min-score", type=float, help="default from settings (75)")
    p.add_argument("--max-score", type=float)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--city")
    p.add_argument("--status", help="new|reviewed|shortlisted|contacted|rejected|hired")
    p.add_argument("--new-only", action="store_true", help="hide contacted/rejected/hired")
    p.add_argument("--export", help="csv, xlsx, pdf, or all (comma separated)")
    p.add_argument("--out", help="output path for the export")
    p.set_defaults(f=cmd_query)

    p = sub.add_parser("show", help="full score breakdown for one candidate")
    p.add_argument("candidate_id", type=int)
    p.add_argument("--jd", help="limit to one vacancy code")
    p.set_defaults(f=cmd_show)

    p = sub.add_parser("status", help="move a candidate through the pipeline")
    p.add_argument("candidate_id", type=int)
    p.add_argument("status")
    p.add_argument("--note")
    p.set_defaults(f=cmd_status)

    sub.add_parser("stats", help="database and pipeline overview").set_defaults(f=cmd_stats)
    sub.add_parser("sources", help="which intake routes are configured").set_defaults(f=cmd_sources)
    sub.add_parser("rescore", help="recompute every score").set_defaults(f=cmd_rescore)

    p = sub.add_parser("ui", help="open the web app in your browser")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p.set_defaults(f=cmd_ui)

    p = sub.add_parser("collect", help="source LinkedIn on this PC and send the profiles to the cloud portal")
    p.add_argument("--url", help="the portal address, e.g. https://your-app.up.railway.app")
    p.add_argument("--token", help="the portal's INGEST_TOKEN")
    p.add_argument("--limit", type=int, help="open at most this many profiles")
    p.add_argument("--push-only", action="store_true", help="skip sourcing, just send what is already here")
    p.add_argument("--now", action="store_true", help="ignore the working hours set in the portal")
    p.set_defaults(f=cmd_collect)

    p = sub.add_parser("purge", help="delete records past their retention date")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(f=cmd_purge)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.f(args) or 0)
    except KeyboardInterrupt:
        _p("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
