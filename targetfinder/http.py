"""Polite HTTP client: one request in flight per host, a minimum delay between requests to the same host, retries, gzip. stdlib only."""
import json, time, urllib.request, urllib.error, urllib.parse, threading, gzip, io, email.utils, datetime as dt
from . import config

_last = {}            # host -> monotonic time the last request to it finished
_host_locks = {}      # host -> lock held for the whole wait + request
_lock = threading.Lock()

class HttpError(Exception):
    def __init__(self, status, body, url):
        super().__init__(f"HTTP {status} for {url}")
        self.status, self.body, self.url = status, body, url

def reset_throttle():
    with _lock:
        _last.clear(); _host_locks.clear()

def _host_lock(host):
    with _lock:
        lk = _host_locks.get(host)
        if lk is None:
            lk = _host_locks[host] = threading.Lock()
        return lk

def _wait(host):
    delay = config.HOST_DELAY.get(host, config.HOST_DELAY["default"])
    wait = _last.get(host, 0) + delay - time.monotonic()
    if wait > 0:
        time.sleep(wait)

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
        with _host_lock(host):           # serialize per host: the delay is measured from the previous request's completion
            _wait(host)
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
                    last = HttpError(e.code, raw, url)
                    backoff = min(60, 2 ** attempt * 3 + (retry_after_seconds(e.headers.get("Retry-After")) or 0))
                else:
                    raise HttpError(e.code, raw, url)
            except Exception as ex:
                last = ex
                backoff = 2 ** attempt
            finally:
                _last[host] = time.monotonic()
        time.sleep(backoff)
    raise last or RuntimeError("fetch failed")

def retry_after_seconds(value):
    """Retry-After is either delay-seconds or an HTTP-date (RFC 9110). Returns seconds >= 0, or None when unparseable."""
    if value is None:
        return None
    s = str(value).strip()
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (d - dt.datetime.now(dt.timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None

def get_json(url, **kw):
    st, raw, hdrs = fetch(url, **kw)
    return json.loads(raw.decode("utf-8", "replace"))

def get_text(url, **kw):
    st, raw, hdrs = fetch(url, **kw)
    return raw.decode("utf-8", "replace")

def post_json(url, payload, **kw):
    st, raw, hdrs = fetch(url, data=payload, **kw)
    return json.loads(raw.decode("utf-8", "replace"))
