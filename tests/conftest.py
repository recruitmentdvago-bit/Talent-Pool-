"""Test fixtures. Every test runs against a throwaway storage tree so the real
database and PDF library are never touched.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A fully isolated project: own settings, own db, own folders."""
    for sub in ("storage/profiles", "storage/pdf_inbox", "storage/exports",
                "storage/imports", "jds"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    settings = {
        "jd_dir": str(tmp_path / "jds"),
        "storage": {
            "db_path": str(tmp_path / "storage/test.db"),
            "profiles_dir": str(tmp_path / "storage/profiles"),
            "pdf_inbox": str(tmp_path / "storage/pdf_inbox"),
            "exports_dir": str(tmp_path / "storage/exports"),
        },
        "sources": {"enabled": ["pdf_inbox", "csv"],
                    "csv": {"watch_dir": str(tmp_path / "storage/imports")}},
        "scoring": {"default_shortlist_threshold": 75},
    }
    cfg_path = tmp_path / "settings.yaml"
    cfg_path.write_text(yaml.safe_dump(settings))
    monkeypatch.setenv("SOURCER_CONFIG", str(cfg_path))

    from sourcer import config
    config.cfg(reload=True)
    yield tmp_path
    monkeypatch.delenv("SOURCER_CONFIG", raising=False)
    config.cfg(reload=True)


@pytest.fixture
def conn(sandbox):
    from sourcer import db
    c = db.init()
    yield c
    try:                       # a test may have closed it to hand over to runner.run()
        c.commit()
        c.close()
    except Exception:
        pass


@pytest.fixture
def jd_analyst():
    from sourcer.models import JD
    return JD(code="DA-1", title="Data Analyst", title_family="data_analyst",
              seniority="mid", location="Karachi, Pakistan", min_years=3, max_years=7,
              education_min="bachelors", industries=["banking"],
              must_have=["SQL", "Power BI", "Excel", "Data Analysis", "Statistics"],
              nice_have=["Python", "Tableau"], hard_requirements=["SQL"])


@pytest.fixture
def strong_analyst():
    from sourcer.models import Candidate, Education, Experience
    return Candidate(
        full_name="Ayesha Khan", headline="Data Analyst | SQL, Power BI",
        location_raw="Clifton, Karachi, Pakistan",
        linkedin_url="https://www.linkedin.com/in/ayesha-khan-da",
        summary="Five years of banking analytics.",
        skills_raw=["SQL", "Power BI", "Advanced Excel", "Statistics", "Data Analysis",
                    "Python"],
        experiences=[
            Experience(title="Data Analyst", company="Habib Bank Limited",
                       start_date="Mar 2021", is_current=True,
                       description="Retail MIS, Power BI dashboards, complex SQL, "
                                   "cohort and funnel analysis."),
            Experience(title="MIS Officer", company="Faysal Bank", start_date="Jan 2019",
                       end_date="Feb 2021", description="Excel and SQL reporting.")],
        educations=[Education(degree="BS", field_of_study="Computer Science",
                              institution="NED University", end_year=2018)])


@pytest.fixture
def weak_candidate():
    from sourcer.models import Candidate, Education, Experience
    return Candidate(
        full_name="Bilal Ahmed", headline="Graphic Designer",
        location_raw="Lahore, Pakistan", skills_raw=["Photoshop", "Illustrator"],
        experiences=[Experience(title="Graphic Designer", company="Studio",
                                start_date="Jan 2022", is_current=True)],
        educations=[Education(degree="Intermediate", field_of_study="Arts")])
