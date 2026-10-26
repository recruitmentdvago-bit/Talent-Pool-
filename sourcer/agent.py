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
    def __init__(self, msg: str, fatal: bool = False, model_problem: bool = False):
        super().__init__(msg)
        self.fatal = fatal                  # True = every further call this run would fail the same way
        self.model_problem = model_problem  # the model name is unknown / retired / not allowed for this key


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
    return _override.get(provider()) or settings().get("model") or PROVIDERS.get(provider(), {}).get("model", "")


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


# --------------------------------------------------------------------------- #
# Models change names. Ask Groq what is available now instead of trusting a name.
# --------------------------------------------------------------------------- #
_NOT_CHAT = ("whisper", "tts", "guard", "embed", "moderation", "transcri", "orpheus", "playai", "distil-whisper")
_PREFER = ("llama-3.3-70b", "llama-3.1-70b", "gpt-oss-120b", "llama-4", "qwen", "llama-3.1-8b", "gpt-oss-20b", "gemma", "llama")


def _rank(mid: str) -> int:
    m = mid.lower()
    for i, p in enumerate(_PREFER):
        if p in m:
            return i
    return len(_PREFER)


def groq_models() -> list[str]:
    """Chat models this key can use right now, best first. [] if the list cannot be fetched."""
    import requests
    if not has_key("groq"):
        return []
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", timeout=20,
                         headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY'].strip()}"})
        if r.status_code != 200:
            return []
        ids = [m.get("id", "") for m in (r.json().get("data") or []) if m.get("active", True)]
    except Exception:
        return []
    ids = [i for i in ids if i and not any(x in i.lower() for x in _NOT_CHAT)]
    return sorted(ids, key=lambda i: (_rank(i), i))


_override: dict[str, str] = {}      # a model we had to switch to during this run


def _switch_model(bad: str, why: str, label: str) -> str | None:
    """The configured model was refused: pick the best live one, tell the log, and remember it."""
    alt = next((m for m in groq_models() if m != bad), None)
    if not alt:
        return None
    events.emit("agent", f"Groq says model '{bad}' is not usable ({why[:120]}). Switching to '{alt}'.",
                candidate=label, level="warn")
    _override["groq"] = alt
    try:
        cfg().save_ui({"scoring": {"agent": {"model": alt}}})        # so the settings page shows what is really used
    except Exception:
        pass
    return alt


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


def _groq_error(r) -> tuple[str, str]:
    try:
        e = (r.json() or {}).get("error") or {}
        return str(e.get("code") or e.get("type") or ""), str(e.get("message") or "")
    except Exception:
        return "", (getattr(r, "text", "") or "")[:200]


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
        if r.status_code >= 400 and r.status_code != 429 and r.status_code < 500:
            code, msg = _groq_error(r)
            if r.status_code in (400, 403, 404) and "model" in (code + " " + msg).lower():
                raise AgentError(f"Groq cannot use the model '{mdl}': {msg or code}", fatal=True, model_problem=True)
            if r.status_code == 401 or "api_key" in code.lower() or "api key" in msg.lower():
                raise AgentError(f"Groq refused the key ({msg or code}) - check GROQ_API_KEY", fatal=True)
            raise AgentError(f"Groq error {r.status_code}: {(msg or code or r.text)[:200]}", fatal=r.status_code == 403)
        if r.status_code >= 500:
            last = f"Groq server error {r.status_code}"
            time.sleep(3 * (attempt + 1))
            continue
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
    if p != "groq":
        text, usage = _claude(SYSTEM, user, mdl)
        return _parse(text, cid), usage
    try:
        text, usage = _groq(SYSTEM, user, mdl, label)
    except AgentError as e:
        if not e.model_problem:
            raise
        alt = _switch_model(mdl, str(e), label)           # the name is stale: use one that exists today
        if not alt:
            now_ok = ", ".join(groq_models()[:8])
            raise AgentError(f"{e} Models available to your key now: {now_ok or 'could not fetch the list'}.", fatal=True)
        text, usage = _groq(SYSTEM, user, alt, label)
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
