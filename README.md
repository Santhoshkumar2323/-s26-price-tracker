# Samsung S26 Price Tracker

Tracks the 256GB listing of Galaxy S26, S26+, and S26 Ultra on Amazon US and
Amazon India. Sends an email immediately when any variant drops 15%+ below
its launch price, and a routine status email every 5 days otherwise.

## Setup

1. **Create a new GitHub repo** and add these four files:
   - `price_tracker.py`
   - `requirements.txt`
   - `.github/workflows/price_tracker.yml`
   - `tracker_state.json` (starting state — the workflow will update this automatically)

2. **Get a Gmail App Password** (not your regular Gmail password):
   - Go to your Google Account → Security → 2-Step Verification must be ON
   - Search "App Passwords" → generate one for "Mail"
   - You'll get a 16-character password — this is what goes in `GMAIL_APP_PASSWORD`

3. **Get a free Groq API key** (used only as a fallback if Amazon's page
   layout breaks the CSS selector):
   - Sign up at console.groq.com — no credit card required
   - Create an API key

4. **Add repo secrets** — in your GitHub repo: Settings → Secrets and
   variables → Actions → New repository secret. Add:
   - `GMAIL_ADDRESS` — the Gmail address you're sending from
   - `GMAIL_APP_PASSWORD` — the 16-character app password from step 2
   - `RECEIVER_EMAIL` — where you want alerts sent (can be the same as GMAIL_ADDRESS)
   - `GROQ_API_KEY` — from step 3

5. **Enable Actions** on the repo if prompted, then either wait for the
   daily schedule or trigger it manually: Actions tab → "Samsung S26 Price
   Tracker" → Run workflow.

## How it works

- Each run tries a CSS selector first (fast, no API cost). If Amazon's
  markup has changed and the selector fails, it falls back to asking a free
  Groq-hosted LLM to read the price off the page text.
- State (last heartbeat date + which variants already triggered an alert)
  is stored in `tracker_state.json`, which the workflow commits back to the
  repo after every run — this is what makes the 5-day timer and "don't
  re-alert on the same dip" logic survive between runs on GitHub's
  ephemeral runners.
- Once a variant alerts, it won't alert again until you manually clear it
  from `alerted_variants` in `tracker_state.json` (e.g. after the deal
  expires and you want to be notified again on the next dip).

## Known limitations to be aware of

- Amazon may serve CAPTCHAs or block repeated automated requests. If prices
  stop being fetched (`price: None` in the workflow logs), this is the
  most likely cause — there's no fully reliable workaround for a personal
  script beyond running it infrequently (daily, not hourly).
- The tracked URLs point to specific color/storage variants. If Amazon
  changes a listing's URL entirely, update the `url` field for that
  variant in `price_tracker.py`.
- Launch prices are hardcoded as the discount baseline. Update
  `launch_price` in `price_tracker.py` if you'd rather compare against a
  different reference point.