"""Server state kept in the repository's data/ folder."""
import gzip
import json
import os
import time

from . import config


def read_json(path, default=None):
    try:
        if path.endswith('.gz'):
            with gzip.open(path, 'rt', encoding='utf-8') as f:
                return json.load(f)
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return default


def write_json(path, value, compact=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    text = json.dumps(value, separators=(',', ':') if compact else None,
                      indent=None if compact else 1, ensure_ascii=False)
    if path.endswith('.gz'):
        with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=9) as f:
            f.write(text)
    else:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
    os.replace(tmp, path)


class State:
    PATH = os.path.join(config.DATA, 'state.json')

    def __init__(self):
        raw = read_json(self.PATH, {}) or {}
        self.blocked = raw.get('blocked', {})
        self.blocked_why = raw.get('blocked_why', {})
        self.name_map = raw.get('name_map', {})
        self.window = raw.get('window')  # [from, to, seen_day]
        self.last_full = raw.get('last_full')
        self.plan_name = raw.get('plan_name')
        self.div_league = raw.get('div_league') or {}
        self.fdorg_teams = raw.get('fdorg_teams') or {}
        self.suspended_until = raw.get('suspended_until') or 0
        self.suspended_why = raw.get('suspended_why') or ''

    def suspend(self, why, hours=20):
        self.suspended_until = time.time() + hours * 3600
        self.suspended_why = why

    @property
    def is_suspended(self):
        return self.suspended_until > time.time()

    def block(self, cap, why, days=7):
        self.blocked[cap] = time.time() + days * 86400
        self.blocked_why[cap] = why

    def save(self):
        now = time.time()
        self.blocked = {k: v for k, v in self.blocked.items() if v > now}
        self.blocked_why = {k: v for k, v in self.blocked_why.items() if k in self.blocked}
        write_json(self.PATH, {
            'blocked': self.blocked, 'blocked_why': self.blocked_why,
            'name_map': self.name_map, 'window': self.window,
            'last_full': self.last_full, 'plan_name': self.plan_name,
            'div_league': self.div_league, 'fdorg_teams': self.fdorg_teams,
            'suspended_until': self.suspended_until if self.is_suspended else 0,
            'suspended_why': self.suspended_why if self.is_suspended else '',
        }, compact=False)


def day_path(key):
    return os.path.join(config.DAYS, f'{key}.json.gz')


def load_day(key):
    return read_json(day_path(key))


def save_day(key, fixtures, final):
    write_json(day_path(key), {'at': config.now_utc().isoformat(), 'final': final, 'f': fixtures})


def all_day_keys():
    if not os.path.isdir(config.DAYS):
        return []
    return sorted(n[:-8] for n in os.listdir(config.DAYS) if n.endswith('.json.gz'))


ENRICH_PATH = os.path.join(config.DATA, 'enrich.json.gz')


def load_enrich():
    e = read_json(ENRICH_PATH, {}) or {}
    for k in ('h2h', 'inj', 'lu', 'odds', 'fdodds'):
        e.setdefault(k, {})
    return e


def save_enrich(e):
    cutoff = config.now_utc().timestamp() - 10 * 86400
    from datetime import datetime
    clean = {}
    for section, items in e.items():
        keep = {}
        for k, v in items.items():
            try:
                at = datetime.fromisoformat(v['at']).timestamp()
            except (KeyError, ValueError, TypeError):
                continue
            if at >= cutoff:
                keep[k] = v
        clean[section] = keep
    write_json(ENRICH_PATH, clean)
