"""The match engine.

A candidate's score is a weighted rubric over eight dimensions, blended with a
calibrated semantic similarity, then capped if a hard requirement is missing.
Every number is explained in `breakdown` so a hiring decision is defensible -
you can always show a candidate (or a hiring manager) why the bot ranked them
where it did.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .config import DATA_DIR, cfg
from .models import EDU_LADDER, JD, Candidate
from .normalize import (geo_index, norm_text, seniority_distance, skills_index,
                        titles_index)
from .semantic import TfidfSpace

LOCATION_CREDIT = {"metro": 1.0, "remote": 0.8, "country": 0.5, "other": 0.15, "unknown": 0.4}
SUBSTITUTE_CREDIT = 0.5   # same skill-group stand-in for a missing must-have
BEST_PAST_DISCOUNT = 0.85  # a relevant past title counts a little less than a current one


@functools.lru_cache(maxsize=1)
def industry_index() -> dict[str, dict]:
    return yaml.safe_load((DATA_DIR / "industries.yaml").read_text()) or {}


@functools.lru_cache(maxsize=1)
def _industry_patterns() -> list[tuple[str, re.Pattern]]:
    """Word-boundary matchers. A bare substring test makes short names like
    'ke' (K-Electric) match inside ordinary words such as 'worked'."""
    pats = []
    for ind, meta in industry_index().items():
        meta = meta or {}
        needles = [norm_text(x) for x in
                   list(meta.get("companies") or []) + list(meta.get("keywords") or [])]
        alts = sorted({n for n in needles if n}, key=len, reverse=True)
        if alts:
            rx = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(a) for a in alts)
                            + r")(?![a-z0-9])")
            pats.append((ind, rx))
    return pats


def infer_industries(text: str) -> set[str]:
    n = norm_text(text)
    if not n:
        return set()
    return {ind for ind, rx in _industry_patterns() if rx.search(n)}


@dataclass
class Dim:
    name: str
    raw: float            # 0..1
    weight: float
    note: str = ""

    @property
    def points(self) -> float:
        return self.raw * self.weight


@dataclass
class ScoreResult:
    score: float
    rubric: float
    semantic: float
    dims: list[Dim] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    substitutes: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    rationale: str = ""

    def breakdown(self) -> dict[str, Any]:
        return {
            d.name: {"raw": round(d.raw, 3), "weight": round(d.weight, 2),
                     "points": round(d.points, 2), "note": d.note}
            for d in self.dims
        }

    def explain(self) -> str:
        lines = [f"Score {self.score:.1f}/100   (rubric {self.rubric:.1f}, semantic {self.semantic:.2f})"]
        for d in sorted(self.dims, key=lambda x: -x.weight):
            bar = "#" * int(round(d.raw * 20))
            lines.append(f"  {d.name:<20} {d.raw*100:5.1f}%  {bar:<20} {d.points:5.1f}/{d.weight:.0f} pts"
                         + (f"   {d.note}" if d.note else ""))
        if self.matched:
            lines.append("  matched : " + ", ".join(self.matched))
        if self.missing:
            lines.append("  missing : " + ", ".join(self.missing))
        if self.substitutes:
            lines.append("  stand-ins: " + ", ".join(f"{k}<-{v}" for k, v in self.substitutes.items()))
        if self.flags:
            lines.append("  flags   : " + ", ".join(self.flags))
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Dimension calculations
# --------------------------------------------------------------------------- #
def _skill_dim(cand_skills: set[str], required: set[str], weight: float,
               name: str) -> tuple[Dim, list[str], list[str], dict[str, str]]:
    """Fraction of required skills evidenced, with partial credit for a
    same-group substitute (e.g. Tableau where the JD asked for Power BI)."""
    si = skills_index()
    if not required:
        return Dim(name, 0.6, weight, "none specified - neutral"), [], [], {}
    matched, missing, subs = [], [], {}
    credit = 0.0
    cand_groups: dict[str, list[str]] = {}
    for s in cand_skills:
        cand_groups.setdefault(si.group_of(s), []).append(s)
    for req in sorted(required):
        if req in cand_skills:
            credit += 1.0
            matched.append(req)
            continue
        group = si.group_of(req)
        peers = [p for p in cand_groups.get(group, []) if p != req]
        if group != "other" and peers:
            credit += SUBSTITUTE_CREDIT
            subs[req] = peers[0]
            missing.append(req)
        else:
            missing.append(req)
    raw = credit / len(required)
    note = f"{len(matched)}/{len(required)} exact" + (f", {len(subs)} substituted" if subs else "")
    return Dim(name, min(1.0, raw), weight, note), matched, missing, subs


def _experience_dim(cand: Candidate, jd: JD, weight: float,
                    flags: list[str]) -> tuple[Dim, float, bool]:
    """-> (dimension, relevant_years, below_hard_floor)"""
    rel = cand.relevant_years(jd.title_family, jd.must_skills())
    total = cand.total_years()
    floor = float(cfg().get("scoring.experience_floor_ratio", 0.5))
    if jd.min_years <= 0:
        raw = 1.0 if rel > 0 else 0.5
        return (Dim("experience_years", raw, weight,
                    f"{rel:g} relevant / {total:g} total yrs"), rel, False)

    ratio = rel / jd.min_years
    if ratio >= 1.0:
        raw = 1.0
        if jd.max_years and rel > jd.max_years * 1.5:
            raw = 0.8
            flags.append(f"over-experienced ({rel:g}y vs band {jd.min_years:g}-{jd.max_years:g}y)")
    elif ratio <= floor:
        raw = 0.0
        flags.append(f"under-experienced ({rel:g}y vs {jd.min_years:g}y required)")
    else:
        raw = ((ratio - floor) / (1.0 - floor)) * 0.85
        flags.append(f"slightly under ({rel:g}y vs {jd.min_years:g}y required)")
    note = f"{rel:g} relevant / {total:g} total yrs vs {jd.min_years:g}+ required"
    below = jd.hard_min_years and rel < jd.min_years
    return Dim("experience_years", raw, weight, note), rel, below


def _title_dim(cand: Candidate, jd: JD, weight: float) -> Dim:
    ti = titles_index()
    cur = ti.relatedness(jd.title_family, cand.current_title())
    best_past = max((ti.relatedness(jd.title_family, e.title) for e in cand.experiences),
                    default=0.0)
    raw = max(cur, best_past * BEST_PAST_DISCOUNT)
    note = f"current '{cand.current_title() or '?'}' vs family '{jd.title_family or '?'}'"
    return Dim("title_relevance", min(1.0, raw), weight, note)


def _seniority_dim(cand: Candidate, jd: JD, weight: float, flags: list[str]) -> Dim:
    cs, js = cand.seniority(), (jd.seniority or "mid")
    dist = seniority_distance(cs, js)
    raw = max(0.0, 1.0 - 0.25 * dist)
    if dist >= 3:
        flags.append(f"seniority gap ({cs} vs {js})")
    return Dim("seniority_fit", raw, weight, f"{cs} vs {js} required")


def _employer_match(cand: Candidate, preferred: list[str]) -> bool:
    """True if the candidate worked at any of the preferred employers."""
    if not preferred:
        return False
    text = norm_text(" ".join(
        [cand.current_company()] + [e.company for e in cand.experiences]))
    for emp in preferred:
        if norm_text(emp) in text:
            return True
    return False


def _industry_dim(cand: Candidate, jd: JD, weight: float) -> Dim:
    preferred = getattr(jd, "preferred_employers", None) or []
    emp_hit = _employer_match(cand, preferred)

    if not jd.industries and not preferred:
        return Dim("domain_industry", 0.6, weight, "none specified - neutral")
    want = {norm_text(i).replace(" ", "_") for i in jd.industries}
    have = infer_industries(" ".join(
        [cand.current_company(), cand.headline, cand.summary]
        + [f"{e.company} {e.description}" for e in cand.experiences]))
    hit = want & have

    if emp_hit:
        raw = 1.0
        note = "preferred employer match"
        if hit:
            note += f" + industry {', '.join(sorted(hit))}"
    elif hit:
        raw = 1.0
        note = f"matched {', '.join(sorted(hit))}"
    elif preferred and not emp_hit:
        raw = 0.25
        note = f"not from preferred employers ({', '.join(preferred[:3])})"
    else:
        raw = 0.35 if have else 0.3
        note = f"has {', '.join(sorted(have)) or 'unknown'}, wanted {', '.join(sorted(want))}"
    return Dim("domain_industry", raw, weight, note)


def _certifications_dim(cand: Candidate, jd: JD, weight: float) -> Dim:
    certs = getattr(cand, "certifications", None) or []
    if not certs:
        return Dim("certifications", 0.4, weight, "none listed")
    all_jd = jd.must_skills() | jd.nice_skills()
    cert_blob = norm_text(" ".join(certs))
    hits = {s for s in all_jd if norm_text(s.replace("_", " ")) in cert_blob}
    ind_hits = set()
    for ind in jd.industries:
        if norm_text(ind) in cert_blob:
            ind_hits.add(ind)
    n_relevant = len(hits) + len(ind_hits)
    if n_relevant == 0:
        raw = 0.5
        note = f"{len(certs)} certs, none matching JD skills"
    else:
        raw = min(1.0, 0.6 + 0.15 * n_relevant)
        note = f"{len(certs)} certs, {n_relevant} relevant ({', '.join(sorted(hits | ind_hits))})"
    return Dim("certifications", raw, weight, note)


def _projects_dim(cand: Candidate, jd: JD, weight: float) -> Dim:
    projects = getattr(cand, "projects", None) or []
    if not projects:
        return Dim("projects", 0.4, weight, "none listed")
    si = skills_index()
    proj_text = " ".join(f"{p.get('title','')} {p.get('description','')}" for p in projects)
    proj_skills = si.expand(si.extract(proj_text, context="prose"))
    all_jd = jd.must_skills() | jd.nice_skills()
    overlap = proj_skills & all_jd
    if not overlap:
        raw = 0.5
        note = f"{len(projects)} projects, no skill overlap with JD"
    else:
        raw = min(1.0, 0.6 + 0.1 * len(overlap))
        note = f"{len(projects)} projects, {len(overlap)} matching skills"
    return Dim("projects", raw, weight, note)


def _education_dim(cand: Candidate, jd: JD, weight: float, flags: list[str]) -> Dim:
    req = jd.education_min if jd.education_min in EDU_LADDER else "none"
    got = cand.education_level()
    ri, gi = EDU_LADDER.index(req), EDU_LADDER.index(got)
    if req == "none":
        raw = 1.0 if gi > 0 else 0.7
    elif gi >= ri:
        raw = 1.0
    elif ri - gi == 1:
        raw = 0.6
        flags.append(f"education below bar ({got} vs {req})")
    else:
        raw = 0.2
        flags.append(f"education well below bar ({got} vs {req})")
    return Dim("education", raw, weight, f"{got} vs {req} required")


def _location_dim(cand: Candidate, jd: JD, weight: float, flags: list[str]) -> Dim:
    _, _, tier = geo_index().classify(cand.location_raw)
    raw = LOCATION_CREDIT.get(tier, 0.4)
    if tier in {"country", "other"}:
        flags.append(f"relocation needed ({cand.location_raw or 'unknown'})")
    return Dim("location", raw, weight, f"{cand.location_raw or 'unknown'} -> {tier}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def score_candidate(cand: Candidate, jd: JD, space: TfidfSpace | None = None) -> ScoreResult:
    c = cfg()
    w = c.weights
    flags: list[str] = []
    cand_skills = cand.skills()

    must_dim, matched, missing, subs = _skill_dim(
        cand_skills, jd.must_skills(), w.get("must_have_skills", 28), "must_have_skills")
    nice_dim, nice_matched, _, _ = _skill_dim(
        cand_skills, jd.nice_skills(), w.get("nice_have_skills", 8), "nice_have_skills")
    exp_dim, _rel, below_floor = _experience_dim(cand, jd, w.get("experience_years", 15), flags)

    dims = [
        must_dim,
        nice_dim,
        exp_dim,
        _title_dim(cand, jd, w.get("title_relevance", 15)),
        _seniority_dim(cand, jd, w.get("seniority_fit", 6), flags),
        _industry_dim(cand, jd, w.get("domain_industry", 5)),
        _education_dim(cand, jd, w.get("education", 7), flags),
        _location_dim(cand, jd, w.get("location", 10), flags),
        _certifications_dim(cand, jd, w.get("certifications", 3)),
        _projects_dim(cand, jd, w.get("projects", 3)),
    ]

    total_w = sum(d.weight for d in dims) or 1.0
    rubric = 100.0 * sum(d.points for d in dims) / total_w

    # semantic, calibrated so a genuinely strong profile reaches ~1.0
    semantic = 0.0
    if space is not None:
        ref = float(c.get("scoring.semantic_reference", 0.30)) or 0.30
        raw_sim = space.similarity(jd.jd_text(), cand.profile_text())
        semantic = min(1.0, raw_sim / ref)

    # How text similarity folds into the headline number:
    #
    #   tiebreak (default) - the rubric IS the score. Similarity is stored and
    #       shown, and only orders candidates who score the same. This keeps
    #       "75%" meaning one fixed, auditable thing you can defend to a hiring
    #       manager or a candidate, and avoids two traps the alternatives hit:
    #       a terse-but-qualified profile losing points for being short, and a
    #       bonus that pushes everyone into a 100-point ceiling and flattens
    #       the real differences (location, seniority) the rubric measured.
    #   bonus    - rubric + semantic_bonus_points * semantic, capped at 100.
    #   blend    - weighted mix; sharpest ranking, but penalises thin profiles.
    mode = c.get("scoring.semantic_mode", "tiebreak")
    if space is None or mode == "tiebreak":
        score = rubric
    elif mode == "blend":
        blend = float(c.get("scoring.semantic_blend", 0.20))
        score = (1.0 - blend) * rubric + blend * (semantic * 100.0)
    else:
        bonus = float(c.get("scoring.semantic_bonus_points", 6.0))
        score = min(100.0, rubric + bonus * semantic)

    # hard requirements are non-negotiable
    cap = float(c.get("scoring.hard_requirement_cap", 45))
    hard = jd.hard_skills()
    hard_missing = sorted(hard - cand_skills)
    if hard_missing:
        flags.append("missing hard requirement: " + ", ".join(hard_missing))
        score = min(score, cap)
    if below_floor:
        flags.append(f"below the hard experience floor ({_rel:g}y vs {jd.min_years:g}y)")
        score = min(score, cap)

    res = ScoreResult(
        score=round(max(0.0, min(100.0, score)), 1),
        rubric=round(rubric, 1),
        semantic=round(semantic, 3),
        dims=dims,
        matched=sorted(set(matched) | set(nice_matched)),
        missing=missing,
        substitutes=subs,
        flags=flags,
    )
    res.rationale = _rationale(cand, jd, res)
    return res


def _rationale(cand: Candidate, jd: JD, res: ScoreResult) -> str:
    strongest = max(res.dims, key=lambda d: d.points)
    weakest = min((d for d in res.dims if d.weight >= 5), key=lambda d: d.raw)
    bits = [
        f"{cand.current_title() or 'Unknown role'}"
        + (f" at {cand.current_company()}" if cand.current_company() else ""),
        f"{cand.total_years():g}y total",
    ]
    if res.matched:
        bits.append("has " + ", ".join(res.matched[:5]))
    if res.missing:
        bits.append("gaps: " + ", ".join(res.missing[:4]))
    bits.append(f"strongest {strongest.name}, weakest {weakest.name}")
    return "; ".join(bits)


def score_many(candidates: list[Candidate], jds: list[JD]) -> dict[tuple[int, int], ScoreResult]:
    """Score a batch, fitting one shared TF-IDF space over the whole corpus."""
    corpus = [j.jd_text() for j in jds] + [c.profile_text() for c in candidates]
    space = TfidfSpace(corpus)
    out = {}
    for ci, cand in enumerate(candidates):
        for ji, jd in enumerate(jds):
            out[(ci, ji)] = score_candidate(cand, jd, space)
    return out
