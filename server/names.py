"""Matches football-data.co.uk team names to API-Football teams."""
import difflib
import re
import unicodedata

# Short forms used by football-data.co.uk -> words API-Football uses.
ALIASES = {
    'man united': 'manchester united', 'man utd': 'manchester united',
    'man city': 'manchester city', "nott'm forest": 'nottingham forest',
    'nottm forest': 'nottingham forest', 'sheffield weds': 'sheffield wednesday',
    'sheffield utd': 'sheffield united', 'wolves': 'wolverhampton wanderers',
    'spurs': 'tottenham', 'west brom': 'west bromwich albion', 'qpr': 'queens park rangers',
    'mk dons': 'milton keynes dons', 'ath madrid': 'atletico madrid',
    'ath bilbao': 'athletic club', 'sociedad': 'real sociedad', 'betis': 'real betis',
    'celta': 'celta vigo', 'vallecano': 'rayo vallecano', 'espanol': 'espanyol',
    'sp gijon': 'sporting gijon', 'la coruna': 'deportivo la coruna',
    'leverkusen': 'bayer leverkusen', 'dortmund': 'borussia dortmund',
    "m'gladbach": 'borussia monchengladbach', 'ein frankfurt': 'eintracht frankfurt',
    'fc koln': 'koln', 'bayern munich': 'bayern munchen', 'hertha': 'hertha berlin',
    'paris sg': 'paris saint germain', 'st etienne': 'saint etienne',
    'inter': 'inter', 'milan': 'ac milan', 'roma': 'as roma',
    'psv eindhoven': 'psv', 'for sittard': 'fortuna sittard', 'nijmegen': 'nec nijmegen',
    'sp lisbon': 'sporting cp', 'sp braga': 'sc braga', 'guimaraes': 'vitoria guimaraes',
    'olympiakos': 'olympiacos', 'st gilloise': 'union st gilloise', 'club brugge': 'club brugge',
    'st mirren': 'saint mirren', 'st johnstone': 'saint johnstone',
}

DROP = {'fc', 'cf', 'afc', 'sc', 'ac', 'as', 'ss', 'sv', 'fk', 'bk', 'if', 'cd', 'ud',
        'sd', 'club', 'de', 'the', 'calcio', 'cp', 'sk', 'ik', 'ff', 'aif', 'bc', 'ca',
        'ec', 'se', 'cr', 'rc', 'us', 'sad', 'vfb', 'vfl', 'tsg', '1', 'fsv', 'spvgg'}

# Reserve, youth and women's sides must never take a first team's results.
YOUTH = re.compile(
    r'(\bU\d{2}\b|\bW\b|women|femen|fem\.?$|ladies|reserves?\b|\bII\b|\bIII\b|'
    r'\bB\b$|\bC\b$|\bjong\b|castilla|youth|academy|primavera|juniors?\b)', re.I)


def norm(name):
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower().strip()
    s = ALIASES.get(s, s)
    s = s.replace('&', ' and ').replace("'", '')
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    s = re.sub(r'\butd\b', 'united', s)
    s = re.sub(r'\bst\b', 'saint', s)
    words = [w for w in s.split() if w not in DROP]
    return ' '.join(words) or s.strip()


def similarity(a, b):
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / max(1, len(ta | tb))
    contain = 0.9 if (len(a) >= 4 and len(b) >= 4 and (a in b or b in a)) else 0
    return max(ratio, jac, contain)


def is_youth(name, league=''):
    return bool(YOUTH.search(name or '')) or bool(re.search(r'women|u\d{2}|youth|reserve', league or '', re.I))


class Matcher:
    """Per-country candidate lists of football-data.co.uk team names."""

    def __init__(self, fd_teams_by_country):
        self.by_country = {c: {n: norm(n) for n in names} for c, names in fd_teams_by_country.items()}

    def match(self, api_name, country, league=''):
        """Returns (fd_key, score) or None. fd_key is 'country|fd name'."""
        if is_youth(api_name, league):
            return None
        cands = self.by_country.get(country)
        if not cands:
            return None
        target = norm(api_name)
        scored = sorted(((similarity(target, n), raw) for raw, n in cands.items()), reverse=True)
        if not scored:
            return None
        best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0
        if best[0] >= 0.84 and best[0] - second >= 0.04:
            return f'{country}|{best[1]}', best[0]
        return None
