"""ESPN's free, keyless soccer scoreboard (server 1.9): every competition ESPN
covers in ONE request per day (the "scorepanel": events grouped by league),
including national teams (UEFA and CONCACAF Nations League, AFCON and World
Cup qualifiers, friendlies) and about 60 club leagues and cups.

It is an unofficial public endpoint: ESPN can change or limit it at any
time. GOAL20 asks politely (a few requests per run, one per second) and
never depends on it alone.

Days are kept in data/espn/<YYYYMMDD>.json.gz (committed by full runs), so
finished days are read once. Youth and reserve competitions are included;
women's, US college and club-friendly competitions are left out.
"""
import gzip
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from . import config
from .net import fetch

BASE = 'https://site.api.espn.com/apis/site/v2/sports/soccer/'
DIR = os.path.join(config.DATA, 'espn')
ENABLED = os.environ.get('GOAL20_ESPN', '1') != '0'
RECENT_FULL = 3          # full runs refresh the dates today-3 .. today+3
RECENT_QUICK = 1         # quick runs refresh yesterday .. tomorrow
BACKFILL_DAYS = int(os.environ.get('ESPN_BACKFILL_DAYS') or 150)
BACKFILL_PER_RUN = int(os.environ.get('ESPN_BACKFILL_PER_RUN') or 40)
PAUSE = 0.8              # seconds between requests

# National-team competitions (their teams are rated together, see
# internationals.py).
INTL = {'uefa.nations', 'concacaf.nations.league', 'caf.nations_qual', 'caf.nations', 'fifa.friendly',
        'fifa.world', 'uefa.euro', 'uefa.euroq', 'afc.asian.cup', 'afc.cupq', 'conmebol.america',
        'concacaf.gold', 'concacaf.goldq', 'fifa.confederations', 'uefa.nations.playoff'}

# ESPN slug prefix -> country (as GOAL20 names it).
COUNTRY = {
    'eng': 'England', 'sco': 'Scotland', 'wal': 'Wales', 'nir': 'Northern-Ireland', 'irl': 'Ireland',
    'esp': 'Spain', 'ger': 'Germany', 'ita': 'Italy', 'fra': 'France', 'ned': 'Netherlands',
    'por': 'Portugal', 'bel': 'Belgium', 'tur': 'Turkey', 'gre': 'Greece', 'aut': 'Austria',
    'sui': 'Switzerland', 'den': 'Denmark', 'nor': 'Norway', 'swe': 'Sweden', 'fin': 'Finland',
    'rus': 'Russia', 'ukr': 'Ukraine', 'pol': 'Poland', 'cze': 'Czech-Republic', 'rou': 'Romania',
    'cro': 'Croatia', 'srb': 'Serbia', 'isr': 'Israel', 'cyp': 'Cyprus', 'usa': 'USA', 'mex': 'Mexico',
    'can': 'Canada', 'bra': 'Brazil', 'arg': 'Argentina', 'col': 'Colombia', 'chi': 'Chile',
    'uru': 'Uruguay', 'per': 'Peru', 'ecu': 'Ecuador', 'par': 'Paraguay', 'bol': 'Bolivia',
    'ven': 'Venezuela', 'gua': 'Guatemala', 'slv': 'El-Salvador', 'hon': 'Honduras', 'crc': 'Costa-Rica',
    'pan': 'Panama', 'jam': 'Jamaica', 'jpn': 'Japan', 'kor': 'South-Korea', 'chn': 'China',
    'aus': 'Australia', 'ksa': 'Saudi-Arabia', 'uae': 'United-Arab-Emirates', 'qat': 'Qatar',
    'ind': 'India', 'idn': 'Indonesia', 'tha': 'Thailand', 'mys': 'Malaysia', 'sgp': 'Singapore',
    'rsa': 'South-Africa', 'nga': 'Nigeria', 'gha': 'Ghana', 'egy': 'Egypt', 'mar': 'Morocco',
    'tun': 'Tunisia', 'ken': 'Kenya', 'uga': 'Uganda',
}

STATUS_PRE = {'STATUS_POSTPONED': 'PST', 'STATUS_CANCELED': 'CANC', 'STATUS_CANCELLED': 'CANC',
              'STATUS_ABANDONED': 'ABD', 'STATUS_SUSPENDED': 'SUSP'}
STATUS_IN = {'STATUS_HALFTIME': 'HT', 'STATUS_FIRST_HALF': '1H', 'STATUS_SECOND_HALF': '2H',
             'STATUS_EXTRA_TIME': 'ET', 'STATUS_OVERTIME': 'ET', 'STATUS_SHOOTOUT': 'P',
             'STATUS_END_OF_REGULATION': 'ET', 'STATUS_END_OF_EXTRATIME': 'ET', 'STATUS_SUSPENDED': 'SUSP'}
STATUS_POST = {'STATUS_FINAL_PEN': 'PEN', 'STATUS_FINAL_AET': 'AET', 'STATUS_FINAL_ET': 'AET',
               'STATUS_ABANDONED': 'ABD', 'STATUS_CANCELED': 'CANC', 'STATUS_POSTPONED': 'PST',
               'STATUS_FORFEIT': 'AWD'}
LONG = {'NS': 'Not Started', 'PST': 'Postponed', 'CANC': 'Cancelled', 'ABD': 'Abandoned', 'SUSP': 'Suspended',
        'HT': 'Halftime', '1H': 'First Half', '2H': 'Second Half', 'ET': 'Extra Time', 'P': 'Penalties',
        'FT': 'Match Finished', 'AET': 'After Extra Time', 'PEN': 'After Penalties', 'AWD': 'Awarded'}

# Left out: women's football, US college football and club friendlies.
# Youth (U17-U23) and reserve competitions are kept (server 1.9: "all
# football"); they are rated from thin data and marked as such.
_EXCLUDE = re.compile(r'(\.w\.|\.w$|^w\.|friendly\.w|ncaa|club\.friendly)', re.I)
_EXCLUDE_NAME = re.compile(r"(women|femenil|feminin|ladies|liga f\b|college|ncaa)", re.I)
_YOUTH = re.compile(r'(\bu-?\d{2}\b|_u\d{2}|\.u\d{2}|under-\d{2}|youth|reserve|\bii\b|\bb team)', re.I)


def excluded(slug, name=''):
    return bool(_EXCLUDE.search(slug or '')) or bool(_EXCLUDE_NAME.search(name or ''))


def is_youth(slug, name=''):
    return bool(_YOUTH.search(slug or '')) or bool(_YOUTH.search(name or ''))


def is_international(slug):
    if is_youth(slug):
        return False  # youth national teams are rated apart from senior ones
    return slug in INTL or (slug or '').startswith(('fifa.worldq', 'fifa.wc_q', 'uefa.euroq'))


def country_of(slug):
    if is_international(slug):
        return 'World'
    return COUNTRY.get((slug or '').split('.')[0], '')


def status_code(status):
    t = (status or {}).get('type') or {}
    name, state = t.get('name') or '', t.get('state') or ''
    if state == 'pre':
        return STATUS_PRE.get(name, 'NS')
    if state == 'in':
        if name in STATUS_IN:
            return STATUS_IN[name]
        return '2H' if (status or {}).get('period', 1) >= 2 else '1H'
    if state == 'post':
        return STATUS_POST.get(name, 'FT')
    return 'NS'


def minute_of(display):
    """"59'" -> 59, "90'+6'" -> 96, "45'+2'" -> 47, "" -> None."""
    nums = [int(x) for x in re.findall(r'\d+', display or '')]
    if not nums:
        return None
    return nums[0] + (nums[1] if len(nums) > 1 else 0)


def _regular_time(details, home_id, away_id):
    """90-minute score from the goal list (for matches with extra time)."""
    h = a = 0
    for d in details or []:
        if not d.get('scoringPlay'):
            continue
        if 'shootout' in ((d.get('type') or {}).get('text') or '').lower():
            continue
        nums = [int(x) for x in re.findall(r'\d+', ((d.get('clock') or {}).get('displayValue')) or '')]
        if nums and nums[0] > 90:
            continue
        tid = str((d.get('team') or {}).get('id') or '')
        if tid == home_id:
            h += 1
        elif tid == away_id:
            a += 1
    return h, a


def parse_ts(text):
    raw = (text or '').replace('Z', '').replace('+00:00', '')
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            return int(datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def parse_event(e, slug, day):
    """One ESPN event -> compact dict (or None)."""
    try:
        comp = (e.get('competitions') or [{}])[0]
        sides = {c.get('homeAway'): c for c in comp.get('competitors') or []}
        home, away = sides.get('home'), sides.get('away')
        if not home or not away:
            return None
        ts = parse_ts(e['date'])
    except (KeyError, ValueError, TypeError):
        return None
    if ts is None:
        return None
    status = e.get('status') or comp.get('status') or {}
    code = status_code(status)
    hg = ag = fh = fa = None
    if code not in ('NS', 'PST', 'CANC'):
        try:
            hg, ag = int(home.get('score')), int(away.get('score'))
        except (TypeError, ValueError):
            hg = ag = None
    if code in ('FT', 'AWD'):
        fh, fa = hg, ag
    elif code in ('AET', 'PEN') and hg is not None:
        fh, fa = _regular_time(comp.get('details'), str(home['team'].get('id')), str(away['team'].get('id')))

    def team(c):
        t = c.get('team') or {}
        return {'id': str(t.get('id') or ''), 'n': t.get('displayName') or t.get('name') or '',
                'logo': t.get('logo') or ''}

    return {
        'id': str(e.get('id')), 'ts': ts, 'lg': slug, 'd': day, 'st': code,
        'el': minute_of(status.get('displayClock')) if code in ('1H', '2H', 'ET') else None,
        'h': team(home), 'a': team(away), 'hg': hg, 'ag': ag, 'fh': fh, 'fa': fa,
    }


def parse_scorepanel(data, day):
    """{leagues: {slug: {id, name}}, events: [...]} without excluded competitions."""
    leagues, events = {}, []
    for g in (data or {}).get('scores') or []:
        lg = (g.get('leagues') or [{}])[0]
        slug, name = lg.get('slug') or '', lg.get('name') or ''
        if not slug or excluded(slug, name):
            continue
        leagues[slug] = {'id': str(lg.get('id') or ''), 'name': name}
        for e in g.get('events') or []:
            ev = parse_event(e, slug, day)
            if ev:
                events.append(ev)
    return {'leagues': leagues, 'events': events}


def _path(day):
    return os.path.join(DIR, f'{day}.json.gz')


def load_day(day):
    try:
        with gzip.open(_path(day), 'rt', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_day(day, body):
    os.makedirs(DIR, exist_ok=True)
    raw = json.dumps(body, separators=(',', ':'), ensure_ascii=False, sort_keys=True).encode()
    old = load_day(day)
    if old is not None and {k: v for k, v in old.items() if k != 'at'} == {k: v for k, v in body.items() if k != 'at'}:
        return False
    with gzip.GzipFile(_path(day), 'wb', mtime=0) as f:
        f.write(raw)
    return True


def fetch_day(day):
    """(parsed day or None, note)."""
    status, headers, body = fetch(f'{BASE}scorepanel?dates={day}',
                                  {'Accept': 'application/json', 'Accept-Encoding': 'gzip'}, timeout=40)
    if status != 200:
        return None, f'HTTP {status}'
    try:
        if (headers.get('Content-Encoding') or headers.get('content-encoding') or '').lower() == 'gzip' \
                or body[:2] == b'\x1f\x8b':
            body = gzip.decompress(body)
        return parse_scorepanel(json.loads(body), day), ''
    except (ValueError, OSError) as e:
        return None, f'bad answer ({e})'


def _dates(center, before, after):
    return [(center + timedelta(days=i)).strftime('%Y%m%d') for i in range(-before, after + 1)]


def refresh(full, log=print):
    """Refreshes the recent dates and, in full runs, back-fills older ones.
    Returns a one-line summary."""
    if not ENABLED:
        return 'off'
    today = config.now_utc().date()
    recent = _dates(today, RECENT_FULL if full else RECENT_QUICK, RECENT_FULL if full else RECENT_QUICK)
    todo = list(recent)
    if full:
        missing = [d for d in _dates(today - timedelta(days=RECENT_FULL + 1), BACKFILL_DAYS, 0)
                   if not os.path.exists(_path(d))]
        todo += sorted(missing, reverse=True)[:BACKFILL_PER_RUN]
    fetched = changed = events = fails = 0
    for i, day in enumerate(todo):
        if i:
            time.sleep(PAUSE)
        parsed, note = fetch_day(day)
        if parsed is None:
            fails += 1
            log(f'ESPN {day}: {note}')
            if fails >= 3:
                break
            continue
        fails = 0
        fetched += 1
        events += len(parsed['events'])
        parsed['at'] = int(time.time())
        if save_day(day, parsed):
            changed += 1
    have = len([n for n in os.listdir(DIR)]) if os.path.isdir(DIR) else 0
    return f'{fetched} day(s) read ({events} matches), {changed} changed, {have} days stored'


def cached_days(since_days=None):
    """{YYYYMMDD: day} for the stored days (optionally only recent ones)."""
    out = {}
    if not os.path.isdir(DIR):
        return out
    cut = None
    if since_days is not None:
        cut = (config.now_utc().date() - timedelta(days=since_days)).strftime('%Y%m%d')
    for name in sorted(os.listdir(DIR)):
        if not name.endswith('.json.gz'):
            continue
        day = name[:8]
        if cut and day < cut:
            continue
        d = load_day(day)
        if d:
            out[day] = d
    return out


def prune(keep_days=500):
    if not os.path.isdir(DIR):
        return
    cut = (config.now_utc().date() - timedelta(days=keep_days)).strftime('%Y%m%d')
    for name in os.listdir(DIR):
        if name[:8] < cut:
            os.remove(os.path.join(DIR, name))
