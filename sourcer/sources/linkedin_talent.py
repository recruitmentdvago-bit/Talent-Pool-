"""LinkedIn Talent Solutions - the official, sanctioned route.

This is the only way to get LinkedIn data out of LinkedIn with LinkedIn's
blessing. It needs a Talent Solutions / Recruiter System Connect contract and
an approved developer app; the endpoints are not open to self-serve accounts.

If you have that contract, set LI_CLIENT_ID / LI_CLIENT_SECRET and the adapter
handles OAuth2 and paging. If you do not, `available()` says so and the runner
skips it quietly - nothing breaks.
"""
from __future__ import annotations

import os
import time

import requests

from ..config import cfg
from ..models import Candidate, Education, Experience
from .base import BaseSource, FetchContext, FetchResult

TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API_BASE = "https://api.linkedin.com/v2"
TIMEOUT = 30


class LinkedInTalentSource(BaseSource):
    name = "linkedin_talent"

    def __init__(self):
        self.conf = cfg().get("sources.linkedin_talent", {}) or {}
        self._token: str | None = None
        self._token_expires = 0.0

    def available(self) -> tuple[bool, str]:
        cid = os.environ.get(self.conf.get("client_id_env", "LI_CLIENT_ID"), "")
        sec = os.environ.get(self.conf.get("client_secret_env", "LI_CLIENT_SECRET"), "")
        if not (cid and sec):
            return False, ("no Talent Solutions credentials - this route needs an approved "
                           "LinkedIn partner app, not a normal account")
        if not self.conf.get("search_path"):
            return False, ("sources.linkedin_talent.search_path not set - fill in the endpoint "
                           "your contract grants you")
        return True, "credentials present"

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        cid = os.environ[self.conf.get("client_id_env", "LI_CLIENT_ID")]
        sec = os.environ[self.conf.get("client_secret_env", "LI_CLIENT_SECRET")]
        r = requests.post(TOKEN_URL, timeout=TIMEOUT, data={
            "grant_type": "client_credentials",
            "client_id": cid, "client_secret": sec,
            "scope": self.conf.get("scope", "r_talent_profile"),
        })
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + float(body.get("expires_in", 3600))
        return self._token

    def _to_candidate(self, item: dict) -> Candidate | None:
        name = " ".join(filter(None, [
            (item.get("firstName") or {}).get("localized", {}).get("en_US", ""),
            (item.get("lastName") or {}).get("localized", {}).get("en_US", ""),
        ])).strip() or item.get("fullName", "")
        if not name:
            return None
        exps = [Experience(
            title=p.get("title", ""), company=(p.get("companyName") or ""),
            location=p.get("locationName", ""),
            start_date=str((p.get("timePeriod") or {}).get("startDate", {}).get("year", "")),
            end_date=str((p.get("timePeriod") or {}).get("endDate", {}).get("year", "")),
            is_current=not (p.get("timePeriod") or {}).get("endDate"),
            description=p.get("description", ""))
            for p in (item.get("positions") or {}).get("elements", [])]
        edus = [Education(degree=e.get("degreeName", ""), field_of_study=e.get("fieldOfStudy", ""),
                          institution=e.get("schoolName", ""))
                for e in (item.get("educations") or {}).get("elements", [])]
        return Candidate(
            full_name=name, headline=item.get("headline", ""), summary=item.get("summary", ""),
            location_raw=(item.get("location") or {}).get("name", ""),
            linkedin_url=item.get("publicProfileUrl", ""),
            skills_raw=[s.get("name", "") for s in (item.get("skills") or {}).get("elements", [])],
            experiences=exps, educations=edus,
            source=self.name, source_ref=item.get("id", ""), raw=item)

    def fetch(self, ctx: FetchContext) -> FetchResult:
        res = FetchResult()
        ok, why = self.available()
        if not ok:
            res.notes.append(f"{self.name} skipped: {why}")
            return res
        try:
            token = self._access_token()
        except requests.RequestException as e:
            res.errors.append(f"{self.name}: token request failed: {e}")
            return res

        headers = {"Authorization": f"Bearer {token}",
                   "X-Restli-Protocol-Version": "2.0.0",
                   "LinkedIn-Version": self.conf.get("api_version", "202405")}
        url = API_BASE + self.conf["search_path"]
        per_jd = max(1, ctx.max_results // max(1, len(ctx.jds) or 1))
        for jd in ctx.jds:
            params = {
                "q": "criteria",
                "keywords": jd.title,
                "locations": f"{cfg().get('location.target_city')}, "
                             f"{cfg().get('location.target_country')}",
                "count": min(per_jd, 50),
            }
            if ctx.dry_run:
                res.notes.append(f"{jd.code}: would GET {url} {params}")
                continue
            try:
                r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
                r.raise_for_status()
                for el in r.json().get("elements", [])[:per_jd]:
                    c = self._to_candidate(el)
                    if c:
                        res.candidates.append(c)
                res.notes.append(f"{jd.code}: {len(res.candidates)} from Talent Solutions")
                time.sleep(float(cfg().get("run.rate_limit_sleep", 2.0)))
            except requests.RequestException as e:
                res.errors.append(f"{jd.code}: {type(e).__name__}: {e}")
        return res
