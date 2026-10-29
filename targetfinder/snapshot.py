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

def export_snapshot(con, out_dir, exclude_platforms=None):
    """Write the snapshot files; `exclude_platforms` leaves whole platforms out (see docs/PLATFORMS.md on redistribution)."""
    os.makedirs(out_dir, exist_ok=True)
    since = config.iso(config.utcnow() - dt.timedelta(days=WINDOW_DAYS))
    excl = sorted(exclude_platforms or [])
    pf = "p.platform NOT IN (%s)" % ",".join("?" * len(excl)) if excl else "1=1"
    files = {}
    files["programs.json"] = _rows(con, f"SELECT p.* FROM programs p WHERE {pf} ORDER BY p.id", excl)
    files["targets.json"] = _rows(con, f"SELECT t.* FROM targets t JOIN programs p ON p.id=t.program_id WHERE {pf} ORDER BY t.program_id, t.norm", excl)
    files["repos.json"] = _rows(con, f"""SELECT r.url, r.provider, r.provider_host, r.full_name, r.ref_name, r.head_sha, r.last_polled_at, r.last_changed_at, r.last_error, r.fail_count, r.active FROM repos r
                                        WHERE EXISTS (SELECT 1 FROM targets t JOIN programs p ON p.id=t.program_id WHERE t.repo_url=r.url AND {pf}) ORDER BY r.url""", excl)
    files["target_changes_90d.json"] = _rows(con, f"""SELECT c.id, c.program_id, t.norm AS target_norm, c.change_type, c.changed_at, c.diff, c.source FROM target_changes c JOIN targets t ON t.id=c.target_id
                                                   JOIN programs p ON p.id=c.program_id WHERE c.changed_at>=? AND {pf} ORDER BY c.id""", [since] + excl)
    files["program_events_90d.json"] = _rows(con, f"SELECT e.id, e.program_id, e.observed_at, e.change_type, e.category, e.before, e.after, e.counts, e.source FROM program_events e JOIN programs p ON p.id=e.program_id WHERE e.observed_at>=? AND {pf} ORDER BY e.id", [since] + excl)
    files["repo_changes_90d.json"] = _rows(con, f"""SELECT rc.id, r.url AS repo_url, rc.event_type, rc.source, rc.ref_name, rc.before_sha, rc.after_sha, rc.observed_at, rc.commit_count, rc.comparison, rc.files, rc.push_kind
                                                 FROM repo_changes rc JOIN repos r ON r.id=rc.repo_id WHERE rc.observed_at>=? AND EXISTS (SELECT 1 FROM targets t JOIN programs p ON p.id=t.program_id WHERE t.repo_url=r.url AND {pf}) ORDER BY rc.id""", [since] + excl)
    files["payouts_90d.json"] = _rows(con, f"SELECT o.program_id, o.metric, o.amount, o.currency, o.unit, o.source_label, o.source_field, o.source_url, o.period, o.basis, o.observed_at FROM payout_observations o JOIN programs p ON p.id=o.program_id WHERE o.observed_at>=? AND {pf} ORDER BY o.id", [since] + excl)
    files["collector_runs.json"] = _rows(con, """SELECT platform, started_at, finished_at, ok, programs, error FROM collector_runs WHERE id IN (SELECT MAX(id) FROM collector_runs GROUP BY platform) ORDER BY platform""")
    marks = ",".join("?" * len(KV_KEYS))
    kv = {r["k"]: db.uj(r["v"]) for r in con.execute(f"SELECT k, v FROM kv WHERE k LIKE 'platform_synced:%' OR k IN ({marks})", KV_KEYS)}
    files["meta.json"] = {"exported_at": config.iso(), "version": config.VERSION, "window_days": WINDOW_DAYS, "kv": kv, "excluded_platforms": excl,
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
