from sourcer.models import Candidate, Education, Experience, edu_level


def test_person_key_prefers_linkedin_then_email_then_name():
    a = Candidate(full_name="A B", linkedin_url="https://linkedin.com/in/ab-123/",
                  email="x@y.com")
    assert a.person_key() == "li:ab-123"
    b = Candidate(full_name="A B", email="X@Y.com")
    assert b.person_key() == "em:x@y.com"
    c = Candidate(full_name="A B", location_raw="Karachi")
    assert c.person_key().startswith("nm:")


def test_same_person_seen_twice_gets_the_same_key():
    k1 = Candidate(full_name="Ali Raza",
                   linkedin_url="https://www.linkedin.com/in/ali-raza-1/").person_key()
    k2 = Candidate(full_name="Ali R.",
                   linkedin_url="http://linkedin.com/in/ali-raza-1?trk=foo").person_key()
    assert k1 == k2


def test_different_people_with_the_same_name_stay_separate():
    a = Candidate(full_name="Faizan Baig", location_raw="Karachi",
                  experiences=[Experience(company="Arbisoft", is_current=True)])
    b = Candidate(full_name="Faizan Baig", location_raw="Karachi",
                  experiences=[Experience(company="i2c", is_current=True)])
    assert a.person_key() != b.person_key()


def test_total_years_merges_overlapping_roles(strong_analyst):
    assert 7.0 < strong_analyst.total_years() < 8.5


def test_relevant_years_excludes_unrelated_roles():
    c = Candidate(
        full_name="X Y",
        skills_raw=["Photoshop"],
        experiences=[
            Experience(title="Data Analyst", company="Bank", start_date="Jan 2020",
                       end_date="Jan 2023", description="SQL and Power BI reporting"),
            Experience(title="Graphic Designer", company="Studio", start_date="Jan 2015",
                       end_date="Jan 2019",
                       description="Brand identity work in Illustrator and Photoshop"),
        ])
    rel = c.relevant_years("data_analyst", {"sql", "power_bi", "excel"})
    assert 2.5 < rel < 3.5, rel
    assert c.total_years() > 6


def test_a_role_with_no_description_falls_back_to_declared_skills():
    """A terse profile must not be read as having no relevant experience."""
    c = Candidate(full_name="X Y", skills_raw=["SQL", "Power BI", "Excel"],
                  experiences=[Experience(title="Specialist", company="Acme",
                                          start_date="Jan 2019", end_date="Jan 2024")])
    assert c.relevant_years(None, {"sql", "power_bi", "excel"}) >= 4.5


def test_education_ladder():
    assert edu_level("BS Computer Science") == "bachelors"
    assert edu_level("MBA") == "masters"
    assert edu_level("Intermediate") == "intermediate"
    assert edu_level("PhD in Statistics") == "phd"
    assert edu_level("") == "none"


def test_highest_education_wins():
    c = Candidate(full_name="X Y", educations=[
        Education(degree="BS", field_of_study="CS"),
        Education(degree="MS", field_of_study="Data Science")])
    assert c.education_level() == "masters"


def test_current_role_is_the_current_one(strong_analyst):
    assert strong_analyst.current_title() == "Data Analyst"
    assert strong_analyst.current_company() == "Habib Bank Limited"


def test_jd_content_hash_changes_with_requirements(jd_analyst):
    before = jd_analyst.content_hash()
    jd_analyst.min_years = 5
    assert jd_analyst.content_hash() != before


def test_jd_must_skills_expand_implied(jd_analyst):
    assert "sql" in jd_analyst.must_skills()
    assert jd_analyst.hard_skills() == {"sql"}
