"""Direct LinkedIn sourcing: for each open vacancy, search, filter, open, read, file.

Flow per run
  1. check the gates: cooldown, daily allowance, working hours, session login
  2. round-robin across vacancies so a small allowance still touches every one
  3. per vacancy: load a search page -> keep only people in the target area ->
     skip anyone seen recently -> open the profile -> parse -> (maybe) open the
     full experience list -> pause like a person -> next
  4. any warning page: stop immediately and start the cooldown

Profiles are returned to the runner, which stores them, renders the PDF, scores
them and files everything exactly as for any other source.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

from .. import db, events
from ..config import cfg
from ..linkedin_browser import BlockedError, LinkedInBrowser, NotLoggedIn, PageLoadError
from ..linkedin_parse import (canonical_profile_url, has_truncated_experience,
                              parse_profile_text, slug_of)
from ..models import JD
from ..normalize import geo_index, norm_text
from ..pacing import (Pacer, bump, cooldown_until, remaining_today, start_cooldown,
                      used_today)
from ..pdfstore import parse_confidence
from .base import BaseSource, FetchContext, FetchResult


class _Stop(Exception):
    """Internal: end the run quietly (the reason is already in the result)."""


def _employer_rank(card: dict, preferred: list[str]) -> int:
    """0 if the card's headline/company matches a preferred employer, 1 otherwise.
    Used to sort search results so profiles from target companies open first."""
    if not preferred:
        return 0
    text = norm_text(f"{card.get('headline', '')} {card.get('company', '')}")
    for emp in preferred:
        if norm_text(emp) in text:
            return 0
    return 1


WIDER = "wider search: "


def search_keywords(jd: JD, companies: list[str] | None = None) -> str:
    """Boolean keywords for one vacancy: any of the target titles (including
    extra_titles the user added), plus the top N must-have skills where N is
    controlled by jd.search_skills_count (default 2, 0 means title-only)."""
    from ..searchplan import build
    plan = build(jd)
    # the vacancy's own title first, then the titles you manage (Search titles tab),
    # then the taxonomy's related titles to fill up to 8
    titles: list[str] = []
    for t in [jd.title, *(jd.extra_titles or []), *plan.titles]:
        if t and t.lower() not in {x.lower() for x in titles} and len(titles) < 8:
            titles.append(t)
    q = "(" + " OR ".join(f'"{t}"' for t in titles) + ")"
    n_skills = max(0, min(2, int(2 if getattr(jd, "search_skills_count", 2) is None else getattr(jd, "search_skills_count", 2))))
    skills = [s.replace("_", " ") for s in jd.must_have[:n_skills]]
    for s in skills:
        q += f' AND "{s}"' if " " in s else f" AND {s}"
    if companies:
        q += " AND (" + " OR ".join('"' + c.replace('"', "") + '"' for c in companies) + ")"
    return q


def search_stages(jd: JD, mode: str = "priority_first", chunk: int = 5, max_chunks: int = 3) -> list[tuple[str, str]]:
    """The searches to run for one vacancy, in order: [(label, keywords)].

    priority_first (default): people at the vacancy's preferred companies first, then everyone.
    only: preferred companies and nothing else.   ignore: everyone, whatever the lists say.
    With no preferred companies there is just the one general search."""
    general = ("everyone on LinkedIn", search_keywords(jd))
    # If the full search finds nothing at all (too many titles and skills ANDed together), wider ones
    # follow - and are used ONLY in that case, never after a search that did return people.
    import dataclasses
    wider: list[tuple[str, str]] = []
    titles_only = search_keywords(dataclasses.replace(jd, search_skills_count=0))
    if titles_only != general[1]:
        wider.append((WIDER + "job titles only", titles_only))
    exact = f'"{jd.title}"'
    if exact not in (general[1], titles_only):
        wider.append((WIDER + "exact title only", exact))
    comps: list[str] = []
    for c in jd.preferred_employers or []:
        if c and c.lower() not in {x.lower() for x in comps}:
            comps.append(c)
    if not comps or mode == "ignore":
        return [general, *wider]
    chunks = [comps[i:i + chunk] for i in range(0, len(comps), chunk)][:max_chunks]
    stages = [(f"preferred companies ({', '.join(ch)})", search_keywords(jd, ch)) for ch in chunks]
    if mode != "only":
        stages += [general, *wider]
    return stages


def location_ok(location: str, mode: str) -> bool:
    mode = (mode or "metro").lower()
    if mode == "any":
        return True
    tier = geo_index().classify(location)[2]
    if tier == "unknown":
        return True                 # a card without a location is not evidence either way
    if mode == "country":
        return tier in {"metro", "country", "remote"}
    return tier in {"metro", "remote"}


class LinkedInBrowserSource(BaseSource):
    name = "linkedin_browser"

    def __init__(self):
        self.c = cfg().get("linkedin", {}) or {}

    # ---- gates ----------------------------------------------------------- #
    def available(self) -> tuple[bool, str]:
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False, "playwright is not installed (pip install playwright)"
        with db.session() as conn:
            until = cooldown_until(conn)
            if until:
                why = db.kv_get(conn, "linkedin_cooldown_reason") or "a warning from LinkedIn"
                return False, f"cooling down until {until:%d %b %H:%M} ({why})"
            if db.kv_get(conn, "linkedin_logged_in") != "1":
                return False, "not logged in - use 'Log in to LinkedIn' in the app first"
            left = remaining_today(conn, self.c)
            if left <= 0:
                return False, "today's profile allowance is used up"
            return True, f"{left} profile views left today"

    # ---- main ------------------------------------------------------------ #
    def fetch(self, ctx: FetchContext) -> FetchResult:
        res = FetchResult()
        log = ctx.log
        pacer = Pacer(self.c)

        okw, why = pacer.in_window()
        if not okw and not ctx.manual:
            res.notes.append(f"linkedin_browser waiting: {why}")
            return res

        with db.session() as conn:
            budget = min(remaining_today(conn, self.c), max(1, ctx.max_results))
            skip_days = int(self.c.get("skip_seen_days", 30))
            search_cap = int(self.c.get("daily_search_pages_cap", 25))
            search_left = max(0, search_cap - used_today(conn, "search"))
        if budget <= 0:
            res.notes.append("linkedin_browser: today's allowance is used up")
            return res
        per_run = int(self.c.get("profiles_per_run", 0) or 0)
        if per_run > 0:
            budget = min(budget, per_run)
        max_rounds = int(self.c.get("max_rounds", 0) or 0)
        rounds = 0
        if not ctx.jds:
            return res
        if ctx.dry_run:
            res.notes.append(f"linkedin_browser: would open up to {budget} profiles "
                             f"for {len(ctx.jds)} vacancies")
            return res

        max_pages = int(self.c.get("max_pages_per_search", 4))
        loc_mode = self.c.get("location_filter", "metro")
        deep = bool(self.c.get("deep_experience", True))

        # Priority vacancies go first, then round-robin by least-recently-sourced.
        batch_size = int(self.c.get("profiles_per_vacancy", 10))
        with db.session() as c0:
            last = {j.code: (db.kv_get(c0, f"linkedin_jd_last:{j.code}") or "") for j in ctx.jds}
            saved_pages = {j.code: int(db.kv_get(c0, f"linkedin_jd_page:{j.code}") or 1) for j in ctx.jds}
        jds = sorted(ctx.jds, key=lambda j: (-j.priority, last[j.code], j.code))
        mode = self.c.get("company_mode", "priority_first")
        stages = {j.code: search_stages(j, mode) for j in jds}
        with db.session() as c8:
            stage_i = {j.code: min(max(0, int(db.kv_get(c8, f"linkedin_jd_stage:{j.code}") or 0)), len(stages[j.code]) - 1)
                       for j in jds}
        queues: dict[str, deque] = {j.code: deque() for j in jds}
        next_page: dict[str, int] = {j.code: (saved_pages[j.code] if saved_pages[j.code] <= int(self.c.get("max_pages_per_search", 4)) else 1)
                                     for j in jds}

        def remember(jd_code: str, page: int | None = None) -> None:
            with db.session() as c9:
                db.kv_set(c9, f"linkedin_jd_last:{jd_code}", datetime.now().isoformat(timespec="seconds"))
                if page is not None:
                    db.kv_set(c9, f"linkedin_jd_page:{jd_code}", str(page))
                    db.kv_set(c9, f"linkedin_jd_stage:{jd_code}", str(stage_i[jd_code]))
        exhausted: set[str] = set()
        visited_slugs: set[str] = set()
        done = 0
        load_failures = 0

        def seen_recently(slug: str) -> bool:
            with db.session() as c2:
                r = c2.execute("SELECT last_seen_at FROM candidates WHERE person_key=?",
                               (f"li:{slug.lower()}",)).fetchone()
            if not r or not r["last_seen_at"]:
                return False
            try:
                last = datetime.fromisoformat(r["last_seen_at"]).replace(tzinfo=None)
            except ValueError:
                return False
            return (datetime.now(timezone.utc).replace(tzinfo=None) - last) < timedelta(days=skip_days)

        try:
            with LinkedInBrowser(self.c, pacer, log, ctx.should_stop) as br:
                if not br.is_logged_in():
                    with db.session() as c3:
                        db.kv_set(c3, "linkedin_logged_in", "0")
                    res.errors.append("LinkedIn session has expired - log in again from the app")
                    return res

                searched_this_run = False

                def widen_or_finish(jd: JD, why: str, empty_first: bool = False) -> None:
                    """This search is used up: go on to the next one (everyone), or finish.
                    The "wider search" stages are for a search that found NOBODY on its first page."""
                    code = jd.code
                    nxt = stage_i[code] + 1
                    while not empty_first and nxt < len(stages[code]) and stages[code][nxt][0].startswith(WIDER):
                        nxt += 1
                    if nxt < len(stages[code]):
                        stage_i[code] = nxt
                        next_page[code] = 1
                        log(f"[{code}] {why} - now searching {stages[code][stage_i[code]][0]}")
                        events.emit("extract", f"{why} - trying: {stages[code][stage_i[code]][0]}", vacancy=code, level="warn")
                        remember(code, 1)
                    else:
                        exhausted.add(code)
                        stage_i[code] = 0
                        remember(code, 1)             # start again from the top next time

                def refill(jd: JD) -> None:
                    nonlocal search_left, searched_this_run
                    code = jd.code
                    for _ in range(len(stages[code]) + 1):
                        if code in exhausted or search_left <= 0:
                            exhausted.add(code)
                            return
                        if ctx.should_stop():
                            return
                        if next_page[code] > max_pages:
                            widen_or_finish(jd, f"reached {max_pages} pages")
                            continue
                        label, kw = stages[code][stage_i[code]]
                        if searched_this_run and pacer.wait(pacer.search_gap(), ctx.should_stop):
                            return
                        log(f"[{code}] searching page {next_page[code]} ({label}): {kw}")
                        events.emit("extract", f"Searching LinkedIn - {label}, page {next_page[code]}", vacancy=code,
                                    detail={"keywords": kw})
                        try:
                            cards = br.search(kw, next_page[code])
                        except PageLoadError as e:
                            res.errors.append(f"[{code}] search page failed to load: {e}")
                            exhausted.add(code)
                            return
                        searched_this_run = True
                        next_page[code] += 1
                        search_left -= 1
                        remember(code, next_page[code])
                        with db.session() as c4:
                            bump(c4, "search")
                        if not cards:
                            first_page_empty = next_page[code] == 2
                            if first_page_empty:                    # keep what LinkedIn actually showed, to find out why
                                try:
                                    br.dump_debug(f"search_{code}", br.page.inner_text("body")[:6000],
                                                  f"no people found for: {kw}")
                                except Exception:
                                    pass
                            widen_or_finish(jd, "LinkedIn found nobody for this search" if first_page_empty
                                            else "no more results", empty_first=first_page_empty)
                            continue
                        kept = skipped_loc = skipped_seen = 0
                        filtered: list[dict] = []
                        for card in cards:
                            if card["slug"] in visited_slugs:
                                continue
                            if not location_ok(card.get("location", ""), loc_mode):
                                skipped_loc += 1
                                continue
                            if seen_recently(card["slug"]):
                                skipped_seen += 1
                                continue
                            visited_slugs.add(card["slug"])
                            filtered.append(card)
                            kept += 1
                        filtered.sort(key=lambda c: _employer_rank(c, jd.preferred_employers))
                        for card in filtered:
                            queues[code].append(card)
                        log(f"[{code}] {len(cards)} results: {kept} to open, "
                            f"{skipped_loc} outside the area, {skipped_seen} already known")
                        events.emit("extract", f"{len(cards)} search results: {kept} to open, {skipped_loc} outside "
                                    f"the area, {skipped_seen} already known", vacancy=code)
                        return

                def _open_one(jd, card):
                    """Open a single profile card. Returns True if it counted."""
                    nonlocal load_failures, done
                    log(f"[{jd.code}] opening {card['name']} ({card.get('location') or '?'})")
                    events.emit("extract", f"Opening the LinkedIn profile page ({card.get('location') or 'location unknown'})",
                                candidate=card["name"], vacancy=jd.code)
                    try:
                        text = br.profile_text(card["url"])
                    except PageLoadError as e:
                        events.emit("error", f"Profile page failed to load: {e}", candidate=card["name"],
                                    vacancy=jd.code, level="error")
                        load_failures += 1
                        res.errors.append(f"{card['name']}: page failed to load ({e})")
                        if load_failures >= 3:
                            res.errors.append("three pages in a row failed to load - "
                                              "stopping (check the internet connection)")
                            raise _Stop()
                        pacer.wait(pacer.profile_gap(), ctx.should_stop)
                        return False
                    load_failures = 0
                    with db.session() as c5:
                        bump(c5, "profile")
                    remember(jd.code)
                    done += 1

                    events.emit("extract", f"Read the page - {len(text):,} characters of profile text",
                                candidate=card["name"], vacancy=jd.code)
                    cand = parse_profile_text(text, card["url"], self.name)
                    if cand is None:
                        events.emit("error", "Could not make a profile out of that page (raw text saved for review)",
                                    candidate=card["name"], vacancy=jd.code, level="error")
                        br.dump_debug(card["slug"], text, "could not read a profile from this page")
                        res.errors.append(f"{card['name']}: page could not be read")
                    else:
                        if deep and has_truncated_experience(text):
                            try:
                                more = br.experience_details(card["url"])
                                if len(more) > len(cand.experiences):
                                    cand.experiences = more
                            except BlockedError:
                                raise
                            except Exception as e:
                                log(f"   full experience list unavailable ({type(e).__name__})")
                        if not cand.headline and card.get("headline"):
                            cand.headline = card["headline"]
                        if not cand.location_raw and card.get("location"):
                            cand.location_raw = card["location"]
                        conf, issues = parse_confidence(cand)
                        cand.raw.update(parse_confidence=conf, parse_issues=issues,
                                        search_vacancy=jd.code)
                        events.emit("parse", f"Structured the profile: {cand.current_title() or 'no title'}"
                                    f"{' at ' + cand.current_company() if cand.current_company() else ''} - "
                                    f"{len(cand.experiences)} roles, {len(cand.certifications)} certifications, "
                                    f"confidence {conf}", candidate=cand.full_name or card["name"], vacancy=jd.code,
                                    level="warn" if issues else "info", detail={"issues": issues})
                        if conf < 0.4:
                            br.dump_debug(card["slug"], text, f"low confidence {conf}: {issues}")
                            res.errors.append(f"{card['name']}: low confidence ({', '.join(issues)}) - "
                                              f"raw text saved for review")
                        else:
                            res.candidates.append(cand)
                            log(f"   saved: {cand.current_title() or '?'} at "
                                f"{cand.current_company() or '?'} · {cand.total_years():g} yrs")
                    return True

                while done < budget and not ctx.should_stop():
                    if pacer.session_over():
                        res.notes.append("session time limit reached - resumes next run")
                        break
                    progressed = False
                    for jd in jds:
                        if done >= budget or ctx.should_stop() or pacer.session_over():
                            break
                        batch_done = 0
                        while batch_done < batch_size and done < budget and not ctx.should_stop() and not pacer.session_over():
                            if not queues[jd.code]:
                                refill(jd)
                            if not queues[jd.code]:
                                break
                            progressed = True
                            card = queues[jd.code].popleft()
                            counted = _open_one(jd, card)
                            if counted:
                                batch_done += 1
                            if done >= budget:
                                break
                            pacer.wait(pacer.reading_time(), ctx.should_stop)
                            slept = pacer.take_break_if_due(ctx.should_stop)
                            if slept:
                                log(f"   taking a {slept/60:.0f} minute break")
                            else:
                                pacer.wait(pacer.profile_gap(), ctx.should_stop)
                    rounds += 1                                   # one pass over every vacancy
                    if max_rounds and rounds >= max_rounds and done < budget:
                        res.notes.append(f"stopped after {rounds} round(s), as set")
                        break
                    if not progressed and all(j.code in exhausted or not queues[j.code] for j in jds):
                        if all(j.code in exhausted for j in jds):
                            res.notes.append("searches exhausted for every vacancy")
                            break
                        if not any(queues[j.code] for j in jds):
                            break
        except _Stop:
            pass
        except NotLoggedIn as e:
            with db.session() as c6:
                db.kv_set(c6, "linkedin_logged_in", "0")
            res.errors.append(f"LinkedIn asked for a login: {e}. Log in again from the app.")
        except BlockedError as e:
            with db.session() as c7:
                until = start_cooldown(c7, str(e))
            msg = (f"LinkedIn showed a warning ({e}). Stopped immediately; "
                   f"sourcing is paused until {until:%d %b %H:%M}.")
            log(msg)
            events.emit("error", msg, level="error")
            res.errors.append(msg)
        except Exception as e:
            events.emit("error", f"LinkedIn sourcing stopped: {type(e).__name__}: {e}", level="error")
            res.errors.append(f"linkedin_browser: {type(e).__name__}: {e}")

        res.notes.append(f"linkedin_browser: opened {done} profile(s), kept {len(res.candidates)}")
        return res
