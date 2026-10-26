# hrbp-sourcer

A candidate sourcing and shortlisting bot for a hiring team. You give it your
vacancies; it builds a searchable, scored, de-duplicated talent pool with a
filed PDF for every person, and answers questions like *"give me the data
analysts and data engineers scoring 75 or above."*

Built for a Karachi-based HRBP filling many roles at once, but the city,
taxonomy and weights are all config.

---

## The short version

Windows, no typing:

1. Double-click **`1_install.bat`** (once).
2. Double-click **`2_open_app.bat`**. The app opens in your browser.
3. **Vacancies** tab: add each designation. Paste the JD and press *Read it for me*.
4. **LinkedIn** tab: *Log in to LinkedIn* (you sign in yourself in the window that opens), then
   *Test with 1 profile*, check it in the **Candidates** tab, then *Start sourcing now*.
5. **Candidates** tab: type what your manager asks, e.g.
   `data analyst manager ke liye resumes nikaldo jiska score 75% se upar ho`.
   Export to Excel, or a zip of the PDFs.
6. Optional: **`3_schedule_daily.bat`** runs the sourcing once a day.

Everything below is detail and the command-line equivalents.

---

## How candidates get in

| Route | How it works | Risk to your LinkedIn account |
|---|---|---|
| **LinkedIn tab (`linkedin_browser`)** | Drives your own Chrome through LinkedIn: for each vacancy it searches, keeps only people in your target city, opens a few profiles, reads them, files a PDF, scores them. You log in yourself once; the bot never sees your password. | **Yes.** This is automated access, which LinkedIn's terms do not allow. Pacing (below) lowers the chance of being noticed; it does not remove it. |
| **Import tab / `pdf_inbox`** | Drop LinkedIn "Save to PDF" files or CVs. | None - a human used LinkedIn's own button. |
| **Import tab / `csv`** | Excel/CSV from Recruiter, job boards, referrals, your ATS. | None. |
| **`proxycurl` / `provider`** | A licensed people-data vendor's API. | None to your account; needs a contract. |
| **`linkedin_talent`** | LinkedIn's official Talent Solutions API. | None; needs a partner contract. |

### How the LinkedIn browsing is paced

Built so the traffic looks like one person working a list, not a script:

- **Daily ceiling** (default 40 profiles) with a **ramp-up**: day 1 is 10, then +5 per active day up to your ceiling.
- **Working hours only** (default 10:00-19:00 local), and a single sitting never runs longer than 25 minutes.
- **Irregular gaps** of 25-85 seconds between profiles, a few minutes' break every 5-9 profiles, scrolling and mouse movement while "reading".
- **Search cards are filtered before opening.** Anyone outside your city is never opened; anyone seen in the last 30 days is never reopened. Fewer page views for the same result.
- **It stops at the first warning.** Any checkpoint, verification page, restriction notice or "unusual activity" text ends the run immediately and pauses sourcing for 48 hours. A person must clear that pause in the app, after resolving it on LinkedIn.

What it deliberately does **not** do: spoof the browser fingerprint, hide that it is automated, rotate IPs, or run invisibly. It is your normal Chrome, in a visible window.

### Things you should know before relying on it

- **I could not test against the real LinkedIn.** The whole browser flow (login, search, location filter, profile reading, quotas, pacing, warning handling) is tested end to end against a stand-in site that mimics LinkedIn's layout, and the page-text parser is tested on realistic fixtures. LinkedIn changes its pages often, and a first run on the real site may read some profiles imperfectly. That is why there is a **Test with 1 profile** button, and why any profile the bot cannot read is saved as raw text in `storage/linkedin_debug/` - send me one and the parser gets fixed in minutes.
- **Free LinkedIn accounts have a monthly search limit**, and show far fewer results than Sales Navigator or Recruiter. If you hit it, the bot sees the warning text and stops.
- **Restriction of the account is a real possibility** even at these volumes. Do not run this on an account you cannot afford to lose.
- **Your city filter**: apply "Karachi" once in a LinkedIn people search, copy the page address into *LinkedIn > City filter*, and every search reuses it. Without it, the bot falls back to filtering cards by the location line, which works but wastes searches.

## Install

```bash
cd hrbp-sourcer
python3 -m pip install -r requirements.txt
cp config/settings.example.yaml config/settings.yaml   # optional, has sane defaults
python3 -m sourcer init
```

Optional extras:

```bash
pip install pytest                   # to run the test suite
pip install anthropic                # to enable the Claude re-rank (off by default)
pip install sentence-transformers    # better semantic matching; then SOURCER_EMBEDDINGS=st
```

Try it on synthetic data before you put anything real in:

```bash
python3 scripts/seed_demo.py --clean      # 45 fake Karachi candidates
python3 -m sourcer run
python3 -m sourcer query "data analyst" --min-score 75
python3 -m sourcer show 3                  # why that person scored what they scored
```

---

## Putting it online (GitHub + Railway + AI agent)

See **DEPLOY.md** - step by step: GitHub, Railway with a volume, the free Groq agent, the live **Agent activity** tab, and the LinkedIn collector that runs on your PC.

### Signing in to LinkedIn (including "Continue with Google")

The **Log in to LinkedIn** button opens a normal Chrome or Edge window (Chrome if installed, otherwise Edge; you can choose in the Pacing card). It is a plain browser with no remote-control switches, so Google lets you sign in with your Google account. It uses the bot's *own* profile folder (`storage/browser_profile_chrome` or `_edge`), not your everyday profile: Chrome blocks automation of the everyday profile, and the bot and you cannot both use it at once. Sign in once, then **close that window** (or press Stop in the app). The bot then checks the saved session and from then on reuses it. If you switch browser, sign in once more.

## Logins, departments and HODs

The app asks for a login. The first time it starts it creates **admin / admin123**; a yellow banner reminds you to change that password (Users tab, Edit on the admin row) until you do.

1. **Department Setup** tab: add each department and the roles that belong to it (or press *Paste structure* and paste lines like `Technology: Data Analyst, Data Engineer`).
2. **Users** tab: create one login per HOD, then tick the departments they head. (You can also pick HODs from the department's own *Edit* window - both write the same link.)
3. Every vacancy goes to a department by itself when its title matches one of that department's roles ("Senior Data Analyst" matches the role "Data Analyst"). To override, open the vacancy and choose a department, or use the *assign to...* box on the Department Setup page for vacancies that match nothing.

A HOD sees only Candidates and Vacancies, only for their departments' vacancies. They can move a candidate through the stages, download PDFs and export - but cannot see other departments' candidates, the LinkedIn / Import / Users / Org tabs, or any setting.

If the app still looks old after an update, an older copy is probably still running: close its black window, or restart the PC. The sidebar shows the version (v2.0.0 or newer); newer copies also pick the next free port and say so in the black window.

## Managing companies and search titles centrally

Admin tabs, so nobody types the same thing into every vacancy:

- **Companies** - make lists of target employers ("Top tech employers: Systems Limited, Engro, Jazz") and say where each applies: departments, specific roles, or every vacancy. Every matching vacancy gets those companies automatically. A vacancy's own "Preferred employers" field still works and is added on top. Saving a list rescores everyone. At the top of the tab you choose how the bot uses them:
  - **Preferred companies first, then everyone** (default) - it searches people at those companies first (up to 3 searches of 5 companies each); when those searches run out of results it widens to all of LinkedIn. Nobody is opened twice.
  - **Preferred companies only** - never looks outside the lists.
  - **Everyone** - ignores the lists when searching (they still raise the score of matching profiles).
  A vacancy with no company list just searches everyone relevant. Note: LinkedIn keyword search matches the company name anywhere on a profile, so the first phase can include people who only *used to* work there; the score and the "preferred employer" rank sort that out. The daily search-page limit still applies across both phases.
- **Search titles** - nothing to type. The bot reads each vacancy's JD and decides which other roles do the same work (taxonomy + the roles whose core skills the JD asks for; with `ANTHROPIC_API_KEY` and `pip install anthropic`, Claude reads the JD as well - a few cents a month). It re-reads a JD whenever you change it. The tab shows what it decided and why; edit a rule only if you disagree, and an edited rule is never overwritten.
- **Templates** - in Vacancies: *Bulk add (Excel)* downloads an Excel template (one row per vacancy, dropdowns for level / education / department, an Example sheet and a How-to sheet) and uploads the filled file; vacancies whose title already exists are skipped, and bad rows are reported by row number. The Word template is the single-vacancy version: fill the lines after each colon, then *Load a Word / PDF file* in the vacancy form and every field fills itself.

## Your vacancies

One file per vacancy in `config/jds/`. The YAML header is what the scorer uses;
the body below is free text and is read for extra signal.

```markdown
---
code: DA-001
title: Data Analyst
seniority: mid                  # intern|junior|mid|senior|lead|manager|director|executive
location: Karachi, Pakistan
min_years: 3
max_years: 7
hard_min_years: true            # below min_years caps the score (see Scoring)
education_min: bachelors
openings: 2
industries: [banking, fintech, telecom]
must_have: [SQL, Power BI, Excel, Data Analysis, Statistics]
nice_have: [Python, Tableau, ETL]
hard_requirements: [SQL]        # non-negotiable; missing one caps the score
---

# Data Analyst

## Requirements
- 3-7 years in analytics or MIS
- Advanced SQL and Power BI
...
```

You can also drop in a plain JD with no header at all - the parser infers the
title, years, education and skills from the text. The header is just more
reliable, and takes two minutes.

`sourcer jds` lists what the bot thinks your vacancies are. Check it once after
writing a JD - if a skill you care about is missing from `must-have`, add it to
the header explicitly.

**Editing a JD bumps its version and triggers a rescore automatically.** Old
scores for the previous version are dropped, so a shortlist is never stale.

---

## The daily loop

```bash
sourcer plan                              # today's searches, per vacancy
#   ... click through, Save to PDF into storage/pdf_inbox,
#   ... and/or drop a CSV/XLSX into storage/imports
sourcer run                               # parse, dedupe, file, score
sourcer query "data analyst" --min-score 75 --export xlsx,pdf
```

Then put it on a schedule:

```bash
./scripts/install_schedule.sh cron        # or: ./scripts/install_schedule.sh systemd
HOUR=6 MIN=15 ./scripts/install_schedule.sh cron     # pick your time
```

`scripts/daily.sh` runs the intake, writes an xlsx shortlist, applies the
retention policy, and logs to `storage/logs/YYYY-MM-DD.log`.

---

## Asking for people

```bash
sourcer query "data analyst"                            # defaults to 75%+
sourcer query "data analyst/data engineer" --min-score 80
sourcer query DE-002 --min-score 70 --limit 10
sourcer query "data analyst" --city karachi --new-only   # hide already-contacted
sourcer query "data analyst" --min-score 75 --export xlsx,pdf
```

A role string is matched against vacancy codes, titles and title families, so
`"data analyst/data engineer"` resolves to both vacancies and returns each
candidate once, at their best-matching role.

Exports land in `storage/exports/`:

- **csv** - flat, for anything
- **xlsx** - a `shortlist` sheet plus a `score detail` sheet with the
  per-dimension percentages
- **pdf** - a folder with every shortlisted PDF, numbered by rank, plus a
  one-page cover sheet. This is the thing you forward to a hiring manager.

Move people through the pipeline so tomorrow's run does not re-surface them:

```bash
sourcer status 42 shortlisted
sourcer status 42 contacted --note "screening call Thursday"
sourcer status 17 rejected --note "wants remote only"
```

Statuses: `new`, `reviewed`, `shortlisted`, `contacted`, `rejected`, `hired`.

---

## How the score works

The score is a weighted rubric over eight dimensions that sums to 100. Nothing
is a black box - `sourcer show <id>` prints every dimension, its weight, the
points earned and why:

```
score 95.8/100  (rubric 95.8, semantic 0.39)
  must_have_skills    100.0%  ####################  32.0/32 pts   5/5 exact
  title_relevance     100.0%  ####################  17.0/17 pts   current 'Senior Data Analyst' vs family 'data_analyst'
  experience_years    100.0%  ####################  15.0/15 pts   7.58 relevant / 7.58 total yrs vs 3+ required
  location            100.0%  ####################  10.0/10 pts   Clifton, Karachi, Pakistan -> metro
  nice_have_skills     66.7%  #############          5.3/8 pts    1/3 exact, 2 substituted
  education           100.0%  ####################   7.0/7 pts    bachelors vs bachelors required
  seniority_fit        75.0%  ###############        4.5/6 pts    senior vs mid required
  domain_industry     100.0%  ####################   5.0/5 pts    matched banking, fintech
```

| Dimension | Weight | What it measures |
|---|---|---|
| `must_have_skills` | 32 | Share of the JD's must-haves evidenced, with half credit for a same-family stand-in (Tableau where you asked for Power BI) |
| `title_relevance` | 17 | How close their titles are to the JD's title family, current role weighted above past ones |
| `experience_years` | 15 | **Relevant** years, not total - only roles whose title or described work match the JD, with overlapping roles counted once |
| `location` | 10 | Karachi metro / remote / elsewhere in Pakistan / abroad |
| `nice_have_skills` | 8 | Same as must-haves, lower stakes |
| `education` | 7 | Degree level against the JD minimum |
| `seniority_fit` | 6 | Their level against the level you are hiring at |
| `domain_industry` | 5 | Industry overlap, inferred from employer names (knows ~180 Pakistani employers) |

Weights are in `config/settings.yaml` and are renormalised, so you can move
them freely. If location is truly non-negotiable for you, raise it.

**Two hard caps.** A candidate missing anything in `hard_requirements`, or
under `min_years` when the JD sets `hard_min_years: true`, cannot score above
`hard_requirement_cap` (default 45) no matter how good the rest looks. This is
what keeps a fresh graduate with the right keywords out of a shortlist for a
five-year role.

**Flags** surface things you want to see rather than silently lose points for:
`over-experienced`, `under-experienced`, `education below bar`,
`relocation needed`, `missing hard requirement`.

### Why the rubric is the score

Text similarity between the JD and the profile is computed and stored, but by
default it only breaks ties between equal scores. It is tempting to blend it
into the number, and I tried both - blending it in penalises a terse but
perfectly qualified profile for being short, and adding it as a bonus pushed
everyone into a ceiling and flattened the real differences the rubric had
measured. A score you can defend line by line to a hiring manager, or to a
candidate who asks, is worth more than a slightly sharper ranking. Set
`scoring.semantic_mode` to `blend` or `bonus` if you disagree.

### Optional: a second opinion from Claude

Off by default. With `anthropic` installed, `ANTHROPIC_API_KEY` set and
`scoring.llm_rerank.enabled: true`, each run asks Claude to review the top
candidates per vacancy. The rubric score is kept untouched in `rubric_score`;
the model's score and reasoning are stored in `llm_score` / `llm_verdict`, and
`score` becomes a blend weighted toward the rubric. Disagreements stay visible
rather than being silently applied. Costs money per run.

### Teaching it your vocabulary

Three editable files, re-read on every run:

- `sourcer/data/skills.yaml` - skills, their aliases, and what they imply
  (`pytorch` implies `deep_learning` implies `machine_learning`)
- `sourcer/data/titles.yaml` - title families, adjacency, core skills
- `sourcer/data/industries.yaml` - industries and the employers that signal them

If the bot keeps missing a skill or misreading a title in your market, add it
here rather than fighting the score. This is the highest-leverage tuning you
can do.

---

## Data protection

You are building a database of real people's personal data. The bot assumes you
want to be able to defend that:

- **Lawful basis** is recorded on every record and stamped into every generated
  PDF (`retention.lawful_basis` in config). Fill it in with what your policy
  actually says.
- **Retention** is enforced. Every record carries a `delete_after` date
  (`retention.delete_after_days`, default 365 from last contact).
  `sourcer purge --yes` deletes anything expired, except candidates who are
  `shortlisted`, `contacted` or `hired`. `daily.sh` runs this for you.
- **Provenance** is kept - every candidate records which source and which file
  or reference they came from, and when they were first and last seen.
- **An audit trail** of every create, update, status change, JD version and
  purge is in the `audit` table.
- **Explainability.** Every score decomposes into job-related factors you can
  show someone. The optional Claude pass is instructed to judge only
  job-related evidence and to ignore name, gender, age, religion, ethnicity,
  nationality and disability.
- **`storage/` and `config/settings.yaml` are gitignored.** Do not commit the
  database or the PDF library. Back them up somewhere access-controlled - the
  whole thing is one SQLite file plus a folder of PDFs, so `cp -r` is a valid
  backup.

None of this makes the bot compliant on its own. It gives you the mechanisms;
the policy decisions are yours, and worth a conversation with whoever owns
data protection at your company before you run it on real candidates.

---

## Commands

| Command | Purpose |
|---|---|
| `sourcer init` | Create folders and the database, load JDs |
| `sourcer jds` | List the vacancies, as parsed |
| `sourcer plan [--jd CODE] [--out FILE]` | Boolean searches, LinkedIn/Recruiter filters and Google x-ray links per vacancy |
| `sourcer run [--dry-run] [--source S] [--only-new] [--max N]` | The daily job |
| `sourcer query ROLE [filters] [--export ...]` | Ask for candidates |
| `sourcer show ID [--jd CODE]` | Full profile and score breakdown |
| `sourcer status ID STATUS [--note]` | Move someone through the pipeline |
| `sourcer stats` | Pool size, cities, per-vacancy pipeline, recent runs |
| `sourcer sources` | Which intake routes are configured and ready |
| `sourcer rescore` | Recompute every score (after editing weights or taxonomy) |
| `sourcer ui [--port N] [--no-browser]` | The web app, at http://127.0.0.1:8765 |
| `sourcer purge --yes` | Apply the retention policy |

---

## Layout

```
sourcer/
  cli.py          command line
  config.py       settings, with defaults
  db.py           SQLite schema + in-place migrations
  models.py       Candidate / JD, and everything derived from them
  normalize.py    skills, titles, seniority, dates, locations
  jd.py           JD parsing and versioning
  scoring.py      the match engine
  semantic.py     TF-IDF similarity (no heavy dependencies)
  store.py        persistence, identity resolution, retention
  pdfstore.py     PDF parsing (LinkedIn exports + CVs) and generation
  searchplan.py   JD -> boolean searches and links
  report.py       querying and exports
  runner.py       the daily pipeline
  llm.py          optional Claude re-rank
  ask.py          "data analyst, 75% se upar" -> role + minimum score
  pacing.py       daily quota, ramp-up, working hours, breaks, warning detection
  linkedin_browser.py   the browser driver (your Chrome, you log in)
  linkedin_parse.py     LinkedIn page text -> candidate
  web/            the local web app (Flask, one HTML file)
  sources/        intake adapters (pdf_inbox, csv, provider, linkedin_talent)
  data/           skills.yaml, titles.yaml, locations.yaml, industries.yaml
config/jds/       your vacancies
storage/          database, PDF library, inbox, exports, logs  (gitignored)
scripts/          daily.sh, install_schedule.sh, seed_demo.py
tests/            128 tests (the browser ones use a local stand-in for LinkedIn)
```

Adding an intake route means writing one class in `sources/` with an
`available()` and a `fetch()`. Dedupe, scoring, PDF filing and querying are
source-agnostic and need no changes.

---

## Tests

```bash
python3 -m pytest tests/ -q        # 128 tests; the browser ones take ~2 minutes
```

They run against a throwaway storage tree, so they never touch your real
database. Worth running after you edit the taxonomy or the weights.

---

## Things worth knowing

- **PDF parsing is good, not perfect.** Both LinkedIn's export layout and
  ordinary CVs parse cleanly in testing, but CV formats vary endlessly.
  Anything the parser is not confident about goes to
  `storage/pdf_inbox/needs_review/` instead of being stored as a bad record,
  and low-confidence parses are noted in the run log.
- **Scores are comparable across days**, because the whole pool is rescored
  each run against one shared text model. `--only-new` skips that if you ever
  have enough candidates for it to matter.
- **Dedupe keys on LinkedIn slug, then email, then name + city + employer.**
  Two different people with the same name at different employers stay separate;
  the same person seen via a PDF today and a CSV tomorrow merges.
- **Performance**: 45 candidates × 2 vacancies scores in well under a second.
  Thousands of candidates and a dozen vacancies is a minute or two - fine for a
  nightly job.
