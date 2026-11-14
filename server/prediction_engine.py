"""predictionEngine: GOAL20's match model, written out in code (no AI guessing).

1. Dixon-Coles ratings per competition (Dixon & Coles 1997): every team gets
   an attack strength and a defence weakness, fitted by weighted iterative
   proportional fitting on up to 450 days of league results. Recent matches
   count more (half-life 180 days). Where the result file has shots on
   target, half of each "goal" is real goals and half is shots-on-target
   expected goals, so lucky scorelines count only half.
2. Venue form: the home side's last 15 HOME league games and the away side's
   last 15 AWAY league games (newest weight 1, then 0.9, 0.81 ...), compared
   with what the ratings expected, gives separate home/away attack strength
   and defence weakness. It is shrunk toward the full-season ratings by 20
   pseudo-games: in GOAL20's walk-forward test a pure last-15 home/away
   model was LESS accurate than the full ratings, so form only adjusts them.
3. Exact scores 0-0 .. 10-10: bivariate Poisson (a shared component lambda3
   for matches where both sides score together) with the Dixon-Coles
   low-score correction rho. Every market is a sum over that matrix.
4. filter_high_confidence_picks(): the single safest market at 70.0% or
   more, otherwise the match is SKIPPED_LOW_EDGE (no bet).
5. explain(): three plain-language bullets for every match.

Two fits per competition: a light pull toward the league average (prior 6)
for 1 / X / 2 / double chance, and a strong pull (prior 40) for goals
markets, where single-team totals proved to be mostly noise.
"""
import math
from datetime import datetime, timezone

PARAMS = {
    'half_life_days': 180,
    'train_days': 450,
    'prior_result': 6,
    'prior_goals': 40,
    'shots_weight': 0.5,
    'min_league_matches': 40,
    'min_team_weight': 3.0,
    'iterations': 40,
    'max_goals': 10,
    'venue_last': 15,
    'venue_decay': 0.9,
    'venue_prior': 20,
    'threshold': 0.70,
}

# The 12 markets, in tie-break order.
MARKETS = ['1', 'X', '2', '1X', 'X2', '12', 'O1.5', 'U1.5', 'O2.5', 'U2.5', 'BTTS_YES', 'BTTS_NO']
LABELS = {
    '1': 'Home win', 'X': 'Draw', '2': 'Away win',
    '1X': 'Home or draw (1X)', 'X2': 'Away or draw (X2)', '12': 'Home or away (12)',
    'O1.5': 'Over 1.5 goals', 'U1.5': 'Under 1.5 goals',
    'O2.5': 'Over 2.5 goals', 'U2.5': 'Under 2.5 goals',
    'BTTS_YES': 'Both teams to score: Yes', 'BTTS_NO': 'Both teams to score: No',
}
STATUS_PICK = 'PICK'
STATUS_SKIP = 'SKIPPED_LOW_EDGE'
FINISHED = ('FT', 'AET', 'PEN')
DAY = 86400
_FACT = [math.factorial(i) for i in range(26)]


# ------------------------------------------------------------- the ratings

def _has_shots(r):
    return len(r) >= 12 and r[10] is not None and r[11] is not None


def fit_league(rows, cutoff, prior, p=PARAMS):
    """Dixon-Coles ratings of one competition from results played before
    cutoff (unix seconds). rows: history rows [id, ts, league, season, home,
    away, hg, ag, hs, as, hst, ast, ...]. None when there are too few."""
    oldest = cutoff - p['train_days'] * DAY
    data = [r for r in rows if oldest <= r[1] < cutoff and r[6] is not None and r[7] is not None]
    if len(data) < p['min_league_matches']:
        return None
    goals = sot = with_shots = 0
    for r in data:
        if _has_shots(r):
            goals += r[6] + r[7]
            sot += r[10] + r[11]
            with_shots += 1
    conv = goals / sot if sot > 50 else 0.32
    xi = math.log(2) / p['half_life_days']
    sw_ = p['shots_weight']

    slot = {}
    hi, ai, gh, ga, yh, ya, w = [], [], [], [], [], [], []
    for r in data:
        hi.append(slot.setdefault(r[4], len(slot)))
        ai.append(slot.setdefault(r[5], len(slot)))
        gh.append(r[6])
        ga.append(r[7])
        w.append(math.exp(-xi * (cutoff - r[1]) / DAY))
        if _has_shots(r) and sw_ > 0:
            yh.append((1 - sw_) * r[6] + sw_ * r[10] * conv)
            ya.append((1 - sw_) * r[7] + sw_ * r[11] * conv)
        else:
            yh.append(float(r[6]))
            ya.append(float(r[7]))
    n, t = len(data), len(slot)
    att, de, wt = [1.0] * t, [1.0] * t, [0.0] * t
    for i in range(n):
        wt[hi[i]] += w[i]
        wt[ai[i]] += w[i]
    hl, al = 1.4, 1.1
    rng = range(n)
    for _ in range(p['iterations']):
        num, den = [0.0] * t, [0.0] * t
        for i in rng:
            h, a, wi = hi[i], ai[i], w[i]
            num[h] += wi * yh[i]
            den[h] += wi * de[a] * hl
            num[a] += wi * ya[i]
            den[a] += wi * de[h] * al
        att = [(num[j] + prior) / (den[j] + prior) for j in range(t)]
        num, den = [0.0] * t, [0.0] * t
        for i in rng:
            h, a, wi = hi[i], ai[i], w[i]
            num[a] += wi * yh[i]
            den[a] += wi * att[h] * hl
            num[h] += wi * ya[i]
            den[h] += wi * att[a] * al
        de = [(num[j] + prior) / (den[j] + prior) for j in range(t)]
        gm = math.exp(sum(math.log(x) for x in att) / t)
        att = [x / gm for x in att]
        nh = dh = na = da = 0.0
        for i in rng:
            h, a, wi = hi[i], ai[i], w[i]
            nh += wi * yh[i]
            dh += wi * att[h] * de[a]
            na += wi * ya[i]
            da += wi * att[a] * de[h]
        if dh > 0:
            hl = nh / dh
        if da > 0:
            al = na / da

    lam = [(att[hi[i]] * de[ai[i]] * hl, att[ai[i]] * de[hi[i]] * al) for i in rng]
    # Low-score correction rho: grid search on the real goals.
    rho, best = 0.0, -math.inf
    for step in range(36):
        r_ = -0.25 + step * 0.01
        ll, ok = 0.0, True
        for i in rng:
            tau = _tau(gh[i], ga[i], lam[i][0], lam[i][1], r_)
            if tau <= 0:
                ok = False
                break
            ll += w[i] * math.log(tau)
        if ok and ll > best:
            best, rho = ll, r_
    # Bivariate Poisson shared component from the residual covariance.
    cov = sum(w[i] * (gh[i] - lam[i][0]) * (ga[i] - lam[i][1]) for i in rng)
    tw = sum(w)
    l3 = min(0.25, max(0.0, cov / tw)) if tw > 0 else 0.0

    teams = [0] * t
    for team, j in slot.items():
        teams[j] = team
    return {
        'teams': teams, 'att': [round(x, 6) for x in att], 'def': [round(x, 6) for x in de],
        'wt': [round(x, 4) for x in wt], 'hl': round(hl, 6), 'al': round(al, 6),
        'rho': round(rho, 4), 'l3': round(l3, 6), 'conv': round(conv, 6),
        'n': n, 'shots': with_shots, 'prior': prior, 'cutoff': cutoff,
    }


def _tau(x, y, lh, la, rho):
    if x == 0 and y == 0:
        return 1 - lh * la * rho
    if x == 0 and y == 1:
        return 1 + lh * rho
    if x == 1 and y == 0:
        return 1 + la * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


class Ratings:
    """A fit with a team lookup."""

    def __init__(self, fit):
        self.f = fit
        self.slot = {team: j for j, team in enumerate(fit['teams'])}

    def covers(self, team):
        j = self.slot.get(team)
        return j is not None and self.f['wt'][j] >= PARAMS['min_team_weight']

    def att(self, team):
        return self.f['att'][self.slot[team]]

    def de(self, team):
        return self.f['def'][self.slot[team]]

    def weight(self, team):
        j = self.slot.get(team)
        return 0.0 if j is None else self.f['wt'][j]


# ------------------------------------------------------------ score matrix

def score_matrix(lh, la, rho=0.0, l3=0.0, n_max=PARAMS['max_goals']):
    """P(home goals = x, away goals = y) for x, y in 0..n_max, summing to 1:
    bivariate Poisson with shared component l3, Dixon-Coles correction rho."""
    f = _FACT
    l3 = max(0.0, min(l3, min(lh, la) * 0.5))
    a, b = lh - l3, la - l3
    m = [[0.0] * (n_max + 1) for _ in range(n_max + 1)]
    total = 0.0
    for x in range(n_max + 1):
        for y in range(n_max + 1):
            if l3 > 0:
                s = sum((f[x] / (f[k] * f[x - k])) * (f[y] / (f[k] * f[y - k])) * f[k] * (l3 / (a * b)) ** k
                        for k in range(min(x, y) + 1))
                v = math.exp(-(a + b + l3)) * a ** x / f[x] * b ** y / f[y] * s
            else:
                v = math.exp(-lh) * lh ** x / f[x] * math.exp(-la) * la ** y / f[y]
            v *= max(0.0, _tau(x, y, lh, la, rho))
            m[x][y] = v
            total += v
    if total > 0:
        m = [[v / total for v in row] for row in m]
    return m


def _sum(m, test):
    return sum(m[x][y] for x in range(len(m)) for y in range(len(m)) if test(x, y))


def market_probabilities(result_matrix, goals_matrix):
    """The 12 markets. 1/X/2 and double chance from the result matrix,
    goals markets from the goals matrix."""
    h = _sum(result_matrix, lambda x, y: x > y)
    d = _sum(result_matrix, lambda x, y: x == y)
    a = _sum(result_matrix, lambda x, y: x < y)
    o15 = _sum(goals_matrix, lambda x, y: x + y >= 2)
    o25 = _sum(goals_matrix, lambda x, y: x + y >= 3)
    bt = _sum(goals_matrix, lambda x, y: x > 0 and y > 0)
    return {
        '1': h, 'X': d, '2': a, '1X': h + d, 'X2': a + d, '12': h + a,
        'O1.5': o15, 'U1.5': 1 - o15, 'O2.5': o25, 'U2.5': 1 - o25,
        'BTTS_YES': bt, 'BTTS_NO': 1 - bt,
    }


# Extra markets used by the ticket builders (server 1.8): draw no bet
# (chance of winning when it is not a draw), both teams score AND 3+ goals,
# and each team scoring 2+.
EXTRA_MARKETS = ['DNB1', 'DNB2', 'BTTS_O2.5', 'H_O1.5', 'A_O1.5']


def extra_probabilities(result_matrix, goals_matrix):
    h = _sum(result_matrix, lambda x, y: x > y)
    a = _sum(result_matrix, lambda x, y: x < y)
    decided = h + a
    return {
        'DNB1': h / decided if decided > 0 else 0.0,
        'DNB2': a / decided if decided > 0 else 0.0,
        'BTTS_O2.5': _sum(goals_matrix, lambda x, y: x > 0 and y > 0 and x + y >= 3),
        'H_O1.5': _sum(goals_matrix, lambda x, y: x >= 2),
        'A_O1.5': _sum(goals_matrix, lambda x, y: y >= 2),
    }


def settle_leg(market, hg, ag):
    """True = won, False = lost, None = void (draw no bet on a draw)."""
    if market in ('DNB1', 'DNB2') and hg == ag:
        return None
    return {
        'DNB1': hg > ag, 'DNB2': ag > hg,
        'BTTS_O2.5': hg > 0 and ag > 0 and hg + ag >= 3,
        'H_O1.5': hg >= 2, 'A_O1.5': ag >= 2,
    }.get(market, None) if market in EXTRA_MARKETS else settle(market, hg, ag)


def settle(market, hg, ag):
    """True when the market won with this 90-minute score."""
    return {
        '1': hg > ag, 'X': hg == ag, '2': hg < ag, '1X': hg >= ag, 'X2': hg <= ag, '12': hg != ag,
        'O1.5': hg + ag >= 2, 'U1.5': hg + ag < 2, 'O2.5': hg + ag >= 3, 'U2.5': hg + ag < 3,
        'BTTS_YES': hg > 0 and ag > 0, 'BTTS_NO': not (hg > 0 and ag > 0),
    }[market]


# ------------------------------------------------------------------ filter

def filter_high_confidence_picks(probs, threshold=PARAMS['threshold']):
    """The single safest market (highest probability) if it reaches the
    threshold (70.0%): (STATUS_PICK, market, probability). Otherwise
    (STATUS_SKIP, best market, its probability): no bet."""
    best = max(MARKETS, key=lambda k: (probs[k], -MARKETS.index(k)))
    p = probs[best]
    if p >= threshold - 1e-12:
        return STATUS_PICK, best, p
    return STATUS_SKIP, best, p


filterHighConfidencePicks = filter_high_confidence_picks  # the name used in the spec


# ------------------------------------------------------------- venue form

def venue_form(team_rows, team, cutoff, side, conv, p=PARAMS):
    """The team's last 15 home (side='home') or away league games before
    cutoff, newest first, weights 1, 0.9, 0.81 ... Goals blended with shots
    on target like the ratings. -> {gf, ga, sw, n} (weighted sums)."""
    gf = ga = sw = 0.0
    w, n = 1.0, 0
    sw_ = p['shots_weight']
    for r in reversed(team_rows):
        if n >= p['venue_last']:
            break
        if r[1] >= cutoff:
            continue
        if (side == 'home' and r[4] != team) or (side == 'away' and r[5] != team):
            continue
        f, a = (r[6], r[7]) if side == 'home' else (r[7], r[6])
        if _has_shots(r) and sw_ > 0:
            sf, sa = (r[10], r[11]) if side == 'home' else (r[11], r[10])
            f = (1 - sw_) * f + sw_ * sf * conv
            a = (1 - sw_) * a + sw_ * sa * conv
        gf += w * f
        ga += w * a
        sw += w
        w *= p['venue_decay']
        n += 1
    return {'gf': gf, 'ga': ga, 'sw': sw, 'n': n}


def _shrunk(total, weight, expected, k):
    """Observed / expected, pulled toward 1 by k pseudo-games."""
    if expected <= 0:
        return 1.0
    return (total + k * expected) / ((weight + k) * expected)


# ----------------------------------------------------------------- explain

def _pct(x):
    return f'{100 * x:.0f}%'


def explain(home, away, pred):
    """Three short bullets in plain language."""
    form, probs, xg = pred['form'], pred['probs'], pred['xg']
    hf, af = form['home'], form['away']
    b1 = (f"{home} score {hf['for']:.2f} and concede {hf['against']:.2f} per home game "
          f"(last {hf['n']} at home); {away} score {af['for']:.2f} and concede {af['against']:.2f} "
          f"per away game (last {af['n']} away). Recent games count most.")
    x, y, ps = pred['score']
    b2 = (f"Model (Dixon-Coles + bivariate Poisson, {pred['model']['n']} league results) expects "
          f"{home} {xg[0]:.2f} - {xg[1]:.2f} {away}; most likely score {x}-{y} ({_pct(ps)}).")
    m = (pred.get('pick') or {}).get('market') or pred['best']['market']
    p = probs[m]
    tot = xg[0] + xg[1]
    lines = {
        '1': f"{home} win {_pct(p)} (draw {_pct(probs['X'])}, {away} {_pct(probs['2'])}).",
        '2': f"{away} win {_pct(p)} (draw {_pct(probs['X'])}, {home} {_pct(probs['1'])}).",
        'X': f"Draw {_pct(p)}: the sides are rated close ({home} {_pct(probs['1'])}, {away} {_pct(probs['2'])}).",
        '1X': f"{home} avoid defeat {_pct(p)}: win {_pct(probs['1'])} + draw {_pct(probs['X'])}; {away} win only {_pct(probs['2'])}.",
        'X2': f"{away} avoid defeat {_pct(p)}: win {_pct(probs['2'])} + draw {_pct(probs['X'])}; {home} win only {_pct(probs['1'])}.",
        '12': f"A winner {_pct(p)}: the draw is only {_pct(probs['X'])}.",
        'O1.5': f"2+ goals {_pct(p)}: only 0-0, 1-0 and 0-1 lose it; expected total {tot:.2f} goals.",
        'U1.5': f"Under 2 goals {_pct(p)}: expected total only {tot:.2f} goals.",
        'O2.5': f"3+ goals {_pct(p)}: expected total {tot:.2f} goals.",
        'U2.5': f"At most 2 goals {_pct(p)}: expected total {tot:.2f} goals.",
        'BTTS_YES': f"Both score {_pct(p)}: {home} score in {_pct(pred['scores'][0])} of outcomes, {away} in {_pct(pred['scores'][1])}.",
        'BTTS_NO': f"At least one side blank {_pct(p)}: {home} score in only {_pct(pred['scores'][0])}, {away} in {_pct(pred['scores'][1])}.",
    }
    if pred['status'] == STATUS_PICK:
        b3 = f"{LABELS[m]}: {lines[m]} Fair odds {1 / p:.2f}: worth it only at a higher price."
    else:
        b3 = (f"No bet: the safest market, {LABELS[m]}, is only {_pct(p)}, "
              f"below the {_pct(PARAMS['threshold'])} line.")
    return [b1, b2, b3]


def _skip(fx, reason, why):
    return {
        'id': fx['id'], 'ts': fx['ts'], 'lg': fx['lg']['id'],
        'h': fx['h']['id'], 'a': fx['a']['id'], 'home': fx['h'].get('name') or '',
        'away': fx['a'].get('name') or '', 'status': STATUS_SKIP, 'reason': reason,
        'pick': None, 'why': why,
    }


# ------------------------------------------------------------------ engine

class PredictionEngine:
    """Predicts the matches of the published days from the stored results.
    Ratings are cached in SQLite (one fit per competition and day), and a
    prediction is frozen once its match kicks off."""

    def __init__(self, history, cache=None, log=print, now=None):
        self.cache = cache
        self.log = log
        self.now = now if now is not None else int(datetime.now(timezone.utc).timestamp())
        self.leagues = {l['id']: l for l in history.get('l', [])}
        self.by_league = {}
        for r in history.get('m', []):
            if len(r) >= 8 and r[6] is not None and r[7] is not None:
                self.by_league.setdefault(r[2], []).append(r)
        for rows in self.by_league.values():
            rows.sort(key=lambda r: r[1])
        self._fits = {}
        self._venue = {}
        self.fitted = self.reused = 0

    # ratings, cached in memory and in SQLite
    def _fit(self, league, cutoff, prior):
        key = (league, cutoff, prior)
        if key in self._fits:
            return self._fits[key]
        rows = self.by_league.get(league) or []
        oldest = cutoff - PARAMS['train_days'] * DAY
        count = sum(1 for r in rows if oldest <= r[1] < cutoff)
        fit = None
        tag = league * 1000 + prior  # one cache row per competition and prior
        if self.cache is not None and count >= PARAMS['min_league_matches']:
            fit = self.cache.get_fit(tag, cutoff, count)
            if fit is not None:
                self.reused += 1
        if fit is None and count >= PARAMS['min_league_matches']:
            fit = fit_league(rows, cutoff, prior)
            self.fitted += 1
            if fit is not None and self.cache is not None:
                self.cache.put_fit(tag, cutoff, count, fit)
        self._fits[key] = fit
        return fit

    def _team_rows(self, league, team):
        key = (league, team)
        if key not in self._venue:
            self._venue[key] = [r for r in self.by_league.get(league) or [] if r[4] == team or r[5] == team]
        return self._venue[key]

    def cutoff_for(self, ts):
        """Results up to the start of today (UTC) for coming matches, up to
        the start of the match day for past ones: a match is never rated
        with its own result."""
        return min(self.now - self.now % DAY, ts - ts % DAY)

    def predict_match(self, fx):
        home, away = fx['h'].get('name') or 'Home', fx['a'].get('name') or 'Away'
        league = self.leagues.get(fx['lg']['id']) or fx['lg']
        if 'friendl' in (league.get('name') or fx['lg'].get('name') or '').lower():
            return _skip(fx, 'friendly', [
                'Friendly match: line-ups and effort are unpredictable.',
                'GOAL20 does not rate friendlies.',
                'Status SKIPPED_LOW_EDGE: no bet.'])
        cutoff = self.cutoff_for(fx['ts'])
        lg = fx['lg']['id']
        res = self._fit(lg, cutoff, PARAMS['prior_result'])
        gls = self._fit(lg, cutoff, PARAMS['prior_goals']) if res else None
        h, a = fx['h']['id'], fx['a']['id']
        if not res or not gls:
            n = len(self.by_league.get(lg) or [])
            return _skip(fx, 'not_enough_data', [
                f"{fx['lg'].get('name') or 'This competition'}: {n} stored results, "
                f"GOAL20 needs {PARAMS['min_league_matches']} recent ones to rate it.",
                'GOAL20 does not guess when the data is thin.',
                'Status SKIPPED_LOW_EDGE: no bet.'])
        R, G = Ratings(res), Ratings(gls)
        if not (R.covers(h) and R.covers(a)):
            thin = home if not R.covers(h) else away
            return _skip(fx, 'not_enough_data', [
                f'{thin} has too few recent games in this competition to be rated.',
                'GOAL20 does not guess when the data is thin.',
                'Status SKIPPED_LOW_EDGE: no bet.'])

        conv = res['conv']
        hv = venue_form(self._team_rows(lg, h), h, cutoff, 'home', conv)
        av = venue_form(self._team_rows(lg, a), a, cutoff, 'away', conv)
        k = PARAMS['venue_prior']
        # What the full ratings expect from each side at this venue against
        # an average opponent, and how the venue form compares with it.
        e_hf, e_ha = R.att(h) * res['hl'], R.de(h) * res['al']
        e_af, e_aa = R.att(a) * res['al'], R.de(a) * res['hl']
        vhA, vhD = _shrunk(hv['gf'], hv['sw'], e_hf, k), _shrunk(hv['ga'], hv['sw'], e_ha, k)
        vaA, vaD = _shrunk(av['gf'], av['sw'], e_af, k), _shrunk(av['ga'], av['sw'], e_aa, k)
        adj_h, adj_a = math.sqrt(vhA * vaD), math.sqrt(vaA * vhD)

        def lambdas(F, fit):
            lh = F.att(h) * F.de(a) * fit['hl'] * adj_h
            la = F.att(a) * F.de(h) * fit['al'] * adj_a
            return min(6.0, max(0.05, lh)), min(6.0, max(0.05, la))

        rlh, rla = lambdas(R, res)
        glh, gla = lambdas(G, gls)
        rm = score_matrix(rlh, rla, res['rho'], res['l3'])
        gm = score_matrix(glh, gla, gls['rho'], gls['l3'])
        probs = market_probabilities(rm, gm)
        status, market, p = filter_high_confidence_picks(probs)
        bx, by = max(((x, y) for x in range(len(rm)) for y in range(len(rm))), key=lambda c: rm[c[0]][c[1]])
        pred = {
            'id': fx['id'], 'ts': fx['ts'], 'lg': lg, 'h': h, 'a': a, 'home': home, 'away': away,
            'status': status, 'reason': 'ok' if status == STATUS_PICK else 'below_70',
            'pick': ({'market': market, 'label': LABELS[market], 'probability': round(p, 4),
                      'fair_odds': round(1 / p, 2)} if status == STATUS_PICK else None),
            'best': {'market': market, 'label': LABELS[market], 'probability': round(p, 4)},
            'probs': {m: round(v, 4) for m, v in probs.items()},
            'more': {m: round(v, 4) for m, v in extra_probabilities(rm, gm).items()},
            'xg': [round(rlh, 2), round(rla, 2)],
            'xg_goals': [round(glh, 2), round(gla, 2)],
            'score': [bx, by, round(rm[bx][by], 4)],
            'scores': [round(1 - sum(gm[0]), 4), round(1 - sum(row[0] for row in gm), 4)],
            # Attack strength / defence weakness at this venue (1.00 = league
            # average; defence above 1 = concedes more than average).
            'strength': {
                'home_attack_home': round(R.att(h) * vhA, 3), 'home_defence_home': round(R.de(h) * vhD, 3),
                'away_attack_away': round(R.att(a) * vaA, 3), 'away_defence_away': round(R.de(a) * vaD, 3),
            },
            'form': {
                'home': {'n': hv['n'], 'for': round(hv['gf'] / hv['sw'], 2) if hv['sw'] else 0.0,
                         'against': round(hv['ga'] / hv['sw'], 2) if hv['sw'] else 0.0},
                'away': {'n': av['n'], 'for': round(av['gf'] / av['sw'], 2) if av['sw'] else 0.0,
                         'against': round(av['ga'] / av['sw'], 2) if av['sw'] else 0.0},
            },
            'model': {'n': res['n'], 'rho': res['rho'], 'l3': round(res['l3'], 3), 'cutoff': cutoff,
                      'evidence': [round(R.weight(h), 1), round(R.weight(a), 1)]},
        }
        pred['why'] = explain(home, away, pred)
        return pred

    def predict_day(self, day_key, fixtures):
        """Predictions for one day's fixtures. Matches that already kicked
        off keep the prediction stored before kickoff (frozen)."""
        out = []
        for fx in sorted(fixtures, key=lambda f: (f['ts'], f['id'])):
            if not fx.get('h', {}).get('id') or not fx.get('a', {}).get('id'):
                continue
            started = fx['ts'] <= self.now
            pred = self.cache.frozen(day_key, fx['id']) if (self.cache is not None and started) else None
            if pred is None:
                pred = self.predict_match(fx)
                pred['computed'] = self.now
                if self.cache is not None and not started:
                    self.cache.put_prediction(day_key, pred, self.now)
            pred = dict(pred)
            pred['frozen'] = started
            pred['st'] = fx.get('st') or ''
            if fx.get('st') in FINISHED:
                hg = fx['fh'] if fx.get('fh') is not None else fx.get('hg')
                ag = fx['fa'] if fx.get('fa') is not None else fx.get('ag')
                if hg is not None and ag is not None:
                    pred['result'] = [hg, ag]
                    if pred.get('pick'):
                        pred['won'] = settle(pred['pick']['market'], hg, ag)
            out.append(pred)
        if self.cache is not None:
            self.cache.commit()
        picks = [p for p in out if p['status'] == STATUS_PICK]
        settled = [p for p in picks if 'won' in p]
        return {
            'day': day_key,
            'model': 'Dixon-Coles + bivariate Poisson with venue form',
            'threshold': PARAMS['threshold'],
            'summary': {'matches': len(out), 'picks': len(picks), 'skipped': len(out) - len(picks),
                        'won': sum(1 for p in settled if p['won']), 'lost': sum(1 for p in settled if not p['won'])},
            'p': out,
        }
