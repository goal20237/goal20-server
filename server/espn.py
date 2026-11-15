"""ESPN's free, keyless soccer scoreboard (server 1.9): one request per day
for ESPN's combined list (the "scorepanel": events grouped by league),
including national teams (UEFA and CONCACAF Nations League, AFCON and World
Cup qualifiers, friendlies).

Server 1.12: the combined list is NOT complete (14 Nov 2026: 12 leagues,
while English League One and Two, the National League, LaLiga 2, the
Scottish Championship, Brazil Serie B and more all had matches). So about
200 competitions are also read one by one (LEAGUES), only on their match
days (each league's own calendar) or, when ESPN gives no calendar, on the
days around today. Competitions ESPN does not know are learnt (HTTP 400)
and left alone for a month.

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
PAUSE_LEAGUE = 0.35      # seconds between league-by-league requests
META = os.path.join(DIR, 'leagues.json')
CALENDAR_EVERY_DAYS = 3  # a league's calendar is re-read every 3 days (full runs)
BAD_RETRY_DAYS = 30      # a slug ESPN does not know is tried again a month later
LEAGUE_DAYS_FULL = (-2, 1)    # ESPN dates read league by league in full runs
LEAGUE_DAYS_QUICK = (-1, 0)   # ... and in quick runs (live scores)
LEAGUE_BACKFILL_PER_RUN = int(os.environ.get('ESPN_LEAGUE_BACKFILL_PER_RUN') or 40)
MAX_LEAGUE_REQUESTS = {True: 450, False: 150}  # per full / quick run: a hard cap
DONE = {'FT', 'AET', 'PEN', 'AWD', 'PST', 'CANC', 'ABD'}

# Competitions read one by one (ESPN slugs). ESPN's own league list without
# women's, college and friendly competitions, plus leagues ESPN answers for
# but does not list (checked 15 Nov 2026). Not on ESPN at all (HTTP 400),
# so not here: J2 / J3 / J.League Cup, China League One, Hong Kong, Korea,
# Ethiopia, Egypt, Morocco, Poland, Croatia, Serbia, Ukraine, Hungary,
# Germany 3. Liga, Spain Primera RFEF, Portugal Liga 2, Belgium second tier.
LEAGUES = [
    'fifa.world', 'uefa.champions', 'eng.1', 'esp.1', 'usa.1', 'uefa.europa', 'uefa.europa.conf',
    'eng.league_cup', 'concacaf.nations.league', 'uefa.nations', 'ger.1', 'ita.1', 'mex.1', 'fifa.friendly',
    'concacaf.leagues.cup', 'campeones.cup', 'fra.1', 'eng.fa', 'esp.copa_del_rey', 'ger.dfb_pokal',
    'ita.coppa_italia', 'fra.coupe_de_france', 'ned.1', 'esp.super_cup', 'ita.super_cup', 'fra.super_cup',
    'ger.playoff.relegation', 'ger.super_cup', 'eng.2', 'eng.charity', 'usa.open', 'conmebol.libertadores',
    'concacaf.champions', 'fifa.worldq.uefa', 'fifa.wcq.ply', 'fifa.worldq.concacaf', 'fifa.worldq.afc',
    'fifa.worldq.caf', 'fifa.worldq.conmebol', 'fifa.worldq.ofc', 'uefa.champions_qual', 'ned.cup', 'sco.1',
    'sco.tennents', 'sco.cis', 'aus.1', 'ksa.1', 'ksa.kings.cup', 'por.1', 'por.taca.portugal', 'tur.1',
    'caf.nations', 'afc.champions', 'caf.nations_qual', 'afc.cup', 'fifa.cwc', 'fifa.olympics',
    'concacaf.gold', 'concacaf.gold_qual', 'concacaf.confederations_playoff', 'uefa.euro', 'uefa.euroq',
    'uefa.euro_u21', 'uefa.super_cup', 'conmebol.america', 'usa.usl.1', 'usa.usl.l1', 'usa.usl.l1.cup',
    'mex.2', 'global.finalissima', 'global.u20.intercontinental_cup', 'fifa.world.u20', 'afc.asian.cup',
    'afc.cupq', 'aff.championship', 'caf.championship', 'uefa.europa_qual', 'uefa.europa.conf_qual',
    'fifa.intercontinental_cup', 'afc.champions_qual', 'afc.cup_qual', 'rus.1', 'rus.1.promotion.relegation',
    'bel.1', 'bel.promotion.relegation', 'esp.2', 'ger.2', 'ita.2', 'fra.1.promotion.relegation', 'fra.2',
    'por.1.promotion.relegation', 'aut.1', 'gre.1', 'chn.1', 'ned.supercup', 'fifa.conmebol.olympicsq',
    'fifa.concacaf.olympicsq', 'fifa.world.u17', 'uefa.euro_u21_qual', 'uefa.euro.u19', 'fifa.friendly_u21',
    'ger.2.promotion.relegation', 'eng.trophy', 'eng.3', 'eng.4', 'eng.5', 'eng.fa_qual',
    'sco.1.promotion.relegation', 'sco.2', 'sco.2.promotion.relegation', 'sco.challenge',
    'sco.tennents_qual', 'ned.playoff.relegation', 'ned.2', 'ned.3.promotion.relegation', 'swe.1',
    'swe.1.promotion.relegation', 'den.1', 'nor.1.promotion.relegation', 'nor.1', 'conmebol.sudamericana',
    'conmebol.recopa', 'arg.1', 'arg.copa', 'arg.copa_de_la_superliga', 'arg.trofeo_de_la_campeones',
    'arg.2', 'arg.supercopa', 'arg.supercopa.internacional', 'arg.3', 'bra.supercopa_do_brazil', 'bra.1',
    'bra.2', 'bra.copa_do_brazil', 'bra.camp.carioca', 'bra.camp.paulista', 'bra.camp.gaucho',
    'bra.camp.mineiro', 'chi.super_cup', 'chi.1', 'chi.1.promotion.relegation', 'chi.copa_chi', 'uru.1',
    'uru.2', 'col.superliga', 'col.1', 'col.copa', 'per.1', 'par.1', 'par.1.supercopa', 'ecu.1', 'ven.1',
    'bol.ply.rel', 'bol.copa', 'bol.1', 'jpn.1', 'mex.campeon', 'concacaf.central.american.cup',
    'concacaf.champions_cup', 'concacaf.u23', 'hon.1', 'crc.1', 'gua.1', 'slv.1',
    'fifa.intercontinental.cup', 'afc.saff.championship', 'chn.1.promotion.relegation', 'ind.1', 'ind.2',
    'global.gulf_cup', 'caf.cosafa', 'caf.champions', 'caf.confed', 'rsa.1', 'ita.3', 'idn.1', 'ken.1',
    'sco.3', 'sco.4', 'tur.2', 'arg.4', 'aut.2', 'bra.3', 'chi.2', 'col.2', 'cyp.1', 'cze.1', 'den.2',
    'ecu.2', 'gha.1', 'irl.1', 'isr.1', 'jam.1', 'mys.1', 'nga.1', 'nir.1', 'nor.2', 'per.2', 'rou.1',
    'sgp.1', 'sui.1', 'sui.2', 'swe.2', 'tha.1', 'uga.1', 'wal.1', 'zam.1'
]

# National-team competitions (their teams are rated together, see
# internationals.py).
INTL = {'uefa.nations', 'concacaf.nations.league', 'caf.nations_qual', 'caf.nations', 'fifa.friendly',
        'fifa.world', 'uefa.euro', 'uefa.euroq', 'afc.asian.cup', 'afc.cupq', 'conmebol.america',
        'concacaf.gold', 'concacaf.goldq', 'concacaf.gold_qual', 'fifa.confederations', 'uefa.nations.playoff',
        'fifa.wcq.ply', 'aff.championship', 'afc.saff.championship', 'global.gulf_cup', 'caf.cosafa',
        'caf.championship', 'global.finalissima'}

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
    'tun': 'Tunisia', 'ken': 'Kenya', 'uga': 'Uganda', 'zam': 'Zambia', 'cyp': 'Cyprus',
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
        _keep_league_events(day, parsed)
        parsed['at'] = int(time.time())
        if save_day(day, parsed):
            changed += 1
    have = len([n for n in os.listdir(DIR)]) if os.path.isdir(DIR) else 0
    return f'{fetched} day(s) read ({events} matches), {changed} changed, {have} days stored'


def _keep_league_events(day, parsed):
    """A fresh combined list must not drop the competitions read league by
    league for that date (those not in the combined list)."""
    old = load_day(day)
    if not old:
        return
    keep = set(old.get('slugs') or []) - set(parsed['leagues'])
    if not keep:
        return
    parsed['events'] = parsed['events'] + [e for e in old.get('events') or [] if e.get('lg') in keep]
    for slug in keep:
        if slug in (old.get('leagues') or {}):
            parsed['leagues'][slug] = old['leagues'][slug]
    parsed['slugs'] = sorted(keep)


# ------------------------------------------------- league by league (1.12)

def load_meta():
    try:
        with open(META, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_meta(meta):
    os.makedirs(DIR, exist_ok=True)
    text = json.dumps(meta, separators=(',', ':'), ensure_ascii=False, sort_keys=True)
    try:
        with open(META, encoding='utf-8') as f:
            if f.read() == text:
                return
    except OSError:
        pass
    with open(META, 'w', encoding='utf-8') as f:
        f.write(text)


def calendar_days(league):
    """ESPN dates (YYYYMMDD) with matches, from a league's calendar."""
    out = set()
    for c in (league or {}).get('calendar') or []:
        v = c if isinstance(c, str) else ((c or {}).get('startDate') or (c or {}).get('value') or '')
        if isinstance(v, str) and len(v) >= 10 and v[4] == '-' and v[7] == '-':
            out.add(v[:10].replace('-', ''))
    return sorted(out)


def parse_league(data, slug, day):
    """One league's scoreboard -> {league, calendar, events} (no events for
    an excluded competition)."""
    lg = ((data or {}).get('leagues') or [{}])[0] or {}
    name = lg.get('name') or slug
    info = {'id': str(lg.get('id') or ''), 'name': name}
    if excluded(slug, name):
        return {'league': info, 'calendar': [], 'events': []}
    events = [ev for ev in (parse_event(e, slug, day) for e in (data or {}).get('events') or []) if ev]
    return {'league': info, 'calendar': calendar_days(lg), 'events': events}


def fetch_league(slug, day):
    """(parsed or None, note); note 'bad' when ESPN does not know the slug."""
    status, headers, body = fetch(f'{BASE}{slug}/scoreboard?dates={day}',
                                  {'Accept': 'application/json', 'Accept-Encoding': 'gzip'}, timeout=30)
    if status in (400, 404):
        return None, 'bad'
    if status != 200:
        return None, f'HTTP {status}'
    try:
        if (headers.get('Content-Encoding') or headers.get('content-encoding') or '').lower() == 'gzip' \
                or body[:2] == b'\x1f\x8b':
            body = gzip.decompress(body)
        return parse_league(json.loads(body), slug, day), ''
    except (ValueError, OSError) as e:
        return None, f'bad answer ({e})'


def merge_league(day, slug, parsed):
    """Puts one league's events for one ESPN date into that date's file
    (replacing that league's earlier copy). True when the file changed."""
    body = load_day(day) or {'leagues': {}, 'events': []}
    body['events'] = [e for e in body.get('events') or [] if e.get('lg') != slug] + parsed['events']
    body.setdefault('leagues', {})[slug] = parsed['league']
    body['slugs'] = sorted(set(body.get('slugs') or []) | {slug})
    body['at'] = int(time.time())
    return save_day(day, body)


def _league_done(body, slug, day, today):
    """Nothing more to learn: an ESPN date before yesterday, already read
    league by league, with every match of that league over."""
    if not body or slug not in (body.get('slugs') or []):
        return False
    if day >= (today - timedelta(days=1)).strftime('%Y%m%d'):
        return False
    return all(e.get('st') in DONE for e in body.get('events') or [] if e.get('lg') == slug)


def _in_combined(body, slug):
    """The combined list already has this league for that date."""
    return bool(body) and slug in (body.get('leagues') or {}) and slug not in (body.get('slugs') or [])


def refresh_leagues(full, log=print, leagues=None):
    """Reads the competitions of LEAGUES one by one on their match days
    (full runs: ESPN dates today-2 .. tomorrow, calendars every 3 days and a
    small back-fill of past match days; quick runs: yesterday and today, for
    live scores). Returns a one-line summary."""
    if not ENABLED:
        return 'off'
    meta = load_meta()
    today = config.now_utc().date()
    tkey = today.isoformat()
    slugs = []
    for slug in leagues if leagues is not None else LEAGUES:
        bad = (meta.get(slug) or {}).get('bad')
        if bad and bad > (today - timedelta(days=BAD_RETRY_DAYS)).isoformat():
            continue
        slugs.append(slug)
    stats = {'read': 0, 'changed': 0, 'bad': 0, 'fails': 0, 'run': 0, 'capped': False}
    seen = set()

    cap = MAX_LEAGUE_REQUESTS[bool(full)]

    def get(slug, day):
        if stats['run'] >= 8:  # ESPN unreachable: stop for this run
            return None
        if stats['read'] + stats['fails'] + stats['bad'] >= cap:
            stats['capped'] = True
            return None
        if stats['read'] or stats['fails']:
            time.sleep(PAUSE_LEAGUE)
        parsed, note = fetch_league(slug, day)
        seen.add((slug, day))
        m = meta.setdefault(slug, {})
        if note == 'bad':
            m['bad'] = tkey
            stats['bad'] += 1
            return None
        if parsed is None:
            stats['fails'] += 1
            stats['run'] += 1
            log(f'ESPN {slug} {day}: {note}')
            return None
        stats['run'] = 0
        stats['read'] += 1
        m.pop('bad', None)
        m['name'] = parsed['league']['name']
        if merge_league(day, slug, parsed):
            stats['changed'] += 1
        return parsed

    tday = today.strftime('%Y%m%d')
    if full:
        stale = (today - timedelta(days=CALENDAR_EVERY_DAYS)).isoformat()
        for slug in slugs:
            if ((meta.get(slug) or {}).get('cal_at') or '') > stale:
                continue
            parsed = get(slug, tday)
            if parsed is not None:
                meta[slug]['cal'] = parsed['calendar']
                meta[slug]['cal_at'] = tkey
    lo, hi = LEAGUE_DAYS_FULL if full else LEAGUE_DAYS_QUICK
    dates = [(today + timedelta(days=i)).strftime('%Y%m%d') for i in range(lo, hi + 1)]
    for day in dates:
        body = load_day(day)
        for slug in slugs:
            if (slug, day) in seen or _in_combined(body, slug) or _league_done(body, slug, day, today):
                continue
            m = meta.get(slug) or {}
            if m.get('bad') == tkey:
                continue
            cal = m.get('cal') or []
            mine = [e for e in (body or {}).get('events') or [] if e.get('lg') == slug]
            if cal:
                wanted = day in cal or any(e.get('st') not in DONE for e in mine)
            else:  # no calendar from ESPN: every day in full runs, then only while a match is open
                wanted = full or any(e.get('st') not in DONE for e in mine)
            if wanted and get(slug, day) is not None:
                body = load_day(day)
    if full and LEAGUE_BACKFILL_PER_RUN > 0:
        cut = (today - timedelta(days=BACKFILL_DAYS)).strftime('%Y%m%d')
        first = dates[0]
        bodies = {}
        todo = []
        for slug in slugs:
            for day in (meta.get(slug) or {}).get('cal') or []:
                if cut <= day < first:
                    todo.append((day, slug))
        todo.sort(reverse=True)
        done = 0
        for day, slug in todo:
            if done >= LEAGUE_BACKFILL_PER_RUN:
                break
            if day not in bodies:
                bodies[day] = load_day(day) or {}
            b = bodies[day]
            if slug in (b.get('slugs') or []) or _in_combined(b, slug):
                continue
            if get(slug, day) is not None:
                bodies[day] = load_day(day) or {}
            done += 1
    save_meta(meta)
    return (f"{stats['read']} league-days read league by league ({len(slugs)} competitions), "
            f"{stats['changed']} changed, {stats['bad']} unknown to ESPN"
            + (f", {stats['fails']} failed" if stats['fails'] else '')
            + (f', stopped at the {cap}-request cap' if stats['capped'] else ''))


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
