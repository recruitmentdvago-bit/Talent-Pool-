"""The collector: LinkedIn sourcing on YOUR PC, results sent to the cloud portal.

LinkedIn needs a real, visible browser with your login, so that part cannot run on a cloud
server. The collector is the same program running on your PC: it pulls the open vacancies from
the portal, sources LinkedIn for them with the usual pacing, and pushes every profile it read
(plus its PDF) back. The portal scores them and its AI agent reads them.

    set SOURCER_DATA_DIR=collector_data
    python -m sourcer ui              # once: Log in to LinkedIn (own folder, own login)
    python -m sourcer collect --url https://YOUR-APP.up.railway.app --token <INGEST_TOKEN>
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import requests

from . import runner
from .config import cfg
from .db import kv_get, kv_set, session


# Pacing settings the portal controls; they are applied here at every run. (The browser choice is
# per computer, so it stays local.)
SHARED_LINKEDIN_KEYS = ("daily_profile_cap", "ramp_up", "window_start", "window_end", "weekdays_only",
                        "session_minutes_max", "location_filter", "deep_experience", "geo_urn",
                        "max_pages_per_search", "profiles_per_vacancy", "profiles_per_run", "max_rounds",
                        "company_mode")


def apply_portal_settings(settings: dict, log=print) -> int:
    """Take the pacing settings the portal sent and make them this collector's settings."""
    li = {k: v for k, v in ((settings or {}).get("linkedin") or {}).items() if k in SHARED_LINKEDIN_KEYS and v is not None}
    if li:
        cfg().save_ui({"linkedin": li})
        log("applied the portal's pacing settings: " + ", ".join(f"{k}={v}" for k, v in li.items()
                                                                  if k not in ("geo_urn",)))
    return len(li)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def pull_vacancies(url: str, token: str, log=print) -> int:
    """Replace the local vacancy files with the portal's, so both always agree."""
    r = requests.get(url.rstrip("/") + "/api/ingest/vacancies", headers=_headers(token), timeout=60)
    if r.status_code == 404:
        raise RuntimeError("the portal has no INGEST_TOKEN set (collector access is switched off)")
    if r.status_code == 401:
        raise RuntimeError("the portal refused the token - check INGEST_TOKEN")
    r.raise_for_status()
    items = r.json().get("vacancies", [])
    jd_dir = cfg().path_of("jd_dir")
    jd_dir.mkdir(parents=True, exist_ok=True)
    for old in jd_dir.glob("*.md"):
        old.unlink()
    for it in items:
        name = Path(it["filename"]).name                  # never a path from the network
        (jd_dir / name).write_text(it["text"], encoding="utf-8")
    log(f"pulled {len(items)} vacancy file(s) from the portal")
    apply_portal_settings(r.json().get("settings"), log)
    return len(items)


def push_candidates(url: str, token: str, since_id: int | None = None, batch: int = 20, log=print) -> dict:
    """Send every candidate newer than the last push (or `since_id`)."""
    from . import store
    with session() as conn:
        start = since_id if since_id is not None else int(kv_get(conn, "collector_last_pushed_id") or 0)
        rows = conn.execute("SELECT id, profile_pdf_path FROM candidates WHERE id > ? ORDER BY id", (start,)).fetchall()
        payload, last = [], start
        for r in rows:
            c = store.load(conn, r["id"])
            if not c:
                continue
            pdf = None
            p = Path(r["profile_pdf_path"] or "")
            if p.is_file():
                pdf = base64.b64encode(p.read_bytes()).decode()
            payload.append((r["id"], {"candidate": json.loads(json.dumps(c.to_dict(), default=str)), "pdf_b64": pdf}))
    tot = {"created": 0, "updated": 0, "failed": 0, "sent": 0}
    for i in range(0, len(payload), batch):
        chunk = payload[i:i + batch]
        r = requests.post(url.rstrip("/") + "/api/ingest/candidates", headers=_headers(token), timeout=300,
                          json={"candidates": [c for _, c in chunk]})
        if r.status_code == 401:
            raise RuntimeError("the portal refused the token - check INGEST_TOKEN")
        r.raise_for_status()
        d = r.json()
        for k in ("created", "updated", "failed"):
            tot[k] += d.get(k, 0)
        tot["sent"] += len(chunk)
        last = chunk[-1][0]
        with session() as conn:
            kv_set(conn, "collector_last_pushed_id", str(last))     # progress survives an interruption
        log(f"sent {tot['sent']}/{len(payload)} profile(s)")
    return tot


def collect(url: str, token: str, limit: int | None = None, push_only: bool = False, log=print,
            now: bool = False) -> int:
    """`now=True` ignores the portal's working hours (for a test, or when you really mean it)."""
    if not push_only:
        pull_vacancies(url, token, log)
        s = runner.run(sources=["linkedin_browser"], manual=now, max_results=limit, log=log)
        if any("waiting" in n for n in s.notes):
            log("Outside the working hours set in the portal - nothing was opened. Run again with --now to ignore them.")
        for line in s.as_text().splitlines():
            log(line)
    tot = push_candidates(url, token, log=log)
    log(f"done: {tot['created']} new, {tot['updated']} updated, {tot['failed']} failed on the portal")
    return 0
