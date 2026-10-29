"""JSON snapshots of the tracked state (public/*.json): export after a sync, import into an empty database on a fresh clone.

Full tables: programs, targets (active and inactive), repos, collector health, sync cursors. Rolling 90 days: target changes,
program events, repository pushes, payout observations. import_snapshot is idempotent: rows are keyed by their natural keys."""
import json, os, datetime as dt
from . import db, config

FULL = {"programs": ("id",), "targets": ("program_id", "norm"), "repos": ("url",)}
WINDOW_DAYS = 90
KV_KEYS = ("tracking_since", "last_sync_at")

def _rows(con, sql, args=()):
    return [db.row_to_dict(r) for r in con.execute(sql, args)]

def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0, sort_keys=True)
        f.write("\n")

def export_snapshot(con, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    since = config.iso(config.utcnow() - dt.timedelta(days=WINDOW_DAYS))
    files = {}
    files["programs.json"] = _rows(con, "SELECT * FROM programs ORDER BY id")
    files["targets.json"] = _rows(con, "SELECT * FROM targets ORDER BY program_id, norm")
    files["repos.json"] = _rows(con, "SELECT url, provider, provider_host, full_name, ref_name, head_sha, last_polled_at, last_changed_at, last_error, fail_count, active FROM repos ORDER BY url")
    files["target_changes_90d.json"] = _rows(con, """SELECT c.id, c.program_id, t.norm AS target_norm, c.change_type, c.changed_at, c.diff, c.source FROM target_changes c JOIN targets t ON t.id=c.target_id
                                                   WHERE c.changed_at>=? ORDER BY c.id""", (since,))
    files["program_events_90d.json"] = _rows(con, "SELECT id, program_id, observed_at, change_type, category, before, after, counts, source FROM program_events WHERE observed_at>=? ORDER BY id", (since,))
    files["repo_changes_90d.json"] = _rows(con, """SELECT rc.id, r.url AS repo_url, rc.event_type, rc.source, rc.ref_name, rc.before_sha, rc.after_sha, rc.observed_at, rc.commit_count, rc.comparison, rc.files, rc.push_kind
                                                 FROM repo_changes rc JOIN repos r ON r.id=rc.repo_id WHERE rc.observed_at>=? ORDER BY rc.id""", (since,))
    files["payouts_90d.json"] = _rows(con, "SELECT program_id, metric, amount, currency, unit, source_label, source_field, source_url, period, basis, observed_at FROM payout_observations WHERE observed_at>=? ORDER BY id", (since,))
    files["collector_runs.json"] = _rows(con, """SELECT platform, started_at, finished_at, ok, programs, error FROM collector_runs WHERE id IN (SELECT MAX(id) FROM collector_runs GROUP BY platform) ORDER BY platform""")
    marks = ",".join("?" * len(KV_KEYS))
    kv = {r["k"]: db.uj(r["v"]) for r in con.execute(f"SELECT k, v FROM kv WHERE k LIKE 'platform_synced:%' OR k IN ({marks})", KV_KEYS)}
    files["meta.json"] = {"exported_at": config.iso(), "version": config.VERSION, "window_days": WINDOW_DAYS, "kv": kv,
                          "counts": {k: len(v) for k, v in files.items() if isinstance(v, list)}}
    for name, obj in files.items():
        _write(os.path.join(out_dir, name), obj)
    return sorted(files)

def _load(in_dir, name, default=None):
    p = os.path.join(in_dir, name)
    if not os.path.exists(p):
        return default if default is not None else []
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def _upsert(con, table, row, keys, drop=("id",)):
    cols = [c for c in row.keys() if c not in drop]
    sets = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    con.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) ON CONFLICT({', '.join(keys)}) DO UPDATE SET {sets}", [row[c] for c in cols])

def import_snapshot(con, in_dir):
    """Load a snapshot. Existing rows with the same natural key are updated, history rows already present are skipped."""
    meta = _load(in_dir, "meta.json", {})
    for row in _load(in_dir, "programs.json"):
        _upsert(con, "programs", row, ("id",), drop=())
    for row in _load(in_dir, "targets.json"):
        _upsert(con, "targets", row, ("program_id", "norm"))
    for row in _load(in_dir, "repos.json"):
        _upsert(con, "repos", row, ("url",))
    tid = {(r["program_id"], r["norm"]): r["id"] for r in con.execute("SELECT id, program_id, norm FROM targets")}
    rid = {r["url"]: r["id"] for r in con.execute("SELECT id, url FROM repos")}
    n = {"changes": 0, "events": 0, "pushes": 0, "payouts": 0}
    for c in _load(in_dir, "target_changes_90d.json"):
        t = tid.get((c["program_id"], c["target_norm"]))
        if t is None: continue
        if con.execute("SELECT 1 FROM target_changes WHERE target_id=? AND change_type=? AND changed_at=?", (t, c["change_type"], c["changed_at"])).fetchone(): continue
        con.execute("INSERT INTO target_changes(target_id, program_id, change_type, changed_at, diff, source) VALUES(?,?,?,?,?,?)", (t, c["program_id"], c["change_type"], c["changed_at"], c.get("diff"), c.get("source"))); n["changes"] += 1
    for e in _load(in_dir, "program_events_90d.json"):
        if con.execute("SELECT 1 FROM program_events WHERE program_id=? AND change_type=? AND observed_at=?", (e["program_id"], e["change_type"], e["observed_at"])).fetchone(): continue
        con.execute("INSERT INTO program_events(program_id, observed_at, change_type, category, before, after, counts, source) VALUES(?,?,?,?,?,?,?,?)",
                    (e["program_id"], e["observed_at"], e["change_type"], e.get("category"), e.get("before"), e.get("after"), e.get("counts"), e.get("source"))); n["events"] += 1
    for p in _load(in_dir, "repo_changes_90d.json"):
        r = rid.get(p["repo_url"])
        if r is None: continue
        if con.execute("SELECT 1 FROM repo_changes WHERE repo_id=? AND after_sha=? AND observed_at=?", (r, p["after_sha"], p["observed_at"])).fetchone(): continue
        con.execute("INSERT INTO repo_changes(repo_id, event_type, source, ref_name, before_sha, after_sha, observed_at, commit_count, comparison, files, push_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (r, p.get("event_type") or "push", p.get("source") or "poll", p.get("ref_name"), p.get("before_sha"), p["after_sha"], p["observed_at"], p.get("commit_count"), p.get("comparison"), p.get("files"), p.get("push_kind"))); n["pushes"] += 1
    for o in _load(in_dir, "payouts_90d.json"):
        if con.execute("SELECT 1 FROM payout_observations WHERE program_id=? AND metric=? AND observed_at=?", (o["program_id"], o["metric"], o["observed_at"])).fetchone(): continue
        con.execute("INSERT INTO payout_observations(program_id, metric, amount, currency, unit, source_label, source_field, source_url, period, basis, observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(o.get(k) for k in ("program_id", "metric", "amount", "currency", "unit", "source_label", "source_field", "source_url", "period", "basis", "observed_at"))); n["payouts"] += 1
    for r in _load(in_dir, "collector_runs.json"):
        if con.execute("SELECT 1 FROM collector_runs WHERE platform=? AND finished_at=?", (r["platform"], r["finished_at"])).fetchone(): continue
        con.execute("INSERT INTO collector_runs(platform, started_at, finished_at, ok, programs, error) VALUES(?,?,?,?,?,?)", (r["platform"], r.get("started_at"), r.get("finished_at"), r.get("ok"), r.get("programs"), r.get("error")))
    for k, v in (meta.get("kv") or {}).items():
        if k == "last_sync_at": continue   # this instance has not synced yet
        if db.kv_get(con, k) is None: db.kv_set(con, k, v)
    db.kv_set(con, "snapshot_imported_at", config.iso()); db.kv_set(con, "snapshot_exported_at", meta.get("exported_at"))
    con.commit()
    return n
