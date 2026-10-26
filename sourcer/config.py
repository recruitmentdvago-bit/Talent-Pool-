"""Configuration loading. Everything is overridable from config/settings.yaml."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"

DEFAULTS: dict[str, Any] = {
    "jd_dir": "config/jds",
    "location": {"target_city": "Karachi", "target_country": "Pakistan"},
    # Dimension weights. They are renormalised, so you can edit freely.
    "scoring": {
        "weights": {
            "must_have_skills": 28,
            "nice_have_skills": 8,
            "experience_years": 15,
            "title_relevance": 15,
            "seniority_fit": 6,
            "domain_industry": 5,
            "education": 7,
            "location": 10,
            "certifications": 3,
            "projects": 3,
        },
        # How text similarity folds in:
        #   "tiebreak" -> score = rubric; semantic only orders equal scores (default)
        #   "bonus" -> score = rubric + semantic_bonus_points * semantic  (never penalises)
        #   "blend" -> score = (1-semantic_blend)*rubric + semantic_blend*semantic*100
        "semantic_mode": "tiebreak",
        "semantic_bonus_points": 6.0,
        "semantic_blend": 0.20,
        # Raw cosine that counts as a full-strength textual match.
        "semantic_reference": 0.30,
        # A profile missing a skill listed under `hard_requirements` is capped here.
        "hard_requirement_cap": 45,
        "default_shortlist_threshold": 75,
        # Candidates whose relevant YOE is below this fraction of the JD minimum
        # get no credit on the experience dimension.
        "experience_floor_ratio": 0.5,
        # The AI agent: reads each shortlisted profile against the JD and gives a second score
        # that is blended with the rubric. provider: off | groq | anthropic. API keys are read
        # from the environment (GROQ_API_KEY / ANTHROPIC_API_KEY), never stored.
        "agent": {
            "provider": "off",
            "model": "",                 # empty = the provider's default
            "top_n": 25,                 # per vacancy, best rubric scores first
            "min_rubric": 45,            # do not spend calls on clear non-fits
            "weight": 0.35,              # share of the final score (0 - 0.5)
        },
        # Legacy switch kept so old settings files still work.
        "llm_rerank": {
            "enabled": False,
            "model": "claude-sonnet-5-5",
            "top_n": 25,
            "weight": 0.25,
        },
    },
    "sourcing": {
        # Let Claude read each JD and suggest the related titles to search (needs
        # ANTHROPIC_API_KEY + `pip install anthropic`); without a key the taxonomy
        # and the JD's own skills decide.
        "ai_titles": True,
        "ai_titles_model": "claude-haiku-5-5",
    },
    "retention": {
        # Personal data is deleted this many days after last contact/update
        # unless the candidate is marked shortlisted or beyond.
        "delete_after_days": 365,
        "lawful_basis": "legitimate interest - recruitment (documented)",
    },
    "sources": {
        "enabled": ["pdf_inbox", "csv"],
        "csv": {"watch_dir": "storage/imports"},
        "proxycurl": {"api_key_env": "PROXYCURL_API_KEY", "max_per_run": 50},
        "linkedin_talent": {"client_id_env": "LI_CLIENT_ID", "client_secret_env": "LI_CLIENT_SECRET"},
    },
    "run": {"max_new_per_day": 200, "rate_limit_sleep": 2.0},
    # Direct LinkedIn browsing through a real, visible browser session that YOU
    # log in to once. Pacing is deliberately slow and human-shaped.
    "linkedin": {
        "base_url": "https://www.linkedin.com",
        "browser": "auto",                  # auto | chrome | edge  (auto = Chrome if installed, else Edge)
        "login_mode": "plain",              # plain = a normal window (Google sign-in works); automated = the bot's window
        "browser_channel": "chrome",        # only used when neither Chrome nor Edge is found
        "browser_executable": "",           # optional explicit path
        "headless": False,                  # keep False - a visible window looks normal
        "user_data_dir": "storage/browser_profile",
        "daily_profile_cap": 40,            # profile pages opened per day, at most
        "ramp_up": True,                    # start at 10/day, +5 per active day up to the cap
        "daily_search_pages_cap": 25,
        "window_start": "10:00",            # only browse inside this local-time window
        "window_end": "19:00",
        "weekdays_only": False,
        "session_minutes_max": 25,          # one sitting never lasts longer than this
        "min_delay": 25,                    # seconds between profile views
        "max_delay": 85,
        "break_every": [5, 9],              # a longer pause after this many profiles
        "break_seconds": [150, 420],
        "skip_seen_days": 30,               # do not reopen someone seen recently
        "deep_experience": True,            # open the full experience list when truncated
        "max_pages_per_search": 4,
        "profiles_per_vacancy": 10,         # open this many profiles for one vacancy, then rotate
        "profiles_per_run": 0,              # stop one run after this many profiles (0 = only the daily/time limits)
        "max_rounds": 0,                    # stop after this many passes over all vacancies (0 = no limit)
        "company_mode": "priority_first",   # priority_first | only | ignore  (see Companies tab)
        "geo_urn": "",                      # set from the UI after filtering to Karachi once
        "location_filter": "metro",         # metro | country | any - applied to search cards
        "cooldown_hours": 48,               # after any warning page, stop for this long
        "time_scale": 1.0,                  # tests set 0
        "debug_dir": "storage/linkedin_debug",
    },
    "storage": {
        "db_path": "storage/sourcer.db",
        "profiles_dir": "storage/profiles",
        "pdf_inbox": "storage/pdf_inbox",
        "exports_dir": "storage/exports",
    },
}


def data_dir() -> Path | None:
    """SOURCER_DATA_DIR puts every piece of state (database, PDFs, vacancies, UI settings,
    browser profile) under one folder - on a cloud host that is the mounted volume."""
    v = os.environ.get("SOURCER_DATA_DIR", "").strip()
    return Path(v).expanduser().resolve() if v else None


def is_production() -> bool:
    return os.environ.get("SOURCER_ENV", "").strip().lower() == "production"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: str | Path | None = None):
        self.root = ROOT
        user: dict = {}
        p = Path(path) if path else ROOT / "config" / "settings.yaml"
        if p.exists():
            user = yaml.safe_load(p.read_text()) or {}
        # Settings changed from the web UI live in their own file so the hand-edited
        # settings.yaml (and its comments) is never rewritten behind your back.
        dd = data_dir()
        self.ui_path = (dd / "config" / "settings.ui.yaml") if dd else p.with_name("settings.ui.yaml")
        ui: dict = {}
        if self.ui_path.exists():
            ui = yaml.safe_load(self.ui_path.read_text()) or {}
        self._d = _deep_merge(_deep_merge(DEFAULTS, user), ui)
        if dd:                                   # state lives on the volume whatever the files above say
            self._d["jd_dir"] = str(dd / "jds")
            self._d["sources"]["csv"]["watch_dir"] = str(dd / "imports")
            self._d["storage"].update(db_path=str(dd / "sourcer.db"), profiles_dir=str(dd / "profiles"),
                                      pdf_inbox=str(dd / "pdf_inbox"), exports_dir=str(dd / "exports"))
            self._d["linkedin"].update(user_data_dir=str(dd / "browser_profile"),
                                       debug_dir=str(dd / "linkedin_debug"))
        self.path = p

    def save_ui(self, updates: dict) -> None:
        """Merge `updates` into settings.ui.yaml and reload."""
        cur: dict = {}
        if self.ui_path.exists():
            cur = yaml.safe_load(self.ui_path.read_text()) or {}
        cur = _deep_merge(cur, updates)
        self.ui_path.parent.mkdir(parents=True, exist_ok=True)
        self.ui_path.write_text(yaml.safe_dump(cur, sort_keys=False, allow_unicode=True))
        self._d = _deep_merge(self._d, updates)

    def get(self, dotted: str, default=None):
        cur: Any = self._d
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path_of(self, dotted: str) -> Path:
        """Resolve a configured relative path against the project root."""
        v = self.get(dotted)
        if v is None:
            raise KeyError(dotted)
        p = Path(v)
        return p if p.is_absolute() else self.root / p

    @property
    def weights(self) -> dict[str, float]:
        return dict(self.get("scoring.weights", {}))

    def __repr__(self) -> str:
        return f"<Config {self.path}>"


_cfg: Config | None = None


def cfg(reload: bool = False) -> Config:
    global _cfg
    if _cfg is None or reload:
        _cfg = Config(os.environ.get("SOURCER_CONFIG"))
    return _cfg
