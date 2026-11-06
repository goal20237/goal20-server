"""Light version of the GOAL20 goal model, used only to decide which
matches deserve the paid-for checks (head-to-head, injuries, odds).
The app's engine makes the actual ratings from the published data."""
import math


def _tail(mu, goals):
    below, term = 0.0, math.exp(-mu)
    for g in range(goals):
        if g > 0:
            term *= mu / g
        below += term
    return max(0.0, min(1.0, 1 - below))


def team_index(rows):
    """rows: [id, ts, league, season, home, away, hg, ag] -> {team: [rows newest first]}"""
    idx = {}
    for r in rows:
        idx.setdefault(r[4], []).append(r)
        idx.setdefault(r[5], []).append(r)
    for lst in idx.values():
        lst.sort(key=lambda r: -r[1])
    return idx


def score(fx, idx, n=10):
    """Returns (priority, details) or None when history is too thin."""
    h, a, ts = fx['h']['id'], fx['a']['id'], fx['ts']
    hm = [r for r in idx.get(h, []) if r[1] < ts][:n]
    am = [r for r in idx.get(a, []) if r[1] < ts][:n]
    if len(hm) < 5 or len(am) < 5:
        return None

    def gf(rows, t):
        return sum(r[6] if r[4] == t else r[7] for r in rows) / len(rows)

    def ga(rows, t):
        return sum(r[7] if r[4] == t else r[6] for r in rows) / len(rows)

    xh = (gf(hm, h) + ga(am, a)) / 2
    xa = (gf(am, a) + ga(hm, h)) / 2
    total = xh + xa
    p15, p25 = _tail(total, 2), _tail(total, 3)
    priority = max(p15 - 0.81, p25 - 0.61)
    return priority, {'xg': round(total, 2), 'o15': round(p15, 3), 'o25': round(p25, 3)}
