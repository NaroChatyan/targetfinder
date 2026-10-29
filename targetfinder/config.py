"""Project constants, paths and small time helpers."""
import os, platform, datetime as dt

PROJECT = "TargetFinder"
VERSION = "0.1.0"
REPO_URL = os.environ.get("TARGETFINDER_REPO_URL", "https://github.com/NaroChatyan/targetfinder")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def in_checkout(root=None):
    """True when running from a git checkout (bin/targetfinder next to the package); False when installed with pip."""
    return os.path.exists(os.path.join(root or ROOT, "bin", "targetfinder"))

def user_data_dir(system=None, env=None):
    """Per-user data directory for pip installs: XDG on Linux, Application Support on macOS, LOCALAPPDATA on Windows."""
    import ntpath, posixpath
    env = os.environ if env is None else env; system = system or platform.system()
    join = ntpath.join if system == "Windows" else posixpath.join
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    if system == "Windows": base = env.get("LOCALAPPDATA") or join(home, "AppData", "Local")
    elif system == "Darwin": base = join(home, "Library", "Application Support")
    else: base = env.get("XDG_DATA_HOME") or join(home, ".local", "share")
    return join(base, "targetfinder")

def default_dirs(root=None, system=None, env=None):
    """{'home', 'data', 'sweeps'}: inside the checkout when there is one, otherwise under the per-user directory."""
    root = root or ROOT
    if in_checkout(root):
        return {"home": root, "data": os.path.join(root, "data"), "sweeps": os.path.join(root, "sweeps")}
    u = user_data_dir(system, env)
    return {"home": u, "data": u, "sweeps": os.path.join(u, "sweeps")}

_dirs = default_dirs()
HOME_DIR = _dirs["home"]   # where relative paths such as `init --out public` land
DATA_DIR = os.environ.get("TARGETFINDER_DATA_DIR", _dirs["data"])
DB_PATH = os.environ.get("TARGETFINDER_DB", os.path.join(DATA_DIR, "targetfinder.db"))
CACHE_DIR = os.environ.get("TARGETFINDER_SWEEP_DIR", _dirs["sweeps"])
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
