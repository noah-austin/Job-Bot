# Job Bot

Finds jobs, applies to them with a real browser, and tracks every application
through to rejection, interview or offer by reading your personal Gmail.

```
discover ──▶ score ──▶ apply (browser agent) ──▶ inbox ──▶ daily digest email
 ATS feeds    Claude    logs in / signs up,        Gmail     to you
                        fills any form, uploads    triage
                        resume, verifies email
```

## How it works

| Stage | Module | Notes |
|---|---|---|
| Discover | `jobbot/discovery/` | Public JSON boards from Greenhouse, Lever and Ashby for the companies in `profile.yaml`. No scraping, no login. |
| Score | `jobbot/scoring.py` | Free keyword/location rules first, then Claude scores fit 0–100 and flags dealbreakers. Jobs ≥ `JOBBOT_MIN_SCORE` are queued. |
| Apply | `jobbot/apply/` | Claude drives Chromium (Playwright) through **any** application site: it sees the page as a numbered list of controls (plus screenshots when needed), and clicks, types, picks options, uploads the resume, signs in or creates accounts, and handles email verification. |
| Credentials | `jobbot/vault.py` | One account per site (per Workday tenant), with generated passwords encrypted using `JOBBOT_VAULT_KEY`. **Passwords never reach the model**: it asks for "the password field" and the code types it in. The browser profile is persistent, so sessions stay logged in. |
| Inbox | `jobbot/gmail/` | Your personal Gmail through its own OAuth token. It reads verification codes and links during sign-up, classifies replies (rejection, interview, assessment, offer), updates statuses and alerts you. |
| Track | `jobbot/db.py` | SQLite locally, Postgres via `DATABASE_URL`. Every job keeps an event timeline, and anything the bot won't decide alone goes to a review queue. |

### Safety rails (enforced in code)

- **Final submit gate.** With `JOBBOT_AUTO_SUBMIT=false` (the default), the agent fills the whole form and then stops at the submit button. It saves a screenshot and adds the job to your review queue. `jobbot approve <id>` submits it on the next run. Set `JOBBOT_AUTO_SUBMIT=true` for fully hands-off runs.
- **No made-up answers.** The model may only use `profile.yaml`. Questions it can't answer from your profile, CAPTCHAs, assessments and identity checks go to the review queue. Add the answer to `profile.yaml`, then run `jobbot retry <id>`.
- **Daily cap** with `JOBBOT_MAX_APPLICATIONS_PER_DAY`.

## Setup

> **This repository is public.** Your `profile.yaml`, resume, `.env` and everything in
> `data/` (database, Gmail token, browser logins) are git-ignored. Keep them that way:
> never commit them.


```bash
pip install -e '.[dev]' && playwright install chromium
cp .env.example .env              # fill in, then: set -a; source .env; set +a
cp profile.example.yaml profile.yaml   # your details, answers and target companies
jobbot vault keygen               # put the output in JOBBOT_VAULT_KEY
```

### Connect your personal Gmail (one time)

1. Go to https://console.cloud.google.com, signed in with your **personal** Google account. Create a project and enable the **Gmail API**.
2. Under **OAuth consent screen**, choose External and add your Gmail as a test user. Then click **Publish app** (to "In production"). If you skip publishing, Google expires the refresh token after 7 days. You'll see an "unverified app" warning; that's expected for a personal-use app.
3. Under **Credentials**, create an OAuth client ID of type **Desktop app** and download the JSON to `data/gmail_client_secret.json`.
4. Run `jobbot gmail auth`, open the printed URL and approve. The token is saved to `data/gmail_token.json` and refreshes itself from then on. For the server, copy that file into the server's `/data` volume.

## Use

```bash
jobbot run                 # one full cycle
jobbot run --loop          # keep going every 2 hours
jobbot discover | score | apply | inbox   # stages individually
jobbot apply --job-id 42   # one job, e.g. to watch it with JOBBOT_HEADLESS=false
jobbot status [--state queued]
jobbot review              # what's waiting on you
jobbot approve 42 | retry 42 | skip 42 | mark 42 interviewing
```

## Deploy (Railway)

The Dockerfile runs `jobbot run --loop`. Create a service from this repo, attach a volume at `/data`, and put `profile.yaml`, `resume.pdf`, `gmail_client_secret.json` and `gmail_token.json` in it. Set the variables from `.env.example`. For Postgres, add a Railway Postgres database and set `DATABASE_URL`.

## Tests

```bash
pytest
```

`tests/test_agent.py` runs a real headless Chromium against a local application form, with a scripted stand-in for Claude. It checks that the bot fills the form, uploads the resume, keeps the password out of the model's context, and enforces the submit gate.
