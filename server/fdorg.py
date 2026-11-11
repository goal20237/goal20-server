"""football-data.org v4 (free plan): same-day fixtures and results for 12 top
competitions. Used while API-Football is unavailable.

Free plan: 10 requests a minute, scores delayed (not live). One /matches
request covers every competition of the plan for a range of dates.
"""
import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import config
from .net import fetch

HOST = 'https://api.football-data.org/v4'
KEY = os.environ.get('FOOTBALL_DATA_ORG_KEY', '').strip()

# Competition code -> football-data.co.uk division (to share league ids with
# the history) or a fixed API-Football league id for the cups.
DIVISION = {'PL': 'E0', 'ELC': 'E1', 'PD': 'SP1', 'BL1': 'D1', 'SA': 'I1',
            'FL1': 'F1', 'DED': 'N1', 'PPL': 'P1', 'BSA': 'BRA'}
CUP_LEAGUE = {'CL': 2, 'EC': 4, 'WC': 1}

STATUS = {
    'SCHEDULED': 'NS', 'TIMED': 'NS', 'IN_PLAY': 'LIVE', 'PAUSED': 'HT',
    'POSTPONED': 'PST', 'SUSPENDED': 'SUSP', 'CANCELLED': 'CANC', 'AWARDED': 'AWD',
}


class FdOrgError(Exception):
    pass


def available():
    return bool(KEY)


_last = [0.0]


def _get(path, params):
    wait = _last[0] + 6.5 - time.time()  # 10 requests a minute
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    url = f'{HOST}/{path}?{urllib.parse.urlencode(params)}'
    status, _, body = fetch(url, {'X-Auth-Token': KEY})
    try:
        data = json.loads(body.decode('utf-8') or '{}')
    except ValueError:
        data = {}
    if status != 200:
        msg = (data.get('message') if isinstance(data, dict) else None) or body.decode(errors='replace')[:160]
        text = f'HTTP {status}: {msg}'
        raise FdOrgError(text.replace(KEY, '[key]') if KEY else text)
    return data


def matches(date_from, date_to, log, depth=0):
    """All plan matches between two UTC dates (inclusive). A range the API
    refuses is split in two and retried."""
    try:
        data = _get('matches', {'dateFrom': date_from.isoformat(), 'dateTo': date_to.isoformat()})
    except FdOrgError as e:
        if depth < 2 and (date_to - date_from).days >= 2 and 'HTTP 400' in str(e):
            mid = date_from + timedelta(days=(date_to - date_from).days // 2)
            return (matches(date_from, mid, log, depth + 1)
                    + matches(mid + timedelta(days=1), date_to, log, depth + 1))
        raise
    out = data.get('matches') if isinstance(data, dict) else None
    return out if isinstance(out, list) else []


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _pair(obj):
    obj = obj or {}
    return _int(obj.get('home')), _int(obj.get('away'))


def to_fixture(m, team_id, league_for):
    """football-data.org match -> GOAL20 fixture JSON. [team_id](team, area,
    competition) returns (id, name, logo); [league_for](competition, area,
    season) returns the league JSON."""
    comp = m.get('competition') or {}
    area = m.get('area') or {}
    try:
        when = datetime.fromisoformat((m.get('utcDate') or '').replace('Z', '+00:00'))
    except ValueError:
        return None
    raw = (m.get('status') or '').upper()
    score = m.get('score') or {}
    duration = (score.get('duration') or 'REGULAR').upper()
    full = _pair(score.get('fullTime'))
    regular = _pair(score.get('regularTime'))
    extra = _pair(score.get('extraTime'))
    if raw == 'FINISHED':
        st = {'EXTRA_TIME': 'AET', 'PENALTY_SHOOTOUT': 'PEN'}.get(duration, 'FT')
    else:
        st = STATUS.get(raw, 'NS')
    # 90-minute score (what goals markets settle on) and the total shown.
    if duration != 'REGULAR' and regular[0] is not None:
        fh, fa = regular
        if extra[0] is not None:
            hg, ag = regular[0] + extra[0], regular[1] + extra[1]
        else:
            hg, ag = regular
    else:
        fh, fa = full
        hg, ag = full
    if st in ('NS', 'PST', 'CANC'):
        hg = ag = fh = fa = None
    season = (m.get('season') or {}).get('startDate') or ''
    season_year = _int(season[:4]) or (when.year if when.month >= 7 else when.year - 1)
    hid, hname, hlogo = team_id(m.get('homeTeam') or {}, area, comp)
    aid, aname, alogo = team_id(m.get('awayTeam') or {}, area, comp)
    if not hid or not aid:
        return None
    return {
        'id': 0,  # set by the caller
        'ts': int(when.timestamp()),
        'st': st,
        'stl': raw.replace('_', ' ').title(),
        'el': _int(m.get('minute')),
        'lg': league_for(comp, area, season_year),
        'rd': f"Matchday {m['matchday']}" if m.get('matchday') else (m.get('stage') or '').replace('_', ' ').title(),
        'vn': '',
        'h': {'id': hid, 'name': hname, 'logo': hlogo},
        'a': {'id': aid, 'name': aname, 'logo': alogo},
        'hg': hg, 'ag': ag, 'fh': fh if st in ('FT', 'AET', 'PEN') else None,
        'fa': fa if st in ('FT', 'AET', 'PEN') else None,
        'src': 'fdorg',
        'xid': _int(m.get('id')),
    }
