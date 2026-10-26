"""Understand a manager's request in plain words.

    "data analyst manager ke liye resumes nikaldo jiska score 70 ya 75% se upar ho"
        -> role "data analyst manager", minimum score 70

English, Urdu-in-Roman-letters, or a mix. Numbers are read as percentages; when
two are given ("70 ya 75") the lower one is used so nobody is silently left out.
"""
from __future__ import annotations

import re

FILLER = set("""
give me show find get need want please pls send list pull fetch
resume resumes cv cvs profile profiles candidate candidates people person banda bande bandy
for of the a an and with to from in on at by is are be
score scores scoring percent percentage pct match matching matches rating
above over more than least minimum min atleast plus upar upr ooper zyada
nikal nikalo nikaldo nikaldein nikalna dikha dikhao dikhado dedo dena do de dijiye
chahiye chahiyen chahye mujhe humein hamein hume ko
ke ki ka liye lye wale wali walay jis jiski jiska jinki jinka jin ho hai hain hon
bhi sirf only all sab
se ya
""".split())

# "data analysts" should find the "Data Analyst" vacancy
PLURAL_ROLES = {"analysts", "engineers", "developers", "managers", "scientists", "officers",
                "executives", "specialists", "consultants", "architects", "leads", "accountants",
                "recruiters", "designers", "administrators", "coordinators", "associates",
                "interns", "directors", "supervisors"}

SEPARATORS = re.compile(r"\s*(?:/|,|;|\||\bor\b|\bya\b|\baur\b|\band\b|\b&\b)\s*", re.I)


def parse_ask(text: str) -> dict:
    raw = (text or "").strip()
    # vacancy codes like DA-001 contain digits that are not scores
    codes = re.findall(r"\b[A-Za-z]{1,6}[-_]\d{1,4}\b", raw)
    work = raw
    for c in codes:
        work = work.replace(c, " ")
    nums = [int(n) for n in re.findall(r"(?<![\w.])(\d{1,3})(?:\s*%|\b)", work) if 1 <= int(n) <= 100]
    min_score = float(min(nums)) if nums else None

    cleaned = re.sub(r"\d+\s*%?", " ", work)
    cleaned = cleaned.replace("%", " ")
    parts = []
    for chunk in SEPARATORS.split(cleaned):
        words = [w[:-1] if w.lower() in PLURAL_ROLES else w
                 for w in re.findall(r"[A-Za-z][A-Za-z\-+&.]*", chunk) if w.lower() not in FILLER]
        if words:
            parts.append(" ".join(words))
    role = "/".join(parts + codes) or None
    return {"role": role, "min_score": min_score, "numbers": nums, "codes": codes}
