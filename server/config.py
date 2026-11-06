import os
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
DAYS = os.path.join(DATA, 'days')
SITE = os.path.join(ROOT, 'site')

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
VERSION = '1.1.2'


def now_utc():
    return datetime.now(timezone.utc)


def today_cm():
    return now_utc().astimezone(CAMEROON).date()


def day_key(d):
    return d.strftime('%Y-%m-%d')
