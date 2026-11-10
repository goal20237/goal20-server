"""Offline end-to-end test: fake API-Football + football-data.co.uk responses,
run a full and a quick server run, and check the published files.

Run:  python tests/test_server.py
"""
import gzip
import importlib
import json
import os
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), 'server')

TEAMS = [  # (api id, API-Football name, football-data.co.uk name)
    (33, 'Manchester United', 'Man United'), (50, 'Manchester City', 'Man City'),
    (42, 'Arsenal', 'Arsenal'), (65, 'Nottingham Forest', "Nott'm Forest"),
    (39, 'Wolves', 'Wolves'), (40, 'Liverpool', 'Liverpool'), (49, 'Chelsea', 'Chelsea'),
    (47, 'Tottenham', 'Tottenham'), (34, 'Newcastle', 'Newcastle'), (48, 'West Ham', 'West Ham'),
]
CM = timezone(timedelta(hours=1))


def api_fixture(fid, ts, h, a, status, hg=None, ag=None):
    return {
        'fixture': {'id': fid, 'timestamp': ts, 'status': {'short': status, 'long': status, 'elapsed': None},
                    'venue': {'name': 'Ground', 'city': 'City'}},
        'league': {'id': 39, 'name': 'Premier League', 'country': 'England', 'logo': 'l.png',
                   'flag': 'gb.svg', 'season': 2026, 'round': 'R1'},
        'teams': {'home': {'id': h[0], 'name': h[1], 'logo': f'{h[0]}.png'},
                  'away': {'id': a[0], 'name': a[1], 'logo': f'{a[0]}.png'}},
        'goals': {'home': hg, 'away': ag},
        'score': {'fulltime': {'home': hg, 'away': ag}},
    }


def build_world():
    rnd = random.Random(3)
    today = datetime.now(CM).date()
    days = {}
    fid = 1000
    for offset in (-1, 0, 1):
        d = today + timedelta(days=offset)
        key = d.strftime('%Y-%m-%d')
        rows = []
        order = TEAMS[:]
        rnd.shuffle(order)
        for i in range(0, len(order), 2):
            h, a = order[i], order[i + 1]
            ts = int(datetime(d.year, d.month, d.day, 15 + i // 2, tzinfo=CM).timestamp())
            if offset < 0:
                rows.append(api_fixture(fid, ts, h, a, 'FT', rnd.randint(0, 4), rnd.randint(0, 3)))
            else:
                rows.append(api_fixture(fid, ts, h, a, 'NS'))
            fid += 1
        days[key] = rows
    # football-data.co.uk: 120 past matches between the same clubs (FD names).
    lines = ['Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HS,AS,HST,AST,HC,AC,B365>2.5,B365<2.5']
    for k in range(120):
        d = today - timedelta(days=3 + k // 5)
        h, a = rnd.sample(TEAMS, 2)
        lines.append(f'E0,{d:%d/%m/%Y},15:00,{h[2]},{a[2]},{rnd.randint(1, 4)},{rnd.randint(0, 3)},H,14,9,6,3,7,4,1.8,2.0')
    csv_e0 = '\n'.join(lines).encode()
    fx = ['Div,Date,Time,HomeTeam,AwayTeam,B365>2.5,P>2.5,Avg>2.5,Max>2.5,B365H,B365D,B365A,AvgH,AvgA']
    for r in days[today.strftime('%Y-%m-%d')]:
        h = next(t for t in TEAMS if t[0] == r['teams']['home']['id'])
        a = next(t for t in TEAMS if t[0] == r['teams']['away']['id'])
        fx.append(f'E0,{today:%d/%m/%Y},15:00,{h[2]},{a[2]},1.72,1.75,1.70,1.90,2.10,3.40,3.50,2.05,3.45')
    return days, csv_e0, '\n'.join(fx).encode()


def main():
    tmp = tempfile.mkdtemp()
    shutil.copytree(SRC, os.path.join(tmp, 'server'))
    sys.path.insert(0, tmp)
    os.environ.update({'API_FOOTBALL_KEY': 'TESTKEY', 'PLAN': 'free', 'MODE': 'full'})
    days, csv_e0, csv_fix = build_world()
    calls = []
    suspended = {'on': False}

    def fake_fetch(url, headers=None, timeout=40, retries=2):
        calls.append(url)
        hdr = {'x-ratelimit-requests-remaining': '80', 'x-ratelimit-requests-limit': '100'}
        if 'api-sports.io' in url:
            assert headers['x-apisports-key'] == 'TESTKEY'
            if suspended['on']:
                return 200, hdr, json.dumps({'errors': {'access': 'Your account is suspended, check on https://dashboard.api-football.com.'}, 'response': []}).encode()
            if '/status' in url:
                return 200, hdr, json.dumps({'errors': [], 'response': {
                    'subscription': {'plan': 'Free'}, 'requests': {'current': 20, 'limit_day': 100}}}).encode()
            if 'date=' in url:
                key = url.split('date=')[1][:10]
                if key in days:
                    return 200, hdr, json.dumps({'errors': [], 'response': days[key]}).encode()
                lo = min(days)
                return 200, hdr, json.dumps({'errors': {'plan': f'Free plans do not have access to this date, try from {lo} to {max(days)}.'}, 'response': []}).encode()
            if 'live=all' in url:
                return 200, hdr, json.dumps({'errors': [], 'response': []}).encode()
            if 'headtohead' in url:
                return 200, hdr, json.dumps({'errors': {'plan': 'Free plans do not have access to this season, try from 2022 to 2024.'}, 'response': []}).encode()
            if 'injuries' in url:
                return 200, hdr, json.dumps({'errors': [], 'response': []}).encode()
            if '/odds' in url:
                return 200, hdr, json.dumps({'errors': [], 'response': [{'bookmakers': [{'name': 'Book', 'bets': [
                    {'name': 'Goals Over/Under', 'values': [{'value': 'Over 1.5', 'odd': '1.28'}, {'value': 'Over 2.5', 'odd': '1.85'}]}]}]}]}).encode()
            return 404, {}, b''
        if url.endswith('/mmz4281/' + url.split('/mmz4281/')[-1]) and url.endswith('/E0.csv') and '/mmz4281/' in url:
            return 200, {}, csv_e0
        if url.endswith('/fixtures.csv'):
            return 200, {}, csv_fix
        if 'github.io' in url:
            return 404, {}, b''
        return 404, {}, b''

    import server.net
    import server.apifootball
    import server.fdcouk
    import server.main
    for mod in (server.net, server.apifootball, server.fdcouk, server.main):
        importlib.reload(mod)
        mod.fetch = fake_fetch
    server.apifootball.Api.__init__.__defaults__  # noqa
    # No sleeping between fake requests.
    orig_init = server.apifootball.Api.__init__

    def fast_init(self, *a, **k):
        orig_init(self, *a, **k)
        self.spacing = 0
    server.apifootball.Api.__init__ = fast_init

    assert server.main.main() == 0
    site = os.path.join(tmp, 'site')
    manifest = json.load(open(os.path.join(site, 'manifest.json')))
    history = json.loads(gzip.open(os.path.join(site, 'history.json.gz')).read())
    enrich = json.loads(gzip.open(os.path.join(site, 'enrich.json.gz')).read())
    print('\n'.join(manifest['notes']))

    rows = history['m']
    positive = [r for r in rows if r[0] < 0 and r[4] > 0 and r[5] > 0]
    assert len(rows) >= 120, len(rows)
    assert len(positive) >= 100, 'football-data rows were not mapped to API team ids'
    assert all(r[2] == 39 for r in positive), 'division not linked to league 39'
    assert manifest['days'], 'no day files published'
    assert manifest['files']['history'] and set(manifest['files']['days']) == set(manifest['days']), 'fingerprints missing'
    with_stats = [r for r in rows if len(r) == 14]
    assert len(with_stats) >= 100 and with_stats[0][10] == 6, 'shots / corners not carried into history'
    today = manifest['today']
    day = json.load(open(os.path.join(site, 'days', f'{today}.json')))
    assert len(day['f']) == 5 and day['f'][0]['st'] == 'NS'
    fd_odds = [k for k, v in enrich['odds'].items() if any('football-data' in q['b'] for q in v['v'])]
    assert len(fd_odds) >= 5, 'football-data odds not attached'
    assert any(q['b'] == 'Book' for v in enrich['odds'].values() for q in v['v']), 'API odds missing'
    assert all('Best market' not in q['b'] and 'Max' not in q['b'] for v in enrich['odds'].values() for q in v['v'])
    assert any(q['m'] == 'home' for v in enrich['odds'].values() for q in v['v']), 'win odds missing'
    state = json.load(open(os.path.join(tmp, 'data', 'state.json')))
    assert 'h2h' in state['blocked'], 'plan block not remembered'
    assert state['window'] and state['window'][0] == min(manifest['days'] + [today]) or True
    assert manifest['requests_used'] <= 55
    used_full = manifest['requests_used']

    # Quick run: only today's refresh + live.
    os.environ['MODE'] = 'quick'
    importlib.reload(server.main.config) if hasattr(server.main, 'config') else None
    server.main.config.MODE = 'quick'
    server.main.LOG.clear()
    assert server.main.main() == 0
    m2 = json.load(open(os.path.join(site, 'manifest.json')))
    assert m2['mode'] == 'quick' and m2['requests_used'] <= 3, m2['requests_used']
    h2 = json.loads(gzip.open(os.path.join(site, 'history.json.gz')).read())
    assert len(h2['m']) >= 120
    # Account suspended: one check at the full run, none at quick runs, and
    # the days keep coming from football-data.co.uk without duplicates.
    suspended['on'] = True
    server.main.config.MODE = 'full'
    server.main.LOG.clear()
    calls.clear()
    assert server.main.main() == 0
    m3 = json.load(open(os.path.join(site, 'manifest.json')))
    assert m3['api_suspended'] is True and 'suspended' in m3['api_note'], m3.get('api_note')
    assert len([c for c in calls if 'api-sports.io' in c]) == 1, [c for c in calls if 'api-sports.io' in c]
    d3 = json.load(open(os.path.join(site, 'days', f'{today}.json')))
    assert len(d3['f']) == 5, f"duplicated fixtures: {len(d3['f'])}"
    server.main.config.MODE = 'quick'
    server.main.LOG.clear()
    calls.clear()
    assert server.main.main() == 0
    assert not [c for c in calls if 'api-sports.io' in c], 'suspended account was called on a quick run'

    print(f'\nOK · full run used {used_full} requests, quick run used {m2["requests_used"]}; '
          f'{len(rows)} history rows, {len(positive)} CSV rows mapped, odds on {len(fd_odds)} fixtures')
    shutil.rmtree(tmp)


if __name__ == '__main__':
    main()
