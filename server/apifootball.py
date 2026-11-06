"""API-Football v3 client with quota protection and plan-limit detection."""
import json
import re
import time
import urllib.parse

from . import config
from .net import fetch

HOST = 'https://v3.football.api-sports.io'


class ApiError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind  # plan, quota, key, rate, network, other
        self.message = message

    @property
    def window(self):
        m = re.search(r'from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})', self.message)
        return (m.group(1), m.group(2)) if m else None


class Api:
    def __init__(self, state, budget):
        self.state = state
        self.budget = budget
        self.used = 0
        self.remaining = None
        self.limit = None
        self._last = 0.0
        self.spacing = 0.25 if config.IS_PRO else 6.5

    @property
    def available(self):
        return bool(config.API_KEY)

    def can_spend(self):
        if not self.available or self.used >= self.budget:
            return False
        return self.remaining is None or self.remaining > config.RESERVE

    def blocked(self, cap):
        until = self.state.blocked.get(cap)
        return until is not None and until > time.time()

    def get(self, endpoint, params=None, cap=None, counts=True):
        if not self.available:
            raise ApiError('key', 'No API_FOOTBALL_KEY secret is set')
        if cap and self.blocked(cap):
            raise ApiError('plan', self.state.blocked_why.get(cap, 'Blocked on this plan'))
        if counts and not self.can_spend():
            raise ApiError('quota', f'Run budget reached ({self.used} used, provider remaining {self.remaining})')
        wait = self._last + self.spacing - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        q = ('?' + urllib.parse.urlencode(params)) if params else ''
        if counts:
            self.used += 1
        status, headers, body = fetch(f'{HOST}/{endpoint}{q}', {'x-apisports-key': config.API_KEY})
        lower = {k.lower(): v for k, v in headers.items()}
        try:
            if 'x-ratelimit-requests-remaining' in lower:
                self.remaining = int(lower['x-ratelimit-requests-remaining'])
            if 'x-ratelimit-requests-limit' in lower:
                self.limit = int(lower['x-ratelimit-requests-limit'])
        except ValueError:
            pass
        if status == 0:
            raise ApiError('network', body.decode(errors='replace')[:200])
        if status in (401, 403):
            raise ApiError('key', f'HTTP {status}')
        if status == 429:
            raise ApiError('rate', 'HTTP 429')
        if status >= 500:
            raise ApiError('other', f'HTTP {status}')
        try:
            data = json.loads(body.decode('utf-8'))
        except Exception:
            raise ApiError('other', 'Unreadable response')
        errors = data.get('errors')
        pairs = []
        if isinstance(errors, dict):
            pairs = [(str(k).lower(), str(v)) for k, v in errors.items()]
        elif isinstance(errors, list):
            pairs = [('', str(v)) for v in errors]
        if pairs:
            text = ' · '.join(v for _, v in pairs).replace(config.API_KEY, '[key]')
            keys = {k for k, _ in pairs}
            low = text.lower()
            if 'token' in keys or 'application key' in low:
                kind = 'key'
            elif 'requests' in keys or 'request limit' in low:
                kind = 'quota'
                self.remaining = 0
            elif 'ratelimit' in keys or 'too many' in low:
                kind = 'rate'
            elif 'plan' in keys or 'do not have access' in low or 'free plan' in low:
                kind = 'plan'
            else:
                kind = 'other'
            if kind == 'plan' and cap and cap != 'date':
                self.state.block(cap, text)
            raise ApiError(kind, text)
        return data

    def rows(self, endpoint, params=None, cap=None):
        data = self.get(endpoint, params, cap)
        r = data.get('response')
        return r if isinstance(r, list) else []

    def status(self):
        data = self.get('status', counts=False)
        r = data.get('response') or {}
        if isinstance(r, dict):
            sub = r.get('subscription') or {}
            req = r.get('requests') or {}
            try:
                self.limit = int(req.get('limit_day'))
                self.remaining = self.limit - int(req.get('current'))
            except (TypeError, ValueError):
                pass
            return sub.get('plan')
        return None


# ---------------------------------------------------------------- conversion

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_app_fixture(row):
    """API-Football fixture -> the GOAL20 app's compact Fixture JSON."""
    f = row.get('fixture') or {}
    st = f.get('status') or {}
    lg = row.get('league') or {}
    teams = row.get('teams') or {}
    goals = row.get('goals') or {}
    ft = (row.get('score') or {}).get('fulltime') or {}
    venue = f.get('venue') or {}
    h = teams.get('home') or {}
    a = teams.get('away') or {}
    return {
        'id': _int(f.get('id')) or 0,
        'ts': _int(f.get('timestamp')) or 0,
        'st': st.get('short') or 'NS',
        'stl': st.get('long') or '',
        'el': _int(st.get('elapsed')),
        'lg': {
            'id': _int(lg.get('id')) or 0,
            'name': lg.get('name') or '',
            'country': lg.get('country') or '',
            'logo': lg.get('logo') or '',
            'flag': lg.get('flag') or '',
            'season': _int(lg.get('season')) or 0,
        },
        'rd': lg.get('round') or '',
        'vn': ', '.join(x for x in [venue.get('name'), venue.get('city')] if x),
        'h': {'id': _int(h.get('id')) or 0, 'name': h.get('name') or '', 'logo': h.get('logo') or ''},
        'a': {'id': _int(a.get('id')) or 0, 'name': a.get('name') or '', 'logo': a.get('logo') or ''},
        'hg': _int(goals.get('home')),
        'ag': _int(goals.get('away')),
        'fh': _int(ft.get('home')),
        'fa': _int(ft.get('away')),
    }


FINISHED = {'FT', 'AET', 'PEN'}


def hist_row(fx):
    """App fixture JSON -> history row [id, ts, league, season, home, away, hg, ag]."""
    if fx['st'] not in FINISHED:
        return None
    hg = fx['fh'] if fx['fh'] is not None else fx['hg']
    ag = fx['fa'] if fx['fa'] is not None else fx['ag']
    if hg is None or ag is None or not fx['h']['id'] or not fx['a']['id']:
        return None
    return [fx['id'], fx['ts'], fx['lg']['id'], fx['lg']['season'], fx['h']['id'], fx['a']['id'], hg, ag]


def parse_odds(row):
    """API-Football /odds row -> GOAL20 quotes [{'b','m','o'}]."""
    out = []
    for bm in row.get('bookmakers') or []:
        name = bm.get('name') or 'Bookmaker'
        for bet in bm.get('bets') or []:
            bet_name = (bet.get('name') or '').strip().lower()
            for v in bet.get('values') or []:
                label = str(v.get('value') or '').strip().lower()
                try:
                    odd = float(v.get('odd'))
                except (TypeError, ValueError):
                    continue
                if odd <= 1:
                    continue
                m = None
                if bet_name == 'goals over/under':
                    m = {'over 1.5': 'over15', 'over 2.5': 'over25'}.get(label)
                elif bet_name == 'both teams score' and label == 'yes':
                    m = 'btts'
                if m:
                    out.append({'b': name, 'm': m, 'o': odd})
    return out
