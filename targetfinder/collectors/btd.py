"""arkadiyt/bounty-targets-data mirror (hourly scopes for H1 / Bugcrowd / Intigriti / YesWeHack)."""
from .. import http
BASE = "https://raw.githubusercontent.com/arkadiyt/bounty-targets-data/main/data/"
_cache = {}

def load(name):
    if name not in _cache:
        _cache[name] = http.get_json(BASE + name + "_data.json")
    return _cache[name]
