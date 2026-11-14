"""football-charts.com developer API (free key): fixtures and results for
about 90 leagues, lower divisions and African / Asian leagues included.

Free key: 200 requests a day per email address, 60 a minute, current and
previous season, no odds, no live scores. Their data updates once a day
(about 06:00 UTC), so GOAL20 reads it once a day and keeps a cache in
data/fc/. Terms: personal use, credit "Data by football-charts.com",
no resale.
"""
import gzip
import json
import os
import time
from datetime import datetime, timedelta, timezone

from . import config
from .net import fetch
from .store import read_json

HOST = 'https://footballcharts-backend.onrender.com/api/v1'
KEY = os.environ.get('FOOTBALL_CHARTS_KEY', '').strip()
ATTRIBUTION = 'Data by football-charts.com'

DAILY_CAP = 185          # of the 200 a day, a few stay spare
FETCH_AFTER_UTC = (6, 45)  # their overnight update is done by then

# Women's and youth competitions: never mixed into GOAL20's picks.
SKIP = {'wbelgium1', 'wengland1', 'wfrance1', 'wgermany1', 'wsweden', 'italy19', 'develop21'}
# Covered the same day by football-data.org: no need to spend requests.
FDORG = {'premier', 'cha', 'spain1', 'germany1', 'italy1', 'france1', 'holland1', 'portugal1', 'brazil1'}
# Leagues GOAL20 already has history for from football-data.co.uk
# (football-charts key -> football-data.co.uk division code).
DIVISION = {
    'eng1': 'E2', 'eng2': 'E3', 'national': 'EC',
    'scot-premier': 'SC0', 'scot-champ': 'SC1', 'scot1': 'SC2', 'scot2': 'SC3',
    'germany2': 'D2', 'italy2': 'I2', 'spain2': 'SP2', 'france2': 'F2',
    'belgium1': 'B1', 'turkey1': 'T1', 'greece1': 'G1',
    'austria1': 'AUT', 'swiss1': 'SWZ', 'denmark1': 'DNK', 'norway': 'NOR',
    'sweden1': 'SWE', 'poland1': 'POL', 'romania1': 'ROU', 'russia': 'RUS',
    'finland1': 'FIN', 'ireland1': 'IRL', 'japan1': 'JPN', 'china': 'CHN', 'usa1': 'USA',
}
STATUS = {'scheduled': 'NS', 'postponed': 'PST', 'cancelled': 'CANC', 'canceled': 'CANC',
          'abandoned': 'ABD', 'suspended': 'SUSP'}

DIR = os.path.join(config.DATA, 'fc')


class FcError(Exception):
    pass


def available():
    return bool(KEY)


def _mask(text):
    text = str(text)
    if KEY:
        text = text.replace(KEY, '[key]')
    return text


# ------------------------------------------------------------------ cache

def _path(*parts):
    return os.path.join(DIR, *parts)


def load(name, default=None):
    return read_json(_path(name), default)


def save(name, value):
    """Writes only when the content changed, so git keeps no copy of an
    unchanged file (gzip headers carry a time stamp)."""
    path = _path(name)
    if read_json(path) == value:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(value, separators=(',', ':'), ensure_ascii=False, sort_keys=True)
    tmp = path + '.tmp'
    if path.endswith('.gz'):
        with open(tmp, 'wb') as f:
            with gzip.GzipFile(fileobj=f, mode='wb', compresslevel=9, mtime=0) as g:
                g.write(text.encode('utf-8'))
    else:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
    os.replace(tmp, path)
    return True


def results_name(league, season):
    return f'res/{league}_{season}.json.gz'


def fixtures_name(league):
    return f'fx/{league}.json.gz'


# ------------------------------------------------------------------ client

class Client:
    def __init__(self, meta, log):
        self.meta = meta
        self.log = log
        self.used = 0
        self._last = 0.0
        self.stopped = ''
        self.fails = 0  # network errors / server errors in a row
        today = config.now_utc().date().isoformat()
        calls = meta.setdefault('calls', {})
        if calls.get('day') != today:
            calls.update({'day': today, 'n': 0})

    @property
    def left(self):
        return max(0, DAILY_CAP - self.meta['calls']['n'])

    def _request(self, path, params=None, count=True):
        if self.stopped:
            raise FcError(self.stopped)
        if count and self.left <= 0:
            raise FcError('daily request budget used')
        wait = self._last + 1.05 - time.time()  # 60 a minute
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        q = ''
        if params:
            q = '?' + '&'.join(f'{k}={v}' for k, v in params.items())
        # The first request can wake their server up (it sleeps when idle).
        status, _, body = fetch(f'{HOST}/{path}{q}', {'Authorization': f'Bearer {KEY}', 'Accept': 'application/json'},
                                timeout=90 if self.used == 0 else 45)
        if count:
            self.used += 1
            self.meta['calls']['n'] += 1
        try:
            data = json.loads(body.decode('utf-8') or '{}')
        except ValueError:
            data = {}
        if status == 0 or status >= 500:
            self.fails += 1
            if self.fails >= 3:
                self.stopped = f'football-charts.com is not responding (HTTP {status})'
                raise FcError(self.stopped)
        else:
            self.fails = 0
        if status == 429:
            self.stopped = 'daily limit reached at football-charts.com'
            raise FcError(self.stopped)
        if status in (401, 403) and 'season' not in _mask(body[:300].decode(errors='replace')).lower():
            self.stopped = f'key refused (HTTP {status})'
            raise FcError(self.stopped)
        if status != 200:
            msg = (data.get('detail') or data.get('error') or data.get('message')) if isinstance(data, dict) else None
            raise FcError(_mask(f'HTTP {status}: {msg or body[:120].decode(errors="replace")}'))
        return data

    def status(self):
        """Key tier and daily limit (free: does not use quota). Only plain
        numbers and short words are logged: never the key or the email."""
        try:
            data = self._request('keys/status/', count=False)
        except FcError as e:
            return f'status unavailable ({e})'
        parts = []
        flat = dict(data) if isinstance(data, dict) else {}
        for k, v in list(flat.items()):
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    flat[f'{k}.{k2}'] = v2
        for k, v in flat.items():
            lk = k.lower()
            if any(w in lk for w in ('key', 'email', 'mail', 'token', 'url')):
                continue
            if isinstance(v, bool) or isinstance(v, (int, float)) or (isinstance(v, str) and len(v) <= 24 and '@' not in v and not v.startswith('fc_')):
                parts.append(f'{k}={v}')
        return ', '.join(parts[:8]) or 'ok'

    def leagues(self):
        data = self._request('leagues/')
        out = {}
        for l in (data.get('leagues') or []):
            key = l.get('league')
            if key:
                out[key] = {'name': l.get('name') or key, 'country': l.get('country') or '',
                            'seasons': [str(s) for s in (l.get('seasons') or [])]}
        return out

    def fixtures(self, league):
        data = self._request(f'leagues/{league}/fixtures/')
        out = []
        for m in (data.get('matches') or []):
            d, t = m.get('match_date') or '', (m.get('time') or '')[:5]
            h, a = (m.get('home_team') or '').strip(), (m.get('away_team') or '').strip()
            if len(d) == 10 and h and a:
                out.append([d, t, h, a, (m.get('status') or 'scheduled').lower()])
        return out

    def results(self, league, season):
        data = self._request(f'leagues/{league}/results/', {'season': season})
        out = []
        for m in (data.get('matches') or []):
            hg, ag = parse_score(m.get('score'))
            h, a = (m.get('homeTeam') or '').strip(), (m.get('awayTeam') or '').strip()
            ts = kickoff_ts(m.get('kickoff_utc'), m.get('date'), m.get('time'))
            if hg is None or not h or not a or ts is None:
                continue
            out.append([ts, h, a, hg, ag])
        return out, (data.get('season_state') or '')


def parse_score(s):
    try:
        h, a = str(s).split(':')[:2]
        return int(h), int(a)
    except (ValueError, TypeError):
        return None, None


def kickoff_ts(iso, d=None, t=None):
    if iso:
        try:
            return int(datetime.fromisoformat(str(iso).replace('Z', '+00:00')).timestamp())
        except ValueError:
            pass
    if d and len(d) >= 10:
        hh, mm = 12, 0
        try:
            hh, mm = [int(x) for x in (t or '12:00').split(':')[:2]]
        except ValueError:
            pass
        try:
            y, mo, dd = [int(x) for x in d[:10].split('-')]
            return int(datetime(y, mo, dd, hh, mm, tzinfo=timezone.utc).timestamp())
        except ValueError:
            return None
    return None


def season_year(season):
    try:
        return int(str(season)[:4])
    except ValueError:
        return None


# ------------------------------------------------------------- planning

def wanted(leagues):
    return sorted(k for k in leagues if k not in SKIP and k not in FDORG)


def due_work(meta, leagues, now=None):
    """What to fetch today, most useful first: fixtures that are close,
    results of leagues that played, then history of new leagues."""
    now = now or config.now_utc()
    today = now.date()
    checked = meta.setdefault('checked', {})
    fixtures_due, results_due, history_due = [], [], []
    for key in wanted(leagues):
        info = leagues[key]
        seasons = info.get('seasons') or []
        if not seasons:
            continue
        c = checked.setdefault(key, {})
        last_fx = c.get('fx')
        dates = sorted(c.get('dates') or [])
        soon = [d for d in dates if today.isoformat() <= d <= (today + timedelta(days=2)).isoformat()]
        if last_fx is None or (last_fx < today.isoformat() and (soon or not [d for d in dates if d >= today.isoformat()]
                                                                 or last_fx <= (today - timedelta(days=2)).isoformat())):
            fixtures_due.append((min(soon) if soon else '9999', key))
        cur = seasons[0]
        needs_history = key not in DIVISION
        through = c.get('rs_through')  # results known up to this date
        played = [d for d in dates if (through is None or d > through) and d < today.isoformat()]
        if (needs_history and c.get('rs_season') != cur) or played:
            results_due.append(key)
        elif through and through < (today - timedelta(days=8)).isoformat() and c.get('rs_state') == 'in_season':
            results_due.append(key)  # weekly safety check
        if needs_history and len(seasons) > 1 and not c.get('prev_done'):
            history_due.append(key)
    work = [('fx', k) for _, k in sorted(fixtures_due)]
    work += [('rs', k) for k in results_due]
    work += [('prev', k) for k in history_due]
    return work


def can_fetch_now(meta, now=None):
    now = now or config.now_utc()
    last = meta.get('last_fetch')
    if not last:
        return True  # first run: start at once
    if last[:10] == now.date().isoformat():
        # Fetched today already; once more only if that was before their update.
        return last[11:16] < f'{FETCH_AFTER_UTC[0]:02d}:{FETCH_AFTER_UTC[1]:02d}' and (now.hour, now.minute) >= FETCH_AFTER_UTC
    return (now.hour, now.minute) >= FETCH_AFTER_UTC or last[:10] < (now.date() - timedelta(days=1)).isoformat()


def refresh(log):
    """One daily read within the budget. Returns a short summary line."""
    meta = load('meta.json', {}) or {}
    now = config.now_utc()
    if not can_fetch_now(meta, now):
        return 'cache only (read once a day after their 06:00 UTC update)'
    client = Client(meta, log)
    log(f'football-charts.com: key {client.status()}')
    leagues = meta.get('leagues') or {}
    try:
        if not leagues or (meta.get('leagues_at') or '') < (now.date() - timedelta(days=7)).isoformat():
            leagues = client.leagues()
            meta['leagues'], meta['leagues_at'] = leagues, now.date().isoformat()
    except FcError as e:
        save('meta.json', meta)
        return f'league list failed: {e}'
    work = due_work(meta, leagues, now)
    done = {'fx': 0, 'rs': 0, 'prev': 0}
    errors = []
    today = now.date().isoformat()
    for kind, key in work:
        if client.left <= 0 or client.stopped:
            break
        c = meta['checked'].setdefault(key, {})
        seasons = leagues[key]['seasons']
        try:
            if kind == 'fx':
                fx = client.fixtures(key)
                save(fixtures_name(key), {'at': now.isoformat(), 'm': fx})
                keep_from = (now.date() - timedelta(days=14)).isoformat()
                dates = {d for d in (c.get('dates') or []) if d >= keep_from}
                dates.update(m[0] for m in fx if m[4] == 'scheduled')
                c['dates'] = sorted(dates)
                c['fx'] = today
            elif kind == 'rs':
                rows, state = client.results(key, seasons[0])
                save(results_name(key, seasons[0]), {'season': seasons[0], 'state': state, 'm': rows})
                c['rs_season'], c['rs_state'] = seasons[0], state
                # Their data at this hour covers matches up to yesterday
                # (before their morning update: up to the day before).
                lag = 1 if (now.hour, now.minute) >= FETCH_AFTER_UTC else 2
                c['rs_through'] = (now.date() - timedelta(days=lag)).isoformat()
            else:
                rows, state = client.results(key, seasons[1])
                save(results_name(key, seasons[1]), {'season': seasons[1], 'state': state, 'm': rows})
                c['prev_done'] = True
            done[kind] += 1
        except FcError as e:
            if 'season' in str(e).lower() and kind == 'prev':
                c['prev_done'] = True  # previous season not on the free key
            errors.append(f'{key}: {e}')
            if client.stopped:
                break
    meta['last_fetch'] = now.isoformat()[:19]
    save('meta.json', meta)
    left = len(work) - sum(done.values())
    line = (f"{done['fx']} fixture lists, {done['rs']} result lists, {done['prev']} past seasons read; "
            f"{client.used} requests today-run, {meta['calls']['n']}/{DAILY_CAP} today")
    if left > 0:
        line += f'; {left} waiting for tomorrow'
    if errors:
        line += '; problems: ' + '; '.join(errors[:3])
    return line


def cached(leagues=None):
    """(leagues, fixtures{league: rows}, results{league: [(season, rows)]}) from the cache."""
    meta = load('meta.json', {}) or {}
    leagues = leagues or meta.get('leagues') or {}
    fixtures, results = {}, {}
    for key in wanted(leagues):
        f = load(fixtures_name(key))
        if f and f.get('m'):
            fixtures[key] = f['m']
        for season in (leagues[key].get('seasons') or [])[:2]:
            r = load(results_name(key, season))
            if r and r.get('m'):
                results.setdefault(key, []).append((season, r['m']))
    return leagues, fixtures, results
