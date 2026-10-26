#!/usr/bin/env python3
"""Generate a realistic demo cohort so you can see the bot work before you have
real data. Writes a CSV to storage/imports and two PDFs to storage/pdf_inbox.

    python3 scripts/seed_demo.py            # ~45 candidates
    python3 scripts/seed_demo.py --clean    # wipe demo data first

Everything here is synthetic. Delete storage/sourcer.db to start clean.
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

random.seed(11)

FIRST_M = ["Ali", "Usman", "Bilal", "Hamza", "Faizan", "Zeeshan", "Omar", "Saad", "Kashif",
           "Imran", "Raza", "Talha", "Shahzaib", "Danish", "Noman", "Asad", "Junaid", "Waqas"]
FIRST_F = ["Ayesha", "Sana", "Hira", "Maria", "Fatima", "Mehwish", "Rabia", "Anum", "Komal",
           "Sadia", "Zainab", "Areeba", "Nida", "Iqra"]
LAST = ["Khan", "Shaikh", "Ahmed", "Siddiqui", "Raza", "Hussain", "Qureshi", "Memon", "Baig",
        "Abbasi", "Farooqui", "Jamil", "Rizvi", "Lakhani", "Ansari", "Chaudhry", "Malik"]

KHI = ["Karachi, Pakistan", "Clifton, Karachi, Pakistan", "Gulshan-e-Iqbal, Karachi, Pakistan",
       "DHA Karachi, Pakistan", "North Nazimabad, Karachi, Pakistan",
       "Karachi Division, Sindh, Pakistan"]
OTHER_PK = ["Lahore, Pakistan", "Islamabad, Pakistan", "Rawalpindi, Pakistan"]
ABROAD = ["Dubai, United Arab Emirates", "Riyadh, Saudi Arabia"]

BANKS = ["Habib Bank Limited", "United Bank Limited", "Meezan Bank", "Bank Alfalah",
         "Faysal Bank", "Standard Chartered Pakistan", "JS Bank"]
FINTECH = ["Easypaisa", "JazzCash", "NayaPay", "SadaPay", "Abhi", "Keenu"]
TELCO = ["Jazz", "Telenor Pakistan", "Ufone", "PTCL"]
SOFT = ["Systems Limited", "10Pearls", "Contour Software", "Arbisoft", "Folio3", "VentureDive",
        "Confiz", "i2c", "Techlogix", "Emumba"]
ECOM = ["Daraz", "foodpanda Pakistan", "Bazaar Technologies", "PriceOye"]
FMCG = ["Unilever Pakistan", "Nestle Pakistan", "National Foods", "Engro Foods"]

UNIS = ["NED University of Engineering and Technology", "NUST", "FAST-NUCES",
        "Institute of Business Administration (IBA)", "SZABIST", "Bahria University",
        "University of Karachi", "DHA Suffa University", "Habib University"]


def name() -> str:
    first = random.choice(FIRST_M + FIRST_F)
    return f"{first} {random.choice(LAST)}"


def exp(title, company, start, end, desc, loc="Karachi, Pakistan") -> str:
    return f"{title} | {company} | {start} | {end} | {desc}"


def edu(degree, field, inst, year) -> str:
    return f"{degree} | {field} | {inst} | {year}"


def row(**kw) -> dict:
    base = dict(full_name="", headline="", location="", linkedin_url="", email="", phone="",
                summary="", skills="", experiences="", educations="")
    base.update(kw)
    return base


ROWS: list[dict] = []


def add(**kw):
    ROWS.append(row(**kw))


# ---- strong data analysts ------------------------------------------------- #
for i in range(6):
    n = name()
    slug = n.lower().replace(" ", "-") + f"-da{i}"
    bank = random.choice(BANKS + FINTECH)
    prev = random.choice(BANKS + TELCO)
    start_cur = random.choice(["Jan 2021", "Mar 2021", "Jul 2022", "Feb 2022"])
    add(full_name=n, headline=f"Data Analyst at {bank} | SQL, Power BI, Python",
        location=random.choice(KHI),
        linkedin_url=f"https://www.linkedin.com/in/{slug}",
        email=f"{slug.split('-')[0]}.{slug.split('-')[1]}@example.com",
        summary="Analytics professional turning transactional banking data into dashboards "
                "and decisions. Owner of daily and monthly MIS.",
        skills="SQL; Power BI; Advanced Excel; Data Analysis; Statistics; Python; DAX",
        experiences=" ;; ".join([
            exp("Data Analyst", bank, start_cur, "Present",
                "Own retail MIS reporting. Build Power BI dashboards with DAX measures. "
                "Write complex SQL against the data warehouse. Run cohort and funnel analysis "
                "and present findings to leadership."),
            exp("MIS Officer", prev, "Jun 2018", "Dec 2020",
                "Daily reporting in Excel and SQL, automated recurring reports in Python."),
        ]),
        educations=edu("BS", "Computer Science", random.choice(UNIS), 2018))

# ---- mid / partial data analysts ----------------------------------------- #
for i in range(6):
    n = name()
    slug = n.lower().replace(" ", "-") + f"-mis{i}"
    co = random.choice(FMCG + ECOM + TELCO)
    add(full_name=n, headline=f"MIS Analyst at {co}", location=random.choice(KHI),
        linkedin_url=f"https://www.linkedin.com/in/{slug}",
        summary="Reporting and MIS for a commercial team.",
        skills=random.choice(["SQL; Advanced Excel; Data Analysis",
                              "Advanced Excel; Power BI; Reporting",
                              "SQL; Excel; Tableau"]),
        experiences=" ;; ".join([
            exp("MIS Analyst", co, random.choice(["Jan 2022", "Aug 2021", "Apr 2023"]), "Present",
                "Monthly sales and operations reporting, Excel models, some SQL extracts."),
        ]),
        educations=edu(random.choice(["BBA", "BS"]), random.choice(["Business", "Statistics"]),
                       random.choice(UNIS), random.choice([2019, 2020, 2021])))

# ---- strong data engineers ----------------------------------------------- #
for i in range(5):
    n = name()
    slug = n.lower().replace(" ", "-") + f"-de{i}"
    co = random.choice(SOFT)
    add(full_name=n, headline=f"Senior Data Engineer at {co}", location=random.choice(KHI),
        linkedin_url=f"https://www.linkedin.com/in/{slug}",
        email=f"{slug.split('-')[0]}@example.com",
        summary="Data engineer building batch and streaming pipelines for banking and telecom "
                "clients. Owner of the Airflow platform and the dimensional models behind "
                "group reporting.",
        skills="Python; SQL; Apache Airflow; PySpark; Snowflake; dbt; AWS; Kafka; "
               "Data Warehousing; Docker",
        experiences=" ;; ".join([
            exp("Senior Data Engineer", co, random.choice(["Feb 2022", "Oct 2021"]), "Present",
                "Own the Airflow orchestration layer. Rebuilt the warehouse on Snowflake with "
                "dbt. Migrated SSIS packages to PySpark on AWS Glue. Dimensional modelling "
                "for group reporting."),
            exp("Data Engineer", random.choice(TELCO), "Jun 2019", "Jan 2022",
                "Built Kafka ingestion and Hive models for CDR analytics, Spark feature store."),
            exp("ETL Developer", random.choice(BANKS), "Aug 2017", "May 2019",
                "SSIS and T-SQL development for the retail data warehouse."),
        ]),
        educations=edu("BE", "Computer Systems Engineering", random.choice(UNIS), 2017))

# ---- adjacent: BI devs, software engineers, data scientists -------------- #
for i in range(4):
    n = name()
    slug = n.lower().replace(" ", "-") + f"-bi{i}"
    add(full_name=n, headline="Power BI Developer", location=random.choice(KHI),
        linkedin_url=f"https://www.linkedin.com/in/{slug}",
        skills="Power BI; DAX; SQL; SSAS; Data Warehousing",
        experiences=exp("Power BI Developer", random.choice(SOFT), "Mar 2020", "Present",
                        "Build Power BI datasets and reports, star schema models, SQL views."),
        educations=edu("BS", "Software Engineering", random.choice(UNIS), 2019))

for i in range(4):
    n = name()
    slug = n.lower().replace(" ", "-") + f"-se{i}"
    add(full_name=n, headline="Software Engineer | Python, Django", location=random.choice(KHI),
        linkedin_url=f"https://www.linkedin.com/in/{slug}",
        skills="Python; Django; PostgreSQL; Docker; Git; JavaScript",
        experiences=exp("Software Engineer", random.choice(SOFT), "Jan 2021", "Present",
                        "Backend services in Python and Django, PostgreSQL, Docker, CI/CD."),
        educations=edu("BS", "Computer Science", random.choice(UNIS), 2020))

for i in range(3):
    n = name()
    slug = n.lower().replace(" ", "-") + f"-ds{i}"
    add(full_name=n, headline="Data Scientist | ML, Python", location=random.choice(KHI),
        linkedin_url=f"https://www.linkedin.com/in/{slug}",
        skills="Python; Machine Learning; scikit-learn; SQL; Statistics; PyTorch; NLP",
        experiences=" ;; ".join([
            exp("Data Scientist", random.choice(FINTECH + ECOM), "Sep 2021", "Present",
                "Credit risk and churn models in Python, feature engineering on SQL, "
                "A/B testing and statistical analysis."),
            exp("Data Analyst", random.choice(BANKS), "Jan 2019", "Aug 2021",
                "Portfolio analytics, SQL, Power BI reporting."),
        ]),
        educations=edu("MS", "Data Science", random.choice(UNIS), 2021))

# ---- out of city --------------------------------------------------------- #
for i in range(4):
    n = name()
    slug = n.lower().replace(" ", "-") + f"-out{i}"
    add(full_name=n, headline="Data Analyst", location=random.choice(OTHER_PK + ABROAD),
        linkedin_url=f"https://www.linkedin.com/in/{slug}",
        skills="SQL; Power BI; Excel; Data Analysis; Statistics; Python",
        experiences=exp("Data Analyst", random.choice(BANKS), "Jan 2020", "Present",
                        "Retail analytics, SQL, Power BI dashboards, statistical analysis."),
        educations=edu("BS", "Statistics", "Punjab University", 2019))

# ---- under/over experienced ---------------------------------------------- #
for i in range(3):
    n = name()
    slug = n.lower().replace(" ", "-") + f"-jr{i}"
    add(full_name=n, headline="Junior Data Analyst (Fresh Graduate)", location=random.choice(KHI),
        linkedin_url=f"https://www.linkedin.com/in/{slug}",
        skills="SQL; Excel; Power BI; Python",
        experiences=exp("Data Analyst Intern", random.choice(SOFT), "Jul 2025", "Present",
                        "Supporting the reporting team with Excel and SQL."),
        educations=edu("BS", "Computer Science", random.choice(UNIS), 2025))

for i in range(2):
    n = name()
    slug = n.lower().replace(" ", "-") + f"-head{i}"
    add(full_name=n, headline="Head of Business Intelligence", location=random.choice(KHI),
        linkedin_url=f"https://www.linkedin.com/in/{slug}",
        skills="SQL; Power BI; Data Warehousing; Data Analysis; Leadership; Statistics; Python",
        experiences=" ;; ".join([
            exp("Head of Business Intelligence", random.choice(BANKS), "Jan 2019", "Present",
                "Lead a team of 12 analysts and engineers. Own the BI roadmap, warehouse "
                "strategy and executive reporting."),
            exp("BI Manager", random.choice(TELCO), "Mar 2013", "Dec 2018",
                "Managed reporting and warehouse delivery."),
        ]),
        educations=edu("MBA", "Finance", "IBA", 2012))

# ---- irrelevant ----------------------------------------------------------- #
IRRELEVANT = [
    ("Graphic Designer", "Adobe Photoshop; Illustrator; Branding", "Creative studio work."),
    ("Sales Executive", "Sales; Negotiation; CRM", "Field sales for a consumer brand."),
    ("HR Officer", "Recruitment; HR Operations; Payroll", "Generalist HR for a mid-size firm."),
    ("Accounts Officer", "Accounting; Excel; Reconciliation", "Ledger and reconciliation work."),
    ("Customer Support Lead", "Communication; CRM; Team Management", "Contact centre team lead."),
    ("Mechanical Engineer", "AutoCAD; Maintenance; Safety", "Plant maintenance engineering."),
]
for i, (title, skills, desc) in enumerate(IRRELEVANT):
    n = name()
    slug = n.lower().replace(" ", "-") + f"-x{i}"
    add(full_name=n, headline=title, location=random.choice(KHI),
        linkedin_url=f"https://www.linkedin.com/in/{slug}",
        skills=skills,
        experiences=exp(title, random.choice(FMCG + ECOM + SOFT), "Jan 2021", "Present", desc),
        educations=edu("BBA", "Management", random.choice(UNIS), 2020))


def write_pdf_samples(inbox: Path) -> list[Path]:
    """Two PDFs so the pdf_inbox route is exercised too."""
    from sourcer.models import Candidate, Education, Experience
    from sourcer.pdfstore import render_profile_pdf
    inbox.mkdir(parents=True, exist_ok=True)
    made = []
    specs = [
        dict(full_name="Hira Lakhani", headline="Data Analyst | SQL, Power BI, Python",
             location_raw="Clifton, Karachi, Pakistan", email="hira.lakhani@example.com",
             phone="+92 301 2223344",
             linkedin_url="https://www.linkedin.com/in/hira-lakhani-analyst",
             summary="Six years of analytics across banking and fintech.",
             skills_raw=["SQL", "Power BI", "Advanced Excel", "Statistics", "Python",
                         "Data Analysis"],
             experiences=[
                 Experience(title="Senior Data Analyst", company="Easypaisa",
                            location="Karachi, Pakistan", start_date="Apr 2022",
                            is_current=True,
                            description="Lead analyst for wallet growth. Power BI dashboards, "
                                        "SQL on BigQuery, experiment design and readouts."),
                 Experience(title="Data Analyst", company="Meezan Bank",
                            location="Karachi, Pakistan", start_date="Feb 2019",
                            end_date="Mar 2022",
                            description="Retail MIS, SQL, Excel models, statistical analysis.")],
             educations=[Education(degree="BS", field_of_study="Statistics",
                                   institution="University of Karachi", end_year=2018)]),
        dict(full_name="Shahzaib Memon", headline="Data Engineer | Airflow, Spark, Snowflake",
             location_raw="Gulshan-e-Iqbal, Karachi, Pakistan",
             email="s.memon@example.com", phone="+92 333 7778899",
             linkedin_url="https://www.linkedin.com/in/shahzaib-memon-de",
             summary="Pipelines, warehouses and orchestration for fintech.",
             skills_raw=["Python", "SQL", "Apache Airflow", "PySpark", "Snowflake", "dbt",
                         "AWS", "Kafka", "Data Warehousing"],
             experiences=[
                 Experience(title="Data Engineer", company="NayaPay",
                            location="Karachi, Pakistan", start_date="Jan 2021",
                            is_current=True,
                            description="Built the Airflow platform and Snowflake warehouse "
                                        "with dbt. Kafka ingestion, PySpark transforms on AWS."),
                 Experience(title="ETL Developer", company="Systems Limited",
                            location="Karachi, Pakistan", start_date="Sep 2018",
                            end_date="Dec 2020",
                            description="SSIS, T-SQL and dimensional modelling for banking "
                                        "clients.")],
             educations=[Education(degree="BE", field_of_study="Software Engineering",
                                   institution="NED University of Engineering and Technology",
                                   end_year=2018)]),
    ]
    for spec in specs:
        c = Candidate(source="demo", source_ref="seed", **spec)
        dest = inbox / (c.full_name.lower().replace(" ", "_") + "_profile.pdf")
        render_profile_pdf(c, dest)
        made.append(dest)
    return made


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="remove the db and stored profiles first")
    a = ap.parse_args()

    from sourcer.config import cfg
    c = cfg()
    if a.clean:
        import shutil
        for pth in (c.path_of("storage.db_path"), c.path_of("storage.profiles_dir")):
            if pth.is_dir():
                shutil.rmtree(pth, ignore_errors=True)
            elif pth.exists():
                pth.unlink()
        for extra in ("storage/sourcer.db-wal", "storage/sourcer.db-shm"):
            (c.root / extra).unlink(missing_ok=True)
        print("cleaned database and stored profiles")

    imports = c.root / c.get("sources.csv.watch_dir", "storage/imports")
    imports.mkdir(parents=True, exist_ok=True)
    csv_path = imports / "demo_cohort.csv"
    with open(csv_path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(ROWS[0].keys()))
        wr.writeheader()
        wr.writerows(ROWS)
    print(f"wrote {len(ROWS)} candidates -> {csv_path}")

    pdfs = write_pdf_samples(c.path_of("storage.pdf_inbox"))
    for p in pdfs:
        print(f"wrote PDF -> {p}")
    print("\nnow run:  python3 -m sourcer run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
