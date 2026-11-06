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

## Data sources

- **API-Football:** worldwide fixtures, results, live scores, injuries, odds. Free plan: 100 requests/day; it can't read current seasons directly, which is why football-data.co.uk is added.
- **football-data.co.uk:** free CSV results for 22 European divisions plus 16 other countries (Brazil, Argentina, USA, Japan…), with Over/Under 2.5 prices for the main European divisions. Downloaded once a day.

## Files

```
server/main.py        the run (full / quick)
server/apifootball.py API-Football client: quota guard, plan-limit detection
server/fdcouk.py      football-data.co.uk results and odds
server/names.py       team-name matching between the two sources
server/rank.py        picks which matches get the deep checks
data/                 stored days, team-name map, checks (saved by the bot)
.github/workflows/    the schedule
tests/test_server.py  offline end-to-end test:  python tests/test_server.py
```
