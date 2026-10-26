import pandas as pd
import pytest

from sourcer import jd as jdmod
from sourcer import report, runner, store

DA = """---
code: DA-1
title: Data Analyst
min_years: 2
must_have: [SQL, Power BI, Excel]
hard_requirements: [SQL]
---
Own reporting for retail banking.
"""
DE = """---
code: DE-1
title: Senior Data Engineer
min_years: 4
must_have: [SQL, Python, ETL, Airflow]
hard_requirements: [SQL, Python]
---
Build the pipelines.
"""


@pytest.fixture
def seeded(conn, sandbox, strong_analyst, weak_candidate):
    (sandbox / "jds" / "da.md").write_text(DA)
    (sandbox / "jds" / "de.md").write_text(DE)
    jdmod.sync_dir(conn)
    store.upsert(conn, strong_analyst)
    store.upsert(conn, weak_candidate)
    runner.score_all(conn)
    return conn


def test_every_candidate_is_scored_against_every_vacancy(seeded):
    assert seeded.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 4


def test_resolve_jds_by_code_title_and_family(seeded):
    assert [r["code"] for r in report.resolve_jds(seeded, "DA-1")] == ["DA-1"]
    assert [r["code"] for r in report.resolve_jds(seeded, "data analyst")] == ["DA-1"]
    both = {r["code"] for r in report.resolve_jds(seeded, "data analyst/data engineer")}
    assert both == {"DA-1", "DE-1"}
    assert len(report.resolve_jds(seeded, None)) == 2
    assert report.resolve_jds(seeded, "astronaut") == []


def test_role_strings_people_actually_type_all_resolve(seeded):
    for role in ["data analyst", "Data Analyst", "data-analyst", "data_analyst",
                 "DA-1", "da-1"]:
        assert [r["code"] for r in report.resolve_jds(seeded, role)] == ["DA-1"], role


def test_threshold_filters_the_shortlist(seeded):
    high = report.query(seeded, "data analyst", min_score=75)
    assert [r.name for r in high] == ["Ayesha Khan"]
    assert report.query(seeded, "data analyst", min_score=99.9) == []
    allrows = report.query(seeded, "data analyst", min_score=0)
    assert len(allrows) == 2
    assert allrows[0].score > allrows[1].score


def test_query_returns_each_candidate_once_at_their_best_vacancy(seeded):
    rows = report.query(seeded, "data analyst/data engineer", min_score=0)
    assert len(rows) == 2
    assert len({r.candidate_id for r in rows}) == 2


def test_rows_carry_everything_needed_to_act(seeded):
    row, = report.query(seeded, "data analyst", min_score=75)
    assert row.candidate_id and row.name and row.jd_code == "DA-1"
    assert row.matched and row.rationale
    assert row.breakdown and "must_have_skills" in row.breakdown
    assert row.status == "new"


def test_status_and_city_filters(seeded, strong_analyst):
    cid = seeded.execute("SELECT id FROM candidates WHERE full_name=?",
                         (strong_analyst.full_name,)).fetchone()[0]
    store.set_status(seeded, cid, "contacted")
    assert report.query(seeded, None, min_score=0, status="contacted")
    assert not any(r.candidate_id == cid for r in
                   report.query(seeded, None, min_score=0, include_contacted=False))
    assert report.query(seeded, None, min_score=0, city="clifton")
    assert report.query(seeded, None, min_score=0, city="nowhere") == []


def test_limit_is_respected(seeded):
    assert len(report.query(seeded, None, min_score=0, limit=1)) == 1


def test_exports_produce_readable_files(seeded, sandbox):
    rows = report.query(seeded, None, min_score=0)

    csv_path = report.export_csv(rows, sandbox / "out.csv")
    assert pd.read_csv(csv_path).shape[0] == len(rows)

    xlsx_path = report.export_xlsx(rows, sandbox / "out.xlsx")
    sheets = pd.read_excel(xlsx_path, sheet_name=None)
    assert set(sheets) == {"shortlist", "score detail"}
    assert "must_have_skills" in sheets["score detail"].columns

    bundle = report.export_pdf_bundle(rows, sandbox / "bundle", label="test")
    assert (bundle / "00_cover_sheet.pdf").exists()


def test_table_renders_without_rows():
    assert "no candidates" in report.as_table([])


def test_full_pipeline_end_to_end(conn, sandbox, strong_analyst):
    """JDs on disk + a CSV drop + a PDF drop -> scored, filed, queryable."""
    from sourcer.pdfstore import render_profile_pdf
    (sandbox / "jds" / "da.md").write_text(DA)
    pd.DataFrame([{"name": "Sana Khan", "designation": "Data Analyst",
                   "location": "Karachi, Pakistan",
                   "skills": "SQL; Power BI; Excel; Data Analysis",
                   "experiences": "Data Analyst | Habib Bank Limited | Jan 2019 | "
                                  "Present | Power BI dashboards and complex SQL"}]
                 ).to_csv(sandbox / "storage/imports" / "in.csv", index=False)
    render_profile_pdf(strong_analyst, sandbox / "storage/pdf_inbox" / "ayesha.pdf")

    summary = runner.run(verbose=False)
    assert summary.errors == []
    assert summary.fetched == 2
    assert summary.created == 2
    assert summary.scored == 2
    assert summary.pdfs_written >= 1
    assert set(summary.sources) == {"pdf_inbox", "csv"}

    from sourcer.db import session
    with session() as c2:
        rows = report.query(c2, "data analyst", min_score=70)
        assert len(rows) == 2
        for r in rows:
            assert r.pdf_path and r.score >= 70


def test_rerunning_updates_rather_than_duplicating(conn, sandbox, strong_analyst):
    from sourcer.pdfstore import render_profile_pdf
    (sandbox / "jds" / "da.md").write_text(DA)
    for i in range(2):
        render_profile_pdf(strong_analyst, sandbox / "storage/pdf_inbox" / f"a{i}.pdf")
        s = runner.run(verbose=False)
        assert s.errors == []
    from sourcer.db import session
    with session() as c2:
        assert c2.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1


def test_editing_a_jd_rescores_at_the_new_version(conn, sandbox, strong_analyst):
    jd_file = sandbox / "jds" / "da.md"
    jd_file.write_text(DA)
    s1 = runner.run(verbose=False)
    assert s1.errors == []

    from sourcer.db import session
    with session() as c2:
        store.upsert(c2, strong_analyst)
    jd_file.write_text(DA.replace("min_years: 2", "min_years: 9"))
    s2 = runner.run(verbose=False)
    assert any("JD changes" in n for n in s2.notes)

    with session() as c3:
        versions = {r[0] for r in c3.execute("SELECT DISTINCT jd_version FROM scores")}
        jd_version = c3.execute("SELECT version FROM jds WHERE code='DA-1'").fetchone()[0]
        assert versions == {jd_version}, "stale scores should not survive a JD edit"


def test_run_with_no_jds_reports_the_problem(conn, sandbox):
    s = runner.run(verbose=False)
    assert any("no active JDs" in e for e in s.errors)


def test_dry_run_writes_nothing(conn, sandbox, strong_analyst):
    from sourcer.pdfstore import render_profile_pdf
    (sandbox / "jds" / "da.md").write_text(DA)
    render_profile_pdf(strong_analyst, sandbox / "storage/pdf_inbox" / "a.pdf")
    s = runner.run(dry_run=True, verbose=False)
    assert s.created == 0
    from sourcer.db import session
    with session() as c2:
        assert c2.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    assert (sandbox / "storage/pdf_inbox" / "a.pdf").exists()
