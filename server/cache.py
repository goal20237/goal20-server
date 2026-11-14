"""GOAL20's SQLite caching layer (data/cache/goal20.sqlite).

What it holds:
- responses: the last answer of each external request (football-data.org
  fixtures and standings), with the time it was fetched;
- cycles:    how many fetch cycles each source used per UTC day (the cap
             that keeps GOAL20 at 4 cycles a day);
- fits:      team ratings per competition and day (computed once a day,
             reused by every later run);
- predictions: every match prediction, frozen once the match kicks off.

Between GitHub runs the file is kept with actions/cache (never committed:
data/cache/ is in .gitignore). If the cache is ever lost, GOAL20 simply
fetches once more and rebuilds it.
"""
import json
import os
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
  key TEXT PRIMARY KEY, source TEXT NOT NULL, fetched_at INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cycles (
  source TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (source, day));
CREATE TABLE IF NOT EXISTS fetch_log (
  at INTEGER NOT NULL, source TEXT NOT NULL, key TEXT NOT NULL, ok INTEGER NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS fits (
  league INTEGER NOT NULL, cutoff INTEGER NOT NULL, rows INTEGER NOT NULL, body TEXT NOT NULL,
  PRIMARY KEY (league, cutoff));
CREATE TABLE IF NOT EXISTS predictions (
  day TEXT NOT NULL, fixture_id INTEGER NOT NULL, kickoff INTEGER NOT NULL, status TEXT NOT NULL,
  market TEXT, probability REAL, body TEXT NOT NULL, computed_at INTEGER NOT NULL,
  PRIMARY KEY (day, fixture_id));
"""


class Cache:
    def __init__(self, path=None):
        self.path = path or os.path.join(config.CACHE, 'goal20.sqlite')
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            self.db = sqlite3.connect(self.path)
            self.db.executescript(SCHEMA)
        except sqlite3.DatabaseError:
            # A damaged file is replaced: it is only a cache.
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.db = sqlite3.connect(self.path)
            self.db.executescript(SCHEMA)

    # ------------------------------------------------------------ responses
    def get(self, key):
        """(fetched_at, decoded body) or None."""
        row = self.db.execute('SELECT fetched_at, body FROM responses WHERE key = ?', (key,)).fetchone()
        if not row:
            return None
        try:
            return row[0], json.loads(row[1])
        except ValueError:
            return None

    def put(self, key, source, value, at=None):
        self.db.execute('REPLACE INTO responses (key, source, fetched_at, body) VALUES (?, ?, ?, ?)',
                        (key, source, int(at or time.time()), json.dumps(value, separators=(',', ':'))))
        self.db.commit()

    # --------------------------------------------------------------- cycles
    @staticmethod
    def _day(at=None):
        return time.strftime('%Y-%m-%d', time.gmtime(at or time.time()))

    def cycles(self, source, at=None):
        row = self.db.execute('SELECT n FROM cycles WHERE source = ? AND day = ?',
                              (source, self._day(at))).fetchone()
        return row[0] if row else 0

    def add_cycle(self, source, at=None):
        day = self._day(at)
        self.db.execute('INSERT INTO cycles (source, day, n) VALUES (?, ?, 1) '
                        'ON CONFLICT(source, day) DO UPDATE SET n = n + 1', (source, day))
        self.db.commit()

    def set_cycles(self, source, n, at=None):
        """Raise today's count to n (never lowers it)."""
        day = self._day(at)
        self.db.execute('INSERT INTO cycles (source, day, n) VALUES (?, ?, ?) '
                        'ON CONFLICT(source, day) DO UPDATE SET n = MAX(n, excluded.n)', (source, day, int(n)))
        self.db.commit()

    def log(self, source, key, ok, note='', at=None):
        self.db.execute('INSERT INTO fetch_log (at, source, key, ok, note) VALUES (?, ?, ?, ?, ?)',
                        (int(at or time.time()), source, key, 1 if ok else 0, note[:200]))
        self.db.commit()

    def calls_today(self, source, at=None):
        t = int(at or time.time())
        start = t - t % 86400  # start of the UTC day
        row = self.db.execute('SELECT COUNT(*) FROM fetch_log WHERE source = ? AND at >= ?',
                              (source, start)).fetchone()
        return row[0]

    # ----------------------------------------------------------------- fits
    def get_fit(self, league, cutoff, rows):
        row = self.db.execute('SELECT body FROM fits WHERE league = ? AND cutoff = ? AND rows = ?',
                              (league, cutoff, rows)).fetchone()
        return json.loads(row[0]) if row else None

    def put_fit(self, league, cutoff, rows, value):
        self.db.execute('REPLACE INTO fits (league, cutoff, rows, body) VALUES (?, ?, ?, ?)',
                        (league, cutoff, rows, json.dumps(value, separators=(',', ':'))))

    # ---------------------------------------------------------- predictions
    def predictions(self, day):
        rows = self.db.execute('SELECT body FROM predictions WHERE day = ? ORDER BY kickoff, fixture_id',
                               (day,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def frozen(self, day, fixture_id):
        row = self.db.execute('SELECT body FROM predictions WHERE day = ? AND fixture_id = ?',
                              (day, fixture_id)).fetchone()
        return json.loads(row[0]) if row else None

    def put_prediction(self, day, p, at=None):
        pick = p.get('pick') or {}
        self.db.execute(
            'REPLACE INTO predictions (day, fixture_id, kickoff, status, market, probability, body, computed_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (day, p['id'], p['ts'], p['status'], pick.get('market'), pick.get('probability'),
             json.dumps(p, separators=(',', ':'), ensure_ascii=False), int(at or time.time())))

    # ---------------------------------------------------------- housekeeping
    def prune(self, keep_days=45):
        old = int(time.time()) - keep_days * 86400
        self.db.execute('DELETE FROM fetch_log WHERE at < ?', (old,))
        self.db.execute('DELETE FROM fits WHERE cutoff < ?', (old,))
        self.db.execute('DELETE FROM predictions WHERE kickoff < ?', (old,))
        self.db.execute('DELETE FROM cycles WHERE day < ?', (self._day(old),))
        self.db.commit()

    def commit(self):
        self.db.commit()

    def close(self):
        try:
            self.db.commit()
            self.db.close()
        except sqlite3.Error:
            pass
