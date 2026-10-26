import pandas as pd

from sourcer.sources import REGISTRY, build_enabled, get
from sourcer.sources.base import FetchContext
from sourcer.sources.csv_source import (CsvSource, parse_educations,
                                        parse_experiences, rows_to_candidates)


def test_registry_builds_every_adapter():
    for name in REGISTRY:
        s = get(name)
        ok, why = s.available()
        assert isinstance(ok, bool) and isinstance(why, str) and why


def test_unknown_source_raises():
    try:
        get("nope")
        assert False, "expected KeyError"
    except KeyError as e:
        assert "nope" in str(e)


def test_build_enabled_skips_broken_names():
    assert [s.name for s in build_enabled(["csv", "does_not_exist"])] == ["csv"]


def test_compact_experience_format():
    cell = ("Data Engineer | Systems Limited | Jan 2021 | Present | Built Airflow DAGs"
            " ;; MIS Officer | HBL | Feb 2018 | Dec 2020 | Excel reporting")
    exps = parse_experiences(cell)
    assert len(exps) == 2
    assert exps[0].title == "Data Engineer" and exps[0].company == "Systems Limited"
    assert exps[0].is_current is True
    assert exps[1].end_date == "Dec 2020" and exps[1].is_current is False


def test_json_experience_format():
    exps = parse_experiences('[{"title":"Analyst","company":"Bank","start_date":"2020",'
                             '"end_date":"","description":"SQL"}]')
    assert exps[0].title == "Analyst" and exps[0].is_current is True


def test_malformed_experience_cell_does_not_crash():
    assert parse_experiences("[not json") == []
    assert parse_experiences("") == []
    assert parse_experiences("Just A Title")[0].title == "Just A Title"


def test_education_format():
    e = parse_educations("BS | Computer Science | NED University | 2018")[0]
    assert (e.degree, e.field_of_study, e.institution, e.end_year) == \
        ("BS", "Computer Science", "NED University", 2018)


def test_column_aliases_are_matched_loosely():
    df = pd.DataFrame([{"Name": "Ali Khan", "Designation": "Data Analyst",
                        "City": "Karachi, Pakistan", "Key Skills": "SQL; Excel",
                        "Profile URL": "https://linkedin.com/in/ali-k"}])
    c, = rows_to_candidates(df, "csv")
    assert c.full_name == "Ali Khan"
    assert c.headline == "Data Analyst"
    assert c.location_raw == "Karachi, Pakistan"
    assert c.linkedin_url.endswith("ali-k")
    assert {"sql", "excel"} <= c.skills()


def test_rows_without_a_name_are_skipped():
    df = pd.DataFrame([{"name": ""}, {"name": "Real Person"}])
    assert [c.full_name for c in rows_to_candidates(df, "csv")] == ["Real Person"]


def test_csv_source_reads_and_archives(sandbox):
    src = CsvSource()
    imports = sandbox / "storage/imports"
    pd.DataFrame([{"name": "Sana Khan", "designation": "Data Analyst",
                   "location": "Karachi, Pakistan", "skills": "SQL; Power BI",
                   "experiences": "Data Analyst | HBL | Jan 2020 | Present | SQL work"}]
                 ).to_csv(imports / "batch1.csv", index=False)
    ok, why = src.available()
    assert ok and "1 file" in why

    res = src.fetch(FetchContext(max_results=10))
    assert len(res.candidates) == 1
    assert res.candidates[0].full_name == "Sana Khan"
    assert not (imports / "batch1.csv").exists()
    assert (imports / "processed" / "batch1.csv").exists()
    assert src.available()[0] is False


def test_csv_dry_run_leaves_the_file_in_place(sandbox):
    imports = sandbox / "storage/imports"
    pd.DataFrame([{"name": "X Y"}]).to_csv(imports / "b.csv", index=False)
    CsvSource().fetch(FetchContext(max_results=10, dry_run=True))
    assert (imports / "b.csv").exists()


def test_corrupt_csv_is_reported_not_raised(sandbox):
    (sandbox / "storage/imports" / "bad.csv").write_bytes(b"\x00\x01\x02 not,a,csv\n\"")
    res = CsvSource().fetch(FetchContext(max_results=10))
    assert res.errors or res.candidates == []
