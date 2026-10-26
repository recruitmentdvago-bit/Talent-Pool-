from sourcer import jd as jdmod

FRONTMATTER = """---
code: DA-9
title: Data Analyst
seniority: mid
min_years: 3
max_years: 7
hard_min_years: true
education_min: bachelors
openings: 2
industries: [banking, fintech]
must_have: [SQL, Power BI, Excel]
nice_have: [Python]
hard_requirements: [SQL]
---

# Data Analyst
Own reporting for retail banking.
"""

FREE_TEXT = """Senior Data Engineer

Role Overview
Design and run our batch pipelines.

Requirements
- Bachelors degree in a technical field
- At least 5 years of hands-on experience building data pipelines
- Strong Python and SQL, orchestration with Airflow, Spark

Nice to have
- dbt, Snowflake, Kafka
"""


def test_frontmatter_wins_over_inference():
    jd = jdmod.parse_text(FRONTMATTER, source_file="x.md")
    assert jd.code == "DA-9"
    assert jd.title == "Data Analyst"
    assert (jd.min_years, jd.max_years) == (3.0, 7.0)
    assert jd.hard_min_years is True
    assert jd.openings == 2
    assert jd.education_min == "bachelors"
    assert jd.industries == ["banking", "fintech"]
    assert jd.hard_requirements == ["SQL"]
    assert jd.title_family == "data_analyst"


def test_free_text_is_parsed_without_frontmatter():
    jd = jdmod.parse_text(FREE_TEXT, source_file="de.md")
    assert "Data Engineer" in jd.title
    assert jd.min_years == 5.0
    assert jd.seniority == "senior"
    assert jd.title_family == "data_engineer"
    assert jd.education_min == "bachelors"
    must = jd.must_skills()
    assert {"python", "sql", "airflow", "spark"} <= must
    assert "dbt" in jd.nice_skills() or "dbt" in {n.lower() for n in jd.nice_have}


def test_years_patterns():
    assert jdmod.parse_text("Analyst\n\n3-7 years of experience").min_years == 3.0
    assert jdmod.parse_text("Analyst\n\nminimum of 4 years").min_years == 4.0
    assert jdmod.parse_text("Analyst\n\nat least 6 years").min_years == 6.0
    assert jdmod.parse_text("Analyst\n\n8+ years").min_years == 8.0
    assert jdmod.parse_text("Analyst\n\nno numbers here").min_years == 0.0


def test_reversed_year_range_is_corrected():
    jd = jdmod.parse_text("Analyst\n\n7 to 3 years of experience")
    assert (jd.min_years, jd.max_years) == (3.0, 7.0)


def test_upsert_is_idempotent_then_versions_on_change(conn):
    jd = jdmod.parse_text(FRONTMATTER, source_file="x.md")
    jd_id, changed = jdmod.upsert(conn, jd)
    assert changed is True and jd.version == 1

    again = jdmod.parse_text(FRONTMATTER, source_file="x.md")
    same_id, changed2 = jdmod.upsert(conn, again)
    assert same_id == jd_id and changed2 is False and again.version == 1

    edited = jdmod.parse_text(FRONTMATTER.replace("min_years: 3", "min_years: 6"),
                              source_file="x.md")
    same_id2, changed3 = jdmod.upsert(conn, edited)
    assert same_id2 == jd_id and changed3 is True and edited.version == 2


def test_sync_dir_reads_every_jd_file(conn, sandbox):
    (sandbox / "jds" / "a.md").write_text(FRONTMATTER)
    (sandbox / "jds" / "b.txt").write_text(FREE_TEXT)
    (sandbox / "jds" / "ignore.json").write_text("{}")
    results = jdmod.sync_dir(conn)
    assert len(results) == 2
    assert {j.code for _, j, _ in results} == {"DA-9", "senior-data-engineer"}


def test_load_all_round_trips_every_field(conn):
    jdmod.upsert(conn, jdmod.parse_text(FRONTMATTER, source_file="x.md"))
    (_, loaded), = jdmod.load_all(conn)
    assert loaded.code == "DA-9"
    assert loaded.hard_min_years is True
    assert loaded.must_have == ["SQL", "Power BI", "Excel"]
    assert loaded.industries == ["banking", "fintech"]
