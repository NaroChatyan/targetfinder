"""Project constants, paths and small time helpers."""
import os, datetime as dt

PROJECT = "TargetFinder"
VERSION = "0.1.0"
REPO_URL = os.environ.get("TARGETFINDER_REPO_URL", "https://github.com/targetfinder/targetfinder")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("TARGETFINDER_DATA_DIR", os.path.join(ROOT, "data"))
DB_PATH = os.environ.get("TARGETFINDER_DB", os.path.join(DATA_DIR, "targetfinder.db"))
CACHE_DIR = os.environ.get("TARGETFINDER_SWEEP_DIR", os.path.join(ROOT, "sweeps"))
LOG_DIR = os.path.join(DATA_DIR, "logs")
API_HOST = os.environ.get("TARGETFINDER_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("TARGETFINDER_API_PORT", "8787"))
API_LOG = os.environ.get("TARGETFINDER_API_LOG", "0") == "1"
USER_AGENT = os.environ.get("TARGETFINDER_USER_AGENT", f"targetfinder/{VERSION} (+{REPO_URL})")
HTTP_TIMEOUT = 45
# minimum seconds between two requests to the same host (one request in flight per host)
HOST_DELAY = {"default": 1.0, "hackerone.com": 1.5, "bugcrowd.com": 1.0, "hackenproof.com": 1.0, "immunefi.com": 1.0,
              "api.yeswehack.com": 1.0, "raw.githubusercontent.com": 1.0, "app.intigriti.com": 1.0, "audits.sherlock.xyz": 1.0}
# re-fetch per-program detail pages at most this often unless the listing says the program changed
DETAIL_REFRESH_SECONDS = 24 * 3600
REPO_POLL_WORKERS = 4
REPO_LS_REMOTE_TIMEOUT = 25
REPO_LS_REMOTE_TIMEOUT_BY_HOST = {"gitlab.com": 60}
# Immunefi's Terms of Use prohibit automated access (docs/PLATFORMS.md). Collector is OFF unless explicitly enabled.
ENABLE_IMMUNEFI = os.environ.get("TARGETFINDER_ENABLE_IMMUNEFI", "0") == "1"
SCORING_VERSION = 1
# a platform whose collector has not succeeded for this long is reported as stale
STALE_AFTER_SECONDS = 6 * 3600

def utcnow():
    return dt.datetime.now(dt.timezone.utc)

def iso(ts=None):
    ts = ts or utcnow()
    if isinstance(ts, str):
        return ts
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_ts(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return dt.datetime.fromtimestamp(s, dt.timezone.utc)
    try:
        s2 = s.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        pass
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
    return None

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
