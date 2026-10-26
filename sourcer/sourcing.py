"""Which vacancies the bot sources right now.

By default every open vacancy. The admin can pick a few (say only Data Analyst and Admin
today), optionally for today only: tomorrow it goes back to everything on its own.
Scoring is never affected - everyone is still scored against every vacancy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import cfg
from .db import jdump, jload, kv_get, kv_set

KEY = "sourcing_selection"


def today() -> str:
    off = float(cfg().get("location.utc_offset_hours", 5))          # Pakistan time unless told otherwise
    return (datetime.now(timezone.utc) + timedelta(hours=off)).date().isoformat()


def selection(conn) -> dict:
    """-> {"codes": [...] | None, "until": "YYYY-MM-DD" | None, "active": bool}"""
    d = jload(kv_get(conn, KEY), {}) if kv_get(conn, KEY) else {}
    codes = d.get("codes") if isinstance(d, dict) else None
    until = d.get("until") if isinstance(d, dict) else None
    active = bool(codes) and (not until or today() <= until)
    return {"codes": codes if active else None, "until": until if active else None, "active": active}


def effective_codes(conn) -> set[str]:
    """Codes of the open vacancies that are to be sourced now."""
    open_codes = {r["code"] for r in conn.execute("SELECT code FROM jds WHERE active=1")}
    sel = selection(conn)
    return (open_codes & set(sel["codes"])) if sel["active"] else open_codes


def save(conn, codes: list[str] | None, today_only: bool = False) -> dict:
    if not codes:
        kv_set(conn, KEY, jdump({}))                                  # back to "everything"
    else:
        kv_set(conn, KEY, jdump({"codes": list(codes), "until": today() if today_only else None}))
    return selection(conn)
