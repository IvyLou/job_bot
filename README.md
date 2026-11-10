# job_bot

A job-search CRM that runs on your own Mac. It treats a job search like a sales pipeline: every company is a lead,
whether or not it's hiring, and the hard part is reaching the right person. Each morning it:

- **builds a list of companies** from the YC directory, VC and accelerator portfolios, SEC Form D filings (startups
  that just raised), Hacker News "Who is hiring?" threads and your own hand-picked list;
- **pulls open roles** from public job-board APIs (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee,
  Workday) and scores them against the roles and locations you're after;
- **reads each posting** to flag priority matches: roles asking for your years of experience that name a background
  you have;
- **finds who to contact** by company size (a founder at a startup, the team's leader at a growth company, the hiring
  manager and recruiter at a big one), learns each company's email format, and drafts the message with the results
  from your resume that fit the role;
- **tracks the pipeline** in a local dashboard: stages, follow-up reminders, daily targets, and reply rates by
  channel and template, with read-only Gmail sync.

It's rule-based (no LLM APIs) and uses only free, public data sources. Your companies, contacts, messages and
progress stay on your machine.

## Setup

Needs macOS (for the scheduled refresh and the Keychain) and Python 3.9+.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir local && cp config/*.yaml local/   # your own settings, edited below
.venv/bin/python -m jobsearch daily       # first run: builds the company list and pulls roles (10-30 minutes)
.venv/bin/python -m jobsearch install-schedule   # refresh every morning, keep the dashboard running
```

Then open **http://localhost:8501**.

## Your settings

`config/` holds a working example (someone looking for analytics roles in the SF Bay Area or remote). Copy any file to
`local/` and edit it there: a file in `local/` replaces the example, and `local/` is never committed. Edits show up on
the next command or page load.

| File | What it sets |
|---|---|
| `search.yaml` | Your locations (with a pattern for each), company categories, role families and their weights, titles to exclude, Form D filters, Workday search words, and the priority-match rules. Run `rank` after editing to re-score stored roles |
| `crm.yaml` | Your name and links, daily targets, company-size thresholds, the teams you'd join (whose leader to contact), follow-up timing, an optional countdown, refresh time |
| `pitches.yaml` | The results from your resume each message leads with, one version per kind of role, and which version goes with which role or company |
| `templates.yaml` | The messages: founder outreach, after applying, recruiter, networking, warm intro, two follow-ups |
| `seeds.yaml` | Companies you want to track no matter what, by category |
| `vcs.yaml` | VC funds and accelerators whose portfolios to pull |

The SEC requires a name and contact email in the User-Agent for Form D data (`http.sec_user_agent` in
`search.yaml`). Requests stay under its limit of 10 per second.

## Dashboard

| Page | What it's for |
|---|---|
| Today | Progress against daily targets, follow-ups due, priority matches, the next companies to contact (each with a drafted message, a Gmail compose link or LinkedIn profile, and one-click logging), and new roles since yesterday |
| Pipeline | Every company by stage, with filters. Select a row to open the company |
| Company | Details, stage and notes, ranked contacts with drafts, adding or editing a contact, open roles, logging an application, and the activity history |
| Open roles | Every matching role, with filters. Pick one to apply and reach out |
| Results | Funnel, weekly activity and reply rates (by channel, template, tier, category or source) |

- **Apply, then reach out:** every outreach box first lists the company's matching postings (open the posting, log
  the application), then the person to write to, chosen by company size:
  - **Up to 50 people:** the CEO or a founder.
  - **Up to 300:** the leader of the team the role sits in (`teams` in `crm.yaml`).
  - **Bigger, or a hand-picked company:** both the hiring manager and the recruiter. If they aren't known yet, the
    box says who to look for (using the posting's reporting line when it states one) and links to LinkedIn's copy of
    the job ("Meet the hiring team") and a people search. Paste the name and profile into the quick-add.
- **Stages:** Not started → Reached out → In conversation → Applied / Referred → Recruiter screen → Interviewing →
  Offer → Closed. Logging an outreach, reply, application or interview moves a company forward automatically.
- **Follow-ups:** reminders come up 4 days and then 10 days after the first message, until a reply
  (`follow_up_after_days`).
- **Drafts:** any [bracketed] text left in a draft is flagged in orange to fill in before sending. Nothing is ever
  sent for you.
- **Safe from refreshes:** your stages, notes and activity log are never touched by the daily refresh, and the
  database is backed up daily to `data/backups/` (14 days are kept).

The refresh runs at 7:00 (`daily_refresh` in `crm.yaml`), or when the Mac wakes if it was asleep, after waiting for
the network. A step that fails (a site or Gmail unreachable) is logged in `data/logs/daily.log` and skipped.

## Gmail sync (read-only)

Once connected, each refresh (or "Sync Gmail now" in the sidebar) logs the emails you send to anyone at a company in
the database, logs replies (moving the company to In conversation), confirms the addresses people reply from, and
flags guessed addresses that bounce. Emails you already logged by hand aren't counted twice.

It connects over IMAP with a Gmail **app password**, with no Google Cloud setup:
1. Turn on 2-Step Verification for your Google account (app passwords require it).
2. Create an app password at **myaccount.google.com/apppasswords**.
3. Run `.venv/bin/python -m jobsearch gmail-connect` and paste it at the hidden prompt. It's checked against Gmail,
   then saved in the macOS Keychain, not in a file.

The mailbox is opened read-only, so nothing in Gmail changes. To stop, run `gmail-disconnect` and revoke the password
on the same Google page.

## Commands

`.venv/bin/python -m jobsearch <command>`:

| Command | What it does |
|---|---|
| `daily` | The whole pipeline, then writes `data/exports/pipeline_<date>.xlsx` |
| `ingest seeds\|yc\|vc\|formd\|hn\|all` | Pull companies from a source |
| `detect-ats [--recheck]` | Find each company's job system from its site, falling back to slug probes |
| `crawl-yc` | Founders (with LinkedIn) and YC-hosted jobs from YC company pages |
| `fetch-jobs` | Pull open roles from every supported job system |
| `fetch-descriptions` | Pull the full text of open roles whose listing doesn't include it |
| `formd-founders` | Look up officers on EDGAR (by exact company name and state) for startups with no named people |
| `find-domains` | Find websites for companies known only by name (Form D filers) |
| `harvest-emails [--recheck]` | Collect emails each company publishes on its own site; learn its email format |
| `contacts` | Rank who to contact at each company; fill in best-guess emails for the top contacts |
| `import-linkedin <Connections.csv>` | Mark warm paths from your LinkedIn connections export |
| `rank` | Score roles and assign company tiers |
| `report` / `export` | Console summary / Excel workbook (Outreach, Roles, Companies, People) |
| `gmail-connect` / `gmail-disconnect` / `gmail-sync` | Gmail sync, above |
| `install-schedule` / `uninstall-schedule` | Turn the morning refresh and the always-on dashboard on or off (launchd) |
| `dashboard` | Run the dashboard in the current terminal instead |
| `backup` | Back up the database now |

## Sources

| Source | Gives | Access |
|---|---|---|
| `seeds.yaml` | Your hand-picked companies | Your list |
| `vcs.yaml` | Every portfolio company in your locations, for funds whose job board runs on Getro | Getro API |
| YC directory (yc-oss mirror) | YC companies in your locations or fully remote | Free JSON |
| YC company pages | Founders with LinkedIn, YC-hosted job posts | Public pages (robots.txt allows) |
| SEC Form D | Companies that just raised (C-corps, ≥$1M sold by default) and their named officers | Quarterly bulk files + daily EDGAR search |
| HN "Who is hiring?" | Startups hiring in your locations this month | Algolia API |
| Job systems | Open roles and their full text | Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Workday |

No LinkedIn scraping or automation: LinkedIn appears only as links you open yourself.

## Contacts

The Outreach sheet in the export has one row per company: the approach (apply + reach out, founder outreach, or
network in), open roles, what the company does, its investors and latest raise, and the two best contacts with their
LinkedIn and email.

- **Who:** people you know first, then the leaders in `first_contacts` (`crm.yaml`), then the CEO, other founders,
  other executives, and last, board members (usually investors).
- **Email basis:**
  - `published`: found on the company's site
  - `given`: from you or your LinkedIn export
  - `pattern`: the company's format, learned from a named person's published email
  - `guess`: the most common format for a company of that size

  Guesses are only made when the domain accepts email. Verify `guess` addresses before sending anything important.
- **Warm paths:** export your connections from LinkedIn (Settings > Data privacy > Get a copy of your data >
  Connections), then run `import-linkedin path/to/Connections.csv`.

## Priority matches

Roles in your priority families or titles, in your priority locations, whose posting

- asks for **your years of experience**: the posting's range overlaps yours (with 1–3: 0–3, 2+ and 3–5 count, 4–6
  doesn't), using its highest required minimum and ignoring lines marked preferred or "a plus"; and
- names **a background you have** near words such as "experience", "background", "former" or "prior".

They're read from each posting's text by rule (`jobsearch/requirements.py`, rules under `priority_match` in
`search.yaml`). They get their own section at the top of Today, a ★ and a filter on Open roles, and put their company
in Tier 1. Every box quotes the lines it matched, so a misread is quick to spot. Postings that state no years don't
qualify.

## Tiers

Tiers order the outreach by fit. Everyone gets contacted, starting with Tier 1.

- **1**: a priority-match role, an open role scoring 85+, or a company in your home location (the first in
  `locations`) and a priority category where a founder or leader is known.
- **2**: anywhere in your locations, or any matching open role.
- **3**: everything else.

Set `tier_locked = 1` on a company to keep a manual tier.

## What stays private

`local/` (your settings) and `data/` (the database with your companies, contacts, roles and activity, plus exports,
backups and logs) are in `.gitignore`. The Gmail app password lives in the macOS Keychain.

## Not yet built

VC funds on other platforms (Consider, custom sites), and iCIMS / Oracle / SuccessFactors job systems.
