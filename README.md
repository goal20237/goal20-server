# GOAL20 server (free, runs on GitHub)

This repository is your own GOAL20 server. GitHub runs it on a schedule, free of charge, even when your phone is off.

- **06:07 Cameroon time, full run:**
  1. Stores yesterday's results and loads today's and tomorrow's fixtures from API-Football.
  2. Downloads free current-season results and Over 2.5 odds from football-data.co.uk.
  3. Matches the team names between the two sources.
  4. Checks injuries and odds (and head-to-head on paid plans) for the most promising matches.
  5. Publishes everything.
- **Every hour, 08:41–23:41, quick run:** refreshes today's fixtures and live scores (about 2 API requests).

The GOAL20 app reads the published files, so it needs no API key on the phone.

Free plan usage: about 25 requests for the full run plus about 2 per hourly run, roughly 60 of your 100 daily requests.

---

## Setup (one time, about 15 minutes)

### 1. Accounts and tools
- A free GitHub account: https://github.com/signup
- Git for Windows: https://git-scm.com/download/win (install with the default options)
- Your API-Football key: https://dashboard.api-football.com, under Account → My Access

### 2. Create the repository
On github.com, click **+** (top right) → **New repository**:
- Repository name: `goal20-server`
- Select **Public**. This keeps GitHub Actions and Pages free. Your key stays secret; only the football data is public.
- Don't add a README.
- Click **Create repository**.

### 3. Upload this code (Command Prompt)

Replace `YOURNAME` with your GitHub username:

```
cd /d "%USERPROFILE%\Downloads"
mkdir GOAL20_server
tar -xf GOAL20_server_v1.0.0.zip -C GOAL20_server
cd /d "%USERPROFILE%\Downloads\GOAL20_server\goal20-server"
git init
git add .
git commit -m "GOAL20 server"
git branch -M main
git remote add origin https://github.com/YOURNAME/goal20-server.git
git push -u origin main
```

If Git asks who you are, run these two lines, then the `git commit` and `git push` lines again:

```
git config --global user.name "YOURNAME"
git config --global user.email "you@example.com"
```

The first push opens a browser window so you can sign in to GitHub.

### 4. Add your API key as a secret
In the repository: **Settings → Secrets and variables → Actions → New repository secret**
- Name: `API_FOOTBALL_KEY`
- Secret: paste your key
- Click **Add secret**.

If you have a paid plan, open the **Variables** tab → **New repository variable**, with name `PLAN` and value `pro`.

Free data sources (each optional, each its own secret; never share a key):
- `FOOTBALL_DATA_ORG_KEY`: football-data.org, 12 top competitions, results the same day.
- `FOOTBALL_CHARTS_KEY`: football-charts.com, about 80 more leagues (lower divisions, Africa, Asia),
  fixtures and results once a day. Free key: 200 requests a day for personal use; GOAL20 uses at
  most 185. Credit: "Data by football-charts.com". The cache is kept in `data/fc/`.
- `ODDSPAPI_KEY`: OddsPapi, betPawa Cameroon's real prices. Free plan: 250 requests a month;
  GOAL20 reads at most 7 a day (3 leagues each) for the busiest leagues and keeps 6 spare.
  Where a betPawa price exists, it replaces other bookmakers' price for that market.

### 5. Turn on the website (GitHub Pages)
**Settings → Pages → Build and deployment → Source: GitHub Actions**

### 6. First run
1. Open the **Actions** tab. If GitHub asks, click "I understand my workflows, go ahead and enable them".
2. Click **GOAL20 server** → **Run workflow** → mode `full` → **Run workflow**.
3. Wait 3–6 minutes for the green tick.

Then open `https://YOURNAME.github.io/goal20-server/`. It should say **"GOAL20 server is running"**.

### 7. Connect the app
In GOAL20: **Settings → GOAL20 server**:
1. Paste `https://YOURNAME.github.io/goal20-server/`.
2. Tap **Connect server**.

From then on, the app syncs every time it opens.

---

## Troubleshooting

- **The red ✗ is on "Save collected data":** go to Settings → Actions → General → Workflow permissions, choose **Read and write permissions**, save, and run the workflow again.
- **The red ✗ is on "deploy-pages":** check step 5 (Pages source must be GitHub Actions).
- **"No API_FOOTBALL_KEY secret":** check step 4. The name must be exactly `API_FOOTBALL_KEY`.
- **You want to see what happened:** open the last run in the Actions tab, or the status page. The last 40 log lines are on both.
- **GitHub schedules start late:** they can run 5–30 minutes after the set time. That's normal.

## Anti-ban cache, predictions and tickets (server 1.7-1.8)

- **Phones never call a football API.** Every phone only downloads the static
  files this server publishes on GitHub Pages, so 1 user or 10,000 users cost
  the same: zero extra API requests.
- **football-data.org at most every 6 hours, 4 times a day.** `server/data_fetcher.py`
  is the only code that calls it. Between fetch cycles every run is served from
  the SQLite cache `data/cache/goal20.sqlite` (kept between GitHub runs with
  actions/cache, never committed). Standings: once a day.
- **Predictions.** `server/prediction_engine.py` (Dixon-Coles + bivariate Poisson
  with home/away form) writes `predictions/<day>.json` for yesterday, today and
  tomorrow: each match is `PICK` (the single safest of 12 markets at 70%+) or
  `SKIPPED_LOW_EDGE`, with 3 plain-language bullets. A prediction is frozen at
  kickoff.
- **Daily tickets (server 1.8).** `server/tickets.py` adds two tickets to each
  predictions file: **Daily Top 3 Statistical Picks** (server 1.11: 1X, X2,
  Over 1.5 or draw no bet, fair odds 1.20-1.55, one leg per match, graded
  A = 78%+ on a stable full-data match, B = 72%+ or a would-be A with higher
  human variance, C = 65%+ or a cold start; filled A, then B, then C, never
  below 65%; the card's grade is its weakest leg; 5 points off the confidence
  for cup ties, friendlies, youth sides, congestion and cold starts) and the
  **1000+ Odds Value Longshot** (1X2, BTTS & Over 2.5, team Over 1.5 with a
  real price, legs strictly by value = probability x price - 1, up to 14
  legs). Both are frozen at their first kickoff, settled, and archived in
  `data/tickets.json`; the manifest's `tickets_record` holds the results:
  Server 1.12: both tickets are previews until the morning update of their
  day (first full run from 08:00 Cameroon, any run from 11:00, or the first
  kickoff), then locked for good; a leg with no result 48 hours after
  kickoff is void. ESPN is also read league by league (about 200
  competitions, on their match days) because its combined list is
  incomplete.
  `top3` = the Grade A record (headline), `top3_grades` = one record per
  card grade (never added together), `top3_legs` = every leg by its own
  grade against the model's expectation.

### Where the keys go

- **On GitHub (the live server):** Settings → Secrets and variables → Actions.
  `FOOTBALL_DATA_ORG_KEY` is read as `FOOTBALL_DATA_API_KEY` automatically;
  you can also add a secret named `FOOTBALL_DATA_API_KEY` (it then wins).
- **On your PC (only for a local test run):** copy `.env.example` to a file
  named exactly `.env` in this folder and paste the key after
  `FOOTBALL_DATA_API_KEY=`. `.env` is in `.gitignore`: git never uploads it.
  Then run `python -m server.main`.

## Worldwide coverage (server 1.9)

- **ESPN scoreboard** (free, keyless, unofficial): national teams and about
  60 club leagues, youth and reserve competitions included, one request per
  day; days kept in `data/espn/`. Full runs
  also fill in up to 150 past days (40 per run) for competitions only ESPN
  covers. Set `GOAL20_ESPN=0` to switch it off.
- **International results** (martj42, public domain, read once a day): the
  history behind the national-team ratings, kept in `data/intl/`.
- **Cold start**: teams with few stored matches are still rated (marked
  "thin data"). The 1000+ longshot falls back to estimated fair-odds legs when
  real prices run out, and then drops the +EV badge.

## Data sources

- **API-Football:** worldwide fixtures, results, live scores, injuries, odds. Free plan: 100 requests/day; it can't read current seasons directly, which is why football-data.co.uk is added.
- **football-data.org:** fixtures, results and standings of 12 top competitions (free plan, 10 requests/minute; GOAL20 uses about 4 a day plus 12 for standings).
- **football-charts.com:** about 80 more leagues, read once a day.
- **OddsPapi:** betPawa prices, about 7 requests a day.
- **football-data.co.uk:** free CSV results for 22 European divisions plus 16 other countries (Brazil, Argentina, USA, Japan…), with Over/Under 2.5 prices for the main European divisions. Downloaded once a day.

## Files

```
server/main.py        the run (full / quick)
server/data_fetcher.py football-data.org fetcher: 6-hour, 4-a-day anti-ban gate
server/cache.py       SQLite cache (responses, cycles, ratings, frozen predictions)
server/prediction_engine.py Dixon-Coles + bivariate Poisson, 70% filter, bullets
server/tickets.py     Daily Top 3 Statistical Picks (graded A/B/C) and the 1000+ Odds Value Longshot
server/espn.py        ESPN worldwide scoreboard (national teams, ~60 leagues)
server/internationals.py national-team results and ratings pool
server/apifootball.py API-Football client: quota guard, plan-limit detection
server/fdcouk.py      football-data.co.uk results and odds
server/names.py       team-name matching between the two sources
server/rank.py        picks which matches get the deep checks
data/                 stored days, team-name map, checks (saved by the bot)
.github/workflows/    the schedule
tests/test_server.py  offline end-to-end test:  python tests/test_server.py
```

## 1.13.0
Bookmaker check on every Daily Top 3 / longshot leg (model more than 10 points above the price: refused), 40/60 model-bookmaker blend, rating prior 12, goal cap 3.5, same-city derby handling, Grade A rules (price agrees; 1X, Over 1.5, DNB 1 only), suggested stake units on every ticket, longshot "1 chance in N".
