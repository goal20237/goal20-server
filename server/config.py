import os
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
DAYS = os.path.join(DATA, 'days')
SITE = os.path.join(ROOT, 'site')
CACHE = os.path.join(DATA, 'cache')


def load_dotenv(path=None):
    """Reads KEY=VALUE lines from the server folder's .env file (for runs on
    your own PC). The file is listed in .gitignore, so it is never uploaded.
    On GitHub, the keys come from Settings -> Secrets instead; a value that
    is already set always wins over the file."""
    path = path or os.path.join(ROOT, '.env')
    try:
        with open(path, encoding='utf-8-sig') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                name, value = line.split('=', 1)
                name = name.strip().removeprefix('export ').strip()
                value = value.strip().strip('"').strip("'")
                if name and value and not os.environ.get(name):
                    os.environ[name] = value
    except FileNotFoundError:
        pass
    # One football-data.org key under either name.
    key = os.environ.get('FOOTBALL_DATA_API_KEY') or os.environ.get('FOOTBALL_DATA_ORG_KEY') or ''
    for name in ('FOOTBALL_DATA_API_KEY', 'FOOTBALL_DATA_ORG_KEY'):
        if key and not os.environ.get(name):
            os.environ[name] = key


load_dotenv()

API_KEY = os.environ.get('API_FOOTBALL_KEY', '').strip()
PLAN = (os.environ.get('PLAN') or 'free').strip().lower()
MODE = (os.environ.get('MODE') or 'full').strip().lower()
IS_PRO = PLAN in ('pro', 'paid', 'ultra', 'mega')

# Requests GOAL20 may spend per run, and the provider balance it always keeps.
FULL_RUN_BUDGET = 400 if IS_PRO else 55
QUICK_RUN_BUDGET = 60 if IS_PRO else 2
RESERVE = 50 if IS_PRO else 12
ENRICH_MATCHES = 60 if IS_PRO else 10   # candidates checked in a full run
BACKFILL_DAYS = 45 if IS_PRO else 12    # missing past days fetched per run
HISTORY_DAYS = 450

CAMEROON = timezone(timedelta(hours=1))
API_TZ = 'Africa/Douala'
VERSION = '1.11.0'


def now_utc():
    return datetime.now(timezone.utc)


def today_cm():
    return now_utc().astimezone(CAMEROON).date()


def day_key(d):
    return d.strftime('%Y-%m-%d')
