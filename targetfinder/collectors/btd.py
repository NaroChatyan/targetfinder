"""arkadiyt/bounty-targets-data mirror (hourly scopes for H1 / Bugcrowd / Intigriti / YesWeHack)."""
from .. import http
BASE = "https://raw.githubusercontent.com/arkadiyt/bounty-targets-data/main/data/"
import time
_cache = {}
TTL = 1800   # seconds; sync.run also clears the cache at the start of every run

def clear():
    _cache.clear()

def load(name):
    hit = _cache.get(name)
    if hit is None or time.monotonic() - hit[0] > TTL:
        hit = _cache[name] = (time.monotonic(), http.get_json(BASE + name + "_data.json"))
    return hit[1]
