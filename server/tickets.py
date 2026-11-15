"""GOAL20's two automatic daily tickets, built from the predictionEngine's
probabilities right after the predictions.

generate_daily_top3()  "Daily Top 3 Statistical Picks" (graded, server 1.11)
    Only the safest markets (1X, X2, Over 1.5, draw no bet) with a fair price
    of 1.20 - 1.55, one leg per match. Confidence is the model probability,
    cut by 5 points for matches with higher human variance (cup ties,
    international friendlies, youth/reserve sides, fixture congestion,
    cold-start ratings). Every leg gets a grade:
        A  78%+ confidence, stable league match, full data
        B  72%+ confidence (a would-be A with higher variance lands here)
        C  65%+ confidence, or a thin-data (cold-start) rating
    The card is filled A first, then B, then C, up to 3 legs, never below
    65%. The card's grade is its weakest leg. Fewer than 3 legs only when
    not even 3 matches reach 65% ("NO TOP TICKET TODAY" when none does).
    Each grade keeps its own record; the headline record is Grade A only.
    Statistical model edge only - football is played by humans.

generate_high_odds_accumulator(target_odds=1000)  "1000+ Odds Value Longshot"
    Higher-yield markets (1X2, both teams score & Over 2.5, home / away team
    Over 1.5) with a REAL bookmaker price first, strictly by expected value
    (model probability x price - 1), then estimated legs at the model's fair
    odds if needed, until the combined price reaches the target (max 14).

Both tickets are previews until the morning update of their own day: the
first full run from 08:00 Cameroon time (after football-charts.com's
results) LOCKS them, or any run from 11:00, or their first kickoff, whichever
comes first (server 1.12). A locked ticket never changes again. A leg whose
match still has no result 48 hours after kickoff (postponed, moved or never
reported) is void. GOAL20 never places bets.
"""
import json
import os
from datetime import datetime

from . import config
from .prediction_engine import settle_leg

TOP3 = {
    'title': 'Daily Top 3 Statistical Picks',
    'subtitle': 'Statistical model edge only — football is played by humans; never guaranteed. Stake responsibly.',
    'markets': ['1X', 'X2', 'O1.5', 'DNB1', 'DNB2'],
    'legs': 3,
    'grades': {'A': 0.78, 'B': 0.72, 'C': 0.65},   # lowest confidence of each grade
    'floor': 0.65,                                  # never below this
    'fair_band': (1.20, 1.55),
    'min_evidence': 8.0,   # time-weighted league matches per team
    'volatility_penalty': 0.05,
    'empty_note': ('NO TOP TICKET TODAY: Fixtures carry too much human/match volatility '
                   '— no match reaches the 65% Grade C floor.'),
}
GRADES = 'ABC'
GRADE_TEXT = {
    'A': 'Grade A: 78%+ confidence, stable league match, full data',
    'B': 'Grade B: 72%+ confidence, or 78%+ with higher human variance',
    'C': 'Grade C: 65%+ confidence, or a thin-data (cold-start) rating',
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
LOCK_HOUR_FULL = 8      # the first full run from 08:00 Cameroon time on the card's day locks it
LOCK_HOUR_ANY = 11      # any run from 11:00 locks it (if the morning full run was late or failed)
NO_RESULT_HOURS = 48    # a leg with no result this long after kickoff is void
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


def volatility_reasons(pred):
    """Why a match carries higher human variance (empty = stable)."""
    reasons = list((pred.get('volatility') or {}).get('reasons') or [])
    if is_cold(pred) and 'cold-start rating' not in reasons:
        reasons.append('cold-start rating')
    return reasons


def leg_grade(confidence, flagged=False, cold=False):
    """A, B, C or None (below the 65% floor). Grade A needs a stable match
    with full data; a match with higher human variance is at best B, a
    cold-start rating at best C."""
    g = TOP3['grades']
    if confidence is None or confidence < g['C'] - 1e-9:
        return None
    if not flagged and not cold and confidence >= g['A'] - 1e-9:
        return 'A'
    if not cold and confidence >= g['B'] - 1e-9:
        return 'B'
    return 'C'


def grade_of(leg):
    """A leg's grade; older legs (before server 1.11) are graded from what
    they stored, with the same rule."""
    if leg.get('grade') in ('A', 'B', 'C'):
        return leg['grade']
    conf = leg.get('confidence')
    if conf is None:
        conf = leg.get('probability')
    return leg_grade(conf, bool(leg.get('volatility')), bool(leg.get('cold'))) or 'C'


def ticket_grade(ticket):
    """A card's grade = its weakest leg (None without legs)."""
    legs = (ticket or {}).get('legs') or []
    if not legs:
        return None
    if ticket.get('grade') in ('A', 'B', 'C'):
        return ticket['grade']
    return max((grade_of(l) for l in legs), key=GRADES.index)


def _leg(pred, market, p, odds, **extra):
    leg = {
        'id': pred['id'], 'ts': pred['ts'], 'home': pred.get('home', ''), 'away': pred.get('away', ''),
        'market': market, 'label': label(market, pred.get('home', ''), pred.get('away', '')),
        'probability': round(p, 4), 'fair_odds': round(1 / p, 2), 'odds': round(odds, 2),
        'lose': round(lose_chance(pred, market) or 0.0, 4), 'status': 'pending',
        'volatility': volatility_reasons(pred),
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
    """Daily Top 3 Statistical Picks for one day's predictions: Grade A legs
    first, then B, then C, never below 65%, one leg per match."""
    lo, hi = TOP3['fair_band']
    ranked = []
    for pred in preds:
        if not _open(pred, now, TOP3['min_evidence'], allow_cold=True):
            continue
        reasons = volatility_reasons(pred)
        cold = is_cold(pred)
        penalty = TOP3['volatility_penalty'] if reasons else 0.0
        best = None
        for m in TOP3['markets']:
            p = probability(pred, m)
            if p is None or p <= 0 or not lo - 1e-9 <= 1 / p <= hi + 1e-9:
                continue
            conf = p - penalty
            if conf < TOP3['floor'] - 1e-9:
                continue
            if best is None or conf > best[0]:
                best = (conf, m, p)
        if best:
            conf, m, p = best
            grade = leg_grade(conf, bool(reasons), cold)
            ranked.append((GRADES.index(grade), -conf, pred['ts'], pred['id'],
                           _leg(pred, m, p, 1 / p, confidence=round(conf, 4), in_band=True,
                                cold=cold, grade=grade)))
    ranked.sort(key=lambda r: r[:4])
    legs = [r[4] for r in ranked[:TOP3['legs']]]
    legs.sort(key=lambda l: (GRADES.index(l['grade']), -l['confidence'], l['ts'], l['id']))
    g = TOP3['grades']
    ticket = {
        'kind': 'top3', 'title': TOP3['title'], 'subtitle': TOP3['subtitle'], 'day': day, 'badge': None,
        'built': int(now), 'odds_kind': 'fair', 'legs': legs,
        'grade': ticket_grade({'legs': legs}),
        'grades': {k: sum(1 for l in legs if l['grade'] == k) for k in GRADES},
        'rules': (f"1X, X2, Over 1.5 or draw no bet, fair odds {lo:.2f}-{hi:.2f}, one leg per match; "
                  f"Grade A first ({g['A']:.0%}+ confidence, stable league match, full data), then B "
                  f"({g['B']:.0%}+, or {g['A']:.0%}+ with higher human variance), then C ({g['C']:.0%}+, "
                  f"or thin data); never below {TOP3['floor']:.0%}; confidence = model probability, "
                  f"{TOP3['volatility_penalty'] * 100:.0f} points off for higher human variance; "
                  f"the card's grade is its weakest leg"),
    }
    if not legs:
        ticket['status'] = 'SKIPPED_LOW_EDGE'
        ticket['note'] = TOP3['empty_note']
        return _totals(ticket)
    grade = ticket['grade']
    ticket['status'] = 'READY' if len(legs) == TOP3['legs'] else 'PARTIAL'
    if ticket['status'] == 'READY':
        ticket['note'] = {
            'A': 'Grade A card: all 3 legs at 78%+ confidence on stable, full-data matches.',
            'B': 'Grade B card: its weakest leg is Grade B (72-78%, or 78%+ with higher human variance).',
            'C': 'Grade C card: its weakest leg is Grade C (65-72%, or thin data). Expect it to lose more often.',
        }[grade]
    else:
        ticket['note'] = (f"Grade {grade} card with {len(legs)} leg{'s' if len(legs) > 1 else ''}: only "
                          f"{len(legs)} match{'es' if len(legs) > 1 else ''} reach the {TOP3['floor']:.0%} floor "
                          f"today. GOAL20 never goes below it.")
    shaky = sum(1 for l in legs if l['volatility'])
    if shaky:
        ticket['note'] += (f' {shaky} leg(s) carry higher human variance: confidence cut by '
                           f"{TOP3['volatility_penalty'] * 100:.0f} points, never above Grade B.")
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

def settle_ticket(ticket, preds_by_id, now=None):
    """Leg statuses from the results, then the ticket's result. With [now],
    a leg still without a result 48 hours after kickoff is void."""
    for leg in ticket.get('legs') or []:
        p = preds_by_id.get(leg['id'])
        res = (p or {}).get('result')
        leg.pop('void_reason', None)
        if res and len(res) == 2:
            won = settle_leg(leg['market'], res[0], res[1])
            leg['status'] = 'void' if won is None else ('won' if won else 'lost')
            leg['score'] = res
        elif (p or {}).get('st') in VOID_STATUSES:
            leg['status'] = 'void'
        elif now is not None and leg.get('ts') and now - leg['ts'] > NO_RESULT_HOURS * 3600:
            leg['status'] = 'void'
            leg['void_reason'] = f'no result {NO_RESULT_HOURS} h after kickoff (postponed, moved or never reported)'
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


def lock_due(day_key, now, full):
    """True when a card of [day_key] must lock at a run at [now]: a past day,
    or its own day after the morning update (the first full run from 08:00
    Cameroon time, or any run from 11:00)."""
    local = datetime.fromtimestamp(now, config.CAMEROON)
    today = config.day_key(local.date())
    if day_key < today:
        return True
    if day_key > today:
        return False
    return (full and local.hour >= LOCK_HOUR_FULL) or local.hour >= LOCK_HOUR_ANY


def lock_note(ticket, now):
    """One line for the app: preview or locked."""
    if ticket.get('locked'):
        at = ticket.get('locked_at')
        when = datetime.fromtimestamp(at, config.CAMEROON).strftime('%H:%M') if at else ''
        why = 'at its first kickoff' if ticket.get('locked_by') == 'kickoff' else 'after the morning update'
        return f'Locked {when} {why}: this card will not change any more.'.replace('  ', ' ')
    today = config.day_key(datetime.fromtimestamp(now, config.CAMEROON).date())
    if (ticket.get('day') or today) > today:
        return ('Preview for tomorrow: the final card locks tomorrow morning after the update '
                '(about 08:30-09:30) or at its first kickoff. Do not bet the preview.')
    return ('Preview: the final card locks after the morning update (about 08:30-09:30) '
            'or at its first kickoff, whichever comes first. Do not bet the preview.')


def day_tickets(day_key, preds, odds, cache, now, previous=None, full=True):
    """Both tickets for one day. Until the lock they are rebuilt at every
    run (a preview); a locked ticket is taken from the SQLite cache (or, if
    the cache was lost, from the last published file) and never rebuilt.
    It locks after the morning update of its own day or at its first
    kickoff, whichever comes first."""
    by_id = {p['id']: p for p in preds}
    due = lock_due(day_key, now, full)
    out = {}
    for kind in ('top3', 'longshot'):
        key = f'ticket:{kind}:{day_key}'
        hit = cache.get(key) if cache is not None else None
        stored = hit[1] if hit else ((previous or {}).get('tickets') or {}).get(kind)
        first = (stored or {}).get('first_kickoff')
        kicked = bool(stored and stored.get('legs') and first is not None and first <= now)
        if stored and stored.get('legs') and (kicked or stored.get('locked')):
            ticket = json.loads(json.dumps(stored))
            ticket['frozen'] = kicked
            if not ticket.get('locked'):
                ticket.update(locked=True, locked_at=first, locked_by='kickoff')
            if cache is not None and (not hit or hit[1].get('locked') != ticket['locked']):
                cache.put(key, 'tickets', ticket, now)
        else:
            ticket = (generate_daily_top3(preds, now, day_key) if kind == 'top3'
                      else generate_high_odds_accumulator(preds, odds, now, day_key))
            ticket['frozen'] = False
            ticket['locked'] = bool(due and ticket.get('legs'))
            if ticket['locked']:
                ticket.update(locked_at=now, locked_by='morning')
            if cache is not None:
                cache.put(key, 'tickets', ticket, now)
        ticket['lock_note'] = lock_note(ticket, now)
        out[kind] = settle_ticket(ticket, by_id, now)
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


def _tally(entries, odds_basis):
    """won / lost / units / expected wins for (day, ticket) entries."""
    won = lost = void = pending = 0
    units = chance = 0.0
    recent = []
    for day, t in entries:
        res = t.get('result')
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
        item = {'day': day, 'result': res, 'odds': t.get('odds'), 'legs': len(t['legs']),
                'won_legs': sum(1 for l in t['legs'] if l.get('status') == 'won')}
        if t.get('kind', 'top3') == 'top3':
            item['grade'] = ticket_grade(t)
        recent.append(item)
    settled = won + lost
    return {
        'won': won, 'lost': lost, 'void': void, 'pending': pending,
        'units': round(units, 2), 'expected_wins': round(chance, 2) if settled else 0,
        'odds_basis': odds_basis, 'recent': recent[-14:][::-1],
    }


def record(archive):
    """Results of the settled tickets, for the app's Track screen.

    top3         the Grade A record only (the headline; older apps show it)
    top3_grades  one separate record per card grade: A, B and C are never
                 added together
    top3_legs    every Daily Top 3 leg by its own grade: won, lost and the
                 number the model expected
    longshot     the 1000+ longshot
    Top 3 is counted at fair odds (bookmakers pay less); the longshot at the
    listed prices."""
    by_grade = {g: [] for g in GRADES}
    legs = {g: {'won': 0, 'lost': 0, 'void': 0, 'expected': 0.0} for g in GRADES}
    skipped = 0
    longshot, ls_skipped = [], 0
    for day in sorted(archive):
        t = (archive[day] or {}).get('top3')
        if t:
            if t.get('status') == 'SKIPPED_LOW_EDGE' or not t.get('legs'):
                skipped += 1
            else:
                by_grade[ticket_grade(t)].append((day, t))
                for l in t['legs']:
                    row = legs[grade_of(l)]
                    if l.get('status') in ('won', 'lost'):
                        row[l['status']] += 1
                        row['expected'] += 1 - (l.get('lose') or 0)
                    elif l.get('status') == 'void':
                        row['void'] += 1
        t = (archive[day] or {}).get('longshot')
        if t:
            if t.get('status') == 'SKIPPED_LOW_EDGE' or not t.get('legs'):
                ls_skipped += 1
            else:
                longshot.append((day, t))
    grades = {}
    for g in GRADES:
        grades[g] = dict(_tally(by_grade[g], 'fair odds'), grade=g, text=GRADE_TEXT[g])
    for row in legs.values():
        row['expected'] = round(row['expected'], 2)
    return {
        'top3': dict(grades['A'], headline=True, skipped=skipped),
        'top3_grades': grades,
        'top3_legs': legs,
        'top3_skipped': skipped,
        'longshot': dict(_tally(longshot, 'listed prices'), skipped=ls_skipped),
    }
