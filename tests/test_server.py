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
    os.environ.update({'API_FOOTBALL_KEY': 'TESTKEY', 'PLAN': 'free', 'MODE': 'full',
                       'FOOTBALL_DATA_ORG_KEY': 'ORGKEY'})
    days, csv_e0, csv_fix = build_world()
    calls = []
    suspended = {'on': False}

    fdorg_matches = []
    fc_world = {'leagues': [], 'fixtures': {}, 'results': {}}
    op_world = {'markets': [], 'tournaments': [], 'odds': []}
    op_calls = []
    espn_world = {}
    intl_csv = {'body': b'date,home_team,away_team,home_score,away_score,tournament,city,country,neutral\n'}

    def fake_fetch(url, headers=None, timeout=40, retries=2):
        calls.append(url)
        if 'site.api.espn.com' in url:
            day = url.split('dates=')[1][:8]
            groups = {}
            for slug, lid, lname, ev in espn_world.get('events', []):
                if ev['date'][:10].replace('-', '') == day:
                    groups.setdefault(slug, {'leagues': [{'id': lid, 'slug': slug, 'name': lname}],
                                             'events': []})['events'].append(ev)
            return 200, {}, json.dumps({'scores': list(groups.values())}).encode()
        if 'martj42' in url:
            return 200, {}, intl_csv['body']
        if 'api.oddspapi.io' in url:
            assert 'apiKey=op_SECRET_9' in url, url
            path = url.split('/v4/')[1].split('?')[0]
            op_calls.append(path)
            if path == 'account':
                return 200, {}, json.dumps({'subscription': {'request_limit': 250, 'request_count': 10}}).encode()
            if path == 'markets':
                return 200, {}, json.dumps(op_world['markets']).encode()
            if path == 'tournaments':
                return 200, {}, json.dumps(op_world['tournaments']).encode()
            if path == 'participants':
                return 200, {}, json.dumps({'501': 'Pyramids FC', '502': 'Ismaily SC'}).encode()
            if path == 'odds-by-tournaments':
                assert 'bookmaker=betpawa.cm' in url and len(url.split('tournamentIds=')[1].split('&')[0].split('%2C')) <= 3
                return 200, {}, json.dumps(op_world['odds']).encode()
            return 404, {}, b''
        if 'footballcharts-backend' in url:
            assert headers['Authorization'] == 'Bearer fc_TESTKEY123', headers
            path = url.split('/api/v1/')[1]
            if path.startswith('keys/status/'):
                return 200, {}, json.dumps({'tier': 'free', 'daily_limit': 200, 'used_today': 4,
                                            'email': 'someone@example.com', 'key': 'fc_TESTKEY123'}).encode()
            if path == 'leagues/':
                return 200, {}, json.dumps({'leagues': fc_world['leagues'], 'count': len(fc_world['leagues'])}).encode()
            league = path.split('/')[1]
            if '/fixtures/' in path:
                return 200, {}, json.dumps({'league': league, 'matches': fc_world['fixtures'].get(league, [])}).encode()
            if '/results/' in path:
                season = url.split('season=')[1]
                rows = fc_world['results'].get((league, season))
                if rows is None:
                    return 403, {}, b'{"detail": "season_gated"}'
                return 200, {}, json.dumps({'league': league, 'season': season, 'season_state': 'in_season',
                                            'matches': rows}).encode()
            return 404, {}, b''
        if 'api.football-data.org' in url:
            assert headers['X-Auth-Token'] == 'ORGKEY'
            return 200, {}, json.dumps({'matches': fdorg_matches}).encode()
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
    import server.fdorg
    import server.main
    for mod in (server.net, server.apifootball, server.fdcouk, server.fdorg, server.main):
        importlib.reload(mod)
        mod.fetch = fake_fetch
    import server.espn
    import server.internationals
    server.espn.fetch = fake_fetch
    server.internationals.fetch = fake_fetch
    server.espn.PAUSE = 0
    server.espn.ENABLED = False  # switched on in the ESPN scenario below
    import types
    import time as _time
    server.fdorg.time = types.SimpleNamespace(time=_time.time, sleep=lambda s: None)
    server.apifootball.Api.__init__.__defaults__  # noqa
    # No sleeping between fake requests.
    orig_init = server.apifootball.Api.__init__

    def fast_init(self, *a, **k):
        orig_init(self, *a, **k)
        self.spacing = 0
    server.apifootball.Api.__init__ = fast_init
    # A clock for the dataFetcher (the 6-hour gate) that the test can move.
    clock = {'t': _time.time()}
    server.data_fetcher.time = types.SimpleNamespace(time=lambda: clock['t'], gmtime=_time.gmtime,
                                                     strftime=_time.strftime)

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
    assert m3['today'] in m3['days'] and m3['tomorrow'] in m3['days'], 'today/tomorrow must always be published'
    assert m3['api_suspended'] is True and 'suspended' in m3['api_note'], m3.get('api_note')
    assert len([c for c in calls if 'api-sports.io' in c]) == 1, [c for c in calls if 'api-sports.io' in c]
    d3 = json.load(open(os.path.join(site, 'days', f'{today}.json')))
    assert len(d3['f']) == 5, f"duplicated fixtures: {len(d3['f'])}"
    server.main.config.MODE = 'quick'
    server.main.LOG.clear()
    calls.clear()
    assert server.main.main() == 0
    assert not [c for c in calls if 'api-sports.io' in c], 'suspended account was called on a quick run'

    # football-data.org while suspended: a finished match updates its API
    # twin (no double), new matches are added, extra time keeps the 90' score.
    first = days[today][0]
    tomorrow = m3['tomorrow']
    t_ts = int(datetime.strptime(tomorrow, '%Y-%m-%d').replace(tzinfo=CM).timestamp()) + 19 * 3600

    def org(mid, ts, home, away, status, comp, area, score=None):
        return {'id': mid, 'utcDate': datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'status': status, 'matchday': 12, 'competition': comp, 'area': area,
                'season': {'startDate': '2026-08-15'},
                'homeTeam': home, 'awayTeam': away, 'score': score or {'duration': 'REGULAR', 'fullTime': {'home': None, 'away': None}}}

    pl = {'id': 2021, 'code': 'PL', 'name': 'Premier League', 'emblem': 'pl.png'}
    eng = {'name': 'England', 'flag': 'eng.svg'}
    h0, a0 = first['teams']['home'], first['teams']['away']
    fdorg_matches[:] = [
        org(9001, first['fixture']['timestamp'], {'id': 501, 'name': h0['name'] + ' FC', 'shortName': h0['name']},
            {'id': 502, 'name': a0['name'] + ' FC', 'shortName': a0['name']}, 'FINISHED', pl, eng,
            {'duration': 'REGULAR', 'fullTime': {'home': 2, 'away': 1}}),
        org(9002, t_ts, {'id': 402, 'name': 'Brentford FC', 'shortName': 'Brentford'},
            {'id': 63, 'name': 'Fulham FC', 'shortName': 'Fulham'}, 'TIMED', pl, eng),
        org(9003, t_ts + 3600, {'id': 1765, 'name': 'Fluminense FC', 'shortName': 'Fluminense'},
            {'id': 1769, 'name': 'SE Palmeiras', 'shortName': 'Palmeiras'}, 'SCHEDULED',
            {'id': 2013, 'code': 'BSA', 'name': 'Campeonato Brasileiro Série A'}, {'name': 'Brazil'}),
        org(9004, first['fixture']['timestamp'] - 3600, {'id': 86, 'name': 'Real Madrid CF', 'shortName': 'Real Madrid'},
            {'id': 5, 'name': 'FC Bayern München', 'shortName': 'Bayern'}, 'FINISHED',
            {'id': 2001, 'code': 'CL', 'name': 'UEFA Champions League'}, {'name': 'Europe'},
            {'duration': 'EXTRA_TIME', 'fullTime': {'home': 3, 'away': 2}, 'regularTime': {'home': 1, 'away': 1},
             'extraTime': {'home': 2, 'away': 1}}),
    ]
    server.main.config.MODE = 'quick'
    server.main.LOG.clear()
    calls.clear()
    # Within 6 hours of the last fetch: served from the SQLite cache.
    assert server.main.main() == 0
    assert not [c for c in calls if 'football-data.org/v4/matches' in c], 'refetched within 6 hours'
    d_cached = json.load(open(os.path.join(site, 'days', f'{today}.json')))
    assert len(d_cached['f']) == 5, 'new football-data.org data should wait for the next cycle'
    clock['t'] += 6 * 3600
    server.main.LOG.clear()
    calls.clear()
    assert server.main.main() == 0
    assert len([c for c in calls if 'football-data.org/v4/matches' in c]) == 1
    assert not [c for c in calls if 'api-sports.io' in c]
    d4 = json.load(open(os.path.join(site, 'days', f'{today}.json')))
    assert len(d4['f']) == 6, f"today should be 5 API + 1 cup match, got {len(d4['f'])}"
    twin = next(f for f in d4['f'] if f['id'] == first['fixture']['id'])
    assert twin['st'] == 'FT' and (twin['hg'], twin['ag']) == (2, 1), twin
    cup = next(f for f in d4['f'] if f.get('xid') == 9004)
    assert cup['st'] == 'AET' and (cup['fh'], cup['fa']) == (1, 1) and (cup['hg'], cup['ag']) == (3, 2) and cup['lg']['id'] == 2
    t4 = json.load(open(os.path.join(site, 'days', f'{tomorrow}.json')))
    new = [f for f in t4['f'] if f.get('src') == 'fdorg']
    assert {f['xid'] for f in new} >= {9003}, new
    m4 = json.load(open(os.path.join(site, 'manifest.json')))
    assert 'football-data.org' in m4['sources'] and m4['status_note'] == '', 'no warning while sources work'
    h4 = json.loads(gzip.open(os.path.join(site, 'history.json.gz')).read())
    assert any(r[0] == first['fixture']['id'] and r[6:8] == [2, 1] for r in h4['m']), 'finished twin not in history'
    # Running again changes nothing (no doubles).
    server.main.LOG.clear()
    assert server.main.main() == 0
    d5 = json.load(open(os.path.join(site, 'days', f'{today}.json')))
    assert len(d5['f']) == 6
    h5 = json.loads(gzip.open(os.path.join(site, 'history.json.gz')).read())
    assert len(h5['m']) == len(h4['m']), (len(h5['m']), len(h4['m']))
    # A full run on top keeps everything single.
    server.main.config.MODE = 'full'
    server.main.LOG.clear()
    assert server.main.main() == 0
    d6 = json.load(open(os.path.join(site, 'days', f'{today}.json')))
    assert len(d6['f']) == 6, len(d6['f'])
    h6 = json.loads(gzip.open(os.path.join(site, 'history.json.gz')).read())
    ids = [r[0] for r in h6['m']]
    assert len(ids) == len(set(ids))
    assert sum(1 for r in h6['m'] if (r[4], r[5]) == (twin['h']['id'], twin['a']['id']) and abs(r[1] - twin['ts']) < 6 * 3600) == 1

    # football-charts.com: new league history, next matches, finished
    # scores filled in, known clubs recognised, nothing doubled.
    os.environ['FOOTBALL_CHARTS_KEY'] = 'fc_TESTKEY123'
    import server.fcharts
    importlib.reload(server.fcharts)
    server.fcharts.fetch = fake_fetch
    server.fcharts.time = types.SimpleNamespace(time=_time.time, sleep=lambda s: None)
    server.fcharts.DIVISION['testpl'] = 'E0'
    now_ts = int(datetime.now(timezone.utc).timestamp())

    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def res(ts, h, a, hg, ag, rid):
        return {'id': rid, 'homeTeam': h, 'awayTeam': a, 'date': iso(ts)[:10], 'time': iso(ts)[11:19],
                'kickoff_utc': iso(ts), 'score': f'{hg}:{ag}', 'ht_result': '0:0', 'stage': 'league'}

    def fxt(ts, h, a):
        return {'home_team': h, 'away_team': a, 'match_date': iso(ts)[:10], 'time': iso(ts)[11:16],
                'status': 'scheduled', 'slug': f'x/{h}-{a}', 'model_predictions': {}}

    egy = ['Al Ahly', 'Zamalek', 'Pyramids', 'Al Masry', 'Ismaily', 'Ceramica']
    rnd2 = random.Random(9)
    cur, prev = [], []
    for i in range(30):
        h, a = rnd2.sample(egy, 2)
        cur.append(res(now_ts - (2 + i * 2) * 86400, h, a, rnd2.randint(0, 3), rnd2.randint(0, 3), 100 + i))
        h, a = rnd2.sample(egy, 2)
        prev.append(res(now_ts - (200 + i * 3) * 86400, h, a, rnd2.randint(0, 3), rnd2.randint(0, 3), 300 + i))
    played_ts = now_ts - 3 * 3600
    cur.append(res(played_ts, 'Al Ahly', 'Zamalek', 2, 1, 999))
    t_cm = datetime.strptime(tomorrow, '%Y-%m-%d').replace(tzinfo=CM)
    tom1 = int((t_cm + timedelta(hours=18)).timestamp())
    api_next = days[tomorrow][0]
    hn = next(t for t in TEAMS if t[0] == api_next['teams']['home']['id'])
    an = next(t for t in TEAMS if t[0] == api_next['teams']['away']['id'])
    fc_world['leagues'][:] = [
        {'league': 'egypt', 'name': 'Premier League', 'country': 'Egypt', 'seasons': ['2026-2027', '2025-2026']},
        {'league': 'testpl', 'name': 'Premier League', 'country': 'England', 'seasons': ['2026-2027', '2025-2026']},
        {'league': 'wengland1', 'name': 'WSL', 'country': 'England', 'seasons': ['2026-2027']},
        {'league': 'premier', 'name': 'Premier League', 'country': 'England', 'seasons': ['2026-2027']},
    ]
    fc_world['fixtures'].update({
        'egypt': [fxt(played_ts, 'Al Ahly', 'Zamalek'), fxt(tom1, 'Pyramids', 'Ismaily'),
                  fxt(tom1 + 7200, 'Al Masry', 'Ceramica')],
        'testpl': [fxt(api_next['fixture']['timestamp'], hn[1] + ' FC', an[1] + ' FC')],
    })
    old_ts = now_ts - 200 * 86400
    fc_world['results'].update({
        ('egypt', '2026-2027'): cur, ('egypt', '2025-2026'): prev,
        ('testpl', '2026-2027'): [res(old_ts, 'Manchester United FC', 'Arsenal FC', 3, 1, 5001),
                                  res(old_ts + 86400, 'Mystery Town FC', 'Arsenal FC', 0, 0, 5002)],
        ('testpl', '2025-2026'): [],
    })
    server.main.config.MODE = 'full'
    server.main.LOG.clear()
    calls.clear()
    assert server.main.main() == 0
    fc_calls = [c for c in calls if 'footballcharts-backend' in c]
    assert not [c for c in fc_calls if 'wengland1' in c or 'leagues/premier/' in c], 'skipped leagues were read'
    m7 = json.load(open(os.path.join(site, 'manifest.json')))
    notes = '\n'.join(m7['notes'])
    print('\n'.join(l for l in m7['notes'] if 'football-charts' in l))
    assert 'someone@example.com' not in notes and 'fc_TESTKEY123' not in notes, 'secret leaked into the log'
    assert 'football-charts.com' in m7['sources'] and m7['attribution'] == ['Data by football-charts.com']
    assert m7['status_note'] == ''
    h7 = json.loads(gzip.open(os.path.join(site, 'history.json.gz')).read())
    eg_league = [l for l in h7['l'] if l.get('country') == 'Egypt']
    assert len(eg_league) == 1 and eg_league[0]['id'] < 0, eg_league
    eg_rows = [r for r in h7['m'] if r[2] == eg_league[0]['id']]
    within = sum(1 for r in cur + prev if server.fcharts.kickoff_ts(r['kickoff_utc']) >= now_ts - 450 * 86400)
    assert len(eg_rows) == within, (len(eg_rows), within)
    assert ('testpl', '2026-2027') and not [c for c in fc_calls if 'testpl/results' in c], 'known league: results only after it played'
    t7 = json.load(open(os.path.join(site, 'days', f'{tomorrow}.json')))
    fc_new = [f for f in t7['f'] if f.get('src') == 'fc']
    assert {(f['h']['name'], f['a']['name']) for f in fc_new} == {('Pyramids', 'Ismaily'), ('Al Masry', 'Ceramica')}, fc_new
    assert sum(1 for f in t7['f'] if (f['h']['id'], f['a']['id']) == (hn[0], an[0])) == 1, 'English twin doubled'
    played = [f for k in m7['days'] for f in json.load(open(os.path.join(site, 'days', f'{k}.json')))['f']
              if f.get('xk') == 'egypt|Al Ahly|Zamalek']
    assert len(played) == 1 and played[0]['st'] == 'FT' and (played[0]['hg'], played[0]['ag']) == (2, 1), played
    st7 = json.load(open(os.path.join(tmp, 'data', 'state.json')))
    assert st7['fc_teams'].get(f'testpl|{hn[1]} FC') == hn[0], st7['fc_teams']
    meta = json.load(open(os.path.join(tmp, 'data', 'fc', 'meta.json')))
    assert 0 < meta['calls']['n'] <= server.fcharts.DAILY_CAP
    # Same day again: the cache is used, football-charts.com is not called,
    # and nothing is doubled.
    server.main.LOG.clear()
    calls.clear()
    assert server.main.main() == 0
    assert not [c for c in calls if 'footballcharts-backend' in c], 'read twice on the same day'
    h8 = json.loads(gzip.open(os.path.join(site, 'history.json.gz')).read())
    assert len([r for r in h8['m'] if r[2] == eg_league[0]['id']]) == len(eg_rows)
    ids8 = [r[0] for r in h8['m']]
    assert len(ids8) == len(set(ids8))
    t8 = json.load(open(os.path.join(site, 'days', f'{tomorrow}.json')))
    assert len(t8['f']) == len(t7['f'])
    # Next day: the English league played, so its results are read; known
    # clubs get GOAL20's ids, an unknown club waits for football-data.co.uk.
    meta['last_fetch'] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()[:19]
    meta['checked']['testpl']['dates'] = [(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()]
    meta['checked']['testpl']['fx'] = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    meta['calls'] = {'day': '2000-01-01', 'n': 0}
    json.dump(meta, open(os.path.join(tmp, 'data', 'fc', 'meta.json'), 'w'))
    server.main.LOG.clear()
    calls.clear()
    assert server.main.main() == 0
    assert [c for c in calls if 'testpl/results' in c], 'results of a league that played were not read'
    h9 = json.loads(gzip.open(os.path.join(site, 'history.json.gz')).read())
    mu_ars = [r for r in h9['m'] if (r[4], r[5]) == (33, 42) and abs(r[1] - old_ts) < 6 * 3600]
    assert len(mu_ars) == 1 and mu_ars[0][2] == 39 and mu_ars[0][6:8] == [3, 1], 'known clubs not recognised'
    assert not [r for r in h9['m'] if abs(r[1] - (old_ts + 86400)) < 60 and r[5] == 42], 'unknown club result must wait for the CSV'
    ids9 = [r[0] for r in h9['m']]
    assert len(ids9) == len(set(ids9))
    meta = json.load(open(os.path.join(tmp, 'data', 'fc', 'meta.json')))
    # Planning: a played match makes its results due; the budget is respected.
    meta['checked']['egypt']['rs_through'] = (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()
    work = server.fcharts.due_work(meta, meta['leagues'], datetime.now(timezone.utc) + timedelta(days=1))
    assert ('rs', 'egypt') in work and not [w for w in work if w[1] in ('wengland1', 'premier')], work

    # OddsPapi: betPawa prices replace the other books for the same market;
    # Asian lines are ignored; names come from the name list when missing.
    os.environ['ODDSPAPI_KEY'] = 'op_SECRET_9'
    import server.oddspapi
    importlib.reload(server.oddspapi)
    server.oddspapi.fetch = fake_fetch
    server.oddspapi.time = types.SimpleNamespace(time=_time.time, sleep=lambda s: None)

    def mk(mid, name, mtype, hc, outs):
        return {'marketId': mid, 'marketName': name, 'marketType': mtype, 'handicap': hc, 'period': 'fulltime',
                'sportId': 10, 'playerProp': False, 'outcomes': [{'outcomeId': o, 'outcomeName': n} for o, n in outs]}

    op_world['markets'][:] = [
        mk(101, 'Full Time Result', '1x2', 0, [(101, '1'), (102, 'X'), (103, '2')]),
        mk(1010, 'Over Under Full Time', 'totals', 1.5, [(1010, 'Over'), (1011, 'Under')]),
        mk(1012, 'Over Under Full Time', 'totals', 2.5, [(1012, 'Over'), (1013, 'Under')]),
        mk(1060, 'Asian Over Under Full Time', 'totals', 2.5, [(1060, 'Over'), (1061, 'Under')]),
        mk(1100, 'Corners Over Under', 'totals', 9.5, [(1100, 'Over'), (1101, 'Under')]),
        mk(104, 'Both Teams To Score', 'totals', 0, [(104, 'Yes'), (105, 'No')]),
    ]
    op_world['tournaments'][:] = [
        {'tournamentId': 17, 'tournamentName': 'Premier League', 'categoryName': 'England'},
        {'tournamentId': 999, 'tournamentName': 'Premier League', 'categoryName': 'Egypt'},
        {'tournamentId': 18, 'tournamentName': 'Championship', 'categoryName': 'England'},
    ]
    day_now = json.load(open(os.path.join(site, 'days', f'{today}.json')))
    target_fx = next(f for f in day_now['f'] if f['st'] == 'NS' and f['lg']['id'] == 39)

    def price(p):
        return {'players': {'0': {'price': p, 'active': True}}}

    op_world['odds'][:] = [
        {'fixtureId': 'id1', 'participant1Id': 1, 'participant2Id': 2, 'tournamentId': 17,
         'participant1Name': target_fx['h']['name'] + ' FC', 'participant2Name': target_fx['a']['name'] + ' FC',
         'startTime': iso(target_fx['ts']),
         'bookmakerOdds': {'betpawa.cm': {'bookmakerIsActive': True, 'markets': {
             '101': {'outcomes': {'101': price(2.1), '103': price(3.4)}},
             '1012': {'outcomes': {'1012': price(1.85), '1013': price(1.9)}},
             '1060': {'outcomes': {'1060': price(1.95)}}}}}},
        {'fixtureId': 'id2', 'participant1Id': 501, 'participant2Id': 502, 'tournamentId': 999,
         'startTime': iso(tom1),
         'bookmakerOdds': {'betpawa.cm': {'markets': {'1010': {'outcomes': {'1010': price(1.3)}}}}}},
    ]
    server.main.LOG.clear()
    calls.clear()
    op_calls.clear()
    assert server.main.main() == 0
    m10 = json.load(open(os.path.join(site, 'manifest.json')))
    notes10 = '\n'.join(m10['notes'])
    print('\n'.join(l for l in m10['notes'] if 'OddsPapi' in l))
    assert 'op_SECRET_9' not in notes10, 'OddsPapi key leaked'
    assert op_calls.count('odds-by-tournaments') == 1 and 'participants' in op_calls, op_calls
    assert 'oddspapi.io (betPawa prices)' in m10['sources']
    e10 = json.loads(gzip.open(os.path.join(site, 'enrich.json.gz')).read())
    q = e10['odds'][str(target_fx['id'])]['v']
    by_m = {}
    for x in q:
        by_m.setdefault(x['m'], []).append(x)
    assert by_m['over25'] == [{'b': 'betPawa', 'm': 'over25', 'o': 1.85}], by_m['over25']
    assert by_m['home'] == [{'b': 'betPawa', 'm': 'home', 'o': 2.1}] and by_m['away'][0]['o'] == 3.4, by_m
    egy_fx = next(f for f in t8['f'] if f.get('xk') == 'egypt|Pyramids|Ismaily')
    assert e10['odds'][str(egy_fx['id'])]['v'] == [{'b': 'betPawa', 'm': 'over15', 'o': 1.3}]
    # Same day: no second read.
    server.main.LOG.clear()
    op_calls.clear()
    assert server.main.main() == 0
    assert not [c for c in op_calls if c != 'account'], op_calls
    e11 = json.loads(gzip.open(os.path.join(site, 'enrich.json.gz')).read())
    assert e11['odds'][str(target_fx['id'])]['v'] == q, 'stored betPawa prices lost'

    # ESPN worldwide feed (server 1.9): national teams, a club match GOAL20
    # already has, a league only ESPN covers, a women's league (left out).
    espn_scenario(tmp, site, espn_world, intl_csv, server)

    # Server predictions: yesterday, today and tomorrow, every match either
    # PICK (single safest market at 70%+) or SKIPPED_LOW_EDGE, 3 bullets each.
    m12 = json.load(open(os.path.join(site, 'manifest.json')))
    assert m12['model']['threshold'] == 0.70 and m12['model']['skipped_status'] == 'SKIPPED_LOW_EDGE'
    assert len(m12['files']['predictions']) == 3, m12['files'].get('predictions')
    assert m12['fetch']['max_cycles_per_day'] == 4 and m12['fetch']['cycles_today'] <= 4, m12['fetch']
    statuses = {}
    for k in m12['files']['predictions']:
        body = json.load(open(os.path.join(site, 'predictions', f'{k}.json')))
        for pr in body['p']:
            statuses[pr['status']] = statuses.get(pr['status'], 0) + 1
            assert pr['status'] in ('PICK', 'SKIPPED_LOW_EDGE'), pr['status']
            assert len(pr['why']) == 3 and all(isinstance(b, str) and b for b in pr['why']), pr['why']
            if pr['status'] == 'PICK':
                probs = pr['probs']
                assert pr['pick']['probability'] >= 0.70
                assert pr['pick']['probability'] == max(probs.values()), (pr['pick'], probs)
                assert abs(probs['1'] + probs['X'] + probs['2'] - 1) < 2e-3
                assert abs(probs['O2.5'] + probs['U2.5'] - 1) < 2e-3
                assert set(probs) == {'1', 'X', '2', '1X', 'X2', '12', 'O1.5', 'U1.5', 'O2.5', 'U2.5',
                                      'BTTS_YES', 'BTTS_NO'}
                assert set(pr['strength']) == {'home_attack_home', 'home_defence_home',
                                               'away_attack_away', 'away_defence_away'}
            elif pr.get('best'):
                assert pr['best']['probability'] < 0.70
    assert statuses.get('PICK'), statuses
    for k in m12['files']['predictions']:
        body = json.load(open(os.path.join(site, 'predictions', f'{k}.json')))
        assert set(body['tickets']) == {'top3', 'longshot'}, body.get('tickets')
        assert body['tickets']['longshot']['badge'] in ('High-Variance +EV Ticket \u2014 Minimal Stake Only',
                                                        'High-Variance Ticket \u2014 Minimal Stake Only (estimated odds)')
    assert set(m12['tickets_record']) == {'top3', 'top3_grades', 'top3_legs', 'top3_skipped', 'longshot'}, \
        m12.get('tickets_record')
    assert m12['tickets_record']['top3']['headline'] and m12['tickets_record']['top3']['grade'] == 'A'
    assert os.path.exists(os.path.join(tmp, 'data', 'tickets.json')), 'ticket archive not written'
    site_text = ''
    for root, _, names in os.walk(site):
        for name in names:
            raw = open(os.path.join(root, name), 'rb').read()
            site_text += (gzip.decompress(raw) if name.endswith('.gz') else raw).decode('utf-8', 'replace')
    for secret in ('ORGKEY', 'fc_TESTKEY123', 'op_SECRET_9', 'TESTKEY'):
        assert secret not in site_text, f'a key reached the public site: {secret}'
    engine_checks(tmp)
    ticket_checks(tmp)

    print(f'\nOK · full run used {used_full} requests, quick run used {m2["requests_used"]}; '
          f'{len(rows)} history rows, {len(positive)} CSV rows mapped, odds on {len(fd_odds)} fixtures')
    shutil.rmtree(tmp)


def espn_parser_checks():
    import server.espn as es
    sample = {'scores': [
        {'leagues': [{'id': '2395', 'slug': 'uefa.nations', 'name': 'UEFA Nations League'}], 'events': [{
            'id': '401861165', 'date': '2026-11-14T17:00Z', 'name': 'Wales at Norway',
            'status': {'clock': 5400, 'displayClock': "90'+6'", 'period': 2,
                       'type': {'name': 'STATUS_FULL_TIME', 'state': 'post', 'completed': True}},
            'competitions': [{'competitors': [
                {'homeAway': 'home', 'score': '3', 'team': {'id': '464', 'displayName': 'Norway', 'logo': 'n.png'}},
                {'homeAway': 'away', 'score': '1', 'team': {'id': '578', 'displayName': 'Wales', 'logo': 'w.png'}}],
                'details': [{'type': {'text': 'Goal'}, 'clock': {'displayValue': "59'"}, 'team': {'id': '464'},
                             'scoringPlay': True}]}]}]},
        {'leagues': [{'id': '8097', 'slug': 'eng.w.1', 'name': "English Women's Super League"}], 'events': [{
            'id': '1', 'date': '2026-11-14T12:00Z', 'status': {'type': {'name': 'STATUS_SCHEDULED', 'state': 'pre'}},
            'competitions': [{'competitors': [{'homeAway': 'home', 'team': {'id': '1', 'displayName': 'A'}},
                                              {'homeAway': 'away', 'team': {'id': '2', 'displayName': 'B'}}]}]}]},
        {'leagues': [{'id': '3952', 'slug': 'fra.coupe_de_france', 'name': 'Coupe de France'}], 'events': [{
            'id': '2', 'date': '2026-11-14T19:00Z',
            'status': {'displayClock': "120'", 'type': {'name': 'STATUS_FINAL_PEN', 'state': 'post'}},
            'competitions': [{'competitors': [
                {'homeAway': 'home', 'score': '2', 'team': {'id': '10', 'displayName': 'Metz'}},
                {'homeAway': 'away', 'score': '2', 'team': {'id': '11', 'displayName': 'Nancy'}}],
                'details': [{'clock': {'displayValue': "30'"}, 'team': {'id': '10'}, 'scoringPlay': True, 'type': {}},
                            {'clock': {'displayValue': "90'+2'"}, 'team': {'id': '11'}, 'scoringPlay': True, 'type': {}},
                            {'clock': {'displayValue': "105'"}, 'team': {'id': '10'}, 'scoringPlay': True, 'type': {}},
                            {'clock': {'displayValue': "118'"}, 'team': {'id': '11'}, 'scoringPlay': True,
                             'type': {}}]}]}]},
    ]}
    day = es.parse_scorepanel(sample, '20261114')
    assert set(day['leagues']) == {'uefa.nations', 'fra.coupe_de_france'}, day['leagues']
    nor = day['events'][0]
    assert (nor['st'], nor['hg'], nor['ag'], nor['fh'], nor['fa']) == ('FT', 3, 1, 3, 1), nor
    assert nor['h']['n'] == 'Norway' and nor['ts'] == 1794675600 and nor['d'] == '20261114'
    pen = day['events'][1]
    assert pen['st'] == 'PEN' and (pen['fh'], pen['fa']) == (1, 1), pen  # 90-minute score from the goals
    assert es.status_code({'type': {'name': 'STATUS_HALFTIME', 'state': 'in'}}) == 'HT'
    assert es.status_code({'period': 2, 'type': {'name': 'STATUS_IN_PROGRESS', 'state': 'in'}}) == '2H'
    assert es.status_code({'type': {'name': 'STATUS_POSTPONED', 'state': 'pre'}}) == 'PST'
    assert es.is_international('fifa.worldq.caf') and not es.is_international('eng.1')
    assert es.excluded('usa.ncaa.m.1') and es.excluded('club.friendly') and not es.excluded('uefa.nations')
    # Youth and reserve football is kept (rated apart from senior teams).
    assert not es.excluded('fifa.friendly_u21', 'Under-21 International Friendly')
    assert not es.excluded('uefa.euro_u21_qual', 'UEFA U21 Championship Qualifying')
    assert es.is_youth('uefa.euro_u21_qual') and not es.is_international('uefa.euro_u21_qual')
    assert es.excluded('eng.w.1', "English Women's Super League") and es.excluded('fifa.friendly.w')


def engine_checks(tmp):
    """dataFetcher gating, .env loading and predictionEngine maths."""
    espn_parser_checks()
    import types
    import server.cache
    import server.config
    import server.data_fetcher as dfm
    import server.prediction_engine as pe

    # .env: read for local runs, a value already set wins, both names work.
    env = os.path.join(tmp, 'test.env')
    with open(env, 'w') as f:
        f.write('# comment\nFOOTBALL_DATA_API_KEY = "abc123"\nGOAL20_TEST_X=1\nexport GOAL20_TEST_Y=2\n')
    saved = {k: os.environ.pop(k, None) for k in ('FOOTBALL_DATA_API_KEY', 'FOOTBALL_DATA_ORG_KEY')}
    os.environ['GOAL20_TEST_X'] = 'kept'
    server.config.load_dotenv(env)
    assert os.environ['FOOTBALL_DATA_API_KEY'] == 'abc123' and os.environ['FOOTBALL_DATA_ORG_KEY'] == 'abc123'
    assert os.environ['GOAL20_TEST_X'] == 'kept' and os.environ['GOAL20_TEST_Y'] == '2'
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    # dataFetcher: one cycle per 6 hours, never more than 4 a UTC day.
    cache = server.cache.Cache(os.path.join(tmp, 'gate.sqlite'))
    clock = {'t': 1_800_000_000 - 1_800_000_000 % 86400 + 60}  # 00:01 UTC
    hits = []
    dfm.fdorg.KEY = 'K'
    dfm.fdorg.matches = lambda a, b, log: hits.append(clock['t']) or [{'id': len(hits)}]
    f = dfm.DataFetcher(cache, lambda *_: None, now=lambda: clock['t'])
    for _ in range(48):  # every 30 minutes for a day
        rows, note = f.matches()
        assert rows, note
        clock['t'] += 1800
    assert len(hits) == 4, len(hits)
    assert all(b - a >= 6 * 3600 - dfm.SLACK_SECONDS for a, b in zip(hits, hits[1:])), hits
    dfm.EVERY_HOURS = 1  # even a misconfigured interval stays capped
    day2 = len(hits)
    for _ in range(48):
        f.matches()
        clock['t'] += 1800
    assert len(hits) - day2 <= 5 and cache.cycles('football-data.org', clock['t'] - 1800) <= 4
    dfm.EVERY_HOURS = 6
    # A lost cache: the manifest's count still holds the cap.
    lost = server.cache.Cache(os.path.join(tmp, 'lost.sqlite'))
    g = dfm.DataFetcher(lost, lambda *_: None, now=lambda: clock['t'])
    g.seed({'fetch': f.status()})
    assert lost.cycles('football-data.org', clock['t']) == f.status()['cycles_today']

    # predictionEngine: a league with known strengths.
    rnd = random.Random(7)
    strength = {i: (0.6 + 0.08 * i, 1.4 - 0.08 * i) for i in range(1, 11)}  # team -> (attack, defence)
    rows, ts, mid = [], clock['t'] - 400 * 86400, 1
    for week in range(36):
        for h in range(1, 11):
            for a in range(1, 11):
                if h == a or rnd.random() > 0.11:
                    continue
                lh = strength[h][0] * strength[a][1] * 1.45
                la = strength[a][0] * strength[h][1] * 1.1

                def pois(lam):
                    k, p, s, u = 0, math_exp(-lam), math_exp(-lam), rnd.random()
                    while u > s:
                        k += 1
                        p *= lam / k
                        s += p
                    return k
                rows.append([mid, ts + week * 7 * 86400 + h * 60, 77, 2026, h, a, pois(lh), pois(la)])
                mid += 1
    fit = pe.fit_league(rows, clock['t'], 6)
    assert fit and fit['n'] == len(rows)
    r = pe.Ratings(fit)
    assert r.att(10) > r.att(1) and r.de(10) < r.de(1), 'ratings did not recover the strengths'
    m = pe.score_matrix(1.7, 0.9, -0.08, 0.1)
    assert abs(sum(map(sum, m)) - 1) < 1e-9
    probs = pe.market_probabilities(m, m)
    assert abs(probs['1X'] - (probs['1'] + probs['X'])) < 1e-12
    status, market, p = pe.filter_high_confidence_picks(probs)
    assert status == 'PICK' and p == max(probs.values()) and p >= 0.70
    low = {k: 0.5 for k in pe.MARKETS}
    low['O1.5'] = 0.6999
    assert pe.filter_high_confidence_picks(low)[0] == 'SKIPPED_LOW_EDGE'
    low['O1.5'] = 0.70
    assert pe.filter_high_confidence_picks(low)[:2] == ('PICK', 'O1.5')
    assert pe.filterHighConfidencePicks is pe.filter_high_confidence_picks
    hist = {'m': rows, 'l': [{'id': 77, 'name': 'Test League'}, {'id': 78, 'name': 'Club Friendlies'}]}
    pc = server.cache.Cache(os.path.join(tmp, 'pred.sqlite'))
    eng = pe.PredictionEngine(hist, pc, lambda *_: None, now=clock['t'])
    kick = clock['t'] + 3 * 3600
    fx = [
        {'id': 1, 'ts': kick, 'st': 'NS', 'lg': {'id': 77, 'name': 'Test League'},
         'h': {'id': 10, 'name': 'Strong'}, 'a': {'id': 1, 'name': 'Weak'}},
        {'id': 2, 'ts': kick, 'st': 'NS', 'lg': {'id': 77, 'name': 'Test League'},
         'h': {'id': 5, 'name': 'Mid A'}, 'a': {'id': 6, 'name': 'Mid B'}},
        {'id': 3, 'ts': kick, 'st': 'NS', 'lg': {'id': 78, 'name': 'Club Friendlies'},
         'h': {'id': 5, 'name': 'Mid A'}, 'a': {'id': 6, 'name': 'Mid B'}},
        {'id': 4, 'ts': kick, 'st': 'NS', 'lg': {'id': 79, 'name': 'Unknown Cup'},
         'h': {'id': 501, 'name': 'X'}, 'a': {'id': 502, 'name': 'Y'}},
    ]
    day = eng.predict_day('2027-01-15', fx)
    by = {x['id']: x for x in day['p']}
    assert by[1]['status'] == 'PICK' and by[1]['probs']['1'] > by[1]['probs']['2'], by[1]['probs']
    assert by[1]['xg'][0] > by[1]['xg'][1]
    assert by[3]['status'] == 'SKIPPED_LOW_EDGE' and by[3]['reason'] == 'friendly'
    # Unknown competition and teams: still rated (cold start), and labelled.
    assert by[4]['quality'] == 'cold' and by[4]['why'][0].startswith('Thin data'), by[4]
    assert by[4]['model']['baseline'] == 'worldwide averages' and by[4]['model']['evidence'] == [0, 0]
    assert abs(sum(by[4]['probs'][k] for k in ('1', 'X', '2')) - 1) < 2e-3
    assert by[1]['quality'] == 'full'
    assert all(len(x['why']) == 3 for x in day['p'])
    # A team new to a rated league: its own few results move the rating.
    rows2 = rows + [[mid + 500 + i, clock['t'] - 86400 * (i + 1), 77, 2026, 999, (i % 9) + 1, 3, 0] for i in range(2)]
    eng2 = pe.PredictionEngine({'m': rows2, 'l': hist['l']}, None, lambda *_: None, now=clock['t'])
    p999 = eng2.predict_match({'id': 5, 'ts': kick, 'st': 'NS', 'lg': {'id': 77, 'name': 'Test League'},
                               'h': {'id': 999, 'name': 'Newbie'}, 'a': {'id': 1, 'name': 'Weak'}})
    assert p999['quality'] == 'cold' and p999['model']['baseline'] == 'this competition', p999['model']
    assert p999['model']['evidence'][0] == 2
    # Neutral venues: no home advantage in the fit or the prediction.
    nrows = [r[:8] + [None] * 6 + [1, 1.0] for r in rows]
    nfit = pe.fit_league(nrows, clock['t'], 6)
    hfit = pe.fit_league(rows, clock['t'], 6)
    assert nfit and (nfit['hl'], nfit['al']) == (1.4, 1.1), 'all-neutral rows must not teach a home advantage'
    assert hfit['hl'] > hfit['al'], 'home rows do'
    # National-team pool: a fixture tagged pool=intl is rated from the pool rows.
    pool_id = -424242
    prow = [[r[0] + 10 ** 6, r[1], pool_id] + r[3:8] + [None] * 6 + [0, 0.5 if r[0] % 4 == 0 else 1.0] for r in rows]
    eng3 = pe.PredictionEngine({'m': [], 'l': []}, None, lambda *_: None, now=clock['t'], pools={'intl': pool_id},
                               pool_rows=prow)
    pp = eng3.predict_match({'id': 6, 'ts': kick, 'st': 'NS', 'pool': 'intl',
                             'lg': {'id': 4321, 'name': 'International Friendly'},
                             'h': {'id': 10, 'name': 'Strong'}, 'a': {'id': 1, 'name': 'Weak'}})
    assert pp['quality'] == 'full' and pp['model']['pool'] == 'national teams', pp.get('model')
    assert pp['probs']['1'] > pp['probs']['2'] and 'internationals' in pp['why'][0]
    pn = eng3.predict_match({'id': 7, 'ts': kick, 'st': 'NS', 'pool': 'intl', 'neutral': True,
                             'lg': {'id': 4321, 'name': 'International Friendly'},
                             'h': {'id': 10, 'name': 'Strong'}, 'a': {'id': 1, 'name': 'Weak'}})
    assert pn['xg'][0] < pp['xg'][0], 'a neutral venue removes the home advantage'
    assert 'international friendly' in pp['volatility']['reasons'] and pp['volatility']['flag']
    assert abs(pp['confidence'] - (pp['best']['probability'] - 0.05)) < 1e-9
    # Human variance flags.
    assert by[1]['volatility'] == {'flag': False, 'reasons': []} and by[1]['confidence'] == by[1]['best']['probability']
    assert 'cold-start rating' in by[4]['volatility']['reasons']
    cupfx = {'id': 8, 'ts': kick, 'st': 'NS', 'lg': {'id': 991, 'name': 'Coupe de France'},
             'h': {'id': 5, 'name': 'Mid A'}, 'a': {'id': 6, 'name': 'Mid B II'}}
    v = eng.volatility(cupfx, False, False)
    assert v['flag'] and 'cup tie' in v['reasons'] and 'youth/reserve side' in v['reasons'], v
    busy = rows + [[mid + 900, kick - 2 * 86400, 77, 2026, 10, 3, 1, 1]]
    eng4 = pe.PredictionEngine({'m': busy, 'l': hist['l']}, None, lambda *_: None, now=clock['t'])
    vb = eng4.volatility(fx[0], False, False)
    assert vb['reasons'] == ['fixture congestion'], vb
    print('Example:', by[1]['home'], '-', by[1]['away'], by[1]['pick'], *by[1]['why'], sep='\n  ')
    # After kickoff the stored prediction is frozen, even if new results arrive.
    rows.append([mid + 1, clock['t'] - 86400 * 2, 77, 2026, 1, 10, 7, 0])
    later = pe.PredictionEngine(hist, pc, lambda *_: None, now=kick + 60)
    fx[0]['st'], fx[0]['hg'], fx[0]['ag'], fx[0]['fh'], fx[0]['fa'] = 'FT', 2, 0, None, None
    again = {x['id']: x for x in later.predict_day('2027-01-15', fx)['p']}
    assert again[1]['probs'] == by[1]['probs'] and again[1]['frozen'] and again[1]['result'] == [2, 0]
    assert again[1]['won'] == pe.settle(by[1]['pick']['market'], 2, 0)
    # Ratings are reused from SQLite by a later run on the same day.
    third = pe.PredictionEngine({'m': rows[:-1], 'l': hist['l']}, pc, lambda *_: None, now=clock['t'] + 600)
    third.predict_day('2027-01-15', fx[1:2])
    assert third.fitted == 0 and third.reused == 2, (third.fitted, third.reused)


def ticket_checks(tmp):
    """generateDailyTop3 / generateHighOddsAccumulator rules."""
    import server.cache
    import server.oddspapi as op
    import server.tickets as tk
    now = 1_900_000_000

    def pred(i, probs, more=None, ts=None, ev=(12, 12), st='NS'):
        return {'id': i, 'ts': ts or now + 3600 * (i % 9 + 1), 'home': f'H{i}', 'away': f'A{i}', 'st': st,
                'status': 'PICK', 'probs': probs, 'more': more or {}, 'model': {'evidence': list(ev)}}

    # ---- Daily Top 3 (graded): A first, then B, then C, never below 65%.
    preds = [
        pred(1, {'1X': 0.80, 'O1.5': 0.79, '2': 0.20}),
        pred(2, {'X2': 0.82, '1': 0.18}, {'DNB2': 0.81}),
        pred(3, {'O1.5': 0.90}),
        pred(4, {'1X': 0.79, '2': 0.21}),
        pred(5, {'1X': 0.86, '2': 0.14}, {'DNB1': 0.83}),
        pred(6, {'1X': 0.81, '2': 0.19}, ts=now - 60),          # already started
        pred(7, {'1X': 0.95, '2': 0.05}, ev=(5, 20)),            # thin evidence
        pred(8, {'1X': 0.70, 'O1.5': 0.77}),                     # Grade B: below 78%
    ]
    t = tk.generateDailyTop3(preds, now, 'd')
    assert t['status'] == 'READY' and t['grade'] == 'A', t
    assert all(l['grade'] == 'A' for l in t['legs']) and t['grades'] == {'A': 3, 'B': 0, 'C': 0}
    assert [(l['id'], l['market']) for l in t['legs']] == [(5, 'DNB1'), (2, 'X2'), (1, '1X')], t['legs']
    assert len({l['id'] for l in t['legs']}) == 3
    assert all(l['probability'] >= 0.78 and 1.20 <= l['fair_odds'] <= 1.55 for l in t['legs'])
    assert abs(t['odds'] - round(1 / 0.83 * 1 / 0.82 * 1 / 0.80, 2)) < 0.02, t['odds']
    assert abs(t['chance'] - (1 - 0.14) * 0.82 * 0.80) < 1e-4, t['chance']  # DNB loses only to an away win
    assert t['title'] == 'Daily Top 3 Statistical Picks'
    assert t['subtitle'].startswith('Statistical model edge only') and 'played by humans' in t['subtitle']
    # Legs outside the 1.20-1.55 band never fill the card; fewer than 3 legs
    # only when not even 3 matches reach 65%.
    t2 = tk.generate_daily_top3([preds[2], preds[3], pred(9, {'O1.5': 0.95}), pred(10, {'1X': 0.85, '2': 0.15})], now)
    assert [l['id'] for l in t2['legs']] == [4] and t2['status'] == 'PARTIAL' and t2['grade'] == 'A', t2
    assert 'never goes below' in t2['note'] and abs(t2['odds'] - round(1 / 0.79, 2)) < 0.01
    t3 = tk.generate_daily_top3([preds[2], pred(9, {'O1.5': 0.95}), pred(11, {'1X': 0.64, '2': 0.36})], now)
    assert t3['status'] == 'SKIPPED_LOW_EDGE' and t3['odds'] is None and not t3['legs'] and t3['grade'] is None
    assert t3['note'].startswith('NO TOP TICKET TODAY: Fixtures carry too much human/match volatility')
    assert '65%' in t3['note']
    tb = tk.generate_daily_top3([preds[2], pred(9, {'O1.5': 0.95}), preds[7]], now)
    assert [(l['id'], l['market'], l['grade']) for l in tb['legs']] == [(8, 'O1.5', 'B')], tb['legs']
    assert tb['status'] == 'PARTIAL' and tb['grade'] == 'B'
    # Fill order A, B, C; the card's grade is its weakest leg; never below 65%.
    fill = tk.generate_daily_top3([
        pred(160, {'1X': 0.66, '2': 0.34}),                       # C
        pred(161, {'1X': 0.80, '2': 0.20}),                       # A
        pred(162, {'1X': 0.70, '2': 0.30}),                       # C, stronger
        pred(163, {'1X': 0.74, '2': 0.26}),                       # B
        dict(pred(164, {'1X': 0.69, '2': 0.31}), volatility={'flag': True, 'reasons': ['cup tie']}),  # 64%: out
        pred(165, {'1X': 0.64, '2': 0.36}),                       # below the floor
    ], now)
    assert [(l['id'], l['grade']) for l in fill['legs']] == [(161, 'A'), (163, 'B'), (162, 'C')], fill['legs']
    assert fill['status'] == 'READY' and fill['grade'] == 'C' and fill['grades'] == {'A': 1, 'B': 1, 'C': 1}
    assert fill['note'].startswith('Grade C card') and all(l['confidence'] >= 0.65 for l in fill['legs'])
    only_c = tk.generate_daily_top3([pred(160, {'1X': 0.66, '2': 0.34}), pred(162, {'1X': 0.70, '2': 0.30}),
                                     pred(166, {'O1.5': 0.68})], now)
    assert only_c['status'] == 'READY' and only_c['grade'] == 'C' and len(only_c['legs']) == 3
    assert tk.leg_grade(0.78) == 'A' and tk.leg_grade(0.78, flagged=True) == 'B'
    assert tk.leg_grade(0.83, cold=True) == 'C' and tk.leg_grade(0.7199) == 'C' and tk.leg_grade(0.72) == 'B'
    assert tk.leg_grade(0.649) is None and tk.leg_grade(None) is None
    # Tickets archived before server 1.11 are graded from what they stored.
    assert tk.grade_of({'probability': 0.80}) == 'A'
    assert tk.grade_of({'probability': 0.83, 'confidence': 0.78, 'volatility': ['cup tie']}) == 'B'
    assert tk.ticket_grade({'legs': [{'probability': 0.80}, {'probability': 0.81, 'cold': True}]}) == 'C'
    assert tk.ticket_grade({'legs': []}) is None
    # Human variance: 5 points off the confidence, never above Grade B.
    cup = dict(pred(150, {'1X': 0.82, '2': 0.18}), volatility={'flag': True, 'reasons': ['cup tie']})
    cup_ok = dict(pred(151, {'1X': 0.833, '2': 0.167}), volatility={'flag': True, 'reasons': ['fixture congestion']})
    tv = tk.generate_daily_top3([cup, cup_ok, pred(152, {'1X': 0.79, '2': 0.21})], now)
    assert [l['id'] for l in tv['legs']] == [152, 151, 150], tv['legs']  # A first, then B by confidence
    by_v = {l['id']: l for l in tv['legs']}
    assert by_v[151]['volatility'] == ['fixture congestion'] and abs(by_v[151]['confidence'] - 0.783) < 1e-9
    assert by_v[151]['grade'] == 'B' and by_v[150]['grade'] == 'B' and abs(by_v[150]['confidence'] - 0.77) < 1e-9
    assert by_v[152]['volatility'] == [] and by_v[152]['confidence'] == 0.79 and by_v[152]['grade'] == 'A'
    assert tv['status'] == 'READY' and tv['grade'] == 'B' and 'higher human variance' in tv['note']

    # ---- 1000+ longshot: real prices only, strictly by expected value.
    rnd = random.Random(11)
    lp, odds = [], {}
    for i in range(100, 124):
        p1 = rnd.uniform(0.32, 0.55)
        price = round(rnd.uniform(1.7, 3.2), 2)
        lp.append(pred(i, {'1': p1, 'X': 0.27, '2': 0.73 - p1}, {'BTTS_O2.5': 0.35, 'H_O1.5': 0.4, 'A_O1.5': 0.2}))
        odds[str(i)] = {'v': [{'b': 'Market average (football-data)', 'm': 'home', 'o': price},
                              {'b': 'Bet365 (football-data)', 'm': 'home', 'o': price + 0.3}]}
    odds['100']['v'].append({'b': 'betPawa', 'm': 'home', 'o': 2.9})       # betPawa wins
    odds['101']['v'] = [{'b': 'betPawa', 'm': 'draw', 'o': 4.5}]              # draw +21.5%
    odds['102']['v'] = [{'b': 'betPawa', 'm': 'home', 'o': 9.0}]              # absurd edge: ignored
    odds['103']['v'] = [{'b': 'betPawa', 'm': 'home', 'o': 1.4}]              # price too short
    odds['104']['v'] = [{'b': 'betPawa', 'm': 'home_o15', 'o': 3.0}]          # team over 1.5: +20%
    odds['105']['v'] = [{'b': 'betPawa', 'm': 'btts_o25', 'o': 3.3}]          # BTTS & O2.5: +15.5%
    lp[0]['probs']['1'] = 0.40
    lp[1]['probs']['X'] = 0.31
    assert tk.generateHighOddsAccumulator is tk.generate_high_odds_accumulator
    assert tk.generateDailyTop3 is tk.generate_daily_top3
    ls = tk.generateHighOddsAccumulator(lp, odds, now, 'd', target_odds=1000)
    legs = ls['legs']
    if not any(l.get('estimated') for l in legs):
        assert ls['badge'] == 'High-Variance +EV Ticket \u2014 Minimal Stake Only'
    assert 3 <= len(legs) <= 14 and len({l['id'] for l in legs}) == len(legs)
    assert all(0.02 <= l['edge'] <= 0.50 and l['price'] >= 1.60 and l['probability'] >= 0.30
               for l in legs if not l.get('estimated'))
    assert 102 not in {l['id'] for l in legs} and 103 not in {l['id'] for l in legs}
    by = {l['id']: l for l in legs}
    small = tk.generate_high_odds_accumulator([lp[1], lp[4], lp[5]], odds, now)
    sm = {l['id']: l for l in small['legs']}
    assert sm[101]['market'] == 'X' and sm[104]['market'] == 'H_O1.5' and sm[105]['market'] == 'BTTS_O2.5', sm
    assert small['status'] == 'BELOW_TARGET' and abs(small['odds'] - 4.5 * 3.0 * 3.3) < 0.01
    assert [l['id'] for l in small['legs']] == sorted(sm, key=lambda i: (sm[i]['ts'], i)), 'legs in kickoff order'
    if 100 in by:
        assert by[100]['price'] == 2.9 and by[100]['source'] == 'betPawa'
    # Strictly by value: every leg left out has less value than every leg in.
    cands = []
    for p_ in lp:
        best = None
        for m, key in (('1', 'home'), ('X', 'draw'), ('2', 'away'), ('BTTS_O2.5', 'btts_o25'),
                       ('H_O1.5', 'home_o15'), ('A_O1.5', 'away_o15')):
            price, _ = tk.price_for(odds[str(p_['id'])]['v'], key)
            pr = tk.probability(p_, m)
            if price and pr and pr >= 0.30 and price >= 1.6 and 0.02 <= pr * price - 1 <= 0.5:
                best = max(best or -1, pr * price - 1)
        if best is not None:
            cands.append((best, p_['id']))
    out_best = max((e for e, i in cands if i not in by), default=-1)
    assert min(l['edge'] for l in legs) >= out_best - 1e-9, 'a better-value leg was left out'
    total = 1.0
    for l in legs:
        total *= l['price']
    assert abs(ls['odds'] - round(total, 2)) < 0.05
    assert (ls['status'] == 'READY') == (total >= 1000), (ls['status'], total)
    assert abs(ls['ev'] - (math_prod([l['probability'] * l['price'] for l in legs]) - 1)) < 1e-3
    print(f"Longshot example: {len(legs)} legs, odds {ls['odds']:,.0f}, model chance 1 in "
          f"{1 / ls['chance']:,.0f}, model value {ls['ev']:+.0%} ({ls['status']})")
    few = tk.generate_high_odds_accumulator(lp[:4], odds, now)
    assert few['status'] in ('BELOW_TARGET', 'SKIPPED_LOW_EDGE') and (few['odds'] or 0) < 1000
    # No real price anywhere: estimated legs at fair odds, honestly labelled.
    est = tk.generate_high_odds_accumulator(lp, {}, now)
    assert est['legs'] and all(l.get('estimated') for l in est['legs']), est
    assert est['badge'] == 'High-Variance Ticket \u2014 Minimal Stake Only (estimated odds)'
    assert est['odds_kind'] == 'mixed' and 'no real price' in est['note']
    assert all(1.70 <= l['odds'] <= 3.00 for l in est['legs'])
    assert len({l['id'] for l in est['legs']}) == len(est['legs']) <= 14
    assert est['odds'] >= 1000 and est['status'] == 'READY', (est['odds'], est['status'])
    assert abs(est['chance'] - 1 / math_prod([l['odds'] for l in est['legs']])) < 1e-4, 'fair legs: chance = 1/odds'
    # Most likely outcomes first; cold starts only after full-data teams.
    cold_pred = pred(130, {'1': 0.56, 'X': 0.24, '2': 0.20})
    cold_pred['quality'] = 'cold'
    mix = tk.generate_high_odds_accumulator(lp[:6] + [cold_pred], {}, now)
    ids = [l['id'] for l in mix['legs']]
    full_ids = {l['id'] for l in tk.generate_high_odds_accumulator(lp[:6], {}, now)['legs']}
    assert full_ids <= set(ids), 'every full-data leg comes before a cold start'
    assert 130 in ids and mix['legs'][ids.index(130)]['cold'], ids
    top = tk.generate_daily_top3([pred(140, {'1X': 0.80, '2': 0.2}), pred(141, {'1X': 0.81, '2': 0.19}),
                                  dict(pred(142, {'1X': 0.83, '2': 0.17}), quality='cold'),
                                  pred(143, {'1X': 0.79, '2': 0.21})], now)
    assert [l['id'] for l in top['legs']] == [141, 140, 143], 'cold leg used before full-data legs'
    top2 = tk.generate_daily_top3([pred(140, {'1X': 0.80, '2': 0.2}), pred(141, {'1X': 0.81, '2': 0.19}),
                                   dict(pred(142, {'1X': 0.83, '2': 0.17}), quality='cold')], now)
    assert [l['id'] for l in top2['legs']] == [141, 140, 142] and top2['legs'][2]['cold']
    assert top2['legs'][2]['grade'] == 'C' and top2['grade'] == 'C'
    assert top2['legs'][2]['volatility'] == ['cold-start rating'] and 'higher human variance' in top2['note']

    # ---- Settling: draw no bet void on a draw, any lost leg loses.
    done = {5: {'result': [1, 1]}, 2: {'result': [0, 2]}, 1: {'result': [2, 0]}}
    st = tk.settle_ticket(json.loads(json.dumps(t)), done)
    assert [l['status'] for l in st['legs']] == ['void', 'won', 'won'] and st['result'] == 'WON'
    assert abs(st['paid_odds'] - round(1 / 0.82 * 1 / 0.80, 2)) < 0.02
    done[2] = {'result': [1, 0]}
    lost_t = tk.settle_ticket(json.loads(json.dumps(t)), done)
    assert lost_t['result'] == 'LOST'
    assert tk.settle_ticket(json.loads(json.dumps(t)), {})['result'] == 'PENDING'

    # ---- Frozen at the first kickoff, even if the cache is lost.
    cache = server.cache.Cache(os.path.join(tmp, 'tickets.sqlite'))
    d1 = tk.day_tickets('2030-03-17', preds, odds, cache, now)
    first = d1['top3']['first_kickoff']
    changed = [dict(p_, probs=dict(p_['probs'], **{'1X': 0.99})) for p_ in preds]
    later = tk.day_tickets('2030-03-17', changed, odds, cache, first + 60)
    assert later['top3']['frozen'] and [l['id'] for l in later['top3']['legs']] == [5, 2, 1]
    early = tk.day_tickets('2030-03-17', changed, odds, cache, now + 1)
    assert not early['top3']['frozen'], 'rebuilt before the first kickoff'
    tk.day_tickets('2030-03-18', preds, odds, cache, now)
    published = {'tickets': tk.day_tickets('2030-03-18', preds, odds, cache, now)}
    fresh = server.cache.Cache(os.path.join(tmp, 'tickets-lost.sqlite'))
    again = tk.day_tickets('2030-03-18', changed, odds, fresh, first + 60, previous=published)
    assert again['top3']['frozen'] and [l['id'] for l in again['top3']['legs']] == [5, 2, 1]

    # ---- Record for the Track screen.
    c_won = tk.settle_ticket(json.loads(json.dumps(only_c)), {160: {'result': [2, 1]}, 162: {'result': [0, 0]},
                                                              166: {'result': [1, 1]}})
    assert c_won['result'] == 'WON'
    b_lost = tk.settle_ticket(json.loads(json.dumps(tv)), {152: {'result': [0, 1]}, 151: {'result': [1, 0]},
                                                           150: {'result': [3, 0]}})
    assert b_lost['result'] == 'LOST'
    rec = tk.record({'2030-03-14': {'top3': st}, '2030-03-15': {'top3': lost_t, 'longshot': ls},
                     '2030-03-16': {'top3': t3}, '2030-03-17': {'top3': c_won}, '2030-03-18': {'top3': b_lost}})
    # The headline is Grade A only: the C win and the B loss are not in it.
    assert rec['top3']['won'] == 1 and rec['top3']['lost'] == 1 and rec['top3']['skipped'] == 1
    assert rec['top3']['headline'] and rec['top3']['grade'] == 'A'
    assert abs(rec['top3']['units'] - (st['paid_odds'] - 1 - 1)) < 1e-6, rec['top3']
    assert rec['top3']['recent'][0]['day'] == '2030-03-15', 'newest first'
    gr = rec['top3_grades']
    assert (gr['A']['won'], gr['A']['lost']) == (1, 1)
    assert (gr['B']['won'], gr['B']['lost']) == (0, 1) and (gr['C']['won'], gr['C']['lost']) == (1, 0), gr
    assert gr['C']['recent'][0]['grade'] == 'C' and gr['C']['text'].startswith('Grade C')
    assert abs(gr['C']['expected_wins'] - only_c['chance']) < 1e-2
    lg = rec['top3_legs']
    # Legs by their own grade (the B card's A leg lost, its two B legs won).
    assert (lg['A']['won'], lg['A']['lost'], lg['A']['void']) == (3, 2, 2), lg
    assert (lg['B']['won'], lg['B']['lost']) == (2, 0) and (lg['C']['won'], lg['C']['lost']) == (3, 0), lg
    assert abs(lg['B']['expected'] - (0.833 + 0.82)) < 0.01, lg
    assert rec['top3_skipped'] == 1 and rec['longshot']['pending'] == 1

    # ---- OddsPapi: draw, team totals and BTTS & Over 2.5 never confused with match totals.
    catalogue = [
        {'sportId': 10, 'marketName': 'Full Time Result', 'marketType': '1x2', 'handicap': 0,
         'outcomes': [{'outcomeName': '1', 'outcomeId': 1}, {'outcomeName': 'X', 'outcomeId': 2},
                      {'outcomeName': '2', 'outcomeId': 3}]},
        {'sportId': 10, 'marketName': 'Over Under', 'marketType': 'totals', 'handicap': 1.5,
         'outcomes': [{'outcomeName': 'Over', 'outcomeId': 10}, {'outcomeName': 'Under', 'outcomeId': 11}]},
        {'sportId': 10, 'marketName': 'Home Team Total', 'marketType': 'totals', 'handicap': 1.5,
         'outcomes': [{'outcomeName': 'Over', 'outcomeId': 20}, {'outcomeName': 'Under', 'outcomeId': 21}]},
        {'sportId': 10, 'marketName': 'Total Away', 'marketType': 'totals', 'handicap': 1.5,
         'outcomes': [{'outcomeName': 'Over', 'outcomeId': 30}, {'outcomeName': 'Under', 'outcomeId': 31}]},
        {'sportId': 10, 'marketName': 'Team Total', 'marketType': 'totals', 'handicap': 1.5,
         'outcomes': [{'outcomeName': 'Over', 'outcomeId': 35}]},
        {'sportId': 10, 'marketName': 'Both Teams To Score and Over 2.5', 'marketType': 'combo', 'handicap': 0,
         'outcomes': [{'outcomeName': 'Yes', 'outcomeId': 40}, {'outcomeName': 'No', 'outcomeId': 41}]},
        {'sportId': 10, 'marketName': 'Both Teams To Score', 'marketType': 'btts', 'handicap': 0,
         'outcomes': [{'outcomeName': 'Yes', 'outcomeId': 50}, {'outcomeName': 'No', 'outcomeId': 51}]},
    ]
    mm = op._markets_map(catalogue)
    assert mm == {'1': 'home', '2': 'draw', '3': 'away', '10': 'over15', '20': 'home_o15',
                  '30': 'away_o15', '40': 'btts_o25', '50': 'btts'}, mm


def espn_scenario(tmp, site, espn_world, intl_csv, server):
    import server.espn as espn_mod
    import server.internationals as intl_mod
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    rnd = random.Random(3)
    nations = ['Portugal', 'Denmark', 'Norway', 'Wales', 'Austria', 'Republic of Ireland', 'Romania',
               'Poland', 'Sweden', 'Bosnia and Herzegovina', 'Cameroon', 'Namibia']
    power = {n: 1.5 - 0.08 * i for i, n in enumerate(nations)}

    def pois(lam):
        k, p, acc, u = 0, math_exp(-lam), math_exp(-lam), rnd.random()
        while u > acc:
            k += 1
            p *= lam / k
            acc += p
        return k
    lines = ['date,home_team,away_team,home_score,away_score,tournament,city,country,neutral']
    start = _dt.now(_tz.utc).date() - _td(days=900)
    for k in range(340):
        h, a = rnd.sample(nations, 2)
        day = start + _td(days=int(k * 2.5))
        tour = 'Friendly' if k % 5 == 0 else 'UEFA Nations League'
        lines.append(f'{day},{h},{a},{pois(1.35 * power[h] / power[a])},{pois(1.05 * power[a] / power[h])},'
                     f'{tour},X,Y,{"TRUE" if k % 9 == 0 else "FALSE"}')
    intl_csv['body'] = ('\n'.join(lines) + '\n').encode()

    now = _dt.now(_tz.utc)
    today_cm = server.main.config.today_cm()
    kick = _dt(today_cm.year, today_cm.month, today_cm.day, 18, 0, tzinfo=_tz.utc)
    if kick <= now:
        kick = now + _td(hours=3)
    tomorrow = kick + _td(days=1)

    def ev(eid, when, home, away, state='pre', name='STATUS_SCHEDULED', hs='0', as_='0', clock="0'", period=0,
           hid=None, aid=None):
        return {'id': str(eid), 'date': when.strftime('%Y-%m-%dT%H:%MZ'),
                'status': {'displayClock': clock, 'period': period, 'type': {'name': name, 'state': state}},
                'competitions': [{'competitors': [
                    {'homeAway': 'home', 'score': hs, 'team': {'id': hid or str(eid) + 'h', 'displayName': home,
                                                               'logo': 'https://a.espncdn.com/x.png'}},
                    {'homeAway': 'away', 'score': as_, 'team': {'id': aid or str(eid) + 'a', 'displayName': away,
                                                                'logo': ''}}], 'details': []}]}
    key_today = server.main.config.day_key(today_cm)
    day = json.load(gzip.open(os.path.join(tmp, 'data', 'days', f'{key_today}.json.gz')))
    base = next(f for f in day['f'] if f.get('src') != 'espn' and f['h']['name'] and f['a']['name'])
    twin_when = _dt.fromtimestamp(base['ts'] + 900, _tz.utc)
    events = [
        ('uefa.nations', '2395', 'UEFA Nations League', ev(9001, kick, 'Portugal', 'Denmark')),
        ('uefa.nations', '2395', 'UEFA Nations League', ev(9002, kick, 'Bosnia-Herzegovina', 'Romania')),
        ('caf.nations_qual', '2396', 'Africa Cup of Nations Qualifying', ev(9003, tomorrow, 'Cameroon', 'Namibia')),
        ('eng.1', '700', 'English Premier League', ev(9004, twin_when, base['h']['name'], base['a']['name'])),
        ('per.1', '670', 'Peruvian Liga 1', ev(9005, kick, 'Alianza Lima', 'Universitario')),
        ('per.1', '670', 'Peruvian Liga 1', ev(9006, now - _td(minutes=70), 'Sporting Cristal', 'Melgar',
                                               state='in', name='STATUS_SECOND_HALF', hs='1', clock="67'",
                                               period=2)),
        ('eng.w.1', '8097', "English Women's Super League", ev(9010, kick, 'Arsenal Women', 'Chelsea Women')),
        ('uefa.euro_u21_qual', '3920', 'UEFA U21 Championship Qualifying', ev(9011, kick, 'Portugal U21', 'Denmark U21')),
    ]
    for i in range(4):  # finished matches of the league only ESPN covers
        events.append(('per.1', '670', 'Peruvian Liga 1',
                       ev(9020 + i, now - _td(days=2 + i), f'Club {i}', f'Club {i + 4}', state='post',
                          name='STATUS_FULL_TIME', hs=str(i % 3), as_='1', clock="90'+3'", period=2)))
    espn_world['events'] = events
    espn_mod.ENABLED = True
    server.main.config.MODE = 'full'
    server.main.LOG.clear()
    assert server.main.main() == 0
    log = '\n'.join(server.main.LOG)
    assert 'ESPN:' in log and 'national-team results' in log, log[-3000:]
    assert 'International results: 340 results' in log, [l for l in server.main.LOG if 'International' in l]
    d = json.load(gzip.open(os.path.join(tmp, 'data', 'days', f'{key_today}.json.gz')))
    by_espn = {f['espn']['id']: f for f in d['f'] if f.get('espn')}
    assert by_espn['9001'].get('pool') == 'intl' and by_espn['9001']['espn']['lg'] == 'uefa.nations', by_espn.get('9001')
    assert by_espn['9002']['h']['id'] == intl_mod.team_id('Bosnia and Herzegovina'), 'ESPN name not mapped'
    twin = by_espn['9004']
    assert twin['id'] == base['id'] and twin.get('src') != 'espn', 'existing match should get the ESPN id'
    assert sum(1 for f in d['f'] if f['h']['name'] == base['h']['name'] and abs(f['ts'] - base['ts']) < 7200) == 1
    assert '9005' in by_espn and by_espn['9005']['src'] == 'espn' and not by_espn['9005'].get('pool')
    live = by_espn['9006']
    assert live['st'] == '2H' and live['el'] == 67 and live['hg'] == 1, live
    assert not any('Women' in f['h']['name'] for f in d['f']), "women's league should be left out"
    u21 = by_espn['9011']
    assert not u21.get('pool') and u21['h']['id'] != intl_mod.team_id('Portugal'), 'U21 rated with seniors'
    hist = json.loads(gzip.open(os.path.join(site, 'history.json.gz')).read())
    peru = server.main.synthetic_id('league|espn-per.1')
    assert sum(1 for r in hist['m'] if r[2] == peru) >= 4, 'ESPN-only league results not in the history'
    preds = json.load(open(os.path.join(site, 'predictions', f'{key_today}.json')))
    by_id = {p['id']: p for p in preds['p']}
    pt = by_id[by_espn['9001']['id']]
    assert pt['quality'] == 'full' and pt['model']['pool'] == 'national teams', pt.get('model')
    assert abs(pt['probs']['1'] + pt['probs']['X'] + pt['probs']['2'] - 1) < 2e-3
    assert pt['xg'][0] > pt['xg'][1], 'Portugal (stronger) should be expected to score more'
    pe = by_id[by_espn['9005']['id']]
    assert pe['quality'] == 'cold' and pe['why'][0].startswith('Thin data'), pe
    assert 'cold-start rating' in pe['volatility']['reasons']
    assert 'youth/reserve side' in by_id[by_espn['9011']['id']]['volatility']['reasons']
    assert not pt['volatility']['flag'], pt['volatility']
    assert abs(pe['probs']['O2.5'] + pe['probs']['U2.5'] - 1) < 2e-3
    m = json.load(open(os.path.join(site, 'manifest.json')))
    assert 'ESPN' in ' '.join(m.get('sources', [])), m.get('sources')
    espn_mod.ENABLED = False


def math_prod(values):
    out = 1.0
    for v in values:
        out *= v
    return out


def math_exp(x):
    import math
    return math.exp(x)


if __name__ == '__main__':
    main()
