# Deploying Talent Pool: GitHub + Railway + the AI agent

## What runs where

```
 your PC (office)                         Railway (cloud)                    Groq (free AI)
 ┌──────────────────────┐   profiles     ┌──────────────────────────┐  JD + anonymous  ┌───────────┐
 │ LinkedIn collector   │ ─────────────▶ │ the portal               │  profile text    │ Llama     │
 │ (visible Chrome/Edge │  (INGEST_TOKEN)│  logins, HODs, vacancies │ ───────────────▶ │ model     │
 │  with YOUR login)    │ ◀───────────── │  rubric scoring          │ ◀─────────────── │ (agent)   │
 └──────────────────────┘   vacancies    │  AI agent + activity log │  score + reasons └───────────┘
                                         │  /data = volume (DB, PDFs)│
        PDFs / CSVs ───── Import tab ──▶ └──────────────────────────┘ ◀── HODs log in from anywhere
```

* **Railway runs the portal** (everything you see in the browser). It has no screen, so it **cannot** drive LinkedIn - and a data-centre IP logging into your LinkedIn would get the account restricted fast. That is why LinkedIn sourcing stays on **your PC** as the *collector*, which sends what it reads to the portal.
* **Profiles can also come in without the collector**: upload LinkedIn "Save to PDF" files, CVs or spreadsheets in the **Import** tab. The portal reads, scores and the agent reviews them.
* **The AI agent** reads each shortlisted profile against the vacancy and gives its own score and reasons (blended with the rubric at the share you choose). Everything it does shows live in **Agent activity**.

## 0. Accounts you need

| Account | Why | Cost |
|---|---|---|
| GitHub | holds the code | free (use a **private** repo) |
| Railway | runs the portal 24/7 | about USD 5/month (check railway.com/pricing; volumes may need a paid plan) |
| Groq | the free AI agent | free tier with limits (console.groq.com) |

## 1. Put the code on GitHub

1. github.com → **New repository** → name `talent-pool` → **Private** → do not add a README (the project has one) → Create.
2. On your PC, in the project folder (the one with `Dockerfile`):

```bash
git init
git add .
git status          # CHECK: no storage/, no *.db, no settings.yaml, no .env in the list
git commit -m "Talent Pool"
git branch -M main
git remote add origin https://github.com/<your-username>/talent-pool.git
git push -u origin main
```

`.gitignore` already keeps your database, PDFs, personal settings and keys out. **Never** commit a `.env` or an API key. If you did by mistake, delete the key at its provider and make a new one - removing the file from git is not enough.

## 2. Get the AI agent key (Groq)

1. console.groq.com → sign up → **API Keys** → **Create API Key** → copy it (starts with `gsk_`). Keep it for step 3.
2. Free-tier limits change; look at **console.groq.com → Settings → Limits** for your model's requests-per-minute and tokens-per-day. One profile read is roughly 1,500-2,500 tokens, so a free daily allowance covers a limited number of profiles. The agent waits out rate limits by itself (you will see "Groq rate limit reached - waiting…" in the log) and never re-reads a profile it already scored.
3. Start with the default model. If you run out of daily tokens, switch the model in **Agent activity → Agent settings** to a smaller one (for example `llama-3.1-8b-instant`): more profiles per day, slightly less careful reading. The model list at console.groq.com/docs/models is the source of truth - names change over time, and the app tells you plainly if a name is no longer valid.
4. **What leaves your server:** the vacancy text and a profile summary (roles, skills, education, certifications, projects). **Not** the name, email, phone number or LinkedIn address. Read Groq's current data policy before sending real candidates, and keep the agent off if your company says no.

## 3. Deploy on Railway

1. railway.com → **New Project** → **Deploy from GitHub repo** → authorise GitHub → pick `talent-pool`.
   Railway finds the `Dockerfile` and builds it (the first build takes a few minutes).
2. **Add a Volume** (service → *Settings* → *Volumes* → *Add volume*) and set the **mount path to `/data`**.
   *Without this the database and every PDF vanish on each redeploy.*
3. Service → **Variables** → add (see `.env.example`):

   | Variable | Value |
   |---|---|
   | `ADMIN_PASSWORD` | a long password for the `admin` login |
   | `GROQ_API_KEY` | the key from step 2 |
   | `INGEST_TOKEN` | a long random string (only if you will use the collector) |

   Do **not** set `PORT`; Railway sets it. `SOURCER_ENV=production` and `SOURCER_DATA_DIR=/data` are already in the Dockerfile.
4. Service → *Settings* → *Networking* → **Generate Domain**. You get `https://<name>.up.railway.app`.
   Custom domain later? Add it in Railway and also set `SOURCER_ALLOWED_HOSTS=talent.yourcompany.com`.
5. Wait for the deploy to turn green (Railway checks `/healthz`). Open the domain → sign in as `admin` / your `ADMIN_PASSWORD`.

If you forget `ADMIN_PASSWORD`, the first start prints a random one in the **Deploy logs** (`=== FIRST START … ===`). Locked out later? Set `RESET_ADMIN_PASSWORD=<new password>`, redeploy once, then delete that variable.

## 4. First-time setup in the portal (about 15 minutes)

1. **Org structure** → *Paste structure*: `Technology: Data Analyst, Data Engineer` (one department per line).
2. **Users** → add a login for each HOD and tick their departments.
3. **Companies** → add your target-company lists; **Vacancies** → *Download Excel template*, fill it, *Bulk add*.
4. **Agent activity** → *Agent settings* → Provider **Groq** → Save → **Test connection**. It must say *Working*. Then **Import** a few PDFs and watch the log: Extracting → Parsing → Saving → Scoring → AI agent.

## 5. The LinkedIn collector on your PC

Same project folder on your PC (Python + `pip install -r requirements.txt`). Use a separate data folder so it does not mix with anything else:

```bat
set SOURCER_DATA_DIR=collector_data
python -m sourcer ui
```
In the app: **LinkedIn → Log in to LinkedIn** (a normal Chrome/Edge opens; "Continue with Google" works; close it afterwards). Then, whenever you want to source:

```bat
set SOURCER_DATA_DIR=collector_data
set INGEST_TOKEN=<the same value you put on Railway>
python -m sourcer collect --url https://<name>.up.railway.app
```
It pulls the portal's open vacancies, sources LinkedIn with the usual slow pacing, and sends every profile with its PDF. The portal scores them and the agent starts by itself. PC off = no sourcing (see README → "mere pc band hoga").

## 6. Updating, backups, costs

* **Update:** change code → `git push` → Railway redeploys by itself. Data on `/data` is kept.
* **Backup:** Users tab → **⬇ Backup** (database + vacancies + settings) or **⬇ Backup with PDFs**. The ZIP holds personal data and password hashes: store it somewhere private.
* **Logs:** Railway → *Deployments* → *View logs* shows the server; the portal's **Agent activity** tab shows what the pipeline and agent are doing.
* **Cost:** Railway about USD 5/month for one small service plus a volume; Groq free tier USD 0.

## 7. If something is wrong

| You see | Cause / fix |
|---|---|
| Deploy fails at the healthcheck | open *Deploy logs*; usually a typo in a variable. `/healthz` must return `{"ok":true}` |
| Everything is empty after a redeploy | the Volume is not mounted at `/data` (step 3.2) |
| `403` when opening the site | you opened a domain the app does not know: set `SOURCER_ALLOWED_HOSTS` |
| Can't log in over `http://` | production cookies are HTTPS-only; use the `https://` address |
| Agent tab says "GROQ_API_KEY is not set" | add the variable, redeploy |
| Test connection: "Groq refused the key" | key wrong/revoked: make a new one |
| Log says "Groq rate limit reached" | free tier; the agent waits and continues. Use a smaller model or fewer *Profiles per vacancy* |
| Collector: "the portal has no INGEST_TOKEN set" | add `INGEST_TOKEN` on Railway and redeploy |
| Collector: "refused the token" | the value on your PC differs from Railway's |

## Repository layout

```
talent-pool/
├── Dockerfile                  cloud image (gunicorn, one worker)
├── railway.json                Railway build/health-check settings
├── requirements-cloud.txt      what the cloud needs (no browser automation)
├── requirements.txt            what your PC needs (adds Playwright)
├── .env.example                every variable the cloud understands
├── .gitignore  .dockerignore
├── README.md                   how the whole system works
├── DEPLOY.md                   this guide
├── 1_install.bat  2_open_app.bat  3_schedule_daily.bat  daily_run.bat    Windows helpers (PC / collector)
├── config/
│   ├── settings.example.yaml   copy to settings.yaml for local tweaks
│   └── jds/                    sample vacancy files (local mode)
├── scripts/                    macOS/Linux schedulers
├── sourcer/                    the application
│   ├── web/app.py              Flask app: login, API, ingest, backups, security
│   ├── web/static/             index.html (portal UI), login.html, brand/ (Dvago logos)
│   ├── agent.py                the AI agent (Groq / Claude), JSON parsing, rate-limit handling
│   ├── events.py               the live "Agent activity" log
│   ├── runner.py               pipeline: read → save → score → agent
│   ├── scoring.py  semantic.py the explainable rubric + text similarity
│   ├── rules.py                departments, company lists, auto search titles
│   ├── jd.py  models.py  store.py  db.py  config.py  normalize.py   vacancies, data model, storage
│   ├── collector.py            LinkedIn-on-your-PC → portal
│   ├── linkedin_browser.py     browser control, one-time login
│   ├── sources/                pdf_inbox, csv, linkedin_browser, …
│   ├── pdfstore.py  report.py  PDF reading/writing, exports
│   └── data/                   skills, titles, locations, industries taxonomies
└── tests/                      automated tests (pytest)
```
