from datetime import date

from sourcer.normalize import (geo_index, merge_months, parse_date, seniority_of,
                               skills_index, titles_index)


def test_skill_aliases_map_to_one_canonical_name():
    si = skills_index()
    for raw in ["Power BI", "powerbi", "PowerBI", "power-bi", "DAX"]:
        assert si.canonicalize(raw) == "power_bi", raw


def test_extract_finds_skills_in_prose():
    found = skills_index().extract(
        "Built ETL pipelines in PySpark and Airflow, dashboards in Power BI, SQL on Snowflake")
    assert {"etl", "spark", "airflow", "power_bi", "sql", "snowflake"} <= found


def test_short_aliases_are_ignored_in_prose_but_read_in_skill_lists():
    si = skills_index()
    # "r" as a word inside a sentence must not become the R language
    assert "r" not in si.extract("we r going to review the data", context="prose")
    assert "r" in si.extract("R, Python, SQL", context="skills")


def test_implied_skills_expand_transitively():
    assert {"deep_learning", "machine_learning", "statistics", "python"} <= \
        skills_index().expand(["pytorch"])


def test_unknown_skill_gets_a_stable_placeholder():
    si = skills_index()
    assert si.canonicalize("Blorptron 9000") is None
    assert si.unknown_label("Blorptron 9000") == si.unknown_label("blorptron 9000")


def test_title_families_and_adjacency():
    ti = titles_index()
    assert ti.family_of("Senior Data Engineer") == "data_engineer"
    assert ti.family_of("Assistant Manager MIS Analyst") == "data_analyst"
    assert ti.relatedness("data_engineer", "ETL Developer") == 1.0
    assert 0 < ti.relatedness("data_engineer", "Data Analyst") < 1.0
    assert ti.relatedness("data_engineer", "Pastry Chef") < 0.3


def test_leadership_titles_resolve_to_their_family():
    ti = titles_index()
    assert ti.family_of("Head of Business Intelligence") == "data_analyst"
    assert ti.family_of("Data Architect") == "data_engineer"


def test_seniority_detection():
    assert seniority_of("Sr. Data Analyst") == "senior"
    assert seniority_of("Head of Analytics") == "executive"
    assert seniority_of("Data Analyst Intern") == "intern"
    assert seniority_of("Data Analyst") == "mid"


def test_parse_date_shapes():
    assert parse_date("Jan 2020") == date(2020, 1, 1)
    assert parse_date("2019") == date(2019, 1, 1)
    assert parse_date("March 2021") == date(2021, 3, 1)
    assert parse_date("Present") is None
    assert parse_date("Present", default_to_today=True) is not None


def test_overlapping_jobs_are_not_double_counted():
    """Two concurrent roles must not inflate total experience."""
    spans = [(date(2020, 1, 1), date(2022, 1, 1)), (date(2021, 6, 1), date(2023, 1, 1))]
    assert merge_months(spans) == 36.0          # Jan 2020 -> Jan 2023, not 48
    assert merge_months([]) == 0.0


def test_location_tiers():
    g = geo_index()
    assert g.classify("Clifton, Karachi, Pakistan")[2] == "metro"
    assert g.classify("Karachi")[2] == "metro"
    assert g.classify("Lahore, Pakistan")[2] == "country"
    assert g.classify("Dubai, United Arab Emirates")[2] == "other"
    assert g.classify("Remote")[2] == "remote"
    assert g.classify("")[2] == "unknown"


def test_location_splits_city_and_country():
    city, country, _ = geo_index().classify("Clifton, Karachi, Pakistan")
    assert city == "Clifton" and country == "Pakistan"
