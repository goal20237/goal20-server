"""GOAL20's learning loop (server 1.14): the model reviews its own work like a
small engineering team and only ever becomes MORE careful.

  Analyst      walk-forward review: re-predicts finished matches using only the
               data that existed before each match, then compares what the
               model said with what happened (market by market, league by
               league).
  Risk         turns a proven over-confidence into a small negative correction
               (never positive, at most 8 points, half strength, only with
               enough matches).
  QA           post-mortem of every lost Daily Top 3 leg: why it lost.
  Release      a correction goes live only if it would have improved the
               newest 40% of the matches it was NOT trained on; otherwise it is
               rolled back. Every change is written to an audit log.

Honest limits: it learns from results GOAL20 already has (about 90 days), it
cannot invent information the data does not contain, and a lucky or unlucky
week does not change the model (minimum sample sizes and half strength).
"""
import json
import math
import os
import time
from datetime import datetime, timezone

from . import config
from .prediction_engine import DAY, PredictionEngine, settle_leg

PATH = os.path.join(config.DATA, 'learning', 'report.json')
WEEK = 7 * DAY

# (market, lowest model probability worth reviewing). Tickets only use the
# high-probability zone of the safe markets, so that is where we measure.
MARKETS = {
    '1X': 0.55, 'X2': 0.55, 'O1.5': 0.55, 'DNB1': 0.55, 'DNB2': 0.55, 'U2.5': 0.55,
    '1': 0.25, '2': 0.25, 'X': 0.20, 'O2.5': 0.30, 'BTTS_YES': 0.30,
    'BTTS_O2.5': 0.25, 'H_O1.5': 0.25, 'A_O1.5': 0.25,
}
MIN_ALL = 150          # matches before a market-wide correction is considered
MIN_LEAGUE = 40        # matches before a league-specific correction is considered
MIN_COLD = 60
STRENGTH = 0.5         # only half of the measured gap is corrected
MAX_CUT = 0.08         # never more than 8 points
MIN_CUT = 0.005        # smaller than this is noise
MIN_REVIEW = 300       # fewer reviewed picks than this: no correction at all
TRAIN_SHARE = 0.6

LABEL = {'1X': '1X (home or draw)', 'X2': 'X2 (away or draw)', 'O1.5': 'Over 1.5 goals', 'DNB1': 'Draw no bet home',
         'DNB2': 'Draw no bet away', 'U2.5': 'Under 2.5', '1': 'Home win', '2': 'Away win', 'X': 'Draw',
         'O2.5': 'Over 2.5', 'BTTS_YES': 'Both teams score', 'BTTS_O2.5': 'Both score & Over 2.5',
         'H_O1.5': 'Home team over 1.5', 'A_O1.5': 'Away team over 1.5'}


class _Review(PredictionEngine):
    """Same model; ratings are refitted weekly instead of daily (the review
    covers hundreds of matches), always from data older than the match."""

    def cutoff_for(self, ts):
        return min(self.now - self.now % DAY, ts - ts % WEEK)


# ------------------------------------------------------------ the numbers

def _p_adj(p, adj):
    return min(0.999, max(0.001, p + adj))


def _brier(rows, adj_of=None):
    if not rows:
        return None
    tot = 0.0
    for r in rows:
        p = r['p'] + (adj_of(r) if adj_of else 0.0)
        tot += (min(1.0, max(0.0, p)) - (1.0 if r['won'] else 0.0)) ** 2
    return tot / len(rows)


def _groups(rows):
    out = {}
    for r in rows:
        out.setdefault(f"{r['m']}|all", []).append(r)
        out.setdefault(f"{r['m']}|L{r['lg']}", []).append(r)
        if r['cold']:
            out.setdefault(f"{r['m']}|cold", []).append(r)
    return out


def fit_adjustments(rows):
    """{key: {adj, n, pred, obs}}: reduce-only, shrunk, capped corrections."""
    out = {}
    if len(rows) < MIN_REVIEW:
        return out
    for key, g in _groups(rows).items():
        scope = key.split('|', 1)[1]
        need = MIN_ALL if scope == 'all' else MIN_COLD if scope == 'cold' else MIN_LEAGUE
        n = len(g)
        if n < need:
            continue
        pred = sum(r['p'] for r in g) / n
        obs = sum(1 for r in g if r['won']) / n
        diff = obs - pred
        if diff >= 0:
            continue              # under-confident: we never raise a probability
        adj = max(-MAX_CUT, STRENGTH * n / (n + 60.0) * diff)
        if adj <= -MIN_CUT:
            out[key] = {'adj': round(adj, 4), 'n': n, 'pred': round(pred, 4), 'obs': round(obs, 4)}
    return out


def _adj_fn(adjustments):
    def f(r):
        keys = [f"{r['m']}|all", f"{r['m']}|L{r['lg']}"] + ([f"{r['m']}|cold"] if r['cold'] else [])
        return min([0.0] + [adjustments[k]['adj'] for k in keys if k in adjustments])
    return f


def gate(rows):
    """Walk-forward release test: fit on the older 60%, judge on the newest
    40%. Returns (passed, brier_raw, brier_adjusted, test_n)."""
    rows = sorted(rows, key=lambda r: r['ts'])
    cut = int(len(rows) * TRAIN_SHARE)
    train, test = rows[:cut], rows[cut:]
    adj = fit_adjustments(train)
    f = _adj_fn(adj)
    touched = [r for r in test if f(r) < 0]
    if not adj or not touched:
        return None, None, None, len(touched)
    raw = _brier(touched)
    new = _brier(touched, f)
    if new > raw + 0.0005:
        return False, raw, new, len(touched)
    if new < raw - 0.0002:
        return True, raw, new, len(touched)
    return None, raw, new, len(touched)     # no real difference: nothing to release


# ---------------------------------------------------------------- review

def collect(history, cache=None, log=print, now=None, days=90, max_matches=1600, budget_s=150):
    """Walk-forward predictions for finished matches: [{ts, lg, cold, m, p, won}]."""
    now = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    engine = _Review(history, cache, log, now=now)
    teams = {t['id']: t.get('name') or '' for t in history.get('t', [])}
    leagues = {l['id']: l for l in history.get('l', [])}
    lo = now - days * DAY
    pool = [r for r in history.get('m', []) if len(r) >= 8 and r[6] is not None and r[7] is not None
            and lo <= r[1] < now - DAY and r[2] in leagues]
    pool.sort(key=lambda r: r[1])
    if len(pool) > max_matches:
        step = len(pool) / max_matches
        pool = [pool[int(i * step)] for i in range(max_matches)]
    rows, started, skipped = [], time.time(), 0
    for r in pool:
        if time.time() - started > budget_s:
            log(f'Learning review stopped at its time budget ({len(rows)} matches reviewed)')
            break
        lg = leagues[r[2]]
        fx = {'id': r[0], 'ts': r[1], 'h': {'id': r[4], 'name': teams.get(r[4], '')},
              'a': {'id': r[5], 'name': teams.get(r[5], '')}, 'lg': {'id': r[2], 'name': lg.get('name') or ''}}
        try:
            pred = engine.predict_match(fx)
        except Exception:
            skipped += 1
            continue
        if not pred.get('probs'):
            skipped += 1
            continue
        cold = pred.get('quality') == 'cold' or bool((pred.get('model') or {}).get('cold'))
        for m, floor in MARKETS.items():
            p = (pred['probs'] or {}).get(m)
            if p is None:
                p = (pred.get('more') or {}).get(m)
            if p is None or p < floor:
                continue
            won = settle_leg(m, r[6], r[7])
            if won is None:
                continue
            rows.append({'ts': r[1], 'lg': r[2], 'cold': cold, 'm': m, 'p': p, 'won': bool(won)})
    log(f'Learning review: {len(pool) - skipped} matches re-predicted, {len(rows)} market picks scored')
    return rows, len(pool) - skipped


def _name_of(history):
    leagues = {l['id']: l for l in history.get('l', [])}

    def f(lid):
        l = leagues.get(lid) or {}
        return (l.get('name') or f'league {lid}') + (f" ({l['country']})" if l.get('country') else '')
    return f


def calibration_tables(rows):
    markets = []
    for m in MARKETS:
        g = [r for r in rows if r['m'] == m]
        if len(g) >= 20:
            markets.append({'market': m, 'label': LABEL[m], 'n': len(g),
                            'pred': round(sum(r['p'] for r in g) / len(g), 3),
                            'obs': round(sum(1 for r in g if r['won']) / len(g), 3)})
    safe = [r for r in rows if r['m'] in ('1X', 'X2', 'O1.5', 'DNB1', 'DNB2') and r['p'] >= 0.65]
    bands = []
    for lo, hi in ((0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 1.01)):
        g = [r for r in safe if lo <= r['p'] < hi]
        if len(g) >= 20:
            bands.append({'lo': lo, 'hi': min(hi, 1.0), 'n': len(g),
                          'pred': round(sum(r['p'] for r in g) / len(g), 3),
                          'obs': round(sum(1 for r in g if r['won']) / len(g), 3)})
    return markets, bands


# ------------------------------------------------------------ post-mortem

def _cause(leg, ticket_kind):
    """(cause, plain-language note) for one lost leg."""
    vol = ' '.join(leg.get('volatility') or [])
    if 'derby' in vol:
        return 'derby', 'Same-city derby: home advantage and form are unreliable in derbies.'
    if 'youth' in vol or 'reserve' in vol:
        return 'youth', 'Youth/reserve side: line-ups change from week to week.'
    if 'cup' in vol:
        return 'cup', 'Cup tie: sides rotate and play to different goals.'
    if leg.get('cold') or 'cold-start' in vol:
        return 'thin_data', 'Thin data: the teams had few recent matches, so the rating was a rough guess.'
    if leg.get('price_check') == 'unverified':
        return 'no_price', 'No bookmaker price to cross-check: the model was on its own.'
    mp, mo = leg.get('market_prob'), leg.get('model_prob')
    if isinstance(mp, (int, float)) and isinstance(mo, (int, float)) and mo - mp > 0.05:
        return 'market_disagreed', f'The bookmaker was {round((mo - mp) * 100)} points less sure than the model.'
    if 'congestion' in vol:
        return 'congestion', 'Fixture congestion: tired legs.'
    lose = leg.get('lose')
    note = (f'Bad luck: this leg was expected to lose about {round(lose * 100)}% of the time.'
            if isinstance(lose, (int, float)) and lose else 'Bad luck: the model had no warning sign.')
    return 'bad_luck', note


def post_mortems(archive):
    counts, recent, won, lost = {}, [], 0, 0
    for day in sorted(archive):
        t = (archive[day] or {}).get('top3') or {}
        for leg in t.get('legs') or []:
            st = leg.get('status')
            if st == 'won':
                won += 1
            elif st == 'lost':
                lost += 1
                cause, note = _cause(leg, 'top3')
                counts[cause] = counts.get(cause, 0) + 1
                recent.append({'day': day, 'match': f"{leg.get('home', '')} v {leg.get('away', '')}",
                               'pick': leg.get('label', ''), 'score': leg.get('score'),
                               'prob': leg.get('probability'), 'grade': leg.get('grade'),
                               'cause': cause, 'note': note})
    return {'top3_won': won, 'top3_lost': lost, 'counts': counts, 'recent': recent[-10:][::-1]}


# ----------------------------------------------------------------- report

def load(path=PATH):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def active_adjustments(report):
    """{key: points} of a report, or {} when it is not 'active'."""
    if (report or {}).get('status') != 'active':
        return {}
    return {k: v['adj'] for k, v in (report.get('adjustments') or {}).items()}


def _lessons(adjustments, name_of, pm, status, brier):
    out = []
    for key, v in sorted(adjustments.items(), key=lambda kv: kv[1]['adj'])[:6]:
        m, scope = key.split('|', 1)
        where = 'in every league' if scope == 'all' else 'on thin-data matches' if scope == 'cold' \
            else f"in {name_of(int(scope[1:]))}"
        out.append(f"{LABEL.get(m, m)} {where}: the model said {round(v['pred'] * 100)}% but only "
                   f"{round(v['obs'] * 100)}% happened over {v['n']} matches. Now trimmed by "
                   f"{abs(round(v['adj'] * 100, 1)):g} points.")
    if pm['counts']:
        cause, n = max(pm['counts'].items(), key=lambda kv: kv[1])
        names = {'bad_luck': 'bad luck (no warning sign)', 'thin_data': 'thin data', 'derby': 'derbies',
                 'youth': 'youth/reserve sides', 'cup': 'cup ties', 'no_price': 'no bookmaker price',
                 'market_disagreed': 'the bookmaker disagreeing', 'congestion': 'fixture congestion'}
        out.append(f"Most common reason for lost Daily Top 3 legs so far: {names.get(cause, cause)} ({n} of "
                   f"{pm['top3_lost']}).")
    if status == 'none':
        out.append('No proven weakness yet: the model is as sure as it should be on the matches reviewed.')
    elif status == 'rolled_back':
        out.append('A correction was tried but would have made the newest matches worse, so it was rolled back.')
    elif status == 'not_enough_data':
        out.append('Not enough finished matches yet to change anything. Nothing is guessed.')
    return out


def build_report(history, rows, matches, archive, previous=None, now=None):
    now = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    previous = previous or {}
    name_of = _name_of(history)
    pm = post_mortems(archive)
    markets, bands = calibration_tables(rows)
    adjustments, status, quality = {}, 'not_enough_data', {}
    if len(rows) >= MIN_REVIEW:
        passed, raw, new, tn = gate(rows)
        if passed is None:
            status = 'none'
        elif passed:
            status = 'active'
            adjustments = fit_adjustments(rows)
            quality = {'test_matches': tn, 'brier_before': round(raw, 4), 'brier_after': round(new, 4)}
            if not adjustments:
                status = 'none'
        else:
            status = 'rolled_back'
            quality = {'test_matches': tn, 'brier_before': round(raw, 4), 'brier_after': round(new, 4)}
    old = (previous.get('adjustments') or {}) if previous.get('status') == 'active' else {}
    changes = []
    for k, v in adjustments.items():
        if k not in old:
            changes.append(f"NEW  {k}: {v['adj'] * 100:+.1f} pts (n={v['n']})")
        elif abs(old[k]['adj'] - v['adj']) >= 0.005:
            changes.append(f"CHANGED  {k}: {old[k]['adj'] * 100:+.1f} -> {v['adj'] * 100:+.1f} pts (n={v['n']})")
    for k in old:
        if k not in adjustments:
            changes.append(f"REMOVED  {k}: no longer needed")
    brier_all = _brier(rows)
    trend = list(previous.get('trend') or [])
    day = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
    entry = {'day': day, 'n': len(rows), 'brier': None if brier_all is None else round(brier_all, 4),
             'status': status, 'cuts': len(adjustments)}
    trend = [t for t in trend if t.get('day') != day] + [entry]
    audit = list(previous.get('audit') or [])
    if changes:
        audit.append({'day': day, 'changes': changes})
    ts = [r['ts'] for r in rows]
    report = {
        'version': 1, 'at': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'status': status,
        'status_text': {'active': 'Learning is active: some probabilities are trimmed where the model was proven over-confident.',
                        'none': 'Reviewed: no proven weakness, nothing changed.',
                        'rolled_back': 'A correction was tested and rolled back (it did not help on new matches).',
                        'not_enough_data': 'Waiting for more finished matches before changing anything.'}[status],
        'sample': {'matches': matches, 'picks': len(rows), 'from': min(ts) if ts else None,
                   'to': max(ts) if ts else None},
        'quality': quality, 'adjustments': adjustments, 'markets': markets, 'bands': bands,
        'postmortem': pm, 'lessons': _lessons(adjustments, name_of, pm, status, brier_all),
        'changes': changes, 'trend': trend[-30:], 'audit': audit[-30:],
        'team': [
            {'role': 'Analyst', 'job': 'Re-predicts finished matches with only the data known before each one',
             'state': f"{matches} matches, {len(rows)} picks reviewed"},
            {'role': 'Risk', 'job': 'Only trims probabilities (max 8 points, half strength, enough matches)',
             'state': f"{len(adjustments)} correction(s) live"},
            {'role': 'QA', 'job': 'Post-mortem of every lost Daily Top 3 leg',
             'state': f"{pm['top3_lost']} lost, {pm['top3_won']} won"},
            {'role': 'Release', 'job': 'Tests on the newest 40% of matches; rolls back if worse',
             'state': {'active': 'passed', 'none': 'nothing to release', 'rolled_back': 'ROLLED BACK',
                       'not_enough_data': 'waiting for data'}[status]},
        ],
    }
    return report


def save(report, path=PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(report, f, separators=(',', ':'), ensure_ascii=False)


def summary(report):
    """The slice published in manifest.json for the app's Learning card."""
    if not report:
        return {}
    keep = ('version', 'at', 'status', 'status_text', 'sample', 'quality', 'markets', 'bands', 'lessons',
            'changes', 'team', 'trend')
    out = {k: report[k] for k in keep if k in report}
    out['postmortem'] = report.get('postmortem') or {}
    out['cuts'] = len(report.get('adjustments') or {})
    return out


def run(history, cache, archive, log=print, now=None, **kw):
    """Full review: collect, gate, report, save. Never raises into the run."""
    try:
        rows, matches = collect(history, cache, log, now, **kw)
        report = build_report(history, rows, matches, archive, load(), now)
        save(report)
        log(f"Learning: {report['status']} · {len(report['adjustments'])} correction(s) · "
            f"{len(report['changes'])} change(s)")
        return report
    except Exception as e:  # a review problem must never stop the site update
        import traceback
        log(f'Learning review failed: {e}')
        traceback.print_exc()
        return load()
