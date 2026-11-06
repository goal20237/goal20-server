"""football-data.co.uk: free CSV results (and Over/Under 2.5 odds) for
current and previous seasons. Fills the history gap of free API keys."""
import csv
import io
from datetime import datetime, timedelta, timezone

from . import config
from .net import fetch

BASE = 'https://www.football-data.co.uk'

# Main divisions: code -> (country, league name)
MAIN = {
    'E0': ('England', 'Premier League'), 'E1': ('England', 'Championship'),
    'E2': ('England', 'League One'), 'E3': ('England', 'League Two'),
    'EC': ('England', 'National League'),
    'SC0': ('Scotland', 'Premiership'), 'SC1': ('Scotland', 'Championship'),
    'SC2': ('Scotland', 'League One'), 'SC3': ('Scotland', 'League Two'),
    'D1': ('Germany', 'Bundesliga'), 'D2': ('Germany', '2. Bundesliga'),
    'I1': ('Italy', 'Serie A'), 'I2': ('Italy', 'Serie B'),
    'SP1': ('Spain', 'La Liga'), 'SP2': ('Spain', 'Segunda Division'),
    'F1': ('France', 'Ligue 1'), 'F2': ('France', 'Ligue 2'),
    'N1': ('Netherlands', 'Eredivisie'), 'B1': ('Belgium', 'Jupiler Pro League'),
    'P1': ('Portugal', 'Primeira Liga'), 'T1': ('Turkey', 'Super Lig'),
    'G1': ('Greece', 'Super League'),
}
# Extra leagues (one file with all seasons).
EXTRA = ['ARG', 'AUT', 'BRA', 'CHN', 'DNK', 'FIN', 'IRL', 'JPN', 'MEX',
         'NOR', 'POL', 'ROU', 'RUS', 'SWE', 'SWZ', 'USA']


def _seasons(today):
    """European season codes covering the last ~15 months, e.g. ['2627', '2526']."""
    start = today.year if today.month >= 7 else today.year - 1
    return [f'{start % 100:02d}{(start + 1) % 100:02d}', f'{(start - 1) % 100:02d}{start % 100:02d}']


def _decode(raw):
    for enc in ('utf-8-sig', 'latin-1'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def _date(s, t=''):
    s = (s or '').strip()
    for fmt in ('%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d'):
        try:
            d = datetime.strptime(s, fmt)
            break
        except ValueError:
            d = None
    if d is None:
        return None
    hh, mm = 15, 0
    if t and ':' in t:
        try:
            hh, mm = [int(x) for x in t.strip().split(':')[:2]]
        except ValueError:
            pass
    # football-data.co.uk times are UK time; treat as UTC (±1 h is fine for history).
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _odd(v):
    try:
        x = float(v)
        return x if x > 1 else None
    except (TypeError, ValueError):
        return None


def _rows(url):
    status, _, body = fetch(url, timeout=60)
    if status != 200 or not body:
        return None
    text = _decode(body)
    return list(csv.DictReader(io.StringIO(text)))


def load(log):
    """Returns (matches, upcoming). Each match: dict(country, div, league,
    season, ts, home, away, hg, ag, o25) with o25 = best Over 2.5 price."""
    today = config.today_cm()
    cutoff = config.now_utc() - timedelta(days=config.HISTORY_DAYS)
    matches = []
    files = 0
    for season in _seasons(today):
        for code, (country, league) in MAIN.items():
            rows = _rows(f'{BASE}/mmz4281/{season}/{code}.csv')
            if rows is None:
                continue
            files += 1
            for r in rows:
                when = _date(r.get('Date'), r.get('Time', ''))
                hg, ag = _num(r.get('FTHG')), _num(r.get('FTAG'))
                home, away = (r.get('HomeTeam') or '').strip(), (r.get('AwayTeam') or '').strip()
                if when is None or hg is None or ag is None or not home or not away:
                    continue
                if when < cutoff:
                    continue
                matches.append({
                    'country': country, 'div': code, 'league': league,
                    'season': 2000 + int(season[:2]), 'ts': int(when.timestamp()),
                    'home': home, 'away': away, 'hg': hg, 'ag': ag,
                })
    for code in EXTRA:
        rows = _rows(f'{BASE}/new/{code}.csv')
        if rows is None:
            continue
        files += 1
        for r in rows:
            when = _date(r.get('Date'), r.get('Time', ''))
            hg, ag = _num(r.get('HG')), _num(r.get('AG'))
            home, away = (r.get('Home') or '').strip(), (r.get('Away') or '').strip()
            if when is None or hg is None or ag is None or not home or not away or when < cutoff:
                continue
            season = _num((r.get('Season') or '')[:4]) or when.year
            matches.append({
                'country': (r.get('Country') or code).strip(), 'div': code,
                'league': (r.get('League') or code).strip(), 'season': season,
                'ts': int(when.timestamp()), 'home': home, 'away': away, 'hg': hg, 'ag': ag,
            })

    # Upcoming fixtures with Over 2.5 prices (main divisions).
    upcoming = []
    rows = _rows(f'{BASE}/fixtures.csv')
    if rows:
        for r in rows:
            code = (r.get('Div') or '').strip()
            if code not in MAIN:
                continue
            when = _date(r.get('Date'), r.get('Time', ''))
            if when is None:
                continue
            prices = {}
            # Max>2.5 is skipped: a market maximum is rarely available to one bettor.
            for col, book in (('B365>2.5', 'Bet365'), ('P>2.5', 'Pinnacle'),
                              ('Avg>2.5', 'Market average')):
                o = _odd(r.get(col))
                if o:
                    prices[book] = o
            upcoming.append({
                'country': MAIN[code][0], 'div': code, 'ts': int(when.timestamp()),
                'home': (r.get('HomeTeam') or '').strip(), 'away': (r.get('AwayTeam') or '').strip(),
                'o25': prices,
            })
    log(f'football-data.co.uk: {files} files, {len(matches)} results, {len(upcoming)} upcoming with odds')
    return matches, upcoming
