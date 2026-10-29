"""Polite HTTP client: per-host delay, retries, UA, JSON helpers. stdlib only."""
import json, time, urllib.request, urllib.error, urllib.parse, threading, gzip, io
from . import config

_last = {}
_lock = threading.Lock()

class HttpError(Exception):
    def __init__(self, status, body, url):
        super().__init__(f"HTTP {status} for {url}")
        self.status, self.body, self.url = status, body, url

def _throttle(host):
    delay = config.HOST_DELAY.get(host, config.HOST_DELAY["default"])
    with _lock:
        now = time.monotonic()
        wait = _last.get(host, 0) + delay - now
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.monotonic()

def fetch(url, data=None, headers=None, method=None, timeout=None, retries=3, accept="application/json, text/html;q=0.9, */*;q=0.8"):
    host = urllib.parse.urlparse(url).hostname or "default"
    h = {"User-Agent": config.USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    body = data
    if isinstance(data, (dict, list)):
        body = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json")
    last = None
    for attempt in range(retries):
        _throttle(host)
        req = urllib.request.Request(url, headers=h, data=body, method=method or ("POST" if body else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return r.status, raw, dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(min(60, 2 ** attempt * 3 + float(e.headers.get("Retry-After") or 0)))
                last = HttpError(e.code, raw, url)
                continue
            raise HttpError(e.code, raw, url)
        except Exception as ex:
            last = ex
            time.sleep(2 ** attempt)
    raise last or RuntimeError("fetch failed")

def get_json(url, **kw):
    st, raw, hdrs = fetch(url, **kw)
    return json.loads(raw.decode("utf-8", "replace"))

def get_text(url, **kw):
    st, raw, hdrs = fetch(url, **kw)
    return raw.decode("utf-8", "replace")

def post_json(url, payload, **kw):
    st, raw, hdrs = fetch(url, data=payload, **kw)
    return json.loads(raw.decode("utf-8", "replace"))
