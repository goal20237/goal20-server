"""dataFetcher: the only door between GOAL20 and football-data.org.

Anti-ban rules
- Fixtures and results: one fetch cycle at most every FETCH_EVERY_HOURS
  (default 6) and at most MAX_CYCLES_PER_DAY (default 4) per UTC day. A
  cycle is ONE /matches request covering 7 days ago .. tomorrow (two if the
  API asks for a shorter range).
- Standings: one cycle a day (12 small requests, one per competition).
- Between cycles every run, and every app user, is served from the SQLite
  cache (data/cache/goal20.sqlite): zero requests.
- The phones never call football-data.org: they only download the static
  files the server publishes on GitHub Pages.
- The key comes from the environment (GitHub Secret on GitHub, the .env file
  on your PC) and is masked in every message. It is never in the app.
"""
import os
import time
from datetime import timedelta

from . import config, fdorg

EVERY_HOURS = float(os.environ.get('FETCH_EVERY_HOURS') or 6)
MAX_CYCLES_PER_DAY = max(1, min(4, int(os.environ.get('MAX_CYCLES_PER_DAY') or 4)))
STANDINGS_EVERY_HOURS = 24
SLACK_SECONDS = 600  # GitHub starts scheduled runs a few minutes late
COMPETITIONS = ['PL', 'ELC', 'PD', 'BL1', 'SA', 'FL1', 'DED', 'PPL', 'BSA', 'CL', 'EC', 'WC']

SOURCE = 'football-data.org'
SOURCE_TABLES = 'football-data.org:standings'
MATCHES_KEY = 'football-data.org:matches'
STANDINGS_KEY = 'football-data.org:standings'


class DataFetcher:
    def __init__(self, cache, log, now=None):
        self.cache = cache
        self.log = log
        self.now = now or time.time
        self.fetched = False  # True when this run fetched new fixtures

    # ------------------------------------------------------------- gating
    def _due(self, key, every_hours, source, max_cycles):
        """(due?, cached, age seconds, reason when not due)."""
        cached = self.cache.get(key)
        now = self.now()
        age = None if cached is None else max(0, now - cached[0])
        if not fdorg.available():
            return False, cached, age, 'no FOOTBALL_DATA_API_KEY'
        used = self.cache.cycles(source, now)
        if used >= max_cycles:
            return False, cached, age, f'{used}/{max_cycles} cycles used today'
        if age is not None and age < every_hours * 3600 - SLACK_SECONDS:
            return False, cached, age, f'next fetch in {(every_hours * 3600 - age) / 3600:.1f} h'
        return True, cached, age, ''

    def status(self):
        """For the manifest (also a backup of the daily count, see seed)."""
        cached = self.cache.get(MATCHES_KEY)
        return {
            'source': SOURCE,
            'every_hours': EVERY_HOURS,
            'max_cycles_per_day': MAX_CYCLES_PER_DAY,
            'cycles_today': self.cache.cycles(SOURCE, self.now()),
            'day': time.strftime('%Y-%m-%d', time.gmtime(self.now())),
            'last_fetch': int(cached[0]) if cached else None,
        }

    def seed(self, previous):
        """If the SQLite cache was lost, take today's cycle count from the last
        published manifest, so the 4-a-day cap still holds."""
        st = (previous or {}).get('fetch') or {}
        if st.get('day') == time.strftime('%Y-%m-%d', time.gmtime(self.now())) and st.get('cycles_today'):
            self.cache.set_cycles(SOURCE, st['cycles_today'], self.now())

    # ----------------------------------------------------------- fixtures
    def matches(self):
        """football-data.org matches from 7 days ago to tomorrow (UTC):
        (list, note). Served from the cache unless a cycle is due."""
        due, cached, age, why = self._due(MATCHES_KEY, EVERY_HOURS, SOURCE, MAX_CYCLES_PER_DAY)
        if due:
            today = config.now_utc().date()
            # The cycle is counted before the request: a failed request
            # still counts, so errors can never turn into a request storm.
            self.cache.add_cycle(SOURCE, self.now())
            n = self.cache.cycles(SOURCE, self.now())
            try:
                raw = fdorg.matches(today - timedelta(days=7), today + timedelta(days=1), self.log)
            except fdorg.FdOrgError as e:
                self.cache.log(SOURCE, 'matches', False, str(e), self.now())
                why = f'fetch failed ({e})'
            else:
                self.cache.put(MATCHES_KEY, SOURCE, {'matches': raw}, self.now())
                self.cache.log(SOURCE, 'matches', True, f'{len(raw)} matches', self.now())
                self.fetched = True
                return raw, f'fetched {len(raw)} matches (cycle {n}/{MAX_CYCLES_PER_DAY} today)'
        if cached is None:
            return [], f'nothing cached yet ({why})'
        return (cached[1].get('matches') or [],
                f'served from cache, {age / 3600:.1f} h old ({why}): 0 requests')

    # ---------------------------------------------------------- standings
    def standings(self):
        """League tables of the 12 free competitions: ({code: table}, note)."""
        due, cached, age, why = self._due(STANDINGS_KEY, STANDINGS_EVERY_HOURS, SOURCE_TABLES, 1)
        if due:
            self.cache.add_cycle(SOURCE_TABLES, self.now())
            tables, errors = {}, []
            for code in COMPETITIONS:
                try:
                    data = fdorg._get(f'competitions/{code}/standings', {})
                except fdorg.FdOrgError as e:
                    errors.append(f'{code}: {e}')
                    if 'HTTP 429' in str(e):
                        break  # too many requests: stop at once
                    continue
                table = compact_standings(data)
                if table:
                    tables[code] = table
            note = f'{len(tables)} tables' + (f', {len(errors)} unavailable' if errors else '')
            self.cache.log(SOURCE, 'standings', bool(tables), note, self.now())
            if tables:
                self.cache.put(STANDINGS_KEY, SOURCE, tables, self.now())
                return tables, f'fetched {note}'
            why = 'no table available'
        if cached is None:
            return {}, f'nothing cached yet ({why})'
        return cached[1], f'served from cache, {age / 3600:.1f} h old: 0 requests'


def compact_standings(data):
    """One competition's overall table: {name, season, group, rows: [[pos,
    team, played, won, drawn, lost, goals for, goals against, points, form]]}."""
    if not isinstance(data, dict):
        return None
    comp = data.get('competition') or {}
    season = ((data.get('season') or {}).get('startDate') or '')[:4]
    for s in data.get('standings') or []:
        if (s.get('type') or 'TOTAL') != 'TOTAL':
            continue
        rows = []
        for r in s.get('table') or []:
            team = r.get('team') or {}
            rows.append([r.get('position'), team.get('shortName') or team.get('name') or '',
                         r.get('playedGames'), r.get('won'), r.get('draw'), r.get('lost'),
                         r.get('goalsFor'), r.get('goalsAgainst'), r.get('points'), r.get('form') or ''])
        if rows:
            return {'name': comp.get('name') or '', 'season': season,
                    'group': s.get('group') or '', 'rows': rows}
    return None
