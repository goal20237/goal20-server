"""National teams (server 1.9): every international result since 2019 from
the free public-domain dataset github.com/martj42/international_results
(read once a day), topped up with the latest results from ESPN. All national
teams are rated together in one pool ("international football"), with a
longer memory than clubs (national teams play about 10 matches a year) and
friendlies counting half.
"""
import csv
import gzip
import io
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone

from . import config
from .net import fetch

URL = 'https://raw.githubusercontent.com/martj42/international_results/master/results.csv'
PATH = os.path.join(config.DATA, 'intl', 'results.json.gz')
SINCE = '2019-01-01'
POOL_NAME = 'International football'
FRIENDLY_WEIGHT = 0.5

# ESPN / other spellings -> the dataset's spelling (after canonical()).
ALIASES = {
    'bosnia herzegovina': 'bosnia and herzegovina', 'congo dr': 'dr congo', 'dr congo': 'dr congo',
    'democratic republic of congo': 'dr congo', 'congo kinshasa': 'dr congo', 'czechia': 'czech republic',
    'turkiye': 'turkey', 'kyrgyz republic': 'kyrgyzstan', 'chinese taipei': 'taiwan',
    'korea republic': 'south korea', 'republic of korea': 'south korea', 'korea dpr': 'north korea',
    'dpr korea': 'north korea', 'ir iran': 'iran', 'china pr': 'china', 'cote divoire': 'ivory coast',
    'cote d ivoire': 'ivory coast', 'usa': 'united states', 'cabo verde': 'cape verde',
    'cape verde islands': 'cape verde', 'east timor': 'timor leste', 'swaziland': 'eswatini',
    'brunei darussalam': 'brunei', 'macao': 'macau', 'hong kong china': 'hong kong', 'the gambia': 'gambia',
    'trinidad tobago': 'trinidad and tobago', 'antigua': 'antigua and barbuda', 'saint kitts nevis':
    'saint kitts and nevis', 'us virgin islands': 'united states virgin islands', 'ireland': 'republic of ireland',
    'fyr macedonia': 'north macedonia', 'macedonia': 'north macedonia', 'sao tome principe': 'sao tome and principe',
    'curacao': 'curacao', 'palestine': 'palestine', 'state of palestine': 'palestine',
    'saint vincent grenadines': 'saint vincent and the grenadines', 'laos': 'laos', 'lao pdr': 'laos',
    'vietnam': 'vietnam', 'viet nam': 'vietnam', 'central african rep': 'central african republic',
    'equ guinea': 'equatorial guinea', 'guinea bissau': 'guinea bissau',
}


def canonical(name):
    s = unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode().lower()
    s = s.replace('&', ' and ').replace("'", '')
    s = re.sub(r'\bst\.?\s', 'saint ', s + ' ')
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    s = ' '.join(s.split())
    return ALIASES.get(s, s)


def _synthetic(text):
    # Same scheme as main.synthetic_id (kept here to avoid an import cycle).
    h = 0x811c9dc5
    for b in text.encode('utf-8'):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return -((h & 0x3fffffff) + 1)


def team_id(name):
    return _synthetic('nation|' + canonical(name))


POOL = _synthetic('pool|international')


def refresh(log=print):
    """Downloads the dataset (once a day) and keeps results since SINCE."""
    try:
        if time.time() - os.path.getmtime(PATH) < 20 * 3600:
            return f'{len(load())} results (read today)'
    except OSError:
        pass
    status, _, body = fetch(URL, timeout=60)
    if status != 200 or not body:
        return f'HTTP {status}: kept the stored copy ({len(load())} results)'
    rows = []
    for r in csv.DictReader(io.StringIO(body.decode('utf-8', 'replace'))):
        if (r.get('date') or '') < SINCE:
            continue
        try:
            hs, as_ = int(r['home_score']), int(r['away_score'])
        except (TypeError, ValueError, KeyError):
            continue
        rows.append([r['date'], r['home_team'], r['away_team'], hs, as_, r.get('tournament') or '',
                     1 if (r.get('neutral') or '').upper() == 'TRUE' else 0])
    if not rows:
        return f'empty answer: kept the stored copy ({len(load())} results)'
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with gzip.GzipFile(PATH, 'wb', mtime=0) as f:
        f.write(json.dumps(rows, separators=(',', ':'), ensure_ascii=False).encode())
    return f'{len(rows)} results since {SINCE} (latest {rows[-1][0]})'


def load():
    try:
        with gzip.open(PATH, 'rt', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _ts(date):
    return int(datetime.strptime(date, '%Y-%m-%d').replace(hour=18, tzinfo=timezone.utc).timestamp())


def history_rows(espn_events=()):
    """Rows for the national-team pool:
    [id, ts, POOL, season, home, away, hg, ag, None x6, neutral, weight].
    ESPN results fill in the days after the dataset's last date."""
    out, seen = [], set()
    data = load()
    last = data[-1][0] if data else '0000'
    for date, h, a, hg, ag, tour, neutral in data:
        hid, aid = team_id(h), team_id(a)
        key = (date, hid, aid)
        if key in seen:
            continue
        seen.add(key)
        weight = FRIENDLY_WEIGHT if 'friendly' in tour.lower() else 1.0
        out.append([_synthetic(f'intl|{date}|{hid}|{aid}'), _ts(date), POOL, int(date[:4]), hid, aid, hg, ag,
                    None, None, None, None, None, None, neutral, weight])
    for e in espn_events:
        if e.get('fh') is None or e.get('fa') is None:
            continue
        date = datetime.fromtimestamp(e['ts'], timezone.utc).date().isoformat()
        if date <= last:
            continue
        hid, aid = team_id(e['h']['n']), team_id(e['a']['n'])
        if (date, hid, aid) in seen:
            continue
        seen.add((date, hid, aid))
        weight = FRIENDLY_WEIGHT if 'friendly' in e.get('lg', '') else 1.0
        out.append([_synthetic(f'intl|{date}|{hid}|{aid}'), e['ts'], POOL, int(date[:4]), hid, aid,
                    e['fh'], e['fa'], None, None, None, None, None, None, 0, weight])
    out.sort(key=lambda r: r[1])
    return out


def known_teams():
    """Canonical names of every national team in the dataset."""
    names = set()
    for r in load():
        names.add(canonical(r[1]))
        names.add(canonical(r[2]))
    return names
