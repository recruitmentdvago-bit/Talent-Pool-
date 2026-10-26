"""Licensed people-data provider intake.

Point this at a provider you hold a contract with (Proxycurl, People Data Labs,
Coresignal, SignalHire, Apollo, Lusha ...). The adapter is config-driven so you
can switch vendors without code changes: declare the endpoint, auth header and
a field mapping in config/settings.yaml, and the runner handles the rest.

Why this instead of scraping LinkedIn directly: a licensed API gives you a
contract, a rate limit you are allowed to use, and a defensible answer when a
candidate asks where you got their data. Automating a logged-in LinkedIn
session gives you a banned account.

YOUR responsibility before enabling this:
  * hold a current subscription and stay inside its terms
  * confirm the vendor's data is lawfully sourced for your jurisdiction
  * record a lawful basis for processing (config: retention.lawful_basis)
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

from ..config import cfg
from ..models import Candidate, Education, Experience
from .base import BaseSource, FetchContext, FetchResult

DEFAULT_TIMEOUT = 30


def _dig(obj: Any, path: str, default=None):
    """Pull a value out of nested JSON with a dotted path; [] walks a list."""
    cur = obj
    for part in path.split("."):
        if part == "[]":
            return cur if isinstance(cur, list) else []
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur


class ProviderSource(BaseSource):
    """Generic REST adapter driven by a mapping block in settings.yaml."""
    name = "provider"

    def __init__(self, conf_key: str = "sources.provider"):
        self.conf_key = conf_key
        self.conf = cfg().get(conf_key, {}) or {}

    # ---- plumbing ------------------------------------------------------- #
    def _api_key(self) -> str:
        env = self.conf.get("api_key_env", "PROVIDER_API_KEY")
        return os.environ.get(env, "")

    def available(self) -> tuple[bool, str]:
        if not self.conf.get("search_url"):
            return False, f"{self.conf_key}.search_url not configured"
        if not self._api_key():
            return False, f"env {self.conf.get('api_key_env')} is not set"
        return True, "configured"

    def _headers(self) -> dict[str, str]:
        scheme = self.conf.get("auth_scheme", "Bearer")
        header = self.conf.get("auth_header", "Authorization")
        key = self._api_key()
        return {header: f"{scheme} {key}".strip(), "Accept": "application/json"}

    def _query_for(self, jd) -> dict:
        """Build the vendor's query params from a JD using the configured template."""
        tpl = self.conf.get("query_template") or {}
        subs = {
            "title": jd.title,
            "titles": "|".join([jd.title] + (jd.must_have[:0] or [])),
            "city": cfg().get("location.target_city", "Karachi"),
            "country": cfg().get("location.target_country", "Pakistan"),
            "country_code": self.conf.get("country_code", "pk"),
            "skills": ",".join(jd.must_have[:6]),
            "min_years": str(int(jd.min_years or 0)),
            "seniority": jd.seniority or "",
            "page_size": str(int(self.conf.get("page_size", 25))),
        }
        out = {}
        for k, v in tpl.items():
            out[k] = str(v).format(**subs) if isinstance(v, str) else v
        return out

    # ---- mapping -------------------------------------------------------- #
    def _to_candidate(self, item: dict) -> Candidate | None:
        m = self.conf.get("map") or {}
        name = _dig(item, m.get("full_name", "full_name"), "") or ""
        if not name:
            first = _dig(item, m.get("first_name", "first_name"), "") or ""
            last = _dig(item, m.get("last_name", "last_name"), "") or ""
            name = f"{first} {last}".strip()
        if not name:
            return None

        exps: list[Experience] = []
        for e in (_dig(item, m.get("experiences", "experiences"), []) or []):
            em = self.conf.get("map_experience") or {}
            exps.append(Experience(
                title=_dig(e, em.get("title", "title"), "") or "",
                company=_dig(e, em.get("company", "company"), "") or "",
                location=_dig(e, em.get("location", "location"), "") or "",
                start_date=str(_dig(e, em.get("start_date", "starts_at"), "") or ""),
                end_date=str(_dig(e, em.get("end_date", "ends_at"), "") or ""),
                is_current=bool(_dig(e, em.get("is_current", "is_current"), False)),
                description=_dig(e, em.get("description", "description"), "") or ""))

        edus: list[Education] = []
        for d in (_dig(item, m.get("educations", "education"), []) or []):
            dm = self.conf.get("map_education") or {}
            edus.append(Education(
                degree=_dig(d, dm.get("degree", "degree_name"), "") or "",
                field_of_study=_dig(d, dm.get("field", "field_of_study"), "") or "",
                institution=_dig(d, dm.get("institution", "school"), "") or ""))

        skills = _dig(item, m.get("skills", "skills"), []) or []
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(",") if s.strip()]

        return Candidate(
            full_name=name,
            headline=_dig(item, m.get("headline", "headline"), "") or "",
            summary=_dig(item, m.get("summary", "summary"), "") or "",
            location_raw=_dig(item, m.get("location", "city"), "") or "",
            linkedin_url=_dig(item, m.get("linkedin_url", "linkedin_url"), "") or "",
            email=_dig(item, m.get("email", "email"), "") or "",
            phone=_dig(item, m.get("phone", "phone"), "") or "",
            skills_raw=[str(s) for s in skills],
            experiences=exps, educations=edus,
            source=self.name,
            source_ref=str(_dig(item, m.get("id", "id"), "") or ""),
            raw=item)

    # ---- fetch ---------------------------------------------------------- #
    def fetch(self, ctx: FetchContext) -> FetchResult:
        res = FetchResult()
        ok, why = self.available()
        if not ok:
            res.errors.append(f"{self.name}: {why}")
            return res

        url = self.conf["search_url"]
        results_path = self.conf.get("results_path", "results")
        per_jd = max(1, ctx.max_results // max(1, len(ctx.jds) or 1))
        sleep = float(cfg().get("run.rate_limit_sleep", 2.0))

        for jd in ctx.jds:
            params = self._query_for(jd)
            try:
                if ctx.dry_run:
                    res.notes.append(f"{jd.code}: would GET {url} {params}")
                    continue
                r = requests.get(url, headers=self._headers(), params=params,
                                 timeout=DEFAULT_TIMEOUT)
                if r.status_code == 429:
                    res.errors.append(f"{jd.code}: rate limited by provider")
                    time.sleep(sleep * 5)
                    continue
                r.raise_for_status()
                payload = r.json()
                items = _dig(payload, results_path, []) or []
                got = 0
                for item in items[:per_jd]:
                    c = self._to_candidate(item if isinstance(item, dict) else {})
                    if c:
                        res.candidates.append(c)
                        got += 1
                res.notes.append(f"{jd.code}: {got} profiles from provider")
                time.sleep(sleep)
            except requests.RequestException as e:
                res.errors.append(f"{jd.code}: {type(e).__name__}: {e}")
        return res


class ProxycurlSource(ProviderSource):
    """Concrete defaults for Proxycurl, the most common LinkedIn-shaped vendor."""
    name = "proxycurl"

    DEFAULTS = {
        "search_url": "https://nubela.co/proxycurl/api/search/person",
        "api_key_env": "PROXYCURL_API_KEY",
        "auth_scheme": "Bearer",
        "results_path": "results",
        "page_size": 10,
        "country_code": "pk",
        "query_template": {
            "country": "{country_code}",
            "city": "{city}",
            "current_role_title": "{title}",
            "page_size": "{page_size}",
            "enrich_profiles": "enrich",
        },
        "map": {
            "full_name": "profile.full_name",
            "headline": "profile.headline",
            "summary": "profile.summary",
            "location": "profile.city",
            "linkedin_url": "linkedin_profile_url",
            "skills": "profile.skills",
            "experiences": "profile.experiences",
            "educations": "profile.education",
            "id": "linkedin_profile_url",
        },
        "map_experience": {
            "title": "title", "company": "company", "location": "location",
            "start_date": "starts_at.year", "end_date": "ends_at.year",
            "description": "description",
        },
        "map_education": {
            "degree": "degree_name", "field": "field_of_study", "institution": "school",
        },
    }

    def __init__(self):
        super().__init__("sources.proxycurl")
        merged = dict(self.DEFAULTS)
        merged.update(self.conf or {})
        self.conf = merged
