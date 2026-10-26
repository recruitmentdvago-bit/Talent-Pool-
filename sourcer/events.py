"""A step-by-step record of what the pipeline and the AI agent are doing.

Every stage writes one line here as it happens, so the "Agent activity" tab can show
live whether a profile is being *extracted* (PDF / page read), *parsed*, *stored*,
*scored* by the rubric, or *read by the agent* - and what the agent answered.

Writes use their own short connection so a busy job never blocks on the UI, and a
failure to log never breaks the work being logged.
"""
from __future__ import annotations

import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from .db import connect, jdump, jload, now

# Stage names, in the order a profile moves through them.
STAGES = ("job", "extract", "parse", "store", "score", "agent", "error")
STAGE_LABELS = {
    "job": "Job", "extract": "Extracting", "parse": "Parsing", "store": "Saving",
    "score": "Scoring", "agent": "AI agent", "error": "Problem",
}
KEEP_DAYS = 7


_q: "queue.Queue[tuple]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _write_batch(path: str, rows: list[tuple]) -> None:
    for attempt in range(6):
        try:
            conn = connect(path)
            try:
                try:
                    conn.executemany("INSERT INTO events (ts, level, stage, candidate, vacancy, message, detail) "
                                     "VALUES (?,?,?,?,?,?,?)", rows)
                except sqlite3.OperationalError as e:
                    if "no such table" not in str(e):
                        raise
                    from .db import init
                    init(path).close()
                    conn.executemany("INSERT INTO events (ts, level, stage, candidate, vacancy, message, detail) "
                                     "VALUES (?,?,?,?,?,?,?)", rows)
                conn.commit()
                return
            finally:
                conn.close()
        except sqlite3.OperationalError:               # the pipeline holds the write lock: try again shortly
            time.sleep(0.4 * (attempt + 1))
        except Exception:
            return


def _run() -> None:
    while True:
        item = _q.get()
        batch = {item[0]: [item[1]]}
        n = 1
        try:
            while n < 100:
                nxt = _q.get_nowait()
                batch.setdefault(nxt[0], []).append(nxt[1])
                n += 1
        except queue.Empty:
            pass
        try:
            for path, rows in batch.items():
                _write_batch(path, rows)
        finally:
            for _ in range(n):
                _q.task_done()


def emit(stage: str, message: str, *, candidate: str = "", vacancy: str = "",
         level: str = "info", detail=None) -> None:
    """Queue one line for the activity log. Never blocks and never raises: a busy database
    must not slow the work being logged."""
    global _worker
    try:
        from .config import cfg
        path = str(cfg().path_of("storage.db_path"))
        row = (now(), level, stage, candidate or None, vacancy or None, str(message)[:600],
               None if detail is None else jdump(detail)[:4000])
        with _worker_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_run, name="events-writer", daemon=True)
                _worker.start()
        _q.put((path, row))
    except Exception:
        pass


def flush(timeout: float = 10.0) -> None:
    """Wait until everything queued has been written (tests, and the end of a job)."""
    end = time.time() + timeout
    while _q.unfinished_tasks and time.time() < end:
        time.sleep(0.02)


def list_events(conn, after: int = 0, limit: int = 200, stage: str = "", level: str = "",
                candidate: str = "") -> list[dict]:
    q, args = "SELECT * FROM events WHERE id > ?", [int(after)]
    if stage:
        q += " AND stage = ?"; args.append(stage)
    if level == "problems":
        q += " AND level IN ('warn','error')"
    elif level:
        q += " AND level = ?"; args.append(level)
    if candidate:
        q += " AND lower(candidate) LIKE ?"; args.append(f"%{candidate.lower()}%")
    if after:
        q += " ORDER BY id ASC LIMIT ?"
    else:                                          # first load: the newest `limit`, shown oldest-first
        q = f"SELECT * FROM ({q} ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
    args.append(max(1, min(int(limit), 500)))
    return [{"id": r["id"], "ts": r["ts"], "level": r["level"], "stage": r["stage"],
             "candidate": r["candidate"] or "", "vacancy": r["vacancy"] or "", "message": r["message"],
             "detail": jload(r["detail"], {}) if r["detail"] else None}
            for r in conn.execute(q, args)]


def status(conn, job_state: str = "idle") -> dict:
    """What is happening right now: the latest step, and whether it is recent enough to be 'live'."""
    last = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    if not last:
        return {"state": job_state, "stage": "", "message": "", "live": False, "ts": ""}
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["ts"])).total_seconds()
    except ValueError:
        age = 9999
    live = job_state in {"running", "stopping"} and age < 180
    return {"state": job_state, "stage": last["stage"], "message": last["message"], "level": last["level"],
            "candidate": last["candidate"] or "", "vacancy": last["vacancy"] or "",
            "live": live, "seconds_ago": int(age), "ts": last["ts"]}


def counts(conn, minutes: int = 60) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    out = {s: 0 for s in STAGES}
    for r in conn.execute("SELECT stage, COUNT(*) n FROM events WHERE ts >= ? GROUP BY stage", (since,)):
        out[r["stage"]] = r["n"]
    return out


def purge(conn, days: int = KEEP_DAYS) -> int:
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute("DELETE FROM events WHERE ts < ?", (cut,)).rowcount
