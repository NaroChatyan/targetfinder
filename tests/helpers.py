"""Shared fixtures: a temp database, stub collector results, a local API server."""
import os, sys, json, tempfile, threading, urllib.request, urllib.error, socket

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def temp_db():
    """Point the package at a fresh temp database (call before importing db-using modules)."""
    from targetfinder import config
    d = tempfile.mkdtemp(prefix="tf-test-")
    config.DATA_DIR = d; config.DB_PATH = os.path.join(d, "t.db"); config.CACHE_DIR = os.path.join(d, "sweeps"); config.LOG_DIR = os.path.join(d, "logs")
    return config.DB_PATH


def program(pid, targets, **kw):
    plat, handle = pid.split(":", 1)
    p = {"id": pid, "platform": {"testplat": "TestPlat", "otherplat": "OtherPlat"}.get(plat, plat), "handle": handle, "name": handle.replace("-", " ").title(), "link": f"https://example.test/{handle}",
         "program_kind": "bounty", "program_status": "active", "is_active": True, "reward_status": "paid", "bounty_min": 100, "bounty_max": 5000, "reward_currency": "USD",
         "launch_at": "2025-01-01T00:00:00Z", "fee": {"amount": 0, "unit": "USD", "kind": "fee", "source": "test"}, "detail_marker": "m", "targets": targets, "payout_facts": [],
         "report_count": 12, "report_count_source": "test", "report_count_label": "public reports"}
    p.update(kw)
    return p


def target(identifier, **kw):
    t = {"identifier": identifier, "hint_type": "URL", "in_scope": True, "eligible_for_bounty": True}
    t.update(kw)
    return t


def apply(con, programs, platform="TestPlat"):
    from targetfinder import sync
    return sync.apply_platform(con, platform, {"platform": platform, "programs": programs, "complete": True}, lambda m: None)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start_api():
    from http.server import ThreadingHTTPServer
    from targetfinder import api
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), api.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    return srv, f"http://127.0.0.1:{port}"


def http(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode()


def keys_in(obj):
    """Every dict key anywhere inside obj."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k); out |= keys_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= keys_in(v)
    return out
