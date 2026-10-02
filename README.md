# Job Autopilot

A local **website** for any job seeker. The public site is **https://ajeenckya5.github.io/job_autopilot/** — resume, job titles, and location are on that first page.

To actually search boards and read mail, run the same app on your computer:

```bash
cd job-autopilot
python3 -m pip install --user -r requirements.txt
python3 autopilot.py dashboard
```

That opens **http://127.0.0.1:8787/**. First screen: upload a resume, list job titles, set location. After save: **Overview**, **Applications**, and **Mail**, plus **Find jobs** and **Sync mailbox**. **Resume & search** (top right) is where you change those answers later.

## What the app does

The search only includes jobs posted inside your lookback window whose posting overlaps with your resume and role keywords. Senior titles and postings above `filters.max_years_required` are skipped. Results land in SQLite and `jobs.xlsx` for you to apply by hand.

**Experience is a hard gate.** With `filters.max_years_required: 2`, the agent skips postings requiring `3 years`, `3+ years`, `4 years`, `4+ years`, `5-7 years`, etc. before they enter the ledger.

**LinkedIn and Indeed detail enrichment.** Those sources often return title-only search rows. When a thin LinkedIn/Indeed row has a title-only score above `behavior.detail_enrich_min_score`, the agent fetches the full posting before the final experience and score checks.

## How the website matches any field

The website has no list of skills, roles or job families. Every six hours the feed build learns them from the postings themselves and writes `feeds/lexicon.json` (`feed-builder/lexicon.py`):

- **Skills** are phrases that several employers use and that are concentrated in some kinds of job, beyond chance: "pleadings" in paralegal postings, "cdl" in driver postings, "month end close" in accounting ones. Words every kind of job uses ("experience", "team") are filler. Place names come from the location fields and are left out.
- **Rarity**: each skill phrase carries how rare it is, so a rare shared phrase counts for more than a common one.
- **Titles**: how rare each title word is, which words end titles ("accountant", "driver"), and which titles have postings that read alike ("staff accountant" and "senior accountant").

Before postings are kept, text an employer repeats across different kinds of job (its "About us", benefits and equal-opportunity paragraphs) is cut, so the 1,500 characters stored per posting describe the job.

On the site:

- Your skills are what you listed on your resume plus every learned skill phrase in it. Your past titles are read from the lines around the dates in your work history. Nothing is guessed when the resume names none.
- Role fit compares each posting's title with the titles you typed and have held, word by word. Rarer words count for more and the last word ("scientist" in "data scientist") counts twice. Titles whose postings read like yours count as related, from the lexicon or, where it knows too few postings, from the search itself: when several postings with one title ask for about as much of your resume as postings with your own titles do (manufacturing and quality engineer roles for an industrial engineer), that title is related for you.
- Skill fit is the share of a posting's most telling phrases that your resume also uses. "Most telling" means what most postings for your own titles ask for, so one employer's product names do not count as skills you lack.
- Search asks the jobs API for your titles first, then for postings that use your skills, the ones you list first. It matches whole words, so "lean" does not find "clean". Only titles and up to 12 phrases leave the browser, never the resume.
- Found roles are not kept on the device, so each visit searches again, and an open tab repeats it as often as your runs per day say.

Measured on the 330 hand-labelled postings in `site/test/fixtures/holdout` (September 2026): precision in the top 10 went from 0.67 to 0.75, precision of what is shown from 0.61 to 0.82, AUC from 0.92 to 0.93.

## Resume Match Score

The local score is ATS-inspired, not a copy of Workday or Greenhouse internals. Those systems are proprietary. The scorer mirrors common ATS behavior with transparent components:

- `25 pts` structured job scorecard / target title fit
- `25 pts` resume skill evidence from weighted skills
- `20 pts` semantic concept-vector similarity
- `15 pts` skills-cloud / ontology graph coverage
- `10 pts` technical keyword overlap between resume and JD
- `5 pts` exact target-title phrase bonus

It also applies caps for vague or wrong-family roles, for example content, sales, operations, business analyst, and generic software roles without AI/ML/robotics/quant intent. Scores are grouped into tiers: `strong` >= 75, `review` >= 55, `weak_review` >= 35, otherwise `reject`.

Everything in scrape-only mode is free.

## What LLM should I use?

xAI's Grok works, but it's not the cheapest. The agent talks to any **OpenAI-compatible** chat-completions endpoint, so swap by editing three lines in `config.yaml` under `xai:` (base_url, api_key, model). Here are the options I'd actually pick:

| Provider | Cost | Pick when |
| --- | --- | --- |
| **Google Gemini 2.0 Flash** | **Free** (generous limit) | You're cost-sensitive. Get a key at https://aistudio.google.com/app/apikey |
| **Groq** (Llama 3.3 70B) | **Free** tier, very fast | You want zero cost and don't need Google. Get a key at https://console.groq.com/keys |
| **OpenAI gpt-4o-mini** | ~$0.15 / 1M input tokens (cheap) | You want the most reliable JSON output. Key at https://platform.openai.com/api-keys |
| **DeepSeek V3** | ~$0.27 / 1M (very cheap) | Heavy use, on a budget. Key at https://platform.deepseek.com |
| **OpenRouter** | Pass-through to any model | You want to A/B test models. https://openrouter.ai |
| **xAI Grok** (current default) | ~$3 / 1M input | You already paid. |

Paste one of these blocks into your `config.yaml` to switch:

```yaml
# Google Gemini 2.0 Flash (free tier)
xai:
  base_url: "https://generativelanguage.googleapis.com/v1beta/openai"
  api_key:  "AIza..."
  model:    "gemini-2.0-flash"

# Groq (free Llama)
xai:
  base_url: "https://api.groq.com/openai/v1"
  api_key:  "gsk_..."
  model:    "llama-3.3-70b-versatile"

# OpenAI gpt-4o-mini
xai:
  base_url: "https://api.openai.com/v1"
  api_key:  "sk-..."
  model:    "gpt-4o-mini"

# DeepSeek V3
xai:
  base_url: "https://api.deepseek.com/v1"
  api_key:  "sk-..."
  model:    "deepseek-chat"

# OpenRouter (anything)
xai:
  base_url: "https://openrouter.ai/api/v1"
  api_key:  "sk-or-..."
  model:    "google/gemini-2.0-flash-exp:free"
```

The `xai:` section name is historical — keep it; only the values inside change.

## What you need

- macOS with Python 3.10+ (`python3 --version`)
- Your resume as a 1-page PDF, placed at `~/Downloads/resume.pdf` (or any path you point `resume_pdf` at)

## 5-minute install (CLI, optional)

The browser app is the main interface. These commands are optional:

```bash
python3 autopilot.py init     # same setup questions in the terminal
python3 autopilot.py scout    # same as Find jobs
python3 autopilot.py dashboard
```

## config.yaml — what to fill in

```yaml
candidate:
  name: "Your Name"
  email: "you@gmail.com"
  phone: "+1 555 555 5555"
  linkedin: "https://linkedin.com/in/you"

# THIS is your resume. The exact file. Attached as-is to every email.
resume_pdf: "~/Downloads/resume.pdf"

roles:
  - keywords: "senior backend engineer python"
    location: "Remote"
  - keywords: "platform engineer kubernetes"
    location: "United States"

behavior:
  manual_apply_only: true
```

The default `schedule_minutes: 360` runs every 6 hours. Use `1440` for daily.

## Commands

```bash
python3 autopilot.py init               # write config.yaml from template
python3 autopilot.py scout              # scrape + rank + export jobs.xlsx
python3 autopilot.py run                # same: scrape + rank + export jobs.xlsx in manual mode
python3 autopilot.py daemon             # loop forever, scrape/export only in manual mode
python3 autopilot.py status             # DB stats
python3 autopilot.py mail-sync          # scan email and update jobs.xlsx statuses
python3 autopilot.py dashboard          # local command center (mailbox + funnel)
python3 autopilot.py watch acme.com Acme    # manually pre-watch a company
python3 autopilot.py unwatch acme.com       # stop watching
python3 autopilot.py list-watched           # show watchlist
python3 autopilot.py dry <url>              # add one URL to Excel without applying
```

## Where files live

```
~/Downloads/job-autopilot/          # the project
└── (autopilot.py, core.py, sources.py, config.yaml, ...)

~/Downloads/resume.pdf              # your resume - never modified

~/Downloads/job-autopilot/          # output dir (same folder)
├── autopilot.sqlite                # state: jobs, watchlist, applications
└── logs/
    └── autopilot.log
```

## Run on a schedule without a terminal open (launchd)

```bash
cat > ~/Library/LaunchAgents/com.autopilot.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.autopilot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOU/Downloads/job-autopilot/autopilot.py</string>
    <string>daemon</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/Downloads/job-autopilot/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Downloads/job-autopilot/logs/launchd.err</string>
</dict>
</plist>
EOF

# Replace /Users/YOU/ with your actual home path, then:
launchctl load ~/Library/LaunchAgents/com.autopilot.plist
```

To stop: `launchctl unload ~/Library/LaunchAgents/com.autopilot.plist`.

## Costs (per applied job)

- **Grok**: exactly **ONE** call per job. JD extraction (with web search), resume content edits, and cold email draft all come back in one response. ~$0.03-0.10 per job depending on JD length.
- **Everything else**: $0.
- **Gmail** caps at 500 outbound/day. The `throttle_seconds` knob keeps you under.

## How the watchlist works

When the pipeline sees a relevant job at `acme.com`, it auto-marks Acme as watched. The first watched-company scan resolves one trusted careers source, then caches it in SQLite:

- Official site pages first: `https://acme.com/careers`, `https://acme.com/jobs`, and `www` variants.
- Trusted ATS fallback: Greenhouse, Lever, Workable, BambooHR, Ashby, SmartRecruiters, Jobvite, iCIMS, Breezy, Pinpoint, and Workday-hosted boards.

Future runs scan only the cached URL for that company. URLs outside the company domain or trusted ATS hosts are rejected, and duplicate job links are normalized before export.

Manually pre-watch a company before you've seen a job from them:

```bash
python3 autopilot.py watch stripe.com Stripe
```

## Tuning

| What | Where | Default |
| --- | --- | --- |
| Manual scrape-only mode | `behavior.manual_apply_only` | true |
| How fresh jobs need to be | `behavior.max_age_hours` | 24 |
| Max jobs shown in CLI per run | `max_per_run` | 5000 |
| Thin LinkedIn/Indeed detail fetch threshold | `behavior.detail_enrich_min_score` | 55 |
| LinkedIn jobs per role | `source_limits.linkedin_max_jobs_per_role` | 1000 |
| Indeed pages per role | `source_limits.indeed_max_pages_per_role` | 100 |
| Jobright pages per role | `source_limits.jobright_max_pages_per_role` | 250 |
| Watched company discovery candidates | `source_limits.watched_company_discovery_candidates` | 8, then cache one canonical URL |
| Auto-add seen companies to watchlist | `behavior.auto_watch_companies` | true |

## Mail status tracking

`python3 autopilot.py mail-sync` connects to the configured mailbox in read-only IMAP mode, classifies job-related emails, matches them to known company/title rows, updates SQLite, and re-exports `jobs.xlsx`.

It can mark `applied`, `rejected`, `assessment`, `interview`, `offer`, and `withdrawn`. Ambiguous emails are recorded in the local `mail_events` table without changing the job row.

The mailbox can be **any domain**: Gmail, Outlook / Microsoft 365, Yahoo, iCloud, school, or work mail. Auto-detect uses the address you entered; school and work domains try Microsoft 365, then Google Workspace, then `imap.` / `mail.` on that domain. Sync mailbox in the app runs the same read-only IMAP pass.

```yaml
mail_tracking:
  enabled: true
  provider: auto          # or gmail | outlook | yahoo | icloud
  username: "you@school.edu"
  app_password: "xxxx xxxx xxxx xxxx"
  mailboxes: ["INBOX"]
  interval_minutes: 60
```

## Troubleshooting

**"resume_pdf not found"** — fix the `resume_pdf:` path in `config.yaml`. Use absolute path or `~/Downloads/...`.

**"Could not extract any text from PDF"** — your PDF is image-only (scanned). Re-export your resume as text-PDF from Word/Pages/Google Docs, or run it through OCR first.

**LinkedIn returning zero jobs** — they rate-limit. The 6h cadence is fine; running `run` repeatedly in 10 minutes will see blank pages.

## Files

```
job-autopilot/
├── autopilot.py        # CLI entrypoint
├── docs/               # GitHub Pages site + local app UI
├── dashboard.py        # local web app (http://127.0.0.1:8787/)
├── setup_wizard.py     # first-run setup (browser + CLI)
├── core.py             # config, DB, ATS-style score, filters, Excel export
├── sources.py          # LinkedIn, Indeed, Jobright + optional board APIs
├── mail_tracker.py     # read-only IMAP application-status tracker (any domain)
├── imap_presets.py     # Gmail / Outlook / Yahoo / school / work IMAP hosts
├── requirements.txt
├── config.example.yaml
└── README.md
```
