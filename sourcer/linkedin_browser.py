"""Drive a real browser through LinkedIn, one careful step at a time.

You log in yourself, once, in the window this opens - the bot never sees or
stores your password; the session cookie lives in `storage/browser_profile`
exactly as it would in your normal Chrome. After that the bot behaves like a
person working a list: opens a search, scrolls, opens a profile, reads it,
waits, moves on - with the limits in pacing.py.

What this deliberately does NOT do: it does not spoof the browser fingerprint,
hide that it is automated, rotate IPs or run headless. Those are the tricks
that turn a pacing choice into an arms race. If LinkedIn challenges the
session, the bot stops and goes quiet for a day or two.

This is automated access that LinkedIn's terms do not permit. Pacing lowers the
chance of being noticed; it does not remove it, and a restriction on the
account is a real possibility.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urlparse

from .config import cfg
from .linkedin_parse import (dedupe_cards, has_truncated_experience,
                             parse_experience_lines, slug_of)
from .pacing import Pacer, looks_blocked


class BlockedError(Exception):
    """LinkedIn showed a warning, a verification step, or a login page."""


class NotLoggedIn(BlockedError):
    pass


class PageLoadError(Exception):
    """A page failed to load (timeout, dropped connection). NOT a LinkedIn warning:
    it must never start a cooldown, only skip the item."""


CARD_JS = """
() => {
  const root = document.querySelector('main') || document.body;
  const slugOf = h => { const m = (h||'').match(/\\/in\\/([^\\/?#]+)/); return m ? m[1] : null; };
  const out = [], seen = new Set();
  root.querySelectorAll('a[href*="/in/"]').forEach(a => {
    const slug = slugOf(a.href);
    if (!slug || seen.has(slug)) return;
    // climb to the largest ancestor that still contains only this one person
    let best = a, el = a.parentElement;
    while (el && el !== root) {
      const slugs = new Set([...el.querySelectorAll('a[href*="/in/"]')].map(x => slugOf(x.href)));
      if (slugs.size > 1) break;
      best = el; el = el.parentElement;
    }
    const lines = (best.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
    if (lines.length >= 2) { seen.add(slug); out.push({href: a.href, lines}); }
  });
  return out;
}
"""


# --------------------------------------------------------------------------- #
# Which browser, which profile
# --------------------------------------------------------------------------- #
def find_browsers() -> dict[str, str]:
    """Chrome / Edge installed on this computer -> {"chrome": path, "edge": path}."""
    env = os.environ.get
    cands: dict[str, list[str]] = {"chrome": [], "edge": []}
    if sys.platform.startswith("win"):
        for root in (env("PROGRAMFILES"), env("PROGRAMFILES(X86)"), env("LOCALAPPDATA")):
            if root:
                cands["chrome"].append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
                cands["edge"].append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
    elif sys.platform == "darwin":
        cands["chrome"].append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        cands["edge"].append("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    else:
        cands["chrome"] += [shutil.which(n) or "" for n in ("google-chrome", "google-chrome-stable", "chrome")]
        cands["edge"] += [shutil.which(n) or "" for n in ("microsoft-edge", "microsoft-edge-stable")]
    return {b: next(p for p in ps if p and os.path.isfile(p)) for b, ps in cands.items()
            if any(p and os.path.isfile(p) for p in ps)}


def resolve_browser(conf: dict) -> tuple[str | None, str]:
    """-> (executable, brand). brand is 'chrome' | 'edge' | 'custom' | '' (bundled Chromium).
    Login and sourcing both use this, so they always share one browser and one profile."""
    exe = (conf.get("browser_executable") or "").strip()
    if exe:
        return exe, "custom"
    found = find_browsers()
    pref = (conf.get("browser") or "auto").lower()
    if pref in found:
        return found[pref], pref
    if pref == "auto":
        for b in ("chrome", "edge"):
            if b in found:
                return found[b], b
    return None, ""


def profile_dir(conf: dict, brand: str) -> Path:
    """The bot's own browser profile (it keeps the LinkedIn session). One folder per
    browser, because a profile made by Edge must not be opened by Chrome."""
    udir = Path(conf.get("user_data_dir", "storage/browser_profile"))
    if not udir.is_absolute():
        udir = cfg().root / udir
    if brand in ("chrome", "edge"):
        udir = udir.with_name(f"{udir.name}_{brand}")
    udir.mkdir(parents=True, exist_ok=True)
    return udir


def _launch_kwargs(conf: dict) -> dict:
    kw = dict(headless=bool(conf.get("headless", False)),
              viewport={"width": 1366, "height": 860},
              locale="en-US")
    exe = (conf.get("browser_executable") or "").strip()
    if exe:
        kw["executable_path"] = exe
    return kw


class LinkedInBrowser:
    def __init__(self, conf: dict | None = None, pacer: Pacer | None = None,
                 log: Callable[[str], None] = print,
                 should_stop: Callable[[], bool] = lambda: False):
        self.c = conf if conf is not None else (cfg().get("linkedin", {}) or {})
        self.pacer = pacer or Pacer(self.c)
        self.log = log
        self.should_stop = should_stop
        self._pw = None
        self.ctx = None
        self.page = None

    # ---- lifecycle ------------------------------------------------------ #
    def __enter__(self) -> "LinkedInBrowser":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        exe, brand = resolve_browser(self.c)
        udir = profile_dir(self.c, brand)
        kw = _launch_kwargs(self.c)
        if exe:
            kw["executable_path"] = exe
        channel = (self.c.get("browser_channel") or "").strip()
        try:
            if channel and "executable_path" not in kw:
                self.ctx = self._pw.chromium.launch_persistent_context(str(udir), channel=channel, **kw)
            else:
                self.ctx = self._pw.chromium.launch_persistent_context(str(udir), **kw)
        except Exception as e:
            if channel and "executable_path" not in kw:
                self.log(f"could not start '{channel}' ({type(e).__name__}); trying bundled Chromium")
                self.ctx = self._pw.chromium.launch_persistent_context(str(udir), **kw)
            else:
                raise
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(30000)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ---- human-shaped movement ------------------------------------------ #
    def _idle(self, lo: float, hi: float) -> bool:
        return self.pacer.wait(self.pacer.rng.uniform(lo, hi), self.should_stop)

    def human_scroll(self, passes: tuple[int, int] = (3, 6)) -> None:
        rng = self.pacer.rng
        for _ in range(rng.randint(*passes)):
            if self.should_stop():
                return
            try:
                self.page.mouse.move(rng.randint(250, 900), rng.randint(200, 650),
                                     steps=rng.randint(6, 18))
                self.page.mouse.wheel(0, rng.randint(280, 760))
            except Exception:
                return
            self._idle(0.6, 2.2)
        if rng.random() < 0.35:                      # sometimes read back up
            try:
                self.page.mouse.wheel(0, -rng.randint(150, 500))
            except Exception:
                pass
            self._idle(0.5, 1.5)

    # ---- navigation ------------------------------------------------------ #
    def _goto(self, url: str) -> str:
        """Open a page and return its visible text. Raises BlockedError on any warning."""
        status = None
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status = resp.status if resp else None
        except Exception as e:
            raise PageLoadError(f"{type(e).__name__}: {str(e).splitlines()[0][:160]}") from e
        self._idle(1.5, 3.5)
        try:
            body = self.page.inner_text("body")
        except Exception:
            body = ""
        reason = looks_blocked(self.page.url, body, status)
        if reason:
            # Only a plain login/auth wall means "logged out". A /checkpoint or any
            # warning text is LinkedIn challenging the session and MUST cool down.
            path = urlparse(self.page.url).path.lower()
            if path.startswith(("/login", "/authwall", "/signup", "/uas/login", "/m/login")):
                raise NotLoggedIn(reason)
            raise BlockedError(reason)
        return body

    def is_logged_in(self) -> bool:
        try:
            return any(c.get("name") == "li_at" for c in self.ctx.cookies())
        except Exception:
            return False

    def search(self, keywords: str, page_no: int = 1) -> list[dict]:
        base = self.c.get("base_url", "https://www.linkedin.com").rstrip("/")
        url = (f"{base}/search/results/people/?keywords={quote_plus(keywords)}"
               f"&origin=FACETED_SEARCH&page={page_no}")
        geo = (self.c.get("geo_urn") or "").strip()
        if geo:
            url += f"&geoUrn={geo}"
        self._goto(url)
        try:
            self.page.wait_for_selector('main a[href*="/in/"]', timeout=15000)
        except Exception:
            pass                                      # genuinely empty page is fine
        self.human_scroll((2, 4))
        try:
            raw = self.page.evaluate(CARD_JS)
        except Exception:
            raw = []
        return dedupe_cards(raw)

    def profile_text(self, url: str) -> str:
        self._goto(url)
        try:
            self.page.wait_for_selector("main", timeout=15000)
        except Exception:
            pass
        self.human_scroll((4, 8))
        try:
            return self.page.inner_text("main")
        except Exception:
            return self.page.inner_text("body")

    def experience_details(self, profile_url: str) -> list:
        """The full experience list, for profiles whose main page truncates it."""
        base = profile_url.rstrip("/")
        self._idle(4, 11)
        self._goto(base + "/details/experience/")
        self.human_scroll((2, 5))
        try:
            text = self.page.inner_text("main")
        except Exception:
            return []
        lines = [l for l in text.splitlines()]
        for i, l in enumerate(lines):
            if l.strip().lower() == "experience":
                lines = lines[i + 1:]
                break
        return parse_experience_lines(lines)

    def dump_debug(self, slug: str, text: str, reason: str) -> Path | None:
        d = Path(self.c.get("debug_dir", "storage/linkedin_debug"))
        if not d.is_absolute():
            d = cfg().root / d
        try:
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', slug)[:80]}.txt"
            f.write_text(f"# why: {reason}\n# url: {self.page.url}\n\n{text}")
            return f
        except OSError:
            return None


# --------------------------------------------------------------------------- #
# One-time login window
# --------------------------------------------------------------------------- #
def _plain_login(c: dict, log: Callable[[str], None], max_minutes: float,
                 should_stop: Callable[[], bool]) -> bool | None:
    """Sign in in a NORMAL Chrome/Edge window (no automation switches), using the bot's own
    profile folder. That is what lets "Sign in with Google" work: Google refuses to sign in
    inside a browser that is being remote-controlled. Returns None when no Chrome/Edge exists."""
    exe, brand = resolve_browser(c)
    if not exe or brand == "custom":
        return None
    base = c.get("base_url", "https://www.linkedin.com").rstrip("/")
    udir = profile_dir(c, brand)
    name = {"chrome": "Chrome", "edge": "Edge"}[brand]
    try:
        proc = subprocess.Popen([exe, f"--user-data-dir={udir}", "--no-first-run", "--no-default-browser-check",
                                 "--disable-background-mode", f"{base}/login"])
    except OSError as e:
        log(f"could not start {name}: {e}")
        return None
    log(f"{name} is opening LinkedIn. Sign in there (\"Continue with Google\" works). "
        f"When you can see your LinkedIn home page, CLOSE that {name} window - or press Stop here.")
    deadline = time.time() + max_minutes * 60
    while proc.poll() is None and time.time() < deadline and not should_stop():
        time.sleep(1.5)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    time.sleep(2)                                   # let the profile unlock and the cookie reach the disk
    ok = False
    try:
        with LinkedInBrowser({**c, "headless": True}, log=log) as br:     # reads the saved cookie; opens no page
            ok = br.is_logged_in()
    except Exception as e:
        log(f"could not check the saved session ({type(e).__name__})")
    log("logged in - session saved" if ok else "no LinkedIn session found - press Log in again and finish signing in before closing the window")
    return ok


def login_window(conf: dict | None = None, log: Callable[[str], None] = print,
                 max_minutes: float = 10.0, should_stop: Callable[[], bool] = lambda: False) -> bool:
    """Open LinkedIn's login page and wait for the person to sign in.
    Returns True once a session cookie exists."""
    c = conf if conf is not None else (cfg().get("linkedin", {}) or {})
    if (c.get("login_mode") or "plain") == "plain":
        r = _plain_login(c, log, max(max_minutes, 15.0), should_stop)
        if r is not None:
            return r
        log("No Chrome or Edge found - using the built-in browser. Google sign-in may be refused "
            "there; sign in with your LinkedIn email and password instead.")
    base = c.get("base_url", "https://www.linkedin.com").rstrip("/")
    ok = False
    with LinkedInBrowser(c, log=log) as br:
        try:
            br.page.goto(f"{base}/login", wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            log(f"could not open the login page: {type(e).__name__}")
            return False
        log("Sign in to LinkedIn in the window that just opened. "
            "It closes by itself once you are in.")
        deadline = time.time() + max_minutes * 60
        while time.time() < deadline and not should_stop():
            try:
                if not br.ctx.pages:
                    break
                if br.is_logged_in():
                    ok = True
                    time.sleep(4)                    # let the cookie and storage flush
                    break
            except Exception:
                break
            time.sleep(1.5)
    log("logged in - session saved" if ok else "login window closed without a session")
    return ok
