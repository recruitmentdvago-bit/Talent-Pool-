"""The AI agent (Groq / Claude), the activity log, and the cloud-hosting behaviour."""
import io
import json
import os

import pytest
import yaml

pytest.importorskip("flask")
from sourcer import agent, events, runner, store  # noqa: E402
from sourcer.web.app import JOB, create_app  # noqa: E402

H = {"X-Sourcer-UI": "1"}
JD_FORM = {"title": "Data Analyst", "seniority": "mid", "min_years": 2, "education_min": "bachelors",
           "must_have": "SQL, Power BI, Excel", "nice_have": "Python", "hard_requirements": "SQL"}


def _admin(app):
    c = app.test_client()
    assert c.post("/api/login", json={"username": "admin", "password": "admin123"}, headers=H).status_code == 200
    return c


def _use_agent(sandbox, provider="groq", **extra):
    from sourcer import config
    p = sandbox / "settings.yaml"
    d = yaml.safe_load(p.read_text())
    d.setdefault("scoring", {})["agent"] = {"provider": provider, "pause_seconds": 0, "top_n": 10, "min_rubric": 0, "weight": 0.4, **extra}
    p.write_text(yaml.safe_dump(d))
    config.cfg(reload=True)


class FakeResp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code, self._p, self.headers, self.text = status, payload, headers or {}, text

    def json(self):
        return self._p


def _groq_ok(score=80, verdict="strong_fit"):
    body = {"score": score, "verdict": verdict, "reasons": ["5 years of Power BI at a bank"], "concerns": ["no Python"]}
    return FakeResp(200, {"choices": [{"message": {"content": json.dumps(body)}}],
                          "usage": {"prompt_tokens": 900, "completion_tokens": 80, "total_tokens": 980}})


@pytest.fixture
def pool(sandbox, strong_analyst, weak_candidate, monkeypatch):
    """Two candidates, one vacancy, rubric-scored; the agent key present."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    app = create_app()
    c = _admin(app)
    c.post("/api/jds", json=JD_FORM, headers=H)
    from sourcer import db
    strong_analyst.profile_pdf_path = ""
    weak_candidate.profile_pdf_path = ""
    with db.session() as conn:
        store.upsert(conn, strong_analyst)
        store.upsert(conn, weak_candidate)
        runner.score_all(conn)
    return app, c


def _scores(conn):
    return {r["full_name"]: r for r in conn.execute(
        "SELECT c.full_name, s.* FROM scores s JOIN candidates c ON c.id=s.candidate_id")}


# ---- the agent --------------------------------------------------------------- #
def test_agent_is_off_until_a_provider_and_key_are_set(sandbox, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert agent.provider() == "off" and agent.available()[0] is False
    _use_agent(sandbox)
    ok, why = agent.available()
    assert ok is False and "GROQ_API_KEY" in why
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    assert agent.available()[0] is True and agent.model() == "llama-3.3-70b-versatile"
    _use_agent(sandbox, model="llama-3.1-8b-instant")
    assert agent.model() == "llama-3.1-8b-instant"


def test_agent_reads_profiles_blends_the_score_and_does_not_reread(pool, sandbox, monkeypatch):
    app, client = pool
    _use_agent(sandbox)
    import requests
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, json))
        return _groq_ok(90)
    monkeypatch.setattr(requests, "post", fake_post)
    from sourcer import db
    with db.session() as conn:
        before = _scores(conn)
        n, notes = runner.agent_review(conn)
        after = _scores(conn)
    assert n == 2 and len(calls) == 2
    url, body = calls[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions" and body["model"] == "llama-3.3-70b-versatile"
    sent = body["messages"][1]["content"]
    assert "Ayesha" not in sent and "@" not in sent and "PROFILE START" in sent      # no name or contact details leave
    a = after["Ayesha Khan"]
    assert a["llm_score"] == 90 and a["llm_verdict"] == "strong_fit"
    assert a["score"] == round(0.6 * before["Ayesha Khan"]["rubric_score"] + 0.4 * 90, 1)
    assert json.loads(a["llm_notes"])["reasons"] == ["5 years of Power BI at a bank"]
    with db.session() as conn:
        assert runner.agent_review(conn)[0] == 0 and len(calls) == 2                  # already read: no more calls
        assert runner.agent_review(conn, force=True)[0] == 2 and len(calls) == 4
    # rescoring (e.g. after a vacancy edit) keeps the agent's reading and re-blends it
    with db.session() as conn:
        runner.score_all(conn)
        kept = _scores(conn)["Ayesha Khan"]
    assert kept["llm_score"] == 90 and kept["score"] == a["score"]


def test_agent_waits_out_rate_limits_and_stops_on_a_bad_key(pool, sandbox, monkeypatch):
    app, client = pool
    _use_agent(sandbox)
    import requests
    from sourcer import agent as ag
    monkeypatch.setattr(ag.time, "sleep", lambda s: None)
    seq = [FakeResp(429, headers={"retry-after": "3"}), _groq_ok(70), _groq_ok(60)]
    monkeypatch.setattr(requests, "post", lambda *a, **k: seq.pop(0))
    from sourcer import db
    with db.session() as conn:
        assert runner.agent_review(conn)[0] == 2                                      # the 429 was retried
    events.flush()
    with db.session() as conn:
        assert any("rate limit" in e["message"] for e in events.list_events(conn, limit=300))
        conn.execute("UPDATE scores SET llm_score=NULL, llm_verdict=NULL, llm_notes=NULL")
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or FakeResp(401, text="bad key"))
    with db.session() as conn:
        n, notes = runner.agent_review(conn)
    assert n == 0 and len(calls) == 1 and any("GROQ_API_KEY" in x for x in notes)       # fatal: stop, do not hammer


def test_agent_survives_messy_model_output(pool, sandbox, monkeypatch):
    app, client = pool
    _use_agent(sandbox)
    import requests
    wrapped = FakeResp(200, {"choices": [{"message": {"content": 'Sure! {"score": "72", "verdict": "Possible Fit", "reasons": ["ok"]} done'}}]})
    monkeypatch.setattr(requests, "post", lambda *a, **k: wrapped)
    a, _ = agent.assess(__import__("sourcer.models", fromlist=["JD"]).JD(title="X", must_have=["SQL"]), 1,
                        __import__("sourcer.models", fromlist=["Candidate"]).Candidate(full_name="Z"))
    assert a.score == 72 and a.verdict == "possible_fit" and a.concerns == []


def test_a_changed_profile_is_read_again(pool, sandbox, monkeypatch, strong_analyst):
    app, client = pool
    _use_agent(sandbox)
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _groq_ok(88))
    from sourcer import db
    with db.session() as conn:
        runner.agent_review(conn)
        strong_analyst.headline = "Senior Data Analyst | now with dbt"
        strong_analyst.profile_pdf_path = ""
        runner.file_candidate(conn, strong_analyst)
        assert _scores(conn)["Ayesha Khan"]["llm_score"] is None
        assert _scores(conn)["Priya Shah" if "Priya Shah" in _scores(conn) else next(k for k in _scores(conn) if k != "Ayesha Khan")]["llm_score"] == 88


def test_agent_settings_and_test_button(pool, sandbox, monkeypatch):
    app, client = pool
    import requests
    s = client.get("/api/settings").get_json()["agent"]
    assert s["provider"] == "off" and {p["id"] for p in s["providers"]} == {"groq", "anthropic"}
    assert client.post("/api/settings", json={"agent": {"provider": "groq", "model": "llama-3.1-8b-instant", "top_n": 500,
                                                          "weight": 0.9, "min_rubric": 50}}, headers=H).status_code == 200
    s = client.get("/api/settings").get_json()["agent"]
    assert (s["provider"], s["model"], s["top_n"], s["weight"], s["ready"]) == ("groq", "llama-3.1-8b-instant", 100, 0.5, True)
    client.post("/api/settings", json={"agent": {"model": "bad model; drop table"}}, headers=H)
    assert client.get("/api/settings").get_json()["agent"]["model"] == ""
    monkeypatch.setattr(requests, "post", lambda *a, **k: _groq_ok(77))
    r = client.post("/api/agent/test", json={}, headers=H)
    assert r.status_code == 200 and r.get_json()["score"] == 77
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(401, text="no"))
    bad = client.post("/api/agent/test", json={}, headers=H)
    assert bad.status_code == 400 and "GROQ_API_KEY" in bad.get_json()["error"]
    monkeypatch.delenv("GROQ_API_KEY")
    assert client.post("/api/run", json={"kind": "agent"}, headers=H).status_code == 400


# ---- the activity log -------------------------------------------------------- #
def _pdf(sandbox, cand):
    from sourcer.pdfstore import render_profile_pdf
    p = sandbox / "p.pdf"
    render_profile_pdf(cand, p)
    return p.read_bytes()


def test_activity_log_shows_every_step_for_a_profile(pool, sandbox, strong_analyst, monkeypatch):
    app, client = pool
    _use_agent(sandbox)
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _groq_ok(81))
    client.post("/api/upload", headers=H, content_type="multipart/form-data",
                data={"files": [(io.BytesIO(_pdf(sandbox, strong_analyst)), "new.pdf")]})
    assert client.post("/api/run", json={"kind": "files"}, headers=H).status_code == 200
    import time
    for _ in range(200):
        if JOB.state not in ("running", "stopping"):
            break
        time.sleep(0.1)
    events.flush()
    ev = client.get("/api/events?limit=300").get_json()
    stages = [e["stage"] for e in ev["events"]]
    for needed in ("job", "extract", "parse", "store", "score", "agent"):
        assert needed in stages, (needed, stages)
    assert stages.index("extract") < stages.index("parse") < stages.index("store") < stages.index("score") < stages.index("agent")
    agent_lines = [e for e in ev["events"] if e["stage"] == "agent" and e["candidate"]]
    assert any("strong fit" in e["message"] and e["detail"]["reasons"] for e in agent_lines)
    assert ev["status"]["counts"]["agent"] >= 1 and ev["status"]["agent"]["ready"] is True
    last = ev["events"][-1]["id"]
    assert client.get(f"/api/events?after={last}").get_json()["events"] == []             # polling returns only new lines
    only = client.get("/api/events?stage=parse").get_json()["events"]
    assert only and all(e["stage"] == "parse" for e in only)
    assert client.delete("/api/events", headers=H).status_code == 200
    assert client.get("/api/events").get_json()["events"] == []


def test_activity_log_is_admin_only(sandbox):
    app = create_app()
    admin = _admin(app)
    admin.post("/api/users", headers=H, json={"username": "some.hod", "password": "secret1", "role": "hod"})
    hod = app.test_client()
    hod.post("/api/login", json={"username": "some.hod", "password": "secret1"}, headers=H)
    assert hod.get("/api/events").status_code == 403
    assert hod.post("/api/agent/test", json={}, headers=H).status_code == 403


# ---- cloud hosting ------------------------------------------------------------ #
@pytest.fixture
def cloud(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCER_ENV", "production")
    monkeypatch.setenv("SOURCER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-secret-pw")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "talent.example.app")
    monkeypatch.delenv("SOURCER_CONFIG", raising=False)
    from sourcer import config
    config.cfg(reload=True)
    yield tmp_path / "data"
    config.cfg(reload=True)


def test_cloud_state_lives_on_the_volume_and_the_admin_password_comes_from_the_environment(cloud):
    app = create_app()
    c = app.test_client()
    h = {"Host": "talent.example.app"}
    assert c.get("/healthz", headers={"Host": "healthcheck.railway.app"}).status_code == 200      # the platform's probe
    assert c.get("/api/me", headers=h).status_code == 401
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert c.post("/api/login", json={"username": "admin", "password": "admin123"}, headers={**H, **h}).status_code == 401
    r = c.post("/api/login", json={"username": "admin", "password": "a-long-secret-pw"}, headers={**H, **h})
    assert r.status_code == 200
    assert "Secure" in r.headers["Set-Cookie"] and "HttpOnly" in r.headers["Set-Cookie"]
    assert r.headers["Strict-Transport-Security"]
    me = c.get("/api/me", headers=h).get_json()
    assert me["cloud"] is True and me["default_password"] is False
    assert (cloud / "sourcer.db").is_file() and (cloud / ".secret_key").is_file()
    c.post("/api/jds", json=JD_FORM, headers={**H, **h})
    assert list((cloud / "jds").glob("V-001-*.md"))                                              # vacancies on the volume too


def test_wrong_passwords_are_throttled(cloud):
    app = create_app()
    c = app.test_client()
    h = {**H, "Host": "talent.example.app"}
    codes = [c.post("/api/login", json={"username": "admin", "password": f"nope{i}"}, headers=h).status_code for i in range(10)]
    assert codes[:8] == [401] * 8 and codes[8:] == [429, 429]
    assert c.post("/api/login", json={"username": "admin", "password": "a-long-secret-pw"}, headers=h).status_code == 429


def test_a_random_admin_password_is_made_when_none_is_given(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOURCER_ENV", "production")
    monkeypatch.setenv("SOURCER_DATA_DIR", str(tmp_path / "d2"))
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SOURCER_CONFIG", raising=False)
    from sourcer import config
    config.cfg(reload=True)
    app = create_app()
    out = capsys.readouterr().out
    assert "FIRST START" in out
    pw = out.split("admin / ")[1].split(" ")[0]
    c = app.test_client()
    assert c.post("/api/login", json={"username": "admin", "password": "admin123"}, headers=H).status_code in (401, 403)
    config.cfg(reload=True)


def test_admin_lockout_recovery_variable(cloud, monkeypatch):
    create_app()
    monkeypatch.setenv("RESET_ADMIN_PASSWORD", "brand-new-pass")
    app = create_app()
    c = app.test_client()
    h = {**H, "Host": "talent.example.app"}
    assert c.post("/api/login", json={"username": "admin", "password": "brand-new-pass"}, headers=h).status_code == 200


# ---- collector ingest ---------------------------------------------------------- #
def test_ingest_is_off_without_a_token_and_needs_the_right_one(cloud, monkeypatch):
    app = create_app()
    c = app.test_client()
    h = {"Host": "talent.example.app"}
    assert c.get("/api/ingest/vacancies", headers=h).status_code == 404                           # no INGEST_TOKEN: switched off
    monkeypatch.setenv("INGEST_TOKEN", "tok-123")
    assert c.get("/api/ingest/vacancies", headers=h).status_code == 401
    assert c.get("/api/ingest/vacancies", headers={**h, "Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/ingest/vacancies", headers={**h, "Authorization": "Bearer tok-123"}).status_code == 200


def test_collector_round_trip(cloud, monkeypatch, strong_analyst, tmp_path):
    """A profile read on a PC arrives in the portal with its PDF, scored against the portal's vacancies."""
    monkeypatch.setenv("INGEST_TOKEN", "tok-123")
    app = create_app()
    admin = app.test_client()
    h = {"Host": "talent.example.app"}
    assert admin.post("/api/login", json={"username": "admin", "password": "a-long-secret-pw"}, headers={**H, **h}).status_code == 200
    admin.post("/api/jds", json=JD_FORM, headers={**H, **h})
    api = app.test_client()
    auth = {**h, "Authorization": "Bearer tok-123"}
    vac = api.get("/api/ingest/vacancies", headers=auth).get_json()["vacancies"]
    assert vac[0]["filename"].startswith("V-001") and "Data Analyst" in vac[0]["text"]

    from sourcer.pdfstore import render_profile_pdf
    import base64
    pdf = tmp_path / "x.pdf"
    render_profile_pdf(strong_analyst, pdf)
    body = {"candidates": [{"candidate": json.loads(json.dumps(strong_analyst.to_dict(), default=str)),
                            "pdf_b64": base64.b64encode(pdf.read_bytes()).decode()},
                           {"candidate": {"full_name": ""}}]}
    r = api.post("/api/ingest/candidates", json=body, headers=auth).get_json()
    assert (r["created"], r["updated"], r["failed"]) == (1, 0, 1) and r["scored"] >= 1
    rows = admin.get("/api/candidates?min_score=0", headers=h).get_json()
    assert [x["name"] for x in rows] == ["Ayesha Khan"] and rows[0]["has_pdf"] is True
    again = api.post("/api/ingest/candidates", json={"candidates": body["candidates"][:1]}, headers=auth).get_json()
    assert (again["created"], again["updated"]) == (0, 1) or again["created"] == 0               # same person is not duplicated
    assert len(admin.get("/api/candidates?min_score=0", headers=h).get_json()) == 1


def test_collector_client_pulls_vacancies_and_pushes_new_profiles(cloud, monkeypatch, strong_analyst, tmp_path):
    """The PC side, wired straight to the portal's test client."""
    monkeypatch.setenv("INGEST_TOKEN", "tok-123")
    portal = create_app().test_client()
    h = {"Host": "talent.example.app"}
    admin = create_app().test_client()
    admin.post("/api/login", json={"username": "admin", "password": "a-long-secret-pw"}, headers={**H, **h})
    admin.post("/api/jds", json=JD_FORM, headers={**H, **h})

    import requests
    from sourcer import collector

    def adapt(method):
        def call(url, headers=None, json=None, timeout=None):
            path = url.split("talent.example.app", 1)[1]
            from sourcer import config
            pc_dir = os.environ["SOURCER_DATA_DIR"]
            os.environ["SOURCER_DATA_DIR"] = str(cloud)               # the portal is another machine: its own folder
            config.cfg(reload=True)
            try:
                r = getattr(portal, method)(path, headers={**h, **(headers or {})}, **({"json": json} if json is not None else {}))
            finally:
                os.environ["SOURCER_DATA_DIR"] = pc_dir
                config.cfg(reload=True)
            return type("R", (), {"status_code": r.status_code, "json": lambda self: r.get_json(),
                                  "raise_for_status": lambda self: None})()
        return call
    monkeypatch.setattr(requests, "get", adapt("get"))
    monkeypatch.setattr(requests, "post", adapt("post"))

    # the "PC": its own data folder
    pc = tmp_path / "pc"
    monkeypatch.setenv("SOURCER_DATA_DIR", str(pc))
    from sourcer import config, db
    config.cfg(reload=True)
    assert collector.pull_vacancies("https://talent.example.app", "tok-123", log=lambda m: None) == 1
    assert list((pc / "jds").glob("V-001-*.md"))
    strong_analyst.profile_pdf_path = ""
    with db.session() as conn:
        _, _, _ = runner.file_candidate(conn, strong_analyst)
    tot = collector.push_candidates("https://talent.example.app", "tok-123", log=lambda m: None)
    assert tot["sent"] == 1 and tot["created"] == 1
    assert collector.push_candidates("https://talent.example.app", "tok-123", log=lambda m: None)["sent"] == 0   # nothing new


def test_backup_zip_has_a_working_database_and_the_vacancies(cloud):
    import sqlite3
    import zipfile
    app = create_app()
    c = app.test_client()
    h = {**H, "Host": "talent.example.app"}
    c.post("/api/login", json={"username": "admin", "password": "a-long-secret-pw"}, headers=h)
    c.post("/api/jds", json=JD_FORM, headers=h)
    r = c.get("/api/backup", headers=h)
    assert r.status_code == 200 and "talent-pool-backup" in r.headers["Content-Disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert "sourcer.db" in names and any(n.startswith("jds/V-001") for n in names)
    import tempfile, pathlib
    f = pathlib.Path(tempfile.mkdtemp()) / "b.db"
    f.write_bytes(z.read("sourcer.db"))
    assert sqlite3.connect(f).execute("SELECT COUNT(*) FROM jds").fetchone()[0] == 1
    hod_app = app.test_client()
    c.post("/api/users", json={"username": "some.hod", "password": "secret1", "role": "hod"}, headers=h)
    hod_app.post("/api/login", json={"username": "some.hod", "password": "secret1"}, headers=h)
    assert hod_app.get("/api/backup", headers=h).status_code == 403
