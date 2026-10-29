"""SQLite storage. One connection per call site; WAL mode; JSON blobs for nested objects."""
import sqlite3, json, os
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
  id TEXT PRIMARY KEY,            -- Platform:handle
  platform TEXT NOT NULL,
  handle TEXT NOT NULL,
  name TEXT,
  catalog TEXT DEFAULT 'platform',
  program_kind TEXT,              -- bounty | vdp | contest | challenge
  program_status TEXT,            -- active | paused
  is_active INTEGER DEFAULT 1,
  listed INTEGER DEFAULT 1,       -- 0 once it left the platform listing
  reward_status TEXT,             -- paid | no_reward | unknown
  bounty_min REAL, bounty_max REAL, reward_currency TEXT,
  report_count INTEGER,
  launch_at TEXT, added_at TEXT, last_updated TEXT, targets_updated_at TEXT,
  last_seen TEXT, left_at TEXT,
  fee_amount REAL, fee_unit TEXT, fee_kind TEXT, fee_source TEXT, fee_updated_at TEXT,
  source TEXT,                    -- collector name
  detail_fetched_at TEXT, detail_marker TEXT,
  data TEXT NOT NULL              -- JSON: full program record as collected (minus computed fields)
);
CREATE INDEX IF NOT EXISTS ix_programs_platform ON programs(platform);
CREATE INDEX IF NOT EXISTS ix_programs_name ON programs(name);

CREATE TABLE IF NOT EXISTS program_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  program_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  change_type TEXT NOT NULL,      -- first_seen | left_listing | returned | paused | resumed | rewards_changed | updated | scope
  category TEXT,                  -- program | rewards | scope | status
  before TEXT, after TEXT,        -- JSON
  counts TEXT,                    -- JSON {added,removed,updated}
  source TEXT
);
CREATE INDEX IF NOT EXISTS ix_pe_prog ON program_events(program_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_pe_time ON program_events(observed_at);

CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  program_id TEXT NOT NULL,
  norm TEXT NOT NULL,             -- normalization key (dedup within a program)
  identifier TEXT NOT NULL,
  normalized_identifier TEXT,     -- normalized identifier (display)
  display_name TEXT,
  target_type TEXT,
  in_scope INTEGER DEFAULT 1,
  eligible_for_bounty INTEGER DEFAULT 1,
  wildcard INTEGER DEFAULT 0,
  scope_tags TEXT, language_tags TEXT,   -- JSON arrays
  severity_score REAL, severity_label TEXT,
  source_url TEXT, source_added_at TEXT,
  added_at TEXT, last_seen TEXT, last_change_type TEXT, last_changed_at TEXT,
  is_active INTEGER DEFAULT 1, removed_at TEXT,
  metadata TEXT,                  -- JSON
  repo_url TEXT, path_kind TEXT, path_prefix TEXT,
  UNIQUE(program_id, norm)
);
CREATE INDEX IF NOT EXISTS ix_targets_prog ON targets(program_id);
CREATE INDEX IF NOT EXISTS ix_targets_norm ON targets(norm);
CREATE INDEX IF NOT EXISTS ix_targets_repo ON targets(repo_url);

CREATE TABLE IF NOT EXISTS target_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER,
  program_id TEXT NOT NULL,
  change_type TEXT NOT NULL,      -- added | readded | updated | removed
  changed_at TEXT NOT NULL,
  diff TEXT,                      -- JSON {before, after}
  source TEXT
);
CREATE INDEX IF NOT EXISTS ix_tc_type_time ON target_changes(change_type, changed_at);
CREATE INDEX IF NOT EXISTS ix_tc_prog ON target_changes(program_id, changed_at);
CREATE INDEX IF NOT EXISTS ix_tc_target ON target_changes(target_id);

CREATE TABLE IF NOT EXISTS repos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT UNIQUE NOT NULL,       -- canonical https://host/owner/repo
  provider TEXT, provider_host TEXT, full_name TEXT,
  ref_name TEXT, head_sha TEXT,
  last_polled_at TEXT, last_changed_at TEXT, last_error TEXT, fail_count INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS repo_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER NOT NULL,
  event_type TEXT DEFAULT 'push',
  source TEXT DEFAULT 'poll',
  ref_name TEXT, before_sha TEXT, after_sha TEXT,
  observed_at TEXT NOT NULL,
  commit_count INTEGER,
  comparison TEXT                 -- JSON
);
CREATE INDEX IF NOT EXISTS ix_rc_time ON repo_changes(observed_at);
CREATE INDEX IF NOT EXISTS ix_rc_repo ON repo_changes(repo_id, observed_at);

CREATE TABLE IF NOT EXISTS payout_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  program_id TEXT NOT NULL,
  metric TEXT NOT NULL,           -- payout_total | payout_native_90d | reports_received_native_90d | first_response_avg_native_90d | resolved_total | last_report_resolved_at ...
  amount REAL, currency TEXT, unit TEXT,
  source_label TEXT, source_field TEXT, source_url TEXT, period TEXT, basis TEXT,
  observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_po ON payout_observations(program_id, metric, observed_at);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, finished_at TEXT, kind TEXT, status TEXT, stats TEXT, log TEXT
);
CREATE TABLE IF NOT EXISTS collector_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL, started_at TEXT, finished_at TEXT, ok INTEGER, programs INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS ix_cr_plat ON collector_runs(platform, id);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS watchlist (program_id TEXT PRIMARY KEY, added_at TEXT, note TEXT);
"""
MIGRATIONS = [
    ("programs", "pending_status", "ALTER TABLE programs ADD COLUMN pending_status TEXT"),
    ("programs", "pending_status_at", "ALTER TABLE programs ADD COLUMN pending_status_at TEXT"),
    ("repo_changes", "files", "ALTER TABLE repo_changes ADD COLUMN files TEXT"),
    ("repo_changes", "push_kind", "ALTER TABLE repo_changes ADD COLUMN push_kind TEXT"),
]

def connect(path=None):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=OFF")
    con.executescript(SCHEMA)
    for table, col, ddl in MIGRATIONS:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(ddl)
    con.commit()
    return con

def j(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":")) if v is not None else None

def uj(s, default=None):
    if s is None:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default

def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return uj(r["v"]) if r else default

def kv_set(con, k, v):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, j(v)))

def row_to_dict(r):
    return {k: r[k] for k in r.keys()} if r is not None else None
