"""GOAL20's two automatic daily tickets (server 1.8), built from the
predictionEngine's probabilities right after the predictions.

generate_daily_top3()  "Daily 3 Sure-Shot Picks"
    Only the safest markets (1X, X2, Over 1.5, draw no bet) at 78.0%+ model
    probability, one leg per match. GOAL20 prefers legs whose fair odds sit
    between 1.20 and 1.55 (78% - 83%), so the slip pays something real
    rather than 1.05; only when fewer than 3 such legs exist does it take the
    safer ones closest to that band (above 83%, fair odds just under 1.20).
    Fewer than 3 legs: no ticket (SKIPPED_LOW_EDGE).

generate_high_odds_accumulator(target_odds=1000)  "1000+ Odds Value Longshot"
    Higher-yield markets (1X2, both teams score & Over 2.5, home / away team
    Over 1.5) that have a REAL bookmaker price. Each leg's expected value is
    model probability x price - 1; legs are taken strictly in order of that
    value until the combined price reaches the target (at most 14 legs).
    A market without a real price is never used: its value cannot be known.

Both tickets are rebuilt at every run until their first leg kicks off, then
frozen, and settled from the results like the predictions. GOAL20 never
places bets.
"""
import json
import os

from . import config
from .prediction_engine import settle_leg

TOP3 = {
    'title': 'Daily 3 Sure-Shot Picks',
    'markets': ['1X', 'X2', 'O1.5', 'DNB1', 'DNB2'],
    'legs': 3,
    'min_probability': 0.78,
    'fair_band': (1.20, 1.55),
    'min_evidence': 8.0,   # time-weighted league matches per team
}
LONGSHOT = {
    'title': '1000+ Odds Value Longshot',
    'markets': ['1', 'X', '2', 'BTTS_O2.5', 'H_O1.5', 'A_O1.5'],
    'target_odds': 1000.0,
    'max_legs': 14,
    'min_legs': 3,
    'min_probability': 0.30,  # below this the model is least reliable
    'min_price': 1.60,        # 14 legs at 1.64 average reach 1000
    'min_edge': 0.02,
    'max_edge': 0.50,         # a bigger "edge" is almost always a data error
    'min_evidence': 8.0,
    'badge': 'High-Variance +EV Ticket — Minimal Stake Only',
    'badge_estimated': 'High-Variance Ticket — Minimal Stake Only (estimated odds)',
    'fallback_fair': (1.70, 3.00),
}
# GOAL20 market -> key of the bookmaker price in the published odds.
PRICE_KEY = {'1': 'home', 'X': 'draw', '2': 'away', 'O1.5': 'over15',
             'BTTS_O2.5': 'btts_o25', 'H_O1.5': 'home_o15', 'A_O1.5': 'away_o15'}
VOID_STATUSES = ('PST', 'CANC', 'ABD', 'AWD', 'WO')
ARCHIVE = os.path.join(config.DATA, 'tickets.json')
KEEP_DAYS = 180


# ----------------------------------------------------------------- helpers

def probability(pred, market):
    v = (pred.get('probs') or {}).get(market)
    if v is None:
        v = (pred.get('more') or {}).get(market)
    return v


def lose_chance(pred, market):
    """Chance the leg loses (draw no bet: only the other side winning)."""
    probs = pred.get('probs') or {}
    if market == 'DNB1':
        return probs.get('2')
    if market == 'DNB2':
        return probs.get('1')
    p = probability(pred, market)
    return None if p is None else 1 - p


def label(market, home, away):
    return {
        '1': f'{home} to win', 'X': 'Draw', '2': f'{away} to win',
        '1X': f'1X: {home} or draw', 'X2': f'X2: {away} or draw', '12': '12: no draw',
        'O1.5': 'Over 1.5 goals', 'U1.5': 'Under 1.5 goals', 'O2.5': 'Over 2.5 goals', 'U2.5': 'Under 2.5 goals',
        'BTTS_YES': 'Both teams to score', 'BTTS_NO': 'Both teams to score: No',
        'DNB1': f'Draw no bet: {home}', 'DNB2': f'Draw no bet: {away}',
        'BTTS_O2.5': 'Both teams score & Over 2.5', 'H_O1.5': f'{home} over 1.5 goals',
        'A_O1.5': f'{away} over 1.5 goals',
    }.get(market, market)


def price_for(quotes, key):
    """(price, source) for a market: betPawa when known (the price you can
    really get), else the market average, else the lowest quote."""
    found = [q for q in quotes or [] if q.get('m') == key and isinstance(q.get('o'), (int, float)) and q['o'] > 1]
    if not found:
        return None, ''
    for q in found:
        if 'betpawa' in (q.get('b') or '').lower():
            return float(q['o']), 'betPawa'
    for q in found:
        if 'market average' in (q.get('b') or '').lower():
            return float(q['o']), 'market average'
    q = min(found, key=lambda x: x['o'])
    return float(q['o']), (q.get('b') or 'bookmaker').replace(' (football-data)', '')


def is_cold(pred):
    return pred.get('quality') == 'cold' or bool((pred.get('model') or {}).get('cold'))


def _open(pred, now, min_evidence, allow_cold=False):
    """Rated, not started, enough evidence on both teams (or, with
    allow_cold, a cold-start rating)."""
    if pred.get('ts', 0) <= now or not pred.get('probs'):
        return False
    if pred.get('st') and pred['st'] not in ('NS', 'TBD'):
        return False
    if is_cold(pred):
        return allow_cold
    ev = (pred.get('model') or {}).get('evidence') or [0, 0]
    return min(ev) >= min_evidence


def _leg(pred, market, p, odds, **extra):
    leg = {
        'id': pred['id'], 'ts': pred['ts'], 'home': pred.get('home', ''), 'away': pred.get('away', ''),
        'market': market, 'label': label(market, pred.get('home', ''), pred.get('away', '')),
        'probability': round(p, 4), 'fair_odds': round(1 / p, 2), 'odds': round(odds, 2),
        'lose': round(lose_chance(pred, market) or 0.0, 4), 'status': 'pending',
    }
    leg.update(extra)
    return leg


def _product(values):
    out = 1.0
    for v in values:
        out *= v
    return out


# ------------------------------------------------------------ the builders

def generate_daily_top3(preds, now, day=''):
    """Daily 3 Sure-Shot Picks for one day's predictions."""
    lo, hi = TOP3['fair_band']
    ranked = []
    for pred in preds:
        if not _open(pred, now, TOP3['min_evidence'], allow_cold=True):
            continue
        cold = is_cold(pred)
        best = None
        for m in TOP3['markets']:
            p = probability(pred, m)
            if p is None or p < TOP3['min_probability'] - 1e-12:
                continue
            in_band = lo - 1e-9 <= 1 / p <= hi + 1e-9
            # In the band: the highest probability first. Above it (fair odds
            # under 1.20): the one closest to the band first.
            key = (0, -p) if in_band else (1, p)
            if best is None or key < best[0]:
                best = (key, m, p)
        if best:
            # Teams with full data always come before cold-start ratings.
            ranked.append(((1 if cold else 0,) + best[0], pred['ts'], pred['id'],
                           _leg(pred, best[1], best[2], 1 / best[2], in_band=best[0][0] == 0, cold=cold)))
    ranked.sort(key=lambda r: r[:3])
    legs = [r[3] for r in ranked[:TOP3['legs']]]
    ticket = {
        'kind': 'top3', 'title': TOP3['title'], 'day': day, 'badge': None, 'built': int(now),
        'odds_kind': 'fair', 'legs': legs,
        'rules': (f"1X, X2, Over 1.5 or draw no bet at {TOP3['min_probability']:.0%}+, one leg per match, "
                  f"fair odds {lo:.2f}-{hi:.2f} preferred"),
    }
    if len(legs) < TOP3['legs']:
        ticket['legs'] = []
        ticket['status'] = 'SKIPPED_LOW_EDGE'
        ticket['note'] = (f"Only {len(legs)} match(es) reach {TOP3['min_probability']:.0%} on a safe market "
                          f"with enough data: no Daily 3 today.")
        return _totals(ticket)
    ticket['status'] = 'READY'
    in_band = sum(1 for l in legs if l.get('in_band'))
    ticket['note'] = ('All 3 legs in the 1.20-1.55 fair-odds band.' if in_band == 3 else
                      f'{in_band} of 3 legs in the 1.20-1.55 band; the others are safer (fair odds under 1.20).')
    cold = sum(1 for l in legs if l.get('cold'))
    if cold:
        ticket['note'] += f' {cold} leg(s) use a cold-start rating (thin data): trust them less.'
    return _totals(ticket)


def generate_high_odds_accumulator(preds, odds, now, day='', target_odds=LONGSHOT['target_odds']):
    """1000+ Odds Value Longshot for one day's predictions. odds: the
    published odds by fixture id ({'v': [{'b', 'm', 'o'}]}).

    1. Legs with a REAL price and positive value, best value first.
    2. If those cannot reach the target within 14 legs, the rest is filled
       with estimated legs at the model's fair odds (1.70-3.00, the most
       likely outcomes first, full-data teams before cold starts). Their
       value is zero by definition and a bookmaker pays less: the ticket
       then says so and loses the +EV badge."""
    value, fallback = [], []
    priced = 0
    for pred in preds:
        if not _open(pred, now, LONGSHOT['min_evidence'], allow_cold=True):
            continue
        cold = is_cold(pred)
        quotes = ((odds or {}).get(str(pred['id'])) or {}).get('v') or []
        best = est = None
        for m in LONGSHOT['markets']:
            p = probability(pred, m)
            if p is None or p <= 0:
                continue
            price, source = price_for(quotes, PRICE_KEY[m])
            if price is not None:
                priced += 1
                edge = p * price - 1
                if (not cold and p >= LONGSHOT['min_probability'] and price >= LONGSHOT['min_price']
                        and LONGSHOT['min_edge'] <= edge <= LONGSHOT['max_edge']):
                    if best is None or edge > best[0]:
                        best = (edge, m, p, price, source)
            fair = 1 / p
            if LONGSHOT['fallback_fair'][0] <= fair <= LONGSHOT['fallback_fair'][1]:
                key = (1 if cold else 0, -p)
                if est is None or key < est[0]:
                    est = (key, m, p)
        if best:
            edge, m, p, price, source = best
            value.append(_leg(pred, m, p, price, price=round(price, 2), source=source, edge=round(edge, 4)))
        elif est:
            key, m, p = est
            fallback.append(_leg(pred, m, p, 1 / p, source='estimated (model fair odds)', estimated=True,
                                 edge=0.0, cold=bool(key[0])))
    # Strictly by expected value, then earlier kickoff.
    value.sort(key=lambda l: (-l['edge'], l['ts'], l['id']))
    fallback.sort(key=lambda l: (1 if l.get('cold') else 0, -l['probability'], l['ts'], l['id']))
    legs, total = [], 1.0
    for leg in value + fallback:
        if len(legs) >= LONGSHOT['max_legs'] or total >= target_odds:
            break
        legs.append(leg)
        total *= leg['odds']
    legs.sort(key=lambda l: (l['ts'], l['id']))
    estimated = sum(1 for l in legs if l.get('estimated'))
    ticket = {
        'kind': 'longshot', 'title': LONGSHOT['title'], 'day': day,
        'badge': LONGSHOT['badge'] if legs and not estimated else LONGSHOT['badge_estimated'],
        'built': int(now), 'odds_kind': 'price' if not estimated else 'mixed', 'target': target_odds,
        'legs': legs, 'candidates': len(value), 'estimated_legs': estimated, 'priced_markets': priced,
        'rules': (f"real prices first: 1X2, BTTS & Over 2.5, team Over 1.5 at {LONGSHOT['min_price']:.2f}+, "
                  f"model {LONGSHOT['min_probability']:.0%}+, value +{LONGSHOT['min_edge']:.0%} to "
                  f"+{LONGSHOT['max_edge']:.0%}, best value first; then estimated legs at fair odds "
                  f"{LONGSHOT['fallback_fair'][0]:.2f}-{LONGSHOT['fallback_fair'][1]:.2f}, most likely first; "
                  f"up to {LONGSHOT['max_legs']} legs"),
    }
    if len(legs) < LONGSHOT['min_legs']:
        ticket['legs'] = []
        ticket['status'] = 'SKIPPED_LOW_EDGE'
        ticket['note'] = 'Not enough rated matches left today to build a longshot.'
    elif total >= target_odds:
        ticket['status'] = 'READY'
        ticket['note'] = f'{len(legs)} legs reach the {target_odds:,.0f} target.'
    else:
        ticket['status'] = 'BELOW_TARGET'
        ticket['note'] = f'All {len(legs)} usable legs give {total:,.0f}, short of {target_odds:,.0f}.'
    if estimated and ticket['legs']:
        ticket['note'] += (f" {estimated} of {len(legs)} legs have no real price: their odds are GOAL20's fair "
                           f"odds (no proven value), and your bookmaker will pay less.")
    return _totals(ticket)


generateDailyTop3 = generate_daily_top3
generateHighOddsAccumulator = generate_high_odds_accumulator


def _totals(ticket):
    legs = ticket['legs']
    ticket['odds'] = round(_product(l['odds'] for l in legs), 2) if legs else None
    ticket['fair_odds'] = round(_product(l['fair_odds'] for l in legs), 2) if legs else None
    # Chance the slip pays (a void draw-no-bet leg counts as surviving).
    ticket['chance'] = round(_product(1 - l['lose'] for l in legs), 6) if legs else None
    if ticket['odds_kind'] in ('price', 'mixed') and legs:
        ticket['ev'] = round(_product(l['probability'] * l['odds'] for l in legs) - 1, 4)
    ticket['first_kickoff'] = min((l['ts'] for l in legs), default=None)
    return ticket


# ----------------------------------------------------- freeze and settle

def settle_ticket(ticket, preds_by_id):
    """Leg statuses from the results, then the ticket's result."""
    for leg in ticket.get('legs') or []:
        p = preds_by_id.get(leg['id'])
        res = (p or {}).get('result')
        if res and len(res) == 2:
            won = settle_leg(leg['market'], res[0], res[1])
            leg['status'] = 'void' if won is None else ('won' if won else 'lost')
            leg['score'] = res
        elif (p or {}).get('st') in VOID_STATUSES:
            leg['status'] = 'void'
        else:
            leg['status'] = 'pending'
    legs = ticket.get('legs') or []
    if not legs or ticket.get('status') == 'SKIPPED_LOW_EDGE':
        ticket['result'] = None
    elif any(l['status'] == 'lost' for l in legs):
        ticket['result'] = 'LOST'
    elif any(l['status'] == 'pending' for l in legs):
        ticket['result'] = 'PENDING'
    elif all(l['status'] == 'void' for l in legs):
        ticket['result'] = 'VOID'
    else:
        ticket['result'] = 'WON'
        ticket['paid_odds'] = round(_product(l['odds'] for l in legs if l['status'] == 'won'), 2)
    return ticket


def day_tickets(day_key, preds, odds, cache, now, previous=None):
    """Both tickets for one day. A ticket whose first leg has kicked off is
    frozen: taken from the SQLite cache (or, if the cache was lost, from the
    last published file) instead of being rebuilt."""
    by_id = {p['id']: p for p in preds}
    out = {}
    for kind in ('top3', 'longshot'):
        key = f'ticket:{kind}:{day_key}'
        hit = cache.get(key) if cache is not None else None
        stored = hit[1] if hit else ((previous or {}).get('tickets') or {}).get(kind)
        first = (stored or {}).get('first_kickoff')
        if stored and stored.get('legs') and first is not None and first <= now:
            ticket = json.loads(json.dumps(stored))
            ticket['frozen'] = True
            if cache is not None and not hit:
                cache.put(key, 'tickets', stored, now)
        else:
            ticket = (generate_daily_top3(preds, now, day_key) if kind == 'top3'
                      else generate_high_odds_accumulator(preds, odds, now, day_key))
            ticket['frozen'] = False
            if cache is not None:
                cache.put(key, 'tickets', ticket, now)
        out[kind] = settle_ticket(ticket, by_id)
    return out


# --------------------------------------------------- archive and record

def load_archive(path=ARCHIVE):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_archive(archive, path=ARCHIVE):
    """data/tickets.json (committed by full runs): every day's two tickets."""
    data = {k: archive[k] for k in sorted(archive)[-KEEP_DAYS:]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, separators=(',', ':'), ensure_ascii=False, sort_keys=True)
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == text:
                return
    except OSError:
        pass
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def record(archive):
    """Results of the settled tickets, for the app's Track screen. Top 3 is
    counted at fair odds (bookmakers pay less); the longshot at the listed
    real prices."""
    out = {}
    for kind in ('top3', 'longshot'):
        won = lost = void = pending = skipped = 0
        units = chance = 0.0
        recent = []
        for day in sorted(archive):
            t = (archive[day] or {}).get(kind)
            if not t:
                continue
            res = t.get('result')
            if t.get('status') == 'SKIPPED_LOW_EDGE' or not t.get('legs'):
                skipped += 1
                continue
            if res == 'WON':
                won += 1
                units += (t.get('paid_odds') or t.get('odds') or 1) - 1
                chance += t.get('chance') or 0
            elif res == 'LOST':
                lost += 1
                units -= 1
                chance += t.get('chance') or 0
            elif res == 'VOID':
                void += 1
            else:
                pending += 1
            recent.append({'day': day, 'result': res, 'odds': t.get('odds'), 'legs': len(t['legs']),
                           'won_legs': sum(1 for l in t['legs'] if l.get('status') == 'won')})
        settled = won + lost
        out[kind] = {
            'won': won, 'lost': lost, 'void': void, 'pending': pending, 'skipped': skipped,
            'units': round(units, 2), 'expected_wins': round(chance, 2) if settled else 0,
            'odds_basis': 'fair odds' if kind == 'top3' else 'listed prices',
            'recent': recent[-14:][::-1],
        }
    return out
