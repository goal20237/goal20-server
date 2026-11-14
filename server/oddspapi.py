"""OddsPapi (free plan): betPawa Cameroon's real prices.

Free plan: 250 requests a month. 1 request = 1 call, whatever it returns;
one odds call covers up to 3 leagues ("tournaments") for one bookmaker.
/v4/account (remaining requests) is free. The key goes in the address
(apiKey=...), so it is masked in every message GOAL20 writes.

Plan: about 10 requests a month for reference lists (leagues, markets,
team names), then at most 7 a day: the leagues with the most matches
today and tomorrow, read once a day.
"""
import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta

from . import config
from .names import norm, similarity
from .net import fetch
from .store import read_json

HOST = 'https://api.oddspapi.io/v4'
KEY = os.environ.get('ODDSPAPI_KEY', '').strip()
BOOKMAKER = 'betpawa.cm'
BOOK_LABEL = 'betPawa'
SOCCER = 10
PER_CALL = 3            # leagues per odds call (their limit)
DAILY_MAX = 7           # odds calls a day
MONTH_LIMIT = 250       # used if /account cannot be read
KEEP_SPARE = 6          # never spend the last few requests of the month
FETCH_AFTER_UTC = (6, 45)

DIR = os.path.join(config.DATA, 'op')

# Country names that differ between sources ("X Amateur" is OddsPapi's
# category for lower divisions of country X).
COUNTRY = {'czechia': 'czech republic', 'korea republic': 'south korea', 'republic of korea': 'south korea',
           'united states': 'usa', 'turkiye': 'turkey', 'ivory coast': "cote d'ivoire"}

# GOAL20 league (country, name) -> OddsPapi tournament id, checked against
# OddsPapi's list on 14 Nov 2026. Names from football-charts.com,
# football-data.co.uk and football-data.org. Anything else is matched by name.
KNOWN = [
    ('Algeria', ['Ligue 1'], 841), ('Australia', ['A-League'], 136),
    ('Austria', ['Bundesliga'], 45), ('Austria', ['2. Liga'], 135),
    ('Azerbaijan', ['Premier League'], 709),
    ('Belgium', ['Jupiler Pro League', 'Pro League', 'First Division A'], 38),
    ('Belgium', ['Challenger Pro League', 'First Division B'], 9),
    ('Brazil', ['Serie A Betano', 'Serie A', 'Campeonato Brasileiro Serie A', 'Brasileirao'], 325),
    ('Brazil', ['Serie B', 'Campeonato Brasileiro Serie B'], 390),
    ('Bulgaria', ['efbet League', 'First League', 'Parva Liga'], 247),
    ('China', ['Super League', 'Chinese Super League'], 649),
    ('Czech Republic', ['Chance Liga', '1. Liga', 'First League'], 172),
    ('Czech Republic', ['ChNL', 'FNL', 'National Football League'], 205),
    ('Czech Republic', ['3. CFL - Group A', '3. CFL - Group B', 'CFL'], 14151),
    ('Denmark', ['Superliga'], 39), ('Denmark', ['1st Division', '1. Division'], 47),
    ('Egypt', ['Premier League'], 808),
    ('England', ['Premier League'], 17), ('England', ['Championship'], 18),
    ('England', ['League One'], 24), ('England', ['League Two'], 25), ('England', ['National League'], 173),
    ('Estonia', ['Meistriliiga', 'Premium Liiga'], 178),
    ('Finland', ['Veikkausliiga'], 41), ('Finland', ['Ykkosliiga'], 55),
    ('France', ['Ligue 1'], 34), ('France', ['Ligue 2'], 182), ('France', ['Ligue 3', 'National'], 183),
    ('Germany', ['Bundesliga', '1. Bundesliga'], 35), ('Germany', ['2. Bundesliga'], 44), ('Germany', ['3. Liga'], 491),
    ('Greece', ['Super League', 'Super League 1'], 185), ('Greece', ['Super League 2'], 186),
    ('Netherlands', ['Eredivisie'], 37), ('Netherlands', ['Eerste Divisie'], 131),
    ('Netherlands', ['Tweede Divisie'], 26278),
    ('Hungary', ['NB I.', 'NB I'], 187), ('Hungary', ['NB II.', 'NB II'], 1339),
    ('India', ['ISL', 'Indian Super League'], 1900),
    ('Ireland', ['Premier Division'], 192), ('Ireland', ['Division 1', 'First Division'], 193),
    ('Italy', ['Serie A'], 23), ('Italy', ['Serie B'], 53),
    ('Italy', ['Serie C - Group A'], 26554), ('Italy', ['Serie C - Group B'], 26556),
    ('Japan', ['J1 League', 'J-League', 'J League'], 196), ('Japan', ['J2 League'], 402),
    ('South Korea', ['K League 1'], 410), ('South Korea', ['K League 2'], 777),
    ('Kuwait', ['Premier League'], 1002), ('Latvia', ['Virsliga'], 197), ('Lithuania', ['TOPLYGA', 'A Lyga'], 198),
    ('Morocco', ['Botola Pro'], 937), ('Norway', ['Eliteserien'], 20),
    ('Poland', ['Ekstraklasa'], 202), ('Poland', ['Division 1', '1. Liga'], 229),
    ('Portugal', ['Liga Portugal', 'Primeira Liga'], 238), ('Portugal', ['Liga Portugal 2'], 239),
    ('Spain', ['Primera RFEF - Group 1', 'Primera RFEF - Group 2', 'Primera Federacion'], 34834),
    ('Spain', ['Segunda RFEF - Group 1', 'Segunda RFEF - Group 2', 'Segunda RFEF - Group 3',
               'Segunda RFEF - Group 4', 'Segunda RFEF - Group 5', 'Segunda Federacion'], 544),
    ('Spain', ['LaLiga', 'La Liga', 'Primera Division'], 8), ('Spain', ['LaLiga2', 'Segunda Division', 'LaLiga 2'], 54),
    ('Romania', ['Superliga', 'Liga 1', 'Liga I'], 152), ('Russia', ['Premier League'], 203),
    ('Saudi Arabia', ['Saudi Professional League', 'Pro League'], 955), ('Saudi Arabia', ['Division 1'], 2298),
    ('Scotland', ['Premiership'], 36), ('Scotland', ['Championship'], 206),
    ('Scotland', ['League One'], 207), ('Scotland', ['League Two'], 209),
    ('Serbia', ['Mozzart Bet Super Liga', 'Super Liga', 'Superliga'], 210),
    ('Slovakia', ['Nike liga', 'Super Liga', 'Superliga'], 211), ('Slovakia', ['2. liga'], 224),
    ('Slovenia', ['Prva liga', 'PrvaLiga'], 212),
    ('Sweden', ['Allsvenskan'], 40), ('Sweden', ['Superettan'], 46),
    ('Switzerland', ['Super League'], 215), ('Switzerland', ['Challenge League'], 216),
    ('Thailand', ['Thai League 1'], 1032),
    ('Turkey', ['Super Lig'], 52), ('Turkey', ['1. Lig'], 98),
    ('USA', ['MLS', 'Major League Soccer'], 242),
    ('Mexico', ['Liga MX'], 27464), ('Argentina', ['Liga Profesional', 'Primera LPF'], 155),
]


def _plain(text):
    import re
    import unicodedata
    s = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    words = {'one': '1', 'two': '2', 'three': '3', 'nord': 'north', 'sud': 'south', 'ost': 'east'}
    return ' '.join(words.get(w, w) for w in s.split())


class OpError(Exception):
    pass


def available():
    return bool(KEY)


def _mask(text):
    text = str(text)
    if KEY:
        text = text.replace(KEY, '[key]').replace(urllib.parse.quote(KEY), '[key]')
    return text


def _load(name, default=None):
    return read_json(os.path.join(DIR, name), default)


def _save(name, value):
    """Writes only when the content changed (no needless git history)."""
    import gzip
    path = os.path.join(DIR, name)
    if read_json(path) == value:
        return
    os.makedirs(DIR, exist_ok=True)
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


class Client:
    def __init__(self, meta, log):
        self.meta = meta
        self.log = log
        self.used = 0
        self._last = 0.0
        self.stopped = ''
        month = config.now_utc().strftime('%Y-%m')
        if meta.get('month') != month:
            meta['month'], meta['month_n'] = month, 0

    def _get(self, path, params=None, billable=True):
        if self.stopped:
            raise OpError(self.stopped)
        wait = self._last + 1.1 - time.time()  # their cooldown is 1 s per endpoint
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        q = dict(params or {})
        q['apiKey'] = KEY
        status, _, body = fetch(f'{HOST}/{path}?{urllib.parse.urlencode(q)}', {'Accept': 'application/json'}, timeout=60)
        if billable and status != 0 and status != 401:
            self.used += 1
            self.meta['month_n'] = self.meta.get('month_n', 0) + 1
        try:
            data = json.loads(body.decode('utf-8') or 'null')
        except ValueError:
            data = None
        if status == 429:
            self.stopped = 'monthly request limit reached'
            raise OpError(self.stopped)
        if status in (401, 403):
            self.stopped = f'key refused (HTTP {status})'
            raise OpError(self.stopped)
        if status != 200:
            msg = data.get('message') or data.get('details') if isinstance(data, dict) else None
            raise OpError(_mask(f'HTTP {status}: {msg or body[:120].decode(errors="replace")}'))
        return data

    def account(self):
        """(limit, used) this period, free to call. Falls back to GOAL20's own count."""
        try:
            data = self._get('account', billable=False)
        except OpError as e:
            if self.stopped:
                raise
            self.log(f'OddsPapi: account not readable ({e})')
            return MONTH_LIMIT, self.meta.get('month_n', 0)
        found = {}

        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k in ('request_limit', 'requestLimit') and isinstance(v, (int, float)):
                        found.setdefault('limit', int(v))
                    elif k in ('request_count', 'requestCount') and isinstance(v, (int, float)):
                        found.setdefault('count', int(v))
                    else:
                        walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(data)
        return found.get('limit', MONTH_LIMIT), found.get('count', self.meta.get('month_n', 0))


def _markets_map(rows):
    """OddsPapi outcome id -> GOAL20 market key (football, full time)."""
    out = {}
    lines = {0.5: 'over05', 1.5: 'over15', 2.5: 'over25'}
    for m in rows if isinstance(rows, list) else []:
        if m.get('sportId') not in (SOCCER, None) or m.get('playerProp'):
            continue
        if (m.get('period') or 'fulltime') != 'fulltime':
            continue
        name = (m.get('marketName') or '').lower()
        mtype = (m.get('marketType') or '').lower()
        outs = {(o.get('outcomeName') or '').strip().lower(): str(o.get('outcomeId')) for o in m.get('outcomes') or []}
        try:
            line = float(m.get('handicap') or 0)
        except (TypeError, ValueError):
            continue
        if mtype == '1x2' and 'result' in name and '1' in outs and '2' in outs:
            out.setdefault(outs['1'], 'home')
            out.setdefault(outs['2'], 'away')
        elif mtype == 'totals' and 'over' in outs and line in lines and 'asian' not in name and 'corner' not in name \
                and 'card' not in name and ('over under' in name or 'total' in name):
            out.setdefault(outs['over'], lines[line])
        elif 'both teams to score' in name and 'yes' in outs and 'half' not in name:
            out.setdefault(outs['yes'], 'btts')
    return out


SPONSORS = {'betano', 'efbet', 'mozzart', 'bet', 'nike', 'chance', 'superbet', 'betclic', 'tipsport', 'fortuna',
            'betway', 'sky', 'bet365', 'hervis', 'admiral', 'win2day'}
NOT_FIRST_TEAMS = ('women', 'u1', 'u2', 'youth', 'junior', 'reserve', 'primavera', 'virtual', 'srl',
                   'senioren', 'niu', 'not use', 'not in use', 'esoccer', 'electronic', 'kings league')


def _league_name(name):
    words = [w for w in _plain(name).split() if w not in SPONSORS]
    return ' '.join(words) or _plain(name)


def _country(c):
    c = _plain(c)
    if c.endswith(' amateur'):
        c = c[:-8]
    return COUNTRY.get(c, c)


_KNOWN = {(_country(c), _league_name(n)): tid for c, names, tid in KNOWN for n in names}


def match_tournaments(leagues, tournaments):
    """GOAL20 league id -> OddsPapi tournament id: the checked list first,
    then by country and name (first teams only)."""
    ids = {t.get('tournamentId') for t in tournaments}
    by_country = {}
    for t in tournaments:
        tname = (t.get('tournamentName') or '').lower()
        cat = (t.get('categoryName') or '').lower()
        if any(w in tname for w in NOT_FIRST_TEAMS) or 'virtual' in cat or 'simulated' in cat:
            continue
        by_country.setdefault(_country(t.get('categoryName')), []).append(t)
    out = {}
    for lid, (name, country) in leagues.items():
        known = _KNOWN.get((_country(country), _league_name(name)))
        if known in ids:
            out[lid] = known
            continue
        cands = by_country.get(_country(country)) or []
        target = _league_name(name)
        scored = sorted(((similarity(target, _league_name(t.get('tournamentName'))), t) for t in cands),
                        key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.72 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
            out[lid] = scored[0][1]['tournamentId']
    return out


def can_fetch_now(meta, now=None):
    now = now or config.now_utc()
    last = meta.get('last_fetch')
    if not last:
        return True
    if last[:10] == now.date().isoformat():
        return False
    return (now.hour, now.minute) >= FETCH_AFTER_UTC


def _ts(iso):
    try:
        return int(datetime.fromisoformat(str(iso).replace('Z', '+00:00')).timestamp())
    except (ValueError, TypeError):
        return None


def refresh(fixtures, enrich, log, stamp):
    """Reads betPawa prices for the leagues with the most matches in
    [fixtures] (today and tomorrow, not started). Writes them to
    enrich['opodds'][fixture id]. Returns a summary line."""
    meta = _load('meta.json', {}) or {}
    now = config.now_utc()
    if not can_fetch_now(meta, now):
        return 'prices already read today'
    client = Client(meta, log)
    try:
        limit, used = client.account()
    except OpError as e:
        _save('meta.json', meta)
        return f'not used: {e}'
    left = limit - used - KEEP_SPARE
    if left <= 0:
        _save('meta.json', meta)
        return f'monthly requests used ({used}/{limit}); prices again next month'
    day = now.date()
    try:
        if not meta.get('markets') or (meta.get('markets_at') or '') < day.replace(day=1).isoformat():
            mk = _markets_map(client._get('markets', {'language': 'en'}))
            if mk:
                meta['markets'], meta['markets_at'] = mk, day.isoformat()
            left -= 1
        if not meta.get('tournaments') or (meta.get('tournaments_at') or '') < (day - timedelta(days=7)).isoformat():
            rows = client._get('tournaments', {'sportId': SOCCER, 'language': 'en'})
            meta['tournaments'] = [{'tournamentId': t.get('tournamentId'), 'tournamentName': t.get('tournamentName'),
                                    'categoryName': t.get('categoryName')}
                                   for t in rows if isinstance(t, dict) and t.get('tournamentId')] if isinstance(rows, list) else []
            meta['tournaments_at'] = day.isoformat()
            left -= 1
    except OpError as e:
        _save('meta.json', meta)
        return f'reference lists failed: {e}'
    markets = meta.get('markets') or {}
    if not markets:
        _save('meta.json', meta)
        return 'no football markets recognised; nothing read'
    # Leagues with the most not-started matches today and tomorrow.
    leagues, count = {}, {}
    for f in fixtures:
        if f.get('st') not in ('NS', 'TBD'):
            continue
        lg = f['lg']
        leagues[lg['id']] = (lg.get('name') or '', lg.get('country') or '')
        count[lg['id']] = count.get(lg['id'], 0) + 1
    tmap = match_tournaments(leagues, meta.get('tournaments') or [])
    # Leagues betPawa had no matches for are skipped for two days.
    recent_empty = (day - timedelta(days=2)).isoformat()
    empty = {k: v for k, v in (meta.get('empty') or {}).items() if v >= recent_empty}
    ranked = [tmap[l] for l in sorted(count, key=lambda l: -count[l]) if l in tmap]
    ranked = [t for t in dict.fromkeys(ranked) if str(t) not in empty]
    calls = min(DAILY_MAX, left)
    batches = [ranked[i:i + PER_CALL] for i in range(0, len(ranked), PER_CALL)][:calls]
    names = _load('participants.json.gz', {}) or {}
    by_league = {}
    for f in fixtures:
        if f.get('st') in ('NS', 'TBD'):
            by_league.setdefault(f['lg']['id'], []).append(f)
    league_of_t = {}
    for l, t in tmap.items():
        league_of_t.setdefault(t, []).append(l)
    rows_all = []
    spent = 0
    for batch in batches:
        try:
            spent += 1
            rows = client._get('odds-by-tournaments', {'tournamentIds': ','.join(str(t) for t in batch),
                                                       'bookmaker': BOOKMAKER, 'language': 'en', 'oddsFormat': 'decimal'})
        except OpError as e:
            if 'FIXTURE_NOT_FOUND' in str(e) or 'No fixtures found' in str(e):
                for t in batch:
                    empty[str(t)] = day.isoformat()
                continue
            log(f'OddsPapi: {e}')
            if client.stopped:
                break
            continue
        rows = rows if isinstance(rows, list) else ((rows or {}).get('data') if isinstance(rows, dict) else None) or []
        rows = [r for r in rows if isinstance(r, dict)]
        seen = {str(r.get('tournamentId')) for r in rows if (r.get('bookmakerOdds') or {}).get(BOOKMAKER)}
        for t in batch:
            if str(t) not in seen:
                empty[str(t)] = day.isoformat()
        rows_all += rows
    # Team names: in the odds rows, else from OddsPapi's name list (one
    # request, kept for a month).
    unnamed = [r for r in rows_all if not r.get('participant1Name')
               and str(r.get('participant1Id')) not in names]
    if unnamed and not client.stopped and left - spent > 0 and \
            (meta.get('participants_at') or '') < (day - timedelta(days=7)).isoformat():
        try:
            plist = client._get('participants', {'sportId': SOCCER, 'language': 'en'})
            if isinstance(plist, dict) and plist:
                names = {str(k): v for k, v in plist.items() if isinstance(v, str)}
                _save('participants.json.gz', names)
                meta['participants_at'] = day.isoformat()
        except OpError as e:
            log(f'OddsPapi: {e}')
    priced, missing_names, no_book = 0, 0, 0
    for r in rows_all:
        book = ((r.get('bookmakerOdds') or {}).get(BOOKMAKER) or {})
        if not book or book.get('suspended'):
            no_book += 1
            continue
        quotes = []
        for mk in (book.get('markets') or {}).values():
            for oid, o in ((mk or {}).get('outcomes') or {}).items():
                key = markets.get(str(oid))
                if not key or any(q['m'] == key for q in quotes):
                    continue
                for pl in ((o or {}).get('players') or {}).values():
                    price = (pl or {}).get('price')
                    if isinstance(price, (int, float)) and price > 1 and pl.get('active', True) is not False:
                        quotes.append({'b': BOOK_LABEL, 'm': key, 'o': round(float(price), 2)})
                        break
        if not quotes:
            continue
        h = r.get('participant1Name') or names.get(str(r.get('participant1Id')))
        a = r.get('participant2Name') or names.get(str(r.get('participant2Id')))
        ts = _ts(r.get('startTime'))
        if not h or not a or ts is None:
            missing_names += 1
            continue
        best, best_score = None, 0.0
        for l in league_of_t.get(r.get('tournamentId'), []):
            for f in by_league.get(l, []):
                if abs(f['ts'] - ts) > 3 * 3600:
                    continue
                sc = min(similarity(norm(h), norm(f['h']['name'])), similarity(norm(a), norm(f['a']['name'])))
                if sc > best_score:
                    best, best_score = f, sc
        if best is not None and best_score >= 0.7:
            enrich.setdefault('opodds', {})[str(best['id'])] = {'at': stamp(), 'v': quotes}
            priced += 1
    read = len(rows_all)
    if read and no_book == read:
        log(f'OddsPapi: none of the {read} matches carried {BOOKMAKER} prices')
    if missing_names:
        log(f'OddsPapi: {missing_names} priced matches without team names (names list is read again next week)')
    meta['last_fetch'] = now.isoformat()[:19]
    meta['empty'] = empty
    _save('meta.json', meta)
    unlinked = [f'{c} {n}'.strip() for l, (n, c) in leagues.items() if l not in tmap]
    skipped = len([t for t in empty if empty[t] == day.isoformat()])
    line = (f'betPawa prices on {priced} matches ({read} read in {spent} call(s), {len(tmap)} leagues linked'
            f'{f", {skipped} with no betPawa matches skipped for 2 days" if skipped else ""}); '
            f'{used + client.used}/{limit} requests this month')
    if unlinked:
        line += f'; not linked: {", ".join(sorted(unlinked)[:6])}' + (' ...' if len(unlinked) > 6 else '')
    return line
