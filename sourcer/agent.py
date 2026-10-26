"""The AI agent: reads one candidate's profile against one vacancy and gives a second opinion.

The rubric in scoring.py stays the primary, explainable score. The agent adds what a keyword
rubric cannot see - whether a career *reads* like a real fit - and its score is blended in at a
capped weight, with its verdict and reasons stored next to the rubric so any disagreement is
visible.

Providers (pick one in the app, Linkedin tab -> AI agent):
  groq       free tier at console.groq.com, open models, OpenAI-style API.  Key: GROQ_API_KEY
  anthropic  Claude.  Key: ANTHROPIC_API_KEY  (pip install anthropic)

Keys come from the environment only. What is sent: the vacancy text and a profile summary with
NO name, email, phone or LinkedIn address (see llm._candidate_block).
"""
from __future__ import annotations

import json
import os
import time

from . import events
from .config import cfg
from .llm import SYSTEM, Assessment, _candidate_block, _jd_block
from .models import JD, Candidate

PROVIDERS = {
    "groq": {"label": "Groq (free tier)", "env": "GROQ_API_KEY", "model": "llama-3.3-70b-versatile",
             "url": "https://api.groq.com/openai/v1/chat/completions", "pause": 2.5},
    "anthropic": {"label": "Claude", "env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5", "pause": 0.5},
}
VERDICTS = {"strong_fit", "possible_fit", "stretch", "not_a_fit", "insufficient_information"}

REPLY_FORMAT = """Reply with ONE JSON object and nothing else:
{"score": <0-100>, "verdict": "strong_fit" | "possible_fit" | "stretch" | "not_a_fit" | "insufficient_information",
 "reasons": [up to 4 short strings, each tied to evidence in the profile],
 "concerns": [up to 3 short strings]}"""


class AgentError(Exception):
    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal          # True = every further call this run would fail the same way


def settings() -> dict:
    c = dict(cfg().get("scoring.agent", {}) or {})
    if (c.get("provider") or "off") == "off" and (cfg().get("scoring.llm_rerank", {}) or {}).get("enabled"):
        c["provider"] = "anthropic"                     # an old settings file that switched Claude on
    c.setdefault("provider", "off")
    return c


def provider() -> str:
    p = (settings().get("provider") or "off").lower()
    return p if p in PROVIDERS else "off"


def model() -> str:
    return settings().get("model") or PROVIDERS.get(provider(), {}).get("model", "")


def has_key(p: str | None = None) -> bool:
    p = p or provider()
    return bool(p in PROVIDERS and os.environ.get(PROVIDERS[p]["env"], "").strip())


def available() -> tuple[bool, str]:
    p = provider()
    if p == "off":
        return False, "the AI agent is switched off"
    if not has_key(p):
        return False, f"{PROVIDERS[p]['env']} is not set"
    if p == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "the 'anthropic' package is not installed (pip install anthropic)"
    return True, "ready"


def blend(rubric: float, agent_score: float) -> float:
    w = max(0.0, min(0.5, float(settings().get("weight", 0.35))))
    return round((1.0 - w) * rubric + w * agent_score, 1)


# --------------------------------------------------------------------------- #
def _parse(text: str, cid: int) -> Assessment:
    try:
        d = json.loads(text)
    except ValueError:
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            raise AgentError("the model did not return JSON")
        try:
            d = json.loads(text[i:j + 1])
        except ValueError:
            raise AgentError("the model's JSON could not be read")
    if isinstance(d, dict) and isinstance(d.get("assessments"), list) and d["assessments"]:
        d = d["assessments"][0]
    try:
        score = max(0.0, min(100.0, float(d.get("score"))))
    except (TypeError, ValueError):
        raise AgentError("the model returned no score")
    verdict = str(d.get("verdict", "")).strip().lower().replace(" ", "_")
    if verdict not in VERDICTS:
        verdict = "strong_fit" if score >= 80 else "possible_fit" if score >= 60 else "stretch" if score >= 40 else "not_a_fit"
    clean = lambda v: [str(x)[:300] for x in (v or [])][:5] if isinstance(v, list) else []
    return Assessment(candidate_id=cid, score=score, verdict=verdict,
                      reasons=clean(d.get("reasons")), concerns=clean(d.get("concerns")))


def _groq(system: str, user: str, mdl: str, cid_label: str) -> tuple[str, dict]:
    import requests
    spec = PROVIDERS["groq"]
    headers = {"Authorization": f"Bearer {os.environ['GROQ_API_KEY'].strip()}", "Content-Type": "application/json"}
    body = {"model": mdl, "temperature": 0.1, "max_tokens": 700,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    last = ""
    for attempt in range(4):
        try:
            r = requests.post(spec["url"], headers=headers, json=body, timeout=60)
        except requests.RequestException as e:
            last = f"network error ({type(e).__name__})"
            events.emit("agent", f"Groq unreachable - {last}; retrying", candidate=cid_label, level="warn")
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            wait = 8.0 * (attempt + 1)
            try:
                wait = float(r.headers.get("retry-after") or wait)
            except ValueError:
                pass
            wait = min(max(wait, 2.0), 60.0)
            last = "rate limit (free tier)"
            events.emit("agent", f"Groq rate limit reached - waiting {wait:.0f}s then trying again", candidate=cid_label, level="warn")
            time.sleep(wait)
            continue
        if r.status_code in (401, 403):
            raise AgentError("Groq refused the key - check GROQ_API_KEY", fatal=True)
        if r.status_code == 404 or (r.status_code == 400 and "model" in r.text.lower()):
            raise AgentError(f"Groq does not know the model '{mdl}' - pick another in the AI agent card", fatal=True)
        if r.status_code >= 500:
            last = f"Groq server error {r.status_code}"
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise AgentError(f"Groq error {r.status_code}: {r.text[:200]}")
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"], data.get("usage") or {}
        except (KeyError, IndexError, TypeError):
            raise AgentError("Groq returned an empty answer")
    raise AgentError(f"gave up after retries: {last}", fatal=last.startswith("rate limit"))


def _claude(system: str, user: str, mdl: str) -> tuple[str, dict]:
    import anthropic
    try:
        resp = anthropic.Anthropic().messages.create(
            model=mdl, max_tokens=700, system=system, messages=[{"role": "user", "content": user}])
    except anthropic.AuthenticationError:
        raise AgentError("Claude refused the key - check ANTHROPIC_API_KEY", fatal=True)
    except anthropic.RateLimitError:
        raise AgentError("Claude rate limit reached", fatal=True)
    except anthropic.APIError as e:
        raise AgentError(f"Claude error: {type(e).__name__}")
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    u = getattr(resp, "usage", None)
    return text, {"prompt_tokens": getattr(u, "input_tokens", 0), "completion_tokens": getattr(u, "output_tokens", 0)}


def assess(jd: JD, cid: int, cand: Candidate, label: str = "") -> tuple[Assessment, dict]:
    """Read one profile against one vacancy. Raises AgentError. -> (assessment, usage)."""
    ok, why = available()
    if not ok:
        raise AgentError(why, fatal=True)
    p, mdl = provider(), model()
    user = "\n\n".join(["JOB DESCRIPTION", _jd_block(jd), "", "CANDIDATE", _candidate_block(cid, cand), "", REPLY_FORMAT])
    text, usage = _groq(SYSTEM, user, mdl, label) if p == "groq" else _claude(SYSTEM, user, mdl)
    return _parse(text, cid), usage


def test_connection() -> dict:
    """A tiny made-up profile, to prove the key, the model and the JSON round trip work."""
    from .models import Education, Experience
    jd = JD(code="TEST", title="Data Analyst", min_years=2, must_have=["SQL", "Power BI"],
            raw_text="Build dashboards and reports in SQL and Power BI for a retail business.")
    c = Candidate(full_name="Test Person", headline="Data Analyst", skills_raw=["SQL", "Power BI", "Excel"],
                  experiences=[Experience(title="Data Analyst", company="Acme", start_date="2021-01", is_current=True,
                                          description="Built Power BI dashboards on SQL Server for sales reporting.")],
                  educations=[Education(degree="BS", field_of_study="Computer Science", institution="NED")])
    t0 = time.time()
    try:
        a, usage = assess(jd, 0, c, label="connection test")
    except AgentError as e:
        return {"ok": False, "error": str(e), "provider": provider(), "model": model()}
    return {"ok": True, "provider": provider(), "model": model(), "seconds": round(time.time() - t0, 1),
            "score": a.score, "verdict": a.verdict, "reasons": a.reasons, "tokens": usage}
