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
from .apifootball import Api, ApiError, hist_row, parse_odds, to_app_fixture
from .names import Matcher
from .net import fetch
from .store import (State, all_day_keys, load_day, load_enrich, save_day,
                    save_enrich, write_json)

LOG = []


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
    done = all(f['st'] in ('FT', 'AET', 'PEN', 'PST', 'CANC', 'ABD', 'AWD', 'WO') for f in fixtures)
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
    wanted = [keys['today']]
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
    d = load_day(today_key) or {'f': []}
    by_id = {f['id']: f for f in d['f']}
    for r in rows:
        f = to_app_fixture(r)
        if f['id'] in by_id:
            by_id[f['id']] = f
    save_day(today_key, sorted(by_id.values(), key=lambda f: f['ts']), False)
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
        if not tid or tid not in teams:
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

    # Existing API pairs (for de-duplication), keyed by home/away and day.
    api_pairs = set()
    for r in rows.values():
        day = r[1] // 86400
        for dd in (day - 1, day, day + 1):
            api_pairs.add((r[4], r[5], dd))

    added = 0
    for m in fd_matches:
        hk, ak = f"{m['country']}|{m['home']}", f"{m['country']}|{m['away']}"
        hid = fd_to_api.get(hk) or synthetic_id('team|' + hk)
        aid = fd_to_api.get(ak) or synthetic_id('team|' + ak)
        if (hid, aid, m['ts'] // 86400) in api_pairs:
            continue
        lid = div_league.get(m['div']) or synthetic_id('league|' + m['div'])
        mid = synthetic_id(f"match|{m['div']}|{m['ts'] // 86400}|{m['home']}|{m['away']}")
        if mid not in rows:
            added += 1
        rows[mid] = [mid, m['ts'], lid, m['season'], hid, aid, m['hg'], m['ag']]
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
        hid = fd_to_api.get(f"{u['country']}|{u['home']}")
        aid = fd_to_api.get(f"{u['country']}|{u['away']}")
        if not hid or not aid or not (u['o25'] or u.get('win')):
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


def publish(history, enrich, keys, api, state, mode):
    if os.path.isdir(config.SITE):
        shutil.rmtree(config.SITE)
    os.makedirs(os.path.join(config.SITE, 'days'))
    write_json(os.path.join(config.SITE, 'history.json.gz'), history)
    write_json(os.path.join(config.SITE, 'enrich.json.gz'), merged_enrich(enrich))
    today = config.today_cm()
    published = []
    for i in range(-7, 2):
        k = config.day_key(today + timedelta(days=i))
        d = load_day(k)
        if d is None:
            continue
        write_json(os.path.join(config.SITE, 'days', f'{k}.json'), {'at': d.get('at'), 'f': d.get('f', [])})
        published.append(k)
    manifest = {
        'app': 'goal20', 'version': config.VERSION,
        'generated': config.now_utc().isoformat(), 'mode': mode,
        'plan': config.PLAN, 'provider_plan': state.plan_name,
        'requests_used': api.used, 'provider_remaining': api.remaining,
        'provider_limit': api.limit,
        'days': published, 'today': keys['today'], 'tomorrow': keys['tomorrow'],
        'history_results': len(history['m']),
        'blocked': {k: state.blocked_why.get(k, '') for k in state.blocked},
        'notes': LOG[-40:],
    }
    write_json(os.path.join(config.SITE, 'manifest.json'), manifest, compact=False)
    with open(os.path.join(config.SITE, 'index.html'), 'w', encoding='utf-8') as f:
        f.write('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>GOAL20 server</title><body style="font-family:sans-serif;background:#0a0f16;color:#e8eef5;padding:20px">'
                f'<h2 style="color:#b8f23a">GOAL20 server is running</h2><p>Updated {manifest["generated"]} UTC · '
                f'{len(history["m"])} results stored · {api.used} API requests this run.</p>'
                '<p>Put this page\'s address in the GOAL20 app: Settings → GOAL20 server.</p><pre style="white-space:pre-wrap;color:#8a99ab">'
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
    if not api.available:
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
    if previous and previous.get('m'):
        # Quick run: reuse the last full history and add today's finished matches.
        history = previous
        known = {r[0] for r in history['m']}
        for f in (load_day(keys['today']) or {}).get('f', []):
            r = hist_row(f)
            if r and r[0] not in known:
                history['m'].append(r)
        log(f'Quick run: reused published history ({len(history["m"])} results)')
    else:
        fd_matches, fd_upcoming = fdcouk.load(log)
        history, fd_to_api, _ = build_history(state, fd_matches, fd_upcoming)
        enrich['fdodds'] = {}
        add_fd_odds(enrich, fd_upcoming, fd_to_api, upcoming)
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
