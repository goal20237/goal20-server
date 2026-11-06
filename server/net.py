import time
import urllib.error
import urllib.request

UA = 'GOAL20-server/1.0 (personal football research tool)'


def fetch(url, headers=None, timeout=40, retries=2):
    """GET a URL and return (status, headers, bytes). Never raises for HTTP errors."""
    h = {'User-Agent': UA, 'Accept': '*/*'}
    h.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read() if hasattr(e, 'read') else b''
        except Exception as e:  # network problem: retry
            last = e
            time.sleep(2 + attempt * 3)
    return 0, {}, str(last).encode()
