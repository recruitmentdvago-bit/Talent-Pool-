"""The daily pipeline: sync JDs -> fetch -> dedupe -> file PDFs -> score -> report."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import jd as jdmod
from . import store
from .config import cfg
from .db import audit, jdump, now, session
from .models import JD, Candidate
from .pdfstore import store_pdf
from . import agent, events, llm
from .scoring import ScoreResult, score_candidate
from .semantic import TfidfSpace
from .sources import FetchContext, build_enabled


@dataclass
class RunSummary:
    run_id: int = 0
    sources: list[str] = field(default_factory=list)
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    scored: int = 0
    pdfs_written: int = 0
    purged: int = 0
    reranked: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_text(self) -> str:
        lines = [
            f"run #{self.run_id} finished in {self.seconds:.1f}s",
            f"  sources   : {', '.join(self.sources) or 'none available'}",
            f"  fetched   : {self.fetched}",
            f"  new       : {self.created}",
            f"  updated   : {self.updated}",
            f"  pdfs      : {self.pdfs_written}",
            f"  scored    : {self.scored} candidate-vacancy pairs",
        ]
        if self.reranked:
            lines.append(f"  AI agent  : read {self.reranked} profile(s)")
        if self.purged:
            lines.append(f"  purged    : {self.purged} (retention expiry)")
        for n in self.notes:
            lines.append(f"  note      : {n}")
        for e in self.errors:
            lines.append(f"  ERROR     : {e}")
        return "\n".join(lines)


def _persist_score(conn: sqlite3.Connection, cid: int, jd_id: int, jd_version: int,
                   r: ScoreResult) -> float:
    """Write the rubric score. If the AI agent already read this profile for this vacancy
    version, keep its reading and re-blend it, so rescoring never throws it away."""
    old = conn.execute("SELECT llm_score, llm_verdict, llm_notes FROM scores "
                       "WHERE candidate_id=? AND jd_id=? AND jd_version=?", (cid, jd_id, jd_version)).fetchone()
    final = r.score
    if old and old["llm_score"] is not None:
        final = agent.blend(r.rubric, old["llm_score"])
    conn.execute(
        "INSERT OR REPLACE INTO scores (candidate_id, jd_id, jd_version, score, rubric_score,"
        " semantic, breakdown, matched, missing, flags, rationale, scored_at, llm_score, llm_verdict, llm_notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, jd_id, jd_version, final, r.rubric, r.semantic, jdump(r.breakdown()),
         jdump(r.matched), jdump(r.missing), jdump(r.flags), r.rationale, now(),
         old["llm_score"] if old else None, old["llm_verdict"] if old else None,
         old["llm_notes"] if old else None))
    return final


def score_all(conn: sqlite3.Connection, only_new: bool = False,
              progress=None, trace: bool = False) -> int:
    """Score every candidate against every open vacancy.

    One TF-IDF space is fitted over the whole corpus so a score means the same
    thing today as it did yesterday. `only_new` skips pairs already scored at
    the current JD version.
    """
    jds = jdmod.load_all(conn, active_only=True)
    if not jds:
        return 0
    cands = store.load_all(conn)
    if not cands:
        return 0

    space = TfidfSpace([j.jd_text() for _, j in jds] + [c.profile_text() for c in cands])
    existing: set[tuple[int, int, int]] = set()
    if only_new:
        existing = {(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT candidate_id, jd_id, jd_version FROM scores")}

    n = 0
    if trace:
        events.emit("score", f"Scoring {len(cands)} candidate(s) against {len(jds)} vacancy(ies) with the rubric")
    for c in cands:
        cid = getattr(c, "db_id", None)
        if cid is None:
            continue
        best: tuple[float, str] | None = None
        for jd_id, j in jds:
            if only_new and (cid, jd_id, j.version) in existing:
                continue
            final = _persist_score(conn, cid, jd_id, j.version, score_candidate(c, j, space))
            n += 1
            if best is None or final > best[0]:
                best = (final, j.code)
        if trace and best:
            events.emit("score", f"Rubric score {best[0]:.0f}% (best match {best[1]})", candidate=c.full_name,
                        vacancy=best[1])
        if progress:
            progress(c)
    # drop scores for JD versions that no longer exist
    conn.execute("DELETE FROM scores WHERE (jd_id, jd_version) NOT IN "
                 "(SELECT id, version FROM jds)")
    return n


def file_candidate(conn: sqlite3.Connection, cand: Candidate) -> tuple[int, str, int]:
    """Save one candidate and make sure there is a filed PDF. -> (id, 'created'|'updated'|..., pdfs written)."""
    cid, action = store.upsert(conn, cand)
    events.emit("store", f"Saved to the talent pool ({action})", candidate=cand.full_name)
    if action == "updated":                           # the profile changed: the agent must read it again
        conn.execute("UPDATE scores SET llm_score=NULL, llm_verdict=NULL, llm_notes=NULL WHERE candidate_id=?", (cid,))
    row = conn.execute("SELECT profile_pdf_path, pdf_sha256 FROM candidates WHERE id=?", (cid,)).fetchone()
    need = not row["profile_pdf_path"] or not Path(row["profile_pdf_path"]).exists()
    wrote = 0
    if need or not row["pdf_sha256"]:
        path, sha, reused = store_pdf(cand, src=cand.profile_pdf_path or None, candidate_key=cand.person_key())
        conn.execute("UPDATE candidates SET profile_pdf_path=?, pdf_sha256=? WHERE id=?", (str(path), sha, cid))
        wrote = 0 if reused else 1
    return cid, action, wrote


def agent_review(conn: sqlite3.Connection, should_stop=None, vacancy: str | None = None,
                 force: bool = False) -> tuple[int, list[str]]:
    """The AI agent reads the best-scoring profiles of each open vacancy and gives its own score.

    The rubric score stays in `rubric_score`; `score` becomes the blend; `llm_score`,
    `llm_verdict` and `llm_notes` record what the agent said and why. Profiles it already read
    (same vacancy version) are skipped unless `force`, so a free-tier quota is not wasted."""
    ok, why = agent.available()
    if not ok:
        return 0, ([] if agent.provider() == "off" else [f"AI agent skipped: {why}"])
    conf = agent.settings()
    top_n, min_rubric = int(conf.get("top_n", 25)), float(conf.get("min_rubric", 45))
    pause = float(conf.get("pause_seconds", agent.PROVIDERS[agent.provider()]["pause"]))
    total, notes = 0, []
    stopped = False
    events.emit("agent", f"AI agent ({agent.PROVIDERS[agent.provider()]['label']}, {agent.model()}) is starting")

    for jd_id, jd in jdmod.load_all(conn, active_only=True):
        if stopped or (should_stop and should_stop()):
            break
        if vacancy and jd.code != vacancy:
            continue
        rows = conn.execute(
            "SELECT candidate_id, score, rubric_score, llm_score FROM scores WHERE jd_id=? AND jd_version=? "
            "AND COALESCE(rubric_score, score) >= ? ORDER BY COALESCE(rubric_score, score) DESC LIMIT ?",
            (jd_id, jd.version, min_rubric, top_n)).fetchall()
        todo = [r for r in rows if force or r["llm_score"] is None]
        if not rows:
            events.emit("agent", f"{jd.code}: nobody scores {min_rubric:.0f}% or more on the rubric yet - nothing to read",
                        vacancy=jd.code)
            continue
        events.emit("agent", f"{jd.code} {jd.title}: {len(todo)} profile(s) to read "
                    f"({len(rows) - len(todo)} already read)", vacancy=jd.code)
        for r in todo:
            if should_stop and should_stop():
                stopped = True
                events.emit("agent", "Stopped on request", level="warn")
                break
            c = store.load(conn, r["candidate_id"])
            if not c:
                continue
            base = r["rubric_score"] if r["rubric_score"] is not None else r["score"]
            events.emit("agent", f"Reading the profile against {jd.code} (rubric says {base:.0f}%)",
                        candidate=c.full_name, vacancy=jd.code)
            t0 = time.time()
            try:
                a, usage = agent.assess(jd, r["candidate_id"], c, label=c.full_name)
            except agent.AgentError as e:
                events.emit("error", f"Agent could not score this profile: {e}", candidate=c.full_name,
                            vacancy=jd.code, level="error")
                notes.append(f"AI agent: {c.full_name}: {e}")
                if e.fatal:
                    stopped = True
                    events.emit("agent", "Stopping the agent for this run - fix the problem above and run it again",
                                level="warn")
                    break
                continue
            final = agent.blend(base, a.score)
            conn.execute(
                "UPDATE scores SET score=?, llm_score=?, llm_verdict=?, llm_notes=? "
                "WHERE candidate_id=? AND jd_id=? AND jd_version=?",
                (final, a.score, a.verdict, jdump({"reasons": a.reasons, "concerns": a.concerns}),
                 r["candidate_id"], jd_id, jd.version))
            conn.commit()
            total += 1
            tok = (usage.get("total_tokens") or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)))
            events.emit("agent", f"Read it in {time.time() - t0:.1f}s: {a.verdict.replace('_', ' ')} - agent {a.score:.0f}%, "
                        f"rubric {base:.0f}% -> final {final:.0f}%", candidate=c.full_name, vacancy=jd.code,
                        detail={"reasons": a.reasons, "concerns": a.concerns, "tokens": tok})
            if pause:
                time.sleep(pause)
    events.emit("agent", f"AI agent finished: {total} profile(s) read")
    return total, notes


rerank_shortlists = agent_review          # the name the old pipeline and tests knew


def run(sources: list[str] | None = None, dry_run: bool = False,
        only_new_scores: bool = False, max_results: int | None = None,
        verbose: bool = True, manual: bool = False, log=None,
        should_stop=None, vacancy: str | None = None) -> RunSummary:
    t0 = time.time()
    s = RunSummary()
    c = cfg()
    limit = max_results or int(c.get("run.max_new_per_day", 200))

    with session() as conn:
        cur = conn.execute("INSERT INTO runs (started_at, sources) VALUES (?,?)",
                           (now(), jdump(sources or c.get("sources.enabled", []))))
        s.run_id = cur.lastrowid or 0

        # 1. vacancies
        synced = jdmod.sync_dir(conn)
        changed = [j.code for _, j, ch in synced if ch]
        if changed:
            s.notes.append(f"JD changes picked up: {', '.join(changed)} (these get rescored)")
        jds: list[JD] = [j for _, j in jdmod.load_all(conn, active_only=True)]
        if vacancy:
            jds = [j for j in jds if j.code == vacancy]
        else:                                          # honour "only these vacancies today"
            from . import sourcing
            picked = sourcing.effective_codes(conn)
            if picked and len(picked) < len(jds):
                jds = [j for j in jds if j.code in picked]
                s.notes.append(f"sourcing only: {', '.join(j.code for j in jds)}")
        if not jds:
            s.errors.append(f"no active JDs - put a .md file in {c.path_of('jd_dir')}")
            _finish(conn, s, t0)
            return s

        # 2. intake
        ctx = FetchContext(jds=jds, max_results=limit, dry_run=dry_run,
                           manual=manual, log=log or (lambda m: None),
                           should_stop=should_stop or (lambda: False))
        conn.commit()
        for src in build_enabled(sources):
            ok, why = src.available()
            if not ok:
                s.notes.append(f"{src.name} skipped: {why}")
                continue
            s.sources.append(src.name)
            conn.commit()      # release the write lock: a source may write progress itself
            res = src.fetch(ctx)
            s.fetched += len(res.candidates)
            s.notes.extend(f"{src.name}: {n}" for n in res.notes)
            s.errors.extend(f"{src.name}: {e}" for e in res.errors)

            for cand in res.candidates:
                if dry_run:
                    s.skipped += 1
                    continue
                try:
                    cid, action, wrote_pdf = file_candidate(conn, cand)
                    s.created += action == "created"
                    s.updated += action == "updated"
                    s.pdfs_written += wrote_pdf
                except Exception as e:
                    s.errors.append(f"{src.name}/{cand.full_name}: {type(e).__name__}: {e}")
                    events.emit("error", f"Could not save this profile: {type(e).__name__}: {e}",
                                candidate=cand.full_name, level="error")

        if dry_run:
            s.notes.append("dry run - nothing written")
            _finish(conn, s, t0)
            return s

        # 4. score
        s.scored = score_all(conn, only_new=only_new_scores, trace=True)

        # 4b. the AI agent reads each vacancy's best profiles (when switched on)
        conn.commit()
        n, notes = agent_review(conn, should_stop=should_stop, vacancy=vacancy)
        s.reranked = n
        s.notes.extend(notes)

        # 5. retention
        s.purged = store.purge_expired(conn)

        _finish(conn, s, t0)
        audit(conn, "run.complete", "run", s.run_id,
              {"fetched": s.fetched, "created": s.created, "scored": s.scored})
    return s


def _finish(conn: sqlite3.Connection, s: RunSummary, t0: float) -> None:
    s.seconds = time.time() - t0
    conn.execute(
        "UPDATE runs SET finished_at=?, sources=?, fetched=?, created=?, updated=?, skipped=?,"
        " scored=?, errors=?, log=? WHERE id=?",
        (now(), jdump(s.sources), s.fetched, s.created, s.updated, s.skipped, s.scored,
         len(s.errors), jdump({"notes": s.notes, "errors": s.errors}), s.run_id))
