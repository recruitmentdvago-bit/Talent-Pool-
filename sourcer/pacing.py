"""Pacing for direct LinkedIn browsing.

Everything here exists to keep the browsing pattern slow, irregular and
bounded, the way a person working through a candidate list behaves:

  * a hard daily ceiling, with a ramp-up so a new routine starts small
  * only inside normal working hours, never in marathon sittings
  * irregular gaps between profile views, with a longer break every few
  * the profile a person would not reopen is not reopened
  * the moment LinkedIn shows any warning, everything stops for a cooldown

None of this makes automated browsing safe or permitted. It lowers how much
unusual traffic the account generates; it cannot remove the risk.
"""
from __future__ import annotations

import random
import re
import time
from datetime import date, datetime, timedelta
from typing import Callable

from .config import cfg
from .db import kv_get, kv_set, now

BLOCK_PATH_PREFIXES = ("/checkpoint", "/authwall", "/uas/", "/login", "/signup", "/m/login")
BLOCK_TEXT = (
    "unusual activity", "quick security check", "verify you're not a robot",
    "verify you are not a robot", "temporarily restricted", "we've restricted",
    "your account has been restricted", "complete this security check",
    "let's do a quick security check", "you've reached the monthly limit",
    "reached the commercial use limit", "commercial use limit",
)


def _hhmm(s: str) -> tuple[int, int]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(s))
    return (int(m.group(1)), int(m.group(2))) if m else (10, 0)


class Pacer:
    def __init__(self, conf: dict | None = None, rng: random.Random | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] = datetime.now):
        self.c = conf if conf is not None else (cfg().get("linkedin", {}) or {})
        self.rng = rng or random.Random()
        self._sleep = sleep
        self.clock = clock
        self.started = clock()
        self.views_this_session = 0
        self._next_break_at = self._draw_break_gap()

    # ---- time shaping -------------------------------------------------- #
    @property
    def scale(self) -> float:
        return float(self.c.get("time_scale", 1.0))

    def _draw_break_gap(self) -> int:
        lo, hi = self.c.get("break_every", [5, 9])
        return self.rng.randint(int(lo), int(hi))

    def wait(self, seconds: float, should_stop: Callable[[], bool] = lambda: False) -> bool:
        """Sleep in 1s slices so a Stop press is honoured. -> True if stopped."""
        remaining = max(0.0, seconds * self.scale)
        while remaining > 0:
            if should_stop():
                return True
            step = min(1.0, remaining)
            self._sleep(step)
            remaining -= step
        return should_stop()

    def profile_gap(self) -> float:
        """Irregular gap between two profile views. Skewed short with a long tail."""
        lo, hi = float(self.c.get("min_delay", 25)), float(self.c.get("max_delay", 85))
        return self.rng.triangular(lo, hi, lo + 0.35 * (hi - lo))

    def reading_time(self) -> float:
        """Time spent 'reading' a profile before leaving."""
        return self.rng.uniform(6, 22)

    def search_gap(self) -> float:
        return self.rng.uniform(12, 40)

    def take_break_if_due(self, should_stop: Callable[[], bool] = lambda: False) -> float:
        """After a handful of views, step away for a few minutes. -> seconds slept."""
        self.views_this_session += 1
        if self.views_this_session < self._next_break_at:
            return 0.0
        lo, hi = self.c.get("break_seconds", [150, 420])
        secs = self.rng.uniform(float(lo), float(hi))
        self.wait(secs, should_stop)
        self.views_this_session = 0
        self._next_break_at = self._draw_break_gap()
        return secs

    # ---- limits -------------------------------------------------------- #
    def in_window(self) -> tuple[bool, str]:
        t = self.clock()
        if self.c.get("weekdays_only") and t.weekday() >= 5:
            return False, "weekdays only - today is the weekend"
        sh, sm = _hhmm(self.c.get("window_start", "10:00"))
        eh, em = _hhmm(self.c.get("window_end", "19:00"))
        start = t.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = t.replace(hour=eh, minute=em, second=0, microsecond=0)
        if start <= t <= end:
            return True, "inside working window"
        return False, f"outside the working window {self.c.get('window_start')}-{self.c.get('window_end')}"

    def session_over(self) -> bool:
        limit = float(self.c.get("session_minutes_max", 25))
        return (self.clock() - self.started) > timedelta(minutes=limit)

    def session_minutes_left(self) -> float:
        limit = float(self.c.get("session_minutes_max", 25))
        return max(0.0, limit - (self.clock() - self.started).total_seconds() / 60.0)


# --------------------------------------------------------------------------- #
# Persistent counters and cooldown
# --------------------------------------------------------------------------- #
def used_today(conn, kind: str, today: date | None = None) -> int:
    d = (today or date.today()).isoformat()
    r = conn.execute("SELECT n FROM linkedin_activity WHERE day=? AND kind=?", (d, kind)).fetchone()
    return int(r["n"]) if r else 0


def bump(conn, kind: str, n: int = 1, today: date | None = None) -> None:
    d = (today or date.today()).isoformat()
    conn.execute("INSERT INTO linkedin_activity (day, kind, n) VALUES (?,?,?) "
                 "ON CONFLICT(day, kind) DO UPDATE SET n = n + excluded.n", (d, kind, n))
    if kv_get(conn, "linkedin_first_day") is None:
        kv_set(conn, "linkedin_first_day", d)


def active_days_before(conn, today: date | None = None) -> int:
    """How many distinct earlier days had any profile views (drives the ramp-up)."""
    d = (today or date.today()).isoformat()
    return conn.execute("SELECT COUNT(DISTINCT day) FROM linkedin_activity "
                        "WHERE kind='profile' AND day < ? AND n > 0", (d,)).fetchone()[0]


def daily_cap(conn, conf: dict | None = None, today: date | None = None) -> int:
    c = conf if conf is not None else (cfg().get("linkedin", {}) or {})
    cap = int(c.get("daily_profile_cap", 40))
    if c.get("ramp_up", True):
        return max(1, min(cap, 10 + 5 * active_days_before(conn, today)))
    return cap


def remaining_today(conn, conf: dict | None = None, today: date | None = None) -> int:
    return max(0, daily_cap(conn, conf, today) - used_today(conn, "profile", today))


def cooldown_until(conn) -> datetime | None:
    v = kv_get(conn, "linkedin_cooldown_until")
    if not v:
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t > datetime.now() else None


def start_cooldown(conn, reason: str, hours: float | None = None) -> datetime:
    h = float(hours if hours is not None else (cfg().get("linkedin.cooldown_hours", 48)))
    until = datetime.now() + timedelta(hours=h)
    kv_set(conn, "linkedin_cooldown_until", until.isoformat(timespec="seconds"))
    kv_set(conn, "linkedin_cooldown_reason", reason)
    return until


def clear_cooldown(conn) -> None:
    kv_set(conn, "linkedin_cooldown_until", "")
    kv_set(conn, "linkedin_cooldown_reason", "")


# --------------------------------------------------------------------------- #
# Warning-page detection
# --------------------------------------------------------------------------- #
def looks_blocked(url: str, body_text: str, status: int | None = None) -> str | None:
    """-> a reason string if LinkedIn is challenging, limiting or logging us out."""
    if status in (429, 999):
        return f"HTTP {status} from LinkedIn"
    from urllib.parse import urlparse
    path = urlparse((url or "").lower()).path
    for prefix in BLOCK_PATH_PREFIXES:
        if path.startswith(prefix):
            return f"redirected to a login/verification page ({prefix})"
    t = (body_text or "").lower()[:6000]
    for phrase in BLOCK_TEXT:
        if phrase in t:
            return f"LinkedIn warning text: '{phrase}'"
    return None
