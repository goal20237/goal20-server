"""GOAL20 server run.

MODE=full  : results, fixtures, missing history days, football-data.co.uk,
             team matching, head-to-head / injuries / odds for the best
             candidates, then publish.
MODE=quick : refresh today's fixtures and live scores, then publish.
"""
import json
import os
import shutil
import sys
from datetime import datetime, timedelta

from . import config, fdcouk, rank
from . import fdorg
from . import fcharts
from . import oddspapi
from .apifootball import Api, ApiError, hist_row, parse_odds, to_app_fixture
from .names import Matcher, is_youth, norm
from .net import fetch
from .store import (State, all_day_keys, load_day, load_enrich, save_day,
                    save_enrich, write_json)

LOG = []



DONE_CODES = ('FT', 'AET', 'PEN', 'PST', 'CANC', 'ABD', 'AWD', 'WO')
DIV_LEAGUE = {}

def log(msg):
    line = f'[{config.now_utc():%H:%M:%S}] {msg}'
    LOG.append(line)
    print(line, flush=True)


def fnv(text):
    h = 0x811c9dc5
    for ch in text.encode('utf-8'):
        h ^= ch
        h = (h * 0x01000193) & 0xffffffff
    return h


def synthetic_id(text):
    """Stable negative id for teams / leagues / matches known only from CSV data."""
    return -((fnv(text) & 0x3fffffff) + 1)


# ------------------------------------------------------------------ fixtures

def day_is_final(key, fixtures):
    end = datetime.strptime(key, '%Y-%m-%d').replace(tzinfo=config.CAMEROON) + timedelta(days=1)
    long_over = config.now_utc() > end + timedelta(hours=12)
    done = all(f['st'] in DONE_CODES for f in fixtures)
    return key < config.day_key(config.today_cm()) and (done or long_over)


def fetch_day(api, state, key):
    if state.window and state.window[2] == config.day_key(config.today_cm()):
        lo, hi = state.window[0], state.window[1]
        if key < lo or key > hi:
            raise ApiError('plan', f'Plan allows dates {lo} to {hi}')
    try:
        rows = api.rows('fixtures', {'date': key, 'timezone': config.API_TZ}, cap='date')
    except ApiError as e:
        if e.kind == 'plan' and e.window:
            state.window = [e.window[0], e.window[1], config.day_key(config.today_cm())]
        raise
    fixtures = [to_app_fixture(r) for r in rows]
    fixtures = [f for f in fixtures if f['id']]
    fixtures.sort(key=lambda f: f['ts'])
    final = day_is_final(key, fixtures)
    save_day(key, fixtures, final)
    log(f'{key}: {len(fixtures)} fixtures{" (final)" if final else ""}')
    return fixtures


def collect_days(api, state, full):
    today = config.today_cm()
    tk = config.day_key(today)
    keys = {
        'today': tk,
        'tomorrow': config.day_key(today + timedelta(days=1)),
        'yesterday': config.day_key(today - timedelta(days=1)),
    }
    # Between midnight and 06:00 the matches still running or just finished
    # belong to yesterday: refresh yesterday so late results settle overnight.
    late_night = config.now_utc().astimezone(config.CAMEROON).hour < 6
    wanted = [] if (late_night and not full) else [keys['today']]
    yd = load_day(keys['yesterday'])
    unfinished = yd is not None and not yd.get('final') and any(
        f['st'] not in DONE_CODES for f in yd.get('f', []))
    if not full and (late_night or unfinished):
        wanted.insert(0, keys['yesterday'])
    if full or load_day(keys['tomorrow']) is None:
        wanted.append(keys['tomorrow'])
    if full:
        for i in range(1, 8):  # last week, for settling picks
            k = config.day_key(today - timedelta(days=i))
            d = load_day(k)
            if d is None or not d.get('final'):
                wanted.append(k)
    for k in wanted:
        if not api.can_spend():
            log('Request budget reached; remaining days skipped')
            break
        try:
            fetch_day(api, state, k)
        except ApiError as e:
            log(f'{k}: {e.message}')
            if e.kind in ('key', 'quota'):
                break
    if full:
        # Backfill older missing days (worldwide results) while budget allows.
        done = 0
        for i in range(8, 61):
            if done >= config.BACKFILL_DAYS or not api.can_spend():
                break
            k = config.day_key(today - timedelta(days=i))
            d = load_day(k)
            if d is not None and d.get('final'):
                continue
            try:
                fetch_day(api, state, k)
                done += 1
            except ApiError as e:
                log(f'Backfill stopped at {k}: {e.message}')
                break
    return keys


def live_refresh(api, today_key):
    """Merge in-play scores into today's file (1 request)."""
    if not api.can_spend():
        return
    try:
        rows = api.rows('fixtures', {'live': 'all', 'timezone': config.API_TZ}, cap='live')
    except ApiError as e:
        log(f'Live scores: {e.message}')
        return
    live = {f['id']: f for f in (to_app_fixture(r) for r in rows)}
    yesterday_key = config.day_key(config.today_cm() - timedelta(days=1))
    for key in (today_key, yesterday_key):
        d = load_day(key)
        if not d:
            continue
        by_id = {f['id']: f for f in d['f']}
        hit = [i for i in live if i in by_id]
        if not hit:
            continue
        for i in hit:
            by_id[i] = live[i]
        save_day(key, sorted(by_id.values(), key=lambda f: f['ts']), False)
    log(f'Live scores: {len(rows)} matches in play')


# ------------------------------------------------------------------ history

def build_history(state, fd_matches, fd_upcoming):
    """Combine API-Football results and football-data.co.uk results."""
    teams, leagues, rows, archived = {}, {}, {}, []
    api_fixtures = []
    for key in all_day_keys():
        d = load_day(key) or {}
        if d.get('final'):
            archived.append(key)
        for f in d.get('f', []):
            api_fixtures.append(f)
            for side in ('h', 'a'):
                t = f[side]
                if t['id']:
                    teams[t['id']] = {'id': t['id'], 'name': t['name'], 'logo': t['logo']}
            if f['lg']['id']:
                leagues[f['lg']['id']] = f['lg']
            r = hist_row(f)
            if r:
                rows[r[0]] = r

    # Team matching: API teams seen in fixtures -> football-data.co.uk names.
    fd_by_country = {}
    fd_div_of = {}
    for m in fd_matches + fd_upcoming:
        for side in ('home', 'away'):
            fd_by_country.setdefault(m['country'], set()).add(m[side])
            fd_div_of[f"{m['country']}|{m[side]}"] = m['div']
    matcher = Matcher(fd_by_country)
    # A team can appear in its domestic league and in a European cup
    # (country "World"): try every country it was seen in, domestic first.
    team_countries, team_league = {}, {}
    for f in api_fixtures:
        for side in ('h', 'a'):
            team_countries.setdefault(f[side]['id'], set()).add(f['lg']['country'])
            team_league.setdefault(f[side]['id'], f['lg']['name'])
    # Each football-data.co.uk club goes to the single best-matching API team
    # (exact name beats a partial one), recomputed every run.
    best_for = {}
    for tid, countries in team_countries.items():
        if not tid or tid < 0 or tid not in teams:
            continue
        name = teams[tid]['name']
        found = None
        for country in sorted(c for c in countries if c in fd_by_country):
            hit = matcher.match(name, country, team_league.get(tid, ''))
            if hit and (found is None or hit[1] > found[1]):
                found = hit
        if found is None and countries <= {'World', 'Europe', ''}:
            found = matcher.match_any(name, team_league.get(tid, ''))
        if found:
            key, score = found
            if key not in best_for or score > best_for[key][1]:
                best_for[key] = (tid, score)
    fd_to_api = {k: v[0] for k, v in best_for.items()}
    mapped = len(fd_to_api)
    global DIV_LEAGUE
    state.name_map = {str(tid): key for key, tid in fd_to_api.items()}

    # Learn which API league id each football-data.co.uk division is.
    votes = {}
    for f in api_fixtures:
        for side in ('h', 'a'):
            k = state.name_map.get(str(f[side]['id']))
            if k and k in fd_div_of:
                votes.setdefault(fd_div_of[k], {}).setdefault(f['lg']['id'], 0)
                votes[fd_div_of[k]][f['lg']['id']] += 1
    div_league = {}
    for div, v in votes.items():
        best = max(v.items(), key=lambda x: x[1])
        if best[1] >= 6 and best[1] / sum(v.values()) >= 0.6:
            div_league[div] = best[0]
    DIV_LEAGUE = div_league
    state.div_league = dict(div_league)

    # Existing API pairs (for de-duplication), keyed by home/away and day.
    api_pairs = {}
    for r in rows.values():
        day = r[1] // 86400
        for dd in (day - 1, day, day + 1):
            api_pairs[(r[4], r[5], dd)] = r[0]

    added = 0
    for m in fd_matches:
        hk, ak = f"{m['country']}|{m['home']}", f"{m['country']}|{m['away']}"
        hid = fd_to_api.get(hk) or synthetic_id('team|' + hk)
        aid = fd_to_api.get(ak) or synthetic_id('team|' + ak)
        dup = api_pairs.get((hid, aid, m['ts'] // 86400))
        if dup is not None:
            # Same match already known from API-Football: keep its row and
            # add the CSV's shots / corners to it.
            if m.get('st') and len(rows[dup]) == 8:
                rows[dup] = rows[dup] + list(m['st'])
            continue
        lid = div_league.get(m['div']) or synthetic_id('league|' + m['div'])
        mid = synthetic_id(f"match|{m['div']}|{m['ts'] // 86400}|{m['home']}|{m['away']}")
        if mid not in rows:
            added += 1
        rows[mid] = [mid, m['ts'], lid, m['season'], hid, aid, m['hg'], m['ag']] + (list(m['st']) if m.get('st') else [])
        for tid, name in ((hid, m['home']), (aid, m['away'])):
            if tid < 0:
                teams.setdefault(tid, {'id': tid, 'name': name, 'logo': ''})
        if lid < 0:
            leagues.setdefault(lid, {'id': lid, 'name': m['league'], 'country': m['country'],
                                     'logo': '', 'flag': '', 'season': m['season']})
    cutoff = int(config.now_utc().timestamp()) - config.HISTORY_DAYS * 86400
    rows = {k: v for k, v in rows.items() if v[1] >= cutoff}
    log(f'History: {len(rows)} results ({added} from football-data.co.uk), '
        f'{mapped} API teams matched, {len(div_league)} divisions linked')
    history = {
        'v': 1,
        'm': list(rows.values()),
        't': list(teams.values()),
        'l': list(leagues.values()),
        'd': archived,
        's': {},
    }
    return history, fd_to_api, api_fixtures


# ------------------------------------------------- football-data fallback

def fd_fallback_days(history, fd_matches, fd_upcoming, fd_to_api):
    """Without API-Football: fill the published days (last week to
    tomorrow) from football-data.co.uk: its fixtures list (main European
    divisions, with odds) and its results (which arrive a few days late).
    API fixtures already on a day are updated, never duplicated."""
    from datetime import datetime as _dt
    teams = {t['id']: t for t in history.get('t', [])}
    leagues = {l['id']: l for l in history.get('l', [])}
    today = config.today_cm()
    keys = [config.day_key(today + timedelta(days=i)) for i in range(-7, 2)]
    by_day = {k: [] for k in keys}

    def season_of(ts):
        d = _dt.fromtimestamp(ts, config.CAMEROON)
        return d.year if d.month >= 7 else d.year - 1

    def fixture(m, finished):
        hk, ak = f"{m['country']}|{m['home']}", f"{m['country']}|{m['away']}"
        hid = fd_to_api.get(hk) or synthetic_id('team|' + hk)
        aid = fd_to_api.get(ak) or synthetic_id('team|' + ak)
        lid = DIV_LEAGUE.get(m['div']) or synthetic_id('league|' + m['div'])
        info = fdcouk.MAIN.get(m['div'])
        lg = dict(leagues.get(lid) or {
            'id': lid, 'name': m.get('league') or (info[1] if info else m['div']),
            'country': m['country'], 'logo': '', 'flag': '', 'season': season_of(m['ts'])})
        side = lambda tid, name: {'id': tid, 'name': (teams.get(tid) or {}).get('name') or name,
                                  'logo': (teams.get(tid) or {}).get('logo') or ''}
        return {
            'id': synthetic_id(f"match|{m['div']}|{m['ts'] // 86400}|{m['home']}|{m['away']}"),
            'ts': m['ts'], 'st': 'FT' if finished else 'NS',
            'stl': 'Match Finished' if finished else 'Not Started', 'el': None,
            'lg': lg, 'rd': '', 'vn': '',
            'h': side(hid, m['home']), 'a': side(aid, m['away']),
            'hg': m['hg'] if finished else None, 'ag': m['ag'] if finished else None,
            'fh': m['hg'] if finished else None, 'fa': m['ag'] if finished else None,
            'src': 'fd',
        }

    for m in fd_matches:
        k = config.day_key(_dt.fromtimestamp(m['ts'], config.CAMEROON).date())
        if k in by_day:
            by_day[k].append(fixture(m, True))
    for u in fd_upcoming:
        k = config.day_key(_dt.fromtimestamp(u['ts'], config.CAMEROON).date())
        if k in by_day:
            by_day[k].append(fixture(dict(u, league=None, hg=None, ag=None), False))

    changed_days = 0
    for k, found in by_day.items():
        if not found:
            continue
        d = load_day(k) or {'f': [], 'final': False}
        if d.get('final') and not any(f.get('src') == 'fd' for f in d['f']):
            continue  # a complete API-Football day: leave it alone
        current = {f['id']: f for f in d['f']}
        changes = 0
        for fx in found:
            if fx['id'] in current:
                old = current[fx['id']]
                if old['st'] != 'FT' and fx['st'] == 'FT':
                    current[fx['id']] = fx
                    changes += 1
                continue
            # The same match from API-Football (either team matched, kickoff within 36 h).
            twin = next((f for f in current.values() if f.get('src') != 'fd' and abs(f['ts'] - fx['ts']) < 36 * 3600
                         and (f['h']['id'] == fx['h']['id'] or f['a']['id'] == fx['a']['id'])), None)
            if twin is not None:
                if twin['st'] not in DONE_CODES and fx['st'] == 'FT':
                    twin.update({'st': 'FT', 'stl': 'Match Finished', 'el': None,
                                 'hg': fx['hg'], 'ag': fx['ag'], 'fh': fx['fh'], 'fa': fx['fa']})
                    changes += 1
                continue
            current[fx['id']] = fx
            changes += 1
        if changes:
            fixtures = sorted(current.values(), key=lambda f: f['ts'])
            save_day(k, fixtures, day_is_final(k, fixtures))
            changed_days += 1
    log(f'football-data.co.uk fallback: {changed_days} day file(s) updated')


# ------------------------------------------------------ football-data.org

def _team_index(history):
    """country -> {team name: GOAL20 team id}, from the published history."""
    country_of = {l['id']: l.get('country') or '' for l in history.get('l', [])}
    name_of = {t['id']: t.get('name') or '' for t in history.get('t', [])}
    index = {}
    for r in history.get('m', []):
        c = country_of.get(r[2])
        if not c:
            continue
        for tid in (r[4], r[5]):
            n = name_of.get(tid)
            if not n:
                continue
            names = index.setdefault(c, {})
            # Prefer API-Football ids (they carry logos and live data).
            if n not in names or (tid > 0 and names[n] < 0):
                names[n] = tid
    return index


def merge_fdorg(history, state, full):
    """Same-day fixtures and results for football-data.org's 12 competitions,
    merged into the day files (existing matches are updated, never doubled)."""
    from datetime import datetime as _dt
    today_utc = config.now_utc().date()
    start = today_utc - timedelta(days=7 if full else 2)
    try:
        raw = fdorg.matches(start, today_utc + timedelta(days=1), log)
    except fdorg.FdOrgError as e:
        log(f'football-data.org: {e}')
        return 0
    teams = {t['id']: t for t in history.get('t', [])}
    leagues = {l['id']: l for l in history.get('l', [])}
    index = _team_index(history)
    matcher = Matcher({c: set(n) for c, n in index.items()})

    def team_id(team, area, comp):
        xid = team.get('id')
        name = team.get('shortName') or team.get('name') or ''
        crest = team.get('crest') or ''
        if not xid or not name:
            return None, '', ''
        tid = state.fdorg_teams.get(str(xid))
        if tid is None:
            country = area.get('name') or ''
            hit = None
            for candidate in {team.get('name') or '', name}:
                if not candidate:
                    continue
                h = (matcher.match(candidate, country, comp.get('name') or '') if country in index
                     else matcher.match_any(candidate, comp.get('name') or ''))
                if h and (hit is None or h[1] > hit[1]):
                    hit = h
            if hit:
                c, n = hit[0].split('|', 1)
                tid = index[c][n]
                state.fdorg_teams[str(xid)] = tid
            else:
                tid = synthetic_id(f'fdorg-team|{xid}')
        known = teams.get(tid) or {}
        return tid, known.get('name') or name, known.get('logo') or crest

    def league_for(comp, area, season):
        code = comp.get('code') or ''
        if code in fdorg.DIVISION:
            div = fdorg.DIVISION[code]
            lid = state.div_league.get(div) or synthetic_id('league|' + div)
        elif code in fdorg.CUP_LEAGUE:
            lid = fdorg.CUP_LEAGUE[code]
        else:
            lid = synthetic_id(f'league|fdorg-{code or comp.get("id")}')
        base = leagues.get(lid) or {}
        return {
            'id': lid,
            'name': base.get('name') or comp.get('name') or code,
            'country': base.get('country') or area.get('name') or '',
            'logo': base.get('logo') or comp.get('emblem') or '',
            'flag': base.get('flag') or area.get('flag') or '',
            'season': season,
        }

    keys = [config.day_key(config.today_cm() + timedelta(days=i)) for i in range(-7, 2)]
    by_day = {k: [] for k in keys}
    for m in raw:
        fx = fdorg.to_fixture(m, team_id, league_for)
        if fx is None or not fx.get('xid'):
            continue
        fx['id'] = synthetic_id(f"fdorg|{fx['xid']}")
        k = config.day_key(_dt.fromtimestamp(fx['ts'], config.CAMEROON).date())
        if k in by_day:
            by_day[k].append(fx)

    fields = ('st', 'stl', 'el', 'hg', 'ag', 'fh', 'fa')
    changed_days, total = 0, 0
    for k, found in by_day.items():
        if not found:
            continue
        total += len(found)
        d = load_day(k) or {'f': [], 'final': False}
        current = {f['id']: f for f in d['f']}
        changes = 0
        for fx in found:
            old = current.get(fx['id'])
            if old is not None:
                if any(old.get(x) != fx.get(x) for x in fields + ('ts',)):
                    current[fx['id']] = fx
                    changes += 1
                continue
            twin = None
            for f in current.values():
                if f.get('src') == 'fdorg':
                    continue
                gap = abs(f['ts'] - fx['ts'])
                same_h, same_a = f['h']['id'] == fx['h']['id'], f['a']['id'] == fx['a']['id']
                if (same_h and same_a and gap < 36 * 3600) or (
                        (same_h or same_a) and f['lg']['id'] == fx['lg']['id'] and gap < 3 * 3600):
                    twin = f
                    break
            if twin is not None:
                # An API-Football result is kept; anything else takes the newer data.
                if twin.get('src') != 'fd' and twin['st'] in DONE_CODES:
                    continue
                before = {x: twin.get(x) for x in fields + ('ts',)}
                twin.update({x: fx[x] for x in fields})
                if twin.get('src') == 'fd' and fx['st'] == 'NS':
                    twin['ts'] = fx['ts']  # football-data.org has the exact kickoff
                if any(twin.get(x) != before[x] for x in before):
                    changes += 1
                continue
            current[fx['id']] = fx
            changes += 1
        if changes:
            fixtures = sorted(current.values(), key=lambda f: f['ts'])
            save_day(k, fixtures, day_is_final(k, fixtures))
            changed_days += 1
    log(f'football-data.org: {len(raw)} matches read, {total} in published days, {changed_days} day file(s) updated')
    return total


# ------------------------------------------------------ football-charts.com

def merge_fcharts(history, state, fetch_allowed):
    """Fixtures and results of about 80 more leagues from football-charts.com
    (read once a day, kept in data/fc/). Adds their results to the history,
    their next matches to the day files, and fills in finished scores."""
    if not fcharts.available():
        return 0
    if fetch_allowed:
        try:
            log('football-charts.com: ' + fcharts.refresh(log))
        except Exception as e:  # a source problem must never stop the run
            log(f'football-charts.com: {fcharts._mask(e)}')
    leagues, fixtures, results = fcharts.cached()
    if not leagues:
        return 0
    from datetime import datetime as _dt, timezone as _tz
    teams = {t['id']: t for t in history.get('t', [])}
    lgs = {l['id']: l for l in history.get('l', [])}
    index = _team_index(history)
    exact = {c: {} for c in index}
    for c, names in index.items():
        for n, tid in names.items():
            exact[c].setdefault(norm(n), set()).add(tid)
    by_league = {}
    for r in history.get('m', []):
        for tid in (r[4], r[5]):
            n = (teams.get(tid) or {}).get('name')
            if n:
                names = by_league.setdefault(r[2], {})
                if n not in names or (tid > 0 and names[n] < 0):
                    names[n] = tid
    matchers = {}

    def league_id(key):
        div = fcharts.DIVISION.get(key)
        if div:
            return DIV_LEAGUE.get(div) or synthetic_id('league|' + div)
        return synthetic_id('league|fc-' + key)

    def team_id(key, name, country):
        """(GOAL20 team id, True when it is a team GOAL20 already knew)."""
        k = f'{key}|{name}'
        if k in state.fc_teams:
            return state.fc_teams[k], True
        tid = None
        if not is_youth(name):
            lid = league_id(key)
            if key in fcharts.DIVISION and by_league.get(lid):
                if lid not in matchers:
                    matchers[lid] = Matcher({country: set(by_league[lid])})
                hit = matchers[lid].match(name, country)
                if hit:
                    tid = by_league[lid][hit[0].split('|', 1)[1]]
            if tid is None:
                ids = (exact.get(country) or {}).get(norm(name)) or set()
                if len(ids) == 1:
                    tid = next(iter(ids))
        if tid is not None:
            state.fc_teams[k] = tid
            return tid, True
        return synthetic_id(f'fc-team|{country}|{name}'), False

    def side(tid, name):
        known = teams.get(tid) or {}
        if tid not in teams:
            teams[tid] = {'id': tid, 'name': name, 'logo': ''}
            history['t'].append(teams[tid])
        return {'id': tid, 'name': known.get('name') or name, 'logo': known.get('logo') or ''}

    def league_json(key, season):
        lid = league_id(key)
        info = leagues[key]
        if lid not in lgs:
            lgs[lid] = {'id': lid, 'name': info['name'], 'country': info['country'],
                        'logo': '', 'flag': '', 'season': season}
            history['l'].append(lgs[lid])
        return dict(lgs[lid], season=season)

    def match_id(key, ts, home, away):
        day = _dt.fromtimestamp(ts, _tz.utc).date().isoformat()
        return synthetic_id(f'fc|{key}|{day}|{home}|{away}')

    # 1. Results into the history (two seasons for new leagues).
    cutoff = int(config.now_utc().timestamp()) - config.HISTORY_DAYS * 86400
    known = {r[0] for r in history['m']}
    pairs = {}
    for r in history['m']:
        pairs.setdefault((r[4], r[5]), []).append(r[1])
    added = 0
    recent = {}  # for filling in day files: (home id, away id) -> [(ts, hg, ag)]
    window_start = int(config.now_utc().timestamp()) - 9 * 86400
    for key, seasons in results.items():
        country = leagues[key]['country']
        for season, rows in seasons:
            sy = fcharts.season_year(season)
            for ts, h, a, hg, ag in rows:
                if ts < cutoff:
                    continue
                (hid, hk), (aid, ak) = team_id(key, h, country), team_id(key, a, country)
                if ts >= window_start:
                    recent.setdefault((hid, aid), []).append((ts, hg, ag))
                    recent.setdefault(f'{key}|{h}|{a}', []).append((ts, hg, ag))
                # Leagues GOAL20 has from football-data.co.uk: only add a
                # result when both clubs are recognised (never a double).
                if key in fcharts.DIVISION and not (hk and ak):
                    continue
                mid = match_id(key, ts, h, a)
                if mid in known or any(abs(t - ts) < 6 * 3600 for t in pairs.get((hid, aid), [])):
                    continue
                lg = league_json(key, sy)
                side(hid, h)
                side(aid, a)
                history['m'].append([mid, ts, lg['id'], sy, hid, aid, hg, ag])
                known.add(mid)
                pairs.setdefault((hid, aid), []).append(ts)
                added += 1

    # 2. Next matches into the published days, and 3. finished scores.
    today = config.today_cm()
    keys = [config.day_key(today + timedelta(days=i)) for i in range(-7, 2)]
    new_by_day = {k: [] for k in keys}
    for key, rows in fixtures.items():
        info = leagues[key]
        sy = fcharts.season_year((info.get('seasons') or [''])[0])
        for d, t, h, a, st in rows:
            ts = fcharts.kickoff_ts(None, d, t or '12:00')
            if ts is None:
                continue
            k = config.day_key(_dt.fromtimestamp(ts, config.CAMEROON).date())
            if k not in new_by_day:
                continue
            (hid, _), (aid, _) = team_id(key, h, info['country']), team_id(key, a, info['country'])
            code = fcharts.STATUS.get(st, 'NS')
            new_by_day[k].append({
                'id': match_id(key, ts, h, a), 'ts': ts, 'st': code,
                'stl': {'NS': 'Not Started', 'PST': 'Postponed', 'CANC': 'Cancelled'}.get(code, code),
                'el': None, 'lg': league_json(key, sy), 'rd': '', 'vn': '',
                'h': side(hid, h), 'a': side(aid, a),
                'hg': None, 'ag': None, 'fh': None, 'fa': None,
                'src': 'fc', 'xk': f'{key}|{h}|{a}',
            })

    def result_for(f):
        found = recent.get(f.get('xk')) if f.get('xk') else None
        found = found or recent.get((f['h']['id'], f['a']['id'])) or []
        best = [x for x in found if abs(x[0] - f['ts']) < 36 * 3600]
        return min(best, key=lambda x: abs(x[0] - f['ts'])) if best else None

    new_count, filled, changed_days = 0, 0, 0
    for k in keys:
        d = load_day(k)
        found = new_by_day.get(k) or []
        if d is None and not found:
            continue
        d = d or {'f': [], 'final': False}
        current = {f['id']: f for f in d['f']}
        changes = 0
        for fx in found:
            old = current.get(fx['id'])
            if old is not None:
                if old['st'] not in DONE_CODES and (old['ts'], old['st']) != (fx['ts'], fx['st']):
                    old.update(ts=fx['ts'], st=fx['st'], stl=fx['stl'])
                    changes += 1
                continue
            twin = None
            for f in current.values():
                if f.get('src') == 'fc':
                    continue
                gap = abs(f['ts'] - fx['ts'])
                same_h, same_a = f['h']['id'] == fx['h']['id'], f['a']['id'] == fx['a']['id']
                if (same_h and same_a and gap < 36 * 3600) or (
                        (same_h or same_a) and f['lg']['id'] == fx['lg']['id'] and gap < 3 * 3600):
                    twin = f
                    break
            if twin is not None:
                continue
            current[fx['id']] = fx
            new_count += 1
            changes += 1
        for f in current.values():
            if f['st'] in DONE_CODES or f['ts'] > int(config.now_utc().timestamp()):
                continue
            r = result_for(f)
            if r is None:
                continue
            f.update({'st': 'FT', 'stl': 'Match Finished', 'el': None,
                      'hg': r[1], 'ag': r[2], 'fh': r[1], 'fa': r[2]})
            filled += 1
            changes += 1
        if changes:
            fixtures_sorted = sorted(current.values(), key=lambda f: f['ts'])
            save_day(k, fixtures_sorted, day_is_final(k, fixtures_sorted))
            changed_days += 1
    log(f'football-charts.com: {added} results added to the history, {new_count} upcoming matches added, '
        f'{filled} scores filled in, {changed_days} day file(s) updated')
    return added


def add_finished_rows(history):
    """Adds finished matches from the published day files to the history."""
    known = {r[0] for r in history['m']}
    pairs = {}
    for r in history['m']:
        pairs.setdefault((r[4], r[5]), []).append(r[1])
    added = 0
    today = config.today_cm()
    for i in range(-7, 1):
        for f in (load_day(config.day_key(today + timedelta(days=i))) or {}).get('f', []):
            r = hist_row(f)
            if not r or r[0] in known:
                continue
            # The same match already stored under another id (kickoff times
            # of the sources differ by an hour or two at most).
            if any(abs(t - r[1]) < 6 * 3600 for t in pairs.get((r[4], r[5]), [])):
                continue
            history['m'].append(r)
            known.add(r[0])
            pairs.setdefault((r[4], r[5]), []).append(r[1])
            added += 1
            for side in ('h', 'a'):
                t = f[side]
                if t['id'] and not any(x['id'] == t['id'] for x in history['t']):
                    history['t'].append({'id': t['id'], 'name': t['name'], 'logo': t['logo']})
            if f['lg']['id'] and not any(x['id'] == f['lg']['id'] for x in history['l']):
                history['l'].append(f['lg'])
    return added


# ---------------------------------------------------------------- enrichment

def stamp():
    return config.now_utc().isoformat()


def fresh(entry, hours):
    try:
        at = datetime.fromisoformat(entry['at'])
    except (KeyError, ValueError, TypeError):
        return False
    return (config.now_utc() - at).total_seconds() < hours * 3600


def add_fd_odds(enrich, fd_upcoming, fd_to_api, fixtures):
    """Attach free football-data.co.uk Over 2.5 prices to API fixtures."""
    by_pair = {}
    for f in fixtures:
        if f['st'] in ('NS', 'TBD'):
            by_pair.setdefault((f['h']['id'], f['a']['id']), []).append(f)
    n = 0
    for u in fd_upcoming:
        hk, ak = f"{u['country']}|{u['home']}", f"{u['country']}|{u['away']}"
        hid = fd_to_api.get(hk) or synthetic_id('team|' + hk)
        aid = fd_to_api.get(ak) or synthetic_id('team|' + ak)
        if not (u['o25'] or u.get('win')):
            continue
        for f in by_pair.get((hid, aid), []):
            if abs(f['ts'] - u['ts']) > 36 * 3600:
                continue
            quotes = [{'b': f'{b} (football-data)', 'm': 'over25', 'o': o} for b, o in u['o25'].items()]
            for side, books in (u.get('win') or {}).items():
                quotes += [{'b': f'{b} (football-data)', 'm': side, 'o': o} for b, o in books.items()]
            enrich['fdodds'][str(f['id'])] = {'at': stamp(), 'v': quotes}
            n += 1
    if n:
        log(f'football-data.co.uk odds attached to {n} fixtures')


def enrich_candidates(api, enrich, history, fixtures):
    idx = rank.team_index(history['m'])
    now = config.now_utc().timestamp()
    scored = []
    for f in fixtures:
        if f['st'] not in ('NS', 'TBD') or f['ts'] <= now:
            continue
        if 'friendl' in f['lg']['name'].lower():
            continue
        s = rank.score(f, idx)
        if s and s[0] > -0.04:
            scored.append((s[0], f))
    scored.sort(key=lambda x: -x[0])
    picks = [f for _, f in scored[:config.ENRICH_MATCHES]]
    log(f'Enrichment: {len(scored)} promising matches, checking {len(picks)}')

    def run(cap, key, section, endpoint, params, convert, hours):
        if api.blocked(cap) or not api.can_spend():
            return
        if key in enrich[section] and fresh(enrich[section][key], hours):
            return
        try:
            rows = api.rows(endpoint, params, cap=cap)
        except ApiError as e:
            if e.kind != 'plan':
                log(f'{cap}: {e.message}')
            else:
                log(f'{cap}: not available on this plan')
            return
        enrich[section][key] = {'at': stamp(), 'v': convert(rows)}

    def h2h_rows(rows):
        out = []
        for r in rows:
            hr = hist_row(to_app_fixture(r))
            if hr:
                out.append(hr)
        return sorted(out, key=lambda r: -r[1])

    def injuries(rows):
        out = []
        for r in rows:
            p = r.get('player') or {}
            out.append({'t': (r.get('team') or {}).get('id') or 0, 'p': p.get('name') or '',
                        'ty': p.get('type') or '', 'r': p.get('reason') or ''})
        return out

    def odds(rows):
        out = []
        for r in rows:
            out.extend(parse_odds(r))
        return out

    for f in picks:
        a, b = sorted((f['h']['id'], f['a']['id']))
        run('h2h', f'{a}-{b}', 'h2h', 'fixtures/headtohead',
            {'h2h': f"{f['h']['id']}-{f['a']['id']}", 'timezone': config.API_TZ}, h2h_rows, 72)
    for f in picks:
        run('injuries', str(f['id']), 'inj', 'injuries', {'fixture': f['id']}, injuries, 6)
    for f in picks:
        run('odds', str(f['id']), 'odds', 'odds', {'fixture': f['id']}, odds, 3)


def merged_enrich(enrich):
    """App format: API odds plus football-data.co.uk Over 2.5 prices."""
    out = {k: dict(enrich.get(k, {})) for k in ('h2h', 'inj', 'lu', 'odds')}
    for key, entry in enrich.get('fdodds', {}).items():
        api = out['odds'].get(key)
        if api:
            out['odds'][key] = {'at': api['at'], 'v': api['v'] + entry['v']}
        else:
            out['odds'][key] = entry
    # betPawa prices (OddsPapi) are the prices the user can really get: where
    # one exists, it replaces the other bookmakers' price for that market.
    for key, entry in enrich.get('opodds', {}).items():
        cur = out['odds'].get(key)
        own = {q['m'] for q in entry['v']}
        if cur:
            out['odds'][key] = {'at': entry['at'], 'v': [q for q in cur['v'] if q['m'] not in own] + entry['v']}
        else:
            out['odds'][key] = entry
    return out


# ------------------------------------------------------------------ publish

def previous_site_file(name):
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    if '/' not in repo:
        return None
    owner, project = repo.split('/', 1)
    url = f'https://{owner.lower()}.github.io/{project}/{name}'
    status, _, body = fetch(url, timeout=60)
    if status != 200:
        return None
    import gzip
    try:
        return json.loads(gzip.decompress(body).decode('utf-8'))
    except Exception:
        try:
            return json.loads(body.decode('utf-8'))
        except Exception:
            return None


def status_note(api, state):
    """Shown on the app's Home screen when data is limited ('' = nothing)."""
    if api.available:
        return ''
    head = ('API-Football account suspended: ' if state.is_suspended
            else 'No API-Football key: ')
    parts = []
    if fdorg.available():
        parts.append('football-data.org (12 top competitions, results the same day, scores delayed)')
    if fcharts.available():
        parts.append('football-charts.com (about 80 more leagues incl. lower divisions, Africa and Asia; '
                     'results the next morning)')
    parts.append('football-data.co.uk (history and odds for European leagues; results a few days late)')
    if oddspapi.available():
        parts.append('OddsPapi (real betPawa prices, read once a day for the busiest leagues)')
    return head + 'your GOAL20 server now uses ' + ', '.join(parts) + '. No live scores.'



def publish(history, enrich, keys, api, state, mode):
    if os.path.isdir(config.SITE):
        shutil.rmtree(config.SITE)
    os.makedirs(os.path.join(config.SITE, 'days'))
    import hashlib

    def fingerprint(value):
        text = json.dumps(value, separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]

    merged = merged_enrich(enrich)
    write_json(os.path.join(config.SITE, 'history.json.gz'), history)
    write_json(os.path.join(config.SITE, 'enrich.json.gz'), merged)
    # The app downloads only the files whose fingerprint changed.
    files = {'history': fingerprint(history), 'enrich': fingerprint(merged), 'days': {}}
    if mode == 'quick':
        # Quick runs only add today's API results to the history, which the
        # app already gets from the day files: keep the full run's fingerprint
        # so phones do not download the whole history every hour.
        prev = previous_site_file('manifest.json') or {}
        old_fp = (prev.get('files') or {}).get('history')
        if old_fp:
            files['history'] = old_fp
    today = config.today_cm()
    published = []
    for i in range(-7, 2):
        k = config.day_key(today + timedelta(days=i))
        d = load_day(k)
        if d is None:
            if i < 0:
                continue
            # Today and tomorrow are always published, even with no match
            # (e.g. an international break), so the app shows 0 matches
            # instead of a missing-file error.
            d = {'at': config.now_utc().isoformat(), 'f': []}
        body = {'at': d.get('at'), 'f': d.get('f', [])}
        write_json(os.path.join(config.SITE, 'days', f'{k}.json'), body)
        files['days'][k] = fingerprint(body['f'])
        published.append(k)
    manifest = {
        'app': 'goal20', 'version': config.VERSION,
        'generated': config.now_utc().isoformat(), 'mode': mode,
        'plan': config.PLAN, 'provider_plan': state.plan_name,
        'requests_used': api.used, 'provider_remaining': api.remaining,
        'provider_limit': api.limit,
        'days': published, 'today': keys['today'], 'tomorrow': keys['tomorrow'],
        'history_results': len(history['m']),
        'files': files,
        'api_suspended': state.is_suspended,
        'api_note': state.suspended_why if state.is_suspended else '',
        'sources': [x for x, on in (('api-football', api.available), ('football-data.org', not api.available and fdorg.available()),
                                     ('football-charts.com', fcharts.available()),
                                     ('oddspapi.io (betPawa prices)', oddspapi.available()),
                                     ('football-data.co.uk', True)) if on],
        'attribution': [fcharts.ATTRIBUTION] if fcharts.available() else [],
        'status_note': status_note(api, state),
        'blocked': {k: state.blocked_why.get(k, '') for k in state.blocked},
        'notes': LOG[-40:],
    }
    write_json(os.path.join(config.SITE, 'manifest.json'), manifest, compact=False)
    with open(os.path.join(config.SITE, 'index.html'), 'w', encoding='utf-8') as f:
        f.write('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>GOAL20 server</title><body style="font-family:sans-serif;background:#0a0f16;color:#e8eef5;padding:20px">'
                f'<h2 style="color:#b8f23a">GOAL20 server is running</h2><p>Updated {manifest["generated"]} UTC · '
                f'{len(history["m"])} results stored · {api.used} API requests this run.</p>'
                '<p>Put this page\'s address in the GOAL20 app: Settings → GOAL20 server.</p>'
                + (f'<p>{fcharts.ATTRIBUTION} · football-data.org · football-data.co.uk</p>' if fcharts.available() else '')
                + '<pre style="white-space:pre-wrap;color:#8a99ab">'
                + '\n'.join(LOG[-40:]).replace('<', '&lt;') + '</pre></body>')
    open(os.path.join(config.SITE, '.nojekyll'), 'w').close()
    log(f'Published {len(published)} day files')


# --------------------------------------------------------------------- main

def main():
    mode = config.MODE if config.MODE in ('full', 'quick') else 'full'
    state = State()
    budget = config.FULL_RUN_BUDGET if mode == 'full' else config.QUICK_RUN_BUDGET
    api = Api(state, budget)
    log(f'GOAL20 server {config.VERSION} · mode {mode} · plan {config.PLAN}')
    if api.available and state.is_suspended and mode != 'full':
        api.suspended = True
        log('API-Football account suspended: no API requests this run (rechecked once a day at the full run)')
    if api.suspended:
        pass
    elif not api.available:
        log('No API_FOOTBALL_KEY secret: only football-data.co.uk history will be published')
    else:
        try:
            state.plan_name = api.status()
            log(f'API-Football plan: {state.plan_name} · remaining today: {api.remaining}')
        except ApiError as e:
            log(f'API status: {e.message}')

    keys = collect_days(api, state, mode == 'full') if api.available else {
        'today': config.day_key(config.today_cm()),
        'tomorrow': config.day_key(config.today_cm() + timedelta(days=1)),
    }
    if api.available:
        live_refresh(api, keys['today'])

    enrich = load_enrich()
    upcoming = [f for k in (keys['today'], keys['tomorrow']) for f in (load_day(k) or {}).get('f', [])]
    previous = previous_site_file('history.json.gz') if mode == 'quick' else None
    use_fdorg = not api.available and fdorg.available()
    if previous and previous.get('m'):
        # Quick run: reuse the last full history and add the finished matches.
        history = previous
        if use_fdorg:
            merge_fdorg(history, state, full=False)
        added = add_finished_rows(history)
        log(f'Quick run: reused published history ({len(history["m"])} results, {added} new)')
    else:
        fd_matches, fd_upcoming = fdcouk.load(log)
        history, fd_to_api, _ = build_history(state, fd_matches, fd_upcoming)
        if not api.available:
            fd_fallback_days(history, fd_matches, fd_upcoming, fd_to_api)
            if use_fdorg:
                merge_fdorg(history, state, full=True)
        # football-charts.com is read once a day, and only by full runs
        # (quick runs do not save the data folder).
        merge_fcharts(history, state, fetch_allowed=(mode == 'full'))
        add_finished_rows(history)
        upcoming = [f for k in (keys['today'], keys['tomorrow']) for f in (load_day(k) or {}).get('f', [])]
        enrich['fdodds'] = {}
        add_fd_odds(enrich, fd_upcoming, fd_to_api, upcoming)
    if mode == 'full' and oddspapi.available():
        try:
            log('OddsPapi: ' + oddspapi.refresh(upcoming, enrich, log, stamp))
        except Exception as e:  # a source problem must never stop the run
            log(f'OddsPapi: {oddspapi._mask(e)}')
    if mode == 'full' and api.available:
        enrich_candidates(api, enrich, history, upcoming)
        state.last_full = config.now_utc().isoformat()
    save_enrich(enrich)
    state.save()
    publish(history, enrich, keys, api, state, mode)
    log(f'Done · {api.used} API request(s) used this run')
    return 0


if __name__ == '__main__':
    sys.exit(main())
