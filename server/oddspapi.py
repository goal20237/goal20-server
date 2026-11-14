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

# Country names that differ between sources.
COUNTRY = {'czechia': 'czech republic', 'korea republic': 'south korea', 'republic of korea': 'south korea',
           'united states': 'usa', 'usa': 'usa', 'turkiye': 'turkey', 'ivory coast': "cote d'ivoire"}


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


def _league_name(name):
    words = [w for w in norm(name or '').split() if w not in SPONSORS]
    return ' '.join(words) or norm(name or '')


def _country(c):
    c = norm(c or '')
    return COUNTRY.get(c, c)


def match_tournaments(leagues, tournaments):
    """GOAL20 league id -> OddsPapi tournament id, by country and name."""
    by_country = {}
    for t in tournaments:
        by_country.setdefault(_country(t.get('categoryName')), []).append(t)
    out = {}
    for lid, (name, country) in leagues.items():
        cands = by_country.get(_country(country)) or []
        target = _league_name(name)
        scored = sorted(((similarity(target, _league_name(t.get('tournamentName') or '')), t) for t in cands),
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
    ranked = [tmap[l] for l in sorted(count, key=lambda l: -count[l]) if l in tmap]
    ranked = list(dict.fromkeys(ranked))
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
            log(f'OddsPapi: {e}')
            if client.stopped:
                break
            continue
        rows = rows if isinstance(rows, list) else ((rows or {}).get('data') if isinstance(rows, dict) else None) or []
        rows_all += [r for r in rows if isinstance(r, dict)]
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
    _save('meta.json', meta)
    unlinked = [f'{c} {n}'.strip() for l, (n, c) in leagues.items() if l not in tmap]
    line = (f'betPawa prices on {priced} matches ({read} read in {len(batches)} call(s), '
            f'{len(tmap)} leagues linked); {used + client.used}/{limit} requests this month')
    if unlinked:
        line += f'; not linked: {", ".join(sorted(unlinked)[:6])}' + (' ...' if len(unlinked) > 6 else '')
    return line
