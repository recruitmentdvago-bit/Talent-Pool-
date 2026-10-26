"""End-to-end tests of the LinkedIn browser flow against a local stand-in site."""
import os
from pathlib import Path

import pytest
import yaml

pytest.importorskip("playwright")
CHROMIUM = "/opt/pw-browsers/chromium"
pytestmark = pytest.mark.skipif(not Path(CHROMIUM).exists(),
                                reason="no Chromium available for browser tests")

from tests.mock_linkedin import PEOPLE, MockLinkedIn  # noqa: E402

DA = """---
code: DA-1
title: Data Analyst
min_years: 2
must_have: [SQL, Power BI, Excel]
hard_requirements: [SQL]
---
Reporting.
"""


@pytest.fixture
def mock():
    m = MockLinkedIn().start()
    yield m
    m.stop()


@pytest.fixture
def li(sandbox, mock, monkeypatch):
    """Sandbox whose linkedin settings point at the stand-in site, with no real waiting."""
    cfgp = sandbox / "settings.yaml"
    d = yaml.safe_load(cfgp.read_text())
    d["linkedin"] = {
        "base_url": mock.url, "headless": True, "browser_channel": "",
        "browser_executable": CHROMIUM, "user_data_dir": str(sandbox / "browser_profile"),
        "time_scale": 0, "login_mode": "automated", "ramp_up": False, "daily_profile_cap": 10,
        "window_start": "00:00", "window_end": "23:59", "min_delay": 1, "max_delay": 2,
        "break_every": [3, 3], "break_seconds": [1, 2], "max_pages_per_search": 3,
        "debug_dir": str(sandbox / "debug"), "location_filter": "metro",
    }
    cfgp.write_text(yaml.safe_dump(d))
    from sourcer import config
    config.cfg(reload=True)
    (sandbox / "jds" / "da.md").write_text(DA)
    return sandbox


def _ctx(sandbox, **kw):
    from sourcer import db, jd as jdmod
    from sourcer.sources.base import FetchContext
    with db.session() as c:
        jdmod.sync_dir(c)
        jds = [j for _, j in jdmod.load_all(c)]
    logs = []
    return FetchContext(jds=jds, max_results=200, manual=True, log=logs.append, **kw), logs


def _login():
    from sourcer import db
    from sourcer.linkedin_browser import login_window
    assert login_window(log=lambda m: None, max_minutes=1) is True
    with db.session() as c:
        db.kv_set(c, "linkedin_logged_in", "1")


def test_login_window_detects_the_session_cookie(li):
    _login()


def test_source_is_unavailable_until_logged_in(li):
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    ok, why = LinkedInBrowserSource().available()
    assert ok is False and "not logged in" in why


def test_full_run_finds_filters_reads_and_stops_at_the_cap(li, mock):
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    _login()
    ctx, logs = _ctx(li)
    res = LinkedInBrowserSource().fetch(ctx)

    assert res.errors == [], res.errors
    assert 1 <= len(res.candidates) <= 10
    # only Karachi-area people were opened; Lahore and Dubai were filtered on the card
    opened = set(mock.state["profiles"])
    by_slug = {p["slug"]: p for p in PEOPLE}
    for slug in opened:
        assert "Lahore" not in by_slug[slug]["location"] and "Dubai" not in by_slug[slug]["location"]
    # no profile opened twice
    assert len(mock.state["profiles"]) == len(opened)

    c = res.candidates[0]
    assert c.full_name.startswith("Person Number")
    assert c.linkedin_url.endswith("/")
    assert "Karachi" in c.location_raw
    assert {"sql", "power_bi", "excel"} <= c.skills()
    assert c.total_years() > 4
    assert c.person_key().startswith("li:person-")

    # profiles that truncate their experience trigger the details page
    truncated = [s for s in opened if by_slug[s]["truncated"]]
    assert set(mock.state["details"]) == set(truncated)
    deep = next(x for x in res.candidates if x.linkedin_url.split("/in/")[1].strip("/") in truncated)
    assert len(deep.experiences) >= 4

    # the quota was recorded
    from sourcer import db
    from sourcer.pacing import used_today
    with db.session() as conn:
        assert used_today(conn, "profile") == len(opened)
        assert used_today(conn, "search") >= 1


def test_daily_cap_is_never_exceeded_across_runs(li, mock):
    from sourcer import db
    from sourcer.pacing import remaining_today
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    _login()
    ctx, _ = _ctx(li)
    LinkedInBrowserSource().fetch(ctx)
    ctx2, _ = _ctx(li)
    LinkedInBrowserSource().fetch(ctx2)
    with db.session() as conn:
        assert remaining_today(conn) == 0
    assert len(mock.state["profiles"]) <= 10


def test_a_warning_page_stops_everything_and_starts_a_cooldown(li, mock):
    from sourcer import db
    from sourcer.pacing import cooldown_until
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    _login()
    mock.state["block_after_profiles"] = 2
    ctx, logs = _ctx(li)
    res = LinkedInBrowserSource().fetch(ctx)

    assert any("warning" in e.lower() for e in res.errors)
    assert len(mock.state["profiles"]) == 3, "must stop at the very first warning, not carry on"
    with db.session() as conn:
        assert cooldown_until(conn) is not None
    ok, why = LinkedInBrowserSource().available()
    assert ok is False and "cooling down" in why


def test_a_stop_request_ends_the_run_cleanly(li, mock):
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    _login()
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return len(mock.state["profiles"]) >= 2

    ctx, _ = _ctx(li, should_stop=stop)
    res = LinkedInBrowserSource().fetch(ctx)
    assert len(mock.state["profiles"]) == 2
    assert res.errors == []


def test_outside_working_hours_nothing_is_opened(li, mock):
    import yaml as _y
    from sourcer import config
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    cfgp = li / "settings.yaml"
    d = _y.safe_load(cfgp.read_text())
    d["linkedin"]["window_start"] = "00:00"; d["linkedin"]["window_end"] = "00:01"
    cfgp.write_text(_y.safe_dump(d)); config.cfg(reload=True)
    _login()
    ctx, _ = _ctx(li, )
    ctx.manual = False
    res = LinkedInBrowserSource().fetch(ctx)
    assert mock.state["profiles"] == [] and res.candidates == []
    assert any("waiting" in n for n in res.notes)


def test_people_already_known_are_not_reopened(li, mock):
    from sourcer import db, store
    from sourcer.models import Candidate
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    _login()
    with db.session() as conn:
        store.upsert(conn, Candidate(full_name="Person Number00",
                                     linkedin_url=f"{mock.url}/in/person-00/"))
    ctx, _ = _ctx(li)
    LinkedInBrowserSource().fetch(ctx)
    assert "person-00" not in mock.state["profiles"]


def test_a_search_page_warning_also_triggers_the_cooldown(li, mock):
    from sourcer import db
    from sourcer.pacing import cooldown_until
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    _login()
    mock.state["block_on_search_page"] = 1
    ctx, _ = _ctx(li)
    res = LinkedInBrowserSource().fetch(ctx)
    assert mock.state["profiles"] == []
    assert res.errors
    with db.session() as conn:
        assert cooldown_until(conn) is not None


# ---- through the web app, the way the buttons do it ------------------------ #
def test_ui_button_runs_the_whole_pipeline_and_the_results_are_askable(li, mock):
    import time as _t
    from sourcer.web.app import JOB, create_app
    H = {"X-Sourcer-UI": "1"}
    JOB.reset()
    client = create_app().test_client()
    assert client.post("/api/login", json={"username": "admin", "password": "admin123"}, headers=H).status_code == 200

    assert client.post("/api/run", json={"kind": "login"}, headers=H).status_code == 200
    for _ in range(300):
        if client.get("/api/job").get_json()["state"] not in {"running", "stopping"}:
            break
        _t.sleep(0.2)
    assert client.get("/api/overview").get_json()["linkedin"]["logged_in"] is True

    assert client.post("/api/run", json={"kind": "linkedin"}, headers=H).status_code == 200
    for _ in range(600):
        j = client.get("/api/job").get_json()
        if j["state"] not in {"running", "stopping"}:
            break
        _t.sleep(0.2)
    assert j["state"] == "done", j["log"]
    log = "\n".join(j["log"])
    assert "searching page 1" in log and "saved:" in log

    o = client.get("/api/overview").get_json()
    assert o["candidates"] >= 1 and o["linkedin"]["used"] >= 1
    a = client.post("/api/ask", headers=H, json={"text": "data analyst ke liye 50% se upar"}).get_json()
    assert a["rows"], "profiles gathered from LinkedIn must be askable by role and score"
    first = a["rows"][0]
    assert first["has_pdf"] is True, "every sourced profile gets a filed PDF"
    assert client.get(f"/api/candidates/{first['id']}/pdf").data[:4] == b"%PDF"


def test_being_logged_out_does_not_start_a_cooldown(li, mock):
    from sourcer import db
    from sourcer.pacing import cooldown_until
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    _login()
    mock.state["logout_after_profiles"] = 0
    # the mock redirects to /login only when the cookie is missing, so drop it by
    # pointing the browser at a profile dir that has no session
    import yaml as _y
    from sourcer import config
    p = li / "settings.yaml"
    d = _y.safe_load(p.read_text()); d["linkedin"]["user_data_dir"] = str(li / "fresh_profile")
    p.write_text(_y.safe_dump(d)); config.cfg(reload=True)
    ctx, _ = _ctx(li)
    res = LinkedInBrowserSource().fetch(ctx)
    assert any("log in" in e.lower() for e in res.errors)
    with db.session() as conn:
        assert cooldown_until(conn) is None, "a logged-out session is not a LinkedIn warning"
        assert db.kv_get(conn, "linkedin_logged_in") == "0"


def test_many_vacancies_take_turns_and_searches_continue_where_they_stopped(li, mock):
    from sourcer import db
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    (li / "jds" / "b.md").write_text(DA.replace("DA-1", "DA-2").replace("Data Analyst", "BI Analyst"))
    _login()

    def logs_of(run_no):
        ctx, logs = _ctx(li)
        ctx.max_results = 1               # one profile per run, to watch the turn-taking
        res = LinkedInBrowserSource().fetch(ctx)
        from sourcer import store
        with db.session() as c:           # what the runner does with every result
            for cand in res.candidates:
                store.upsert(c, cand)
        return [l for l in logs if "opening" in l]

    first, second, third = logs_of(1), logs_of(2), logs_of(3)
    assert first and second and third
    assert first[0].startswith("[DA-1]") and second[0].startswith("[DA-2]"), (first, second)
    assert third[0].startswith("[DA-1]"), "they must alternate, not restart from the first vacancy"
    with db.session() as c:
        assert int(db.kv_get(c, "linkedin_jd_page:DA-1") or 0) >= 1
    # nobody is opened twice across runs
    assert len(mock.state["profiles"]) == len(set(mock.state["profiles"]))


# ---- preferred companies first, then everyone ------------------------------ #
def test_search_stages_follow_the_company_mode():
    from sourcer.models import JD
    from sourcer.sources.linkedin_browser import search_stages
    jd = JD(code="X", title="Data Analyst", must_have=["SQL"], preferred_employers=["Engro", "Jazz"])
    st = search_stages(jd)
    assert [l.split(" (")[0] for l, _ in st] == ["preferred companies", "everyone on LinkedIn"]
    assert '("Engro" OR "Jazz")' in st[0][1] and "Engro" not in st[1][1]
    assert [l.split(" (")[0] for l, _ in search_stages(jd, "only")] == ["preferred companies"]
    assert [l for l, _ in search_stages(jd, "ignore")] == ["everyone on LinkedIn"]
    none = JD(code="Y", title="Data Analyst", must_have=["SQL"])
    assert [l for l, _ in search_stages(none)] == ["everyone on LinkedIn"]          # no lists = just search everyone
    big = JD(code="Z", title="Data Analyst", preferred_employers=[f"Co{i}" for i in range(40)])
    assert len(search_stages(big)) == 4                                              # 3 company chunks + everyone


def _allow_many_profiles(sandbox, **extra):
    from sourcer import config
    cfgp = sandbox / "settings.yaml"
    d = yaml.safe_load(cfgp.read_text())
    d["linkedin"].update(daily_profile_cap=100, **extra)
    cfgp.write_text(yaml.safe_dump(d))
    config.cfg(reload=True)


def test_the_bot_searches_preferred_companies_first_then_widens(li, mock):
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    _allow_many_profiles(li)
    (li / "jds" / "da.md").write_text(DA.replace("must_have:", "preferred_employers: [Engro, Jazz]\nmust_have:", 1))
    _login()
    ctx, logs = _ctx(li)
    LinkedInBrowserSource().fetch(ctx)
    s = mock.state["searches"]
    first_general = next(i for i, q in enumerate(s) if "Engro" not in q)
    assert first_general >= 1 and all("Engro" in q for q in s[:first_general])
    assert any("now searching everyone on LinkedIn" in m for m in logs)
    assert len(mock.state["profiles"]) == len(set(mock.state["profiles"]))          # nobody opened twice across stages


def test_only_mode_never_widens(li, mock):
    import yaml
    from sourcer import config
    from sourcer.sources.linkedin_browser import LinkedInBrowserSource
    cfgp = li / "settings.yaml"
    d = yaml.safe_load(cfgp.read_text())
    d["linkedin"]["company_mode"] = "only"
    cfgp.write_text(yaml.safe_dump(d))
    config.cfg(reload=True)
    (li / "jds" / "da.md").write_text(DA.replace("must_have:", "preferred_employers: [Engro]\nmust_have:", 1))
    _login()
    ctx, _ = _ctx(li)
    LinkedInBrowserSource().fetch(ctx)
    assert mock.state["searches"] and all("Engro" in q for q in mock.state["searches"])


# ---- which browser, and signing in with Google ------------------------------ #
def test_browser_choice_and_separate_profiles(sandbox, monkeypatch):
    from sourcer import linkedin_browser as lb
    monkeypatch.setattr(lb, "find_browsers", lambda: {"edge": "/x/msedge"})
    assert lb.resolve_browser({}) == ("/x/msedge", "edge")                      # no Chrome -> Edge
    assert lb.resolve_browser({"browser": "chrome"}) == (None, "")             # asked for Chrome, none installed
    monkeypatch.setattr(lb, "find_browsers", lambda: {"chrome": "/x/chrome", "edge": "/x/msedge"})
    assert lb.resolve_browser({}) == ("/x/chrome", "chrome")
    assert lb.resolve_browser({"browser": "edge"}) == ("/x/msedge", "edge")
    assert lb.resolve_browser({"browser_executable": "/y/mine"}) == ("/y/mine", "custom")
    a, b = lb.profile_dir({}, "chrome"), lb.profile_dir({}, "edge")
    assert a != b and a.name.endswith("_chrome") and b.name.endswith("_edge")


def test_plain_login_without_chrome_or_edge_falls_back_cleanly(sandbox, monkeypatch):
    from sourcer import linkedin_browser as lb
    monkeypatch.setattr(lb, "find_browsers", lambda: {})
    assert lb._plain_login({}, lambda m: None, 1, lambda: False) is None


def test_plain_login_opens_a_normal_window_and_checks_the_saved_session(sandbox, monkeypatch, tmp_path):
    import sys
    from sourcer import linkedin_browser as lb
    launched = []
    real_popen = lb.subprocess.Popen

    class Fake:
        def __init__(self, cmd, *a, **k):
            launched.append(cmd)
        def poll(self):
            return 0                          # the person already closed the window

    monkeypatch.setattr(lb.subprocess, "Popen", Fake)
    monkeypatch.setattr(lb, "find_browsers", lambda: {"edge": sys.executable})
    logs = []

    class NoSession:                          # stands in for the check that reads the saved cookie
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def is_logged_in(self): return True
    monkeypatch.setattr(lb, "LinkedInBrowser", NoSession)
    monkeypatch.setattr(lb.time, "sleep", lambda s: None)
    assert lb._plain_login({"base_url": "https://www.linkedin.com"}, logs.append, 1, lambda: False) is True
    cmd = launched[0]
    assert cmd[0] == sys.executable and cmd[-1].endswith("/login")
    assert any(a.startswith("--user-data-dir=") and a.endswith("_edge") for a in cmd)
    assert not any("remote-debugging" in a or "enable-automation" in a for a in cmd)      # a normal browser, so Google allows sign-in
    assert any("CLOSE" in m for m in logs)
