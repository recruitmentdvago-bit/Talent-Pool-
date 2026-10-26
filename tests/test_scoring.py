import pytest

from sourcer.models import JD, Candidate, Experience
from sourcer.scoring import infer_industries, score_candidate
from sourcer.semantic import TfidfSpace


def space(*docs):
    return TfidfSpace(list(docs))


def test_strong_candidate_scores_far_above_a_weak_one(jd_analyst, strong_analyst,
                                                      weak_candidate):
    s = space(jd_analyst.jd_text(), strong_analyst.profile_text(),
              weak_candidate.profile_text())
    strong = score_candidate(strong_analyst, jd_analyst, s)
    weak = score_candidate(weak_candidate, jd_analyst, s)
    assert strong.score > 85
    assert weak.score < 40
    assert strong.score - weak.score > 45


def test_every_dimension_is_explained(jd_analyst, strong_analyst):
    r = score_candidate(strong_analyst, jd_analyst, space(jd_analyst.jd_text()))
    bd = r.breakdown()
    assert set(bd) == {"must_have_skills", "nice_have_skills", "experience_years",
                       "title_relevance", "seniority_fit", "domain_industry",
                       "education", "location", "certifications", "projects"}
    for dim, v in bd.items():
        assert 0.0 <= v["raw"] <= 1.0, dim
        assert v["note"], f"{dim} has no explanation"
    assert r.rationale


def test_points_never_exceed_weight_and_score_is_bounded(jd_analyst, strong_analyst):
    r = score_candidate(strong_analyst, jd_analyst, space(jd_analyst.jd_text()))
    for d in r.dims:
        assert 0 <= d.points <= d.weight + 1e-9
    assert 0 <= r.score <= 100


def test_missing_hard_requirement_caps_the_score(jd_analyst):
    """A candidate strong everywhere but missing a mandatory skill is capped."""
    c = Candidate(full_name="No SQL", headline="Data Analyst",
                  location_raw="Karachi, Pakistan",
                  skills_raw=["Power BI", "Excel", "Statistics", "Data Analysis"],
                  experiences=[Experience(title="Data Analyst", company="Habib Bank Limited",
                                          start_date="Jan 2018", is_current=True,
                                          description="Power BI dashboards and Excel models")])
    r = score_candidate(c, jd_analyst, space(jd_analyst.jd_text()))
    assert r.score <= 45
    assert any("hard requirement" in f for f in r.flags)


def test_hard_min_years_caps_an_under_experienced_candidate():
    jd = JD(code="X", title="Data Analyst", title_family="data_analyst", min_years=3,
            hard_min_years=True, must_have=["SQL", "Excel"])
    junior = Candidate(full_name="Fresh Grad", headline="Data Analyst Intern",
                       location_raw="Karachi, Pakistan", skills_raw=["SQL", "Excel"],
                       experiences=[Experience(title="Data Analyst Intern", company="Acme",
                                               start_date="Jul 2025", is_current=True)])
    r = score_candidate(junior, jd, space(jd.jd_text()))
    assert r.score <= 45
    assert any("experience floor" in f for f in r.flags)


def test_without_the_hard_flag_under_experience_only_loses_its_dimension():
    jd = JD(code="X", title="Data Analyst", title_family="data_analyst", min_years=3,
            hard_min_years=False, must_have=["SQL", "Excel"])
    junior = Candidate(full_name="Fresh Grad", headline="Data Analyst",
                       location_raw="Karachi, Pakistan", skills_raw=["SQL", "Excel"],
                       experiences=[Experience(title="Data Analyst Intern", company="Acme",
                                               start_date="Jul 2025", is_current=True)])
    r = score_candidate(junior, jd, space(jd.jd_text()))
    assert r.score > 45


def test_location_outside_the_city_costs_points(jd_analyst, strong_analyst):
    s = space(jd_analyst.jd_text(), strong_analyst.profile_text())
    here = score_candidate(strong_analyst, jd_analyst, s)
    strong_analyst.location_raw = "Lahore, Pakistan"
    strong_analyst._text_cache = None
    away = score_candidate(strong_analyst, jd_analyst, s)
    assert here.score > away.score
    assert any("relocation" in f for f in away.flags)


def test_a_same_group_skill_earns_partial_credit(jd_analyst):
    """Tableau where the JD asked for Power BI is worth something, not nothing."""
    c = Candidate(full_name="Tab User", headline="Data Analyst",
                  location_raw="Karachi, Pakistan",
                  skills_raw=["SQL", "Tableau", "Excel", "Statistics", "Data Analysis"],
                  experiences=[Experience(title="Data Analyst", company="Habib Bank Limited",
                                          start_date="Jan 2018", is_current=True,
                                          description="Tableau dashboards, SQL extracts")])
    r = score_candidate(c, jd_analyst, space(jd_analyst.jd_text()))
    assert "power_bi" in r.substitutes
    must = next(d for d in r.dims if d.name == "must_have_skills")
    assert 0.8 < must.raw < 1.0


def test_over_experience_is_flagged_not_rewarded(jd_analyst):
    c = Candidate(full_name="Veteran", headline="Head of Business Intelligence",
                  location_raw="Karachi, Pakistan",
                  skills_raw=["SQL", "Power BI", "Excel", "Statistics", "Data Analysis"],
                  experiences=[Experience(title="Head of Business Intelligence",
                                          company="Habib Bank Limited", start_date="Jan 2008",
                                          is_current=True,
                                          description="Own the BI roadmap and warehouse")])
    r = score_candidate(c, jd_analyst, space(jd_analyst.jd_text()))
    assert any("over-experienced" in f for f in r.flags)


def test_unspecified_requirements_are_neutral_not_zero():
    jd = JD(code="X", title="Data Analyst", title_family="data_analyst")
    c = Candidate(full_name="Someone", headline="Data Analyst",
                  location_raw="Karachi, Pakistan", skills_raw=["SQL"],
                  experiences=[Experience(title="Data Analyst", company="Acme",
                                          start_date="Jan 2020", is_current=True)])
    r = score_candidate(c, jd, space(jd.jd_text()))
    nice = next(d for d in r.dims if d.name == "nice_have_skills")
    assert nice.raw == pytest.approx(0.6)


def test_scoring_without_a_semantic_space_still_works(jd_analyst, strong_analyst):
    r = score_candidate(strong_analyst, jd_analyst, None)
    assert r.score == r.rubric and r.semantic == 0.0


def test_industry_inference_recognises_local_employers():
    assert "banking" in infer_industries("Data Analyst at Habib Bank Limited")
    assert "fintech" in infer_industries("Analytics lead at Easypaisa")
    assert "software_it" in infer_industries("Engineer at Systems Limited")
    assert infer_industries("Worked at a small family business") == set()


def test_semantic_similarity_ranks_sensibly():
    jd = "Senior Data Engineer. ETL pipelines with Python, SQL, Airflow, Spark, Snowflake."
    good = "Data Engineer. Airflow DAGs, PySpark jobs, Snowflake models in SQL and Python."
    bad = "Graphic Designer. Illustrator, Photoshop, brand identity, print layout."
    s = TfidfSpace([jd, good, bad])
    assert s.similarity(jd, good) > s.similarity(jd, bad)
    assert s.similarity(jd, bad) < 0.05


def test_semantic_similarity_is_symmetric_and_bounded():
    s = TfidfSpace(["alpha beta gamma", "beta gamma delta"])
    a = s.similarity("alpha beta gamma", "beta gamma delta")
    b = s.similarity("beta gamma delta", "alpha beta gamma")
    assert a == pytest.approx(b)
    assert 0.0 <= a <= 1.0
    assert s.similarity("", "anything") == 0.0
