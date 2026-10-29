"""Query layer shared by the HTTP API, the MCP server, the CLI and the sweep. List responses are {"items": [...], "page": {...}}."""
import math, json, datetime as dt
from . import db, config, model, intel

TIERS = ("top", "hot", "solid", "possible")
SCORE_LABELS = ("top", "strong", "promising", "normal")
COMPETITION = ("low", "medium", "high", "unknown")
CHANGE_TYPES = ("added", "readded", "updated", "removed")

def csv(v):
    if v is None: return None
    if isinstance(v, (list, tuple)): return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).split(",") if x.strip()]

def boolean(v, default=False):
    if v is None: return default
    if isinstance(v, bool): return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def intv(v, default, lo=None, hi=None):
    try:
        x = int(v)
    except Exception:
        x = default
    if lo is not None: x = max(lo, x)
    if hi is not None: x = min(hi, x)
    return x

def page_meta(page, page_size, total, returned=None):
    return {"page": page, "page_size": page_size, "total": total, "total_pages": max(1, math.ceil(total / page_size)) if page_size else 1, "has_more": page * page_size < total,
            **({"returned": returned} if returned is not None else {}), "as_of": config.iso()}

def _platform_clause(platforms, col="p.platform"):
    if not platforms: return "", []
    norm = [x.lower().replace("_", "-").replace(" ", "-") for x in platforms]
    return f" AND LOWER(REPLACE({col},' ','-')) IN (%s)" % ",".join("?" * len(norm)), norm

def _tags_clause(tags, alias="p"):
    if not tags: return "", []
    return f" AND EXISTS (SELECT 1 FROM targets tt, json_each(tt.scope_tags) je WHERE tt.program_id={alias}.id AND tt.is_active=1 AND (je.value IN (%s) OR EXISTS (SELECT 1 FROM json_each(tt.language_tags) jl WHERE jl.value IN (%s))))" % (",".join("?" * len(tags)), ",".join("?" * len(tags))), [t.lower() for t in tags] * 2

def _target_tags_clause(tags):
    if not tags: return "", []
    return " AND (EXISTS (SELECT 1 FROM json_each(t.scope_tags) je WHERE je.value IN (%s)) OR EXISTS (SELECT 1 FROM json_each(t.language_tags) jl WHERE jl.value IN (%s)))" % (",".join("?" * len(tags)), ",".join("?" * len(tags))), [x.lower() for x in tags] * 2

def _free_clause(free_only):
    if not free_only: return "", []
    return " AND (p.fee_amount IS NULL OR p.fee_amount=0)", []

# ---- programs -------------------------------------------------------------------------------
def list_programs(con, platforms=None, tags=None, search=None, updated_since=None, page=1, page_size=100, kind=None, reward_status=None, status=None,
                  include_unlisted=False, free_submission_only=False, competition=None, full=False, order="name"):
    where = "1=1" if include_unlisted else "p.listed=1"
    args = []
    c, a = _platform_clause(platforms); where += c; args += a
    c, a = _tags_clause(tags); where += c; args += a
    c, a = _free_clause(free_submission_only); where += c; args += a
    if search:
        s = f"%{search.lower()}%"
        where += " AND (LOWER(p.name) LIKE ? OR LOWER(p.handle) LIKE ? OR LOWER(p.platform) LIKE ? OR LOWER(p.id) LIKE ? OR EXISTS (SELECT 1 FROM targets tt WHERE tt.program_id=p.id AND tt.is_active=1 AND LOWER(tt.identifier) LIKE ?))"
        args += [s, s, s, s, s]
    if updated_since:
        where += " AND (p.last_updated>=? OR p.targets_updated_at>=?)"; args += [updated_since, updated_since]
    if kind:
        where += " AND p.program_kind=?"; args.append(kind)
    if reward_status:
        where += " AND p.reward_status=?"; args.append(reward_status)
    if status:
        where += " AND p.program_status=?"; args.append(status)
    total = con.execute(f"SELECT COUNT(*) FROM programs p WHERE {where}", args).fetchone()[0]
    ob = {"name": "LOWER(p.name)", "updated": "p.last_updated DESC", "added": "p.added_at DESC", "launch": "p.launch_at DESC", "scope": "p.targets_updated_at DESC"}.get(order, "LOWER(p.name)")
    rows = con.execute(f"SELECT p.* FROM programs p WHERE {where} ORDER BY {ob} LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size]).fetchall()
    items = [model.program_obj(con, r, full=full) for r in rows]
    if competition:
        want = [x.lower() for x in competition]
        items = [p for p in items if p["competition"]["level"] in want]
    return {"items": items, "page": page_meta(page, page_size, total, len(items))}

def program_row(con, pid):
    if pid is None: return None
    r = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
    if r: return r
    r = con.execute("SELECT * FROM programs WHERE LOWER(id)=LOWER(?)", (pid,)).fetchone()
    if r: return r
    if ":" not in pid:
        rows = con.execute("SELECT * FROM programs WHERE LOWER(handle)=LOWER(?) OR LOWER(name)=LOWER(?)", (pid, pid)).fetchall()
        if len(rows) == 1: return rows[0]
    return None

def resolve_program(con, text, limit=10):
    s = f"%{text.lower()}%"
    rows = con.execute("SELECT * FROM programs WHERE LOWER(name) LIKE ? OR LOWER(handle) LIKE ? OR LOWER(id) LIKE ? ORDER BY (LOWER(name)=LOWER(?)) DESC, (LOWER(handle)=LOWER(?)) DESC, listed DESC, LOWER(name) LIMIT ?",
                       (s, s, s, text, text, limit)).fetchall()
    return [{"id": r["id"], "name": r["name"], "handle": r["handle"], "platform": r["platform"], "status": r["program_status"] if r["listed"] else "unlisted"} for r in rows]

def get_program(con, pid):
    r = program_row(con, pid)
    return model.program_obj(con, r, full=True) if r else None

# ---- program targets -----------------------------------------------------------------------
def program_targets(con, pid, eligibility="all", target_type=None, search=None, min_score=None, label=None, competition=None, page=1, page_size=250, include_inactive=False):
    prow = program_row(con, pid)
    if prow is None:
        return None
    where = "program_id=?"; args = [prow["id"]]
    if not include_inactive: where += " AND is_active=1"
    if eligibility == "eligible": where += " AND in_scope=1 AND eligible_for_bounty=1"
    elif eligibility == "ineligible": where += " AND (in_scope=0 OR eligible_for_bounty=0)"
    if target_type: where += " AND target_type=?"; args.append(target_type)
    if search:
        s = f"%{search.lower()}%"; where += " AND (LOWER(identifier) LIKE ? OR LOWER(display_name) LIKE ?)"; args += [s, s]
    rows = con.execute(f"SELECT * FROM targets WHERE {where} ORDER BY in_scope DESC, added_at DESC, id", args).fetchall()
    out = [model.target_obj(con, t, prow) for t in rows]
    if min_score is not None: out = [o for o in out if ((o.get("score") or {}).get("value") or 0) >= min_score]
    if label: out = [o for o in out if (o.get("score") or {}).get("label") == label]
    if competition: out = [o for o in out if (o.get("competition") or {}).get("level") == competition.lower()]
    total = len(out)
    pageo = out[(page - 1) * page_size: page * page_size]
    types = sorted(set(r["target_type"] for r in rows if r["target_type"]))
    return {"program": model.program_ref(prow), "items": pageo, "page": {**page_meta(page, page_size, total, len(pageo)), "eligibility": eligibility, "target_types": types, "scope_updated_at": prow["targets_updated_at"]}}

# ---- target search / export -------------------------------------------------------------------
def _target_where(q, match_mode, platforms, tags, include_out_of_scope, include_ineligible, program_ids=None):
    where = "t.is_active=1 AND p.listed=1"; args = []
    if not include_out_of_scope: where += " AND t.in_scope=1"
    if not include_ineligible: where += " AND t.eligible_for_bounty=1"
    if q:
        ql = q.lower().strip()
        if match_mode == "exact":
            where += " AND (LOWER(t.identifier)=? OR LOWER(t.normalized_identifier)=? OR t.norm=?)"; args += [ql, ql, ql.lstrip("*.")]
        elif match_mode == "suffix":
            where += " AND (LOWER(t.normalized_identifier) LIKE ? OR LOWER(t.normalized_identifier)=? OR LOWER(t.identifier) LIKE ?)"; args += [f"%.{ql.lstrip('*.')}", ql.lstrip("*."), f"%{ql}"]
        elif match_mode == "wildcard":
            pat = ql.replace("*", "%"); where += " AND (LOWER(t.identifier) LIKE ? OR LOWER(t.normalized_identifier) LIKE ?)"; args += [pat, pat]
        else:
            where += " AND (LOWER(t.identifier) LIKE ? OR LOWER(t.display_name) LIKE ? OR LOWER(t.normalized_identifier) LIKE ?)"; args += [f"%{ql}%"] * 3
    c, a = _platform_clause(platforms); where += c; args += a
    c, a = _target_tags_clause(tags); where += c; args += a
    if program_ids:
        where += " AND p.id IN (%s)" % ",".join("?" * len(program_ids)); args += program_ids
    return where, args

def _prow_cache(con):
    cache = {}
    def get(pid):
        if pid not in cache:
            cache[pid] = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
        return cache[pid]
    return get, cache

def search_targets(con, q=None, match_mode="contains", platforms=None, tags=None, include_out_of_scope=False, include_ineligible=False, page=1, page_size=50):
    where, args = _target_where(q, match_mode, platforms, tags, include_out_of_scope, include_ineligible)
    total = con.execute(f"SELECT COUNT(*) FROM targets t JOIN programs p ON p.id=t.program_id WHERE {where}", args).fetchone()[0]
    rows = con.execute(f"SELECT t.*, p.id AS pid FROM targets t JOIN programs p ON p.id=t.program_id WHERE {where} ORDER BY t.added_at DESC, t.id LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size]).fetchall()
    prow, _ = _prow_cache(con)
    items = [model.search_row(con, t, prow(t["pid"])) for t in rows]
    return {"items": items, "page": {**page_meta(page, page_size, total, len(items)), "q": q, "match_mode": match_mode}}

def export_targets(con, program_ids=None, platforms=None, tags=None, tiers=None, include_out_of_scope=False, include_ineligible=False, fmt="json", limit=500):
    limit = min(500, limit or 500)
    if program_ids:
        where, args = _target_where(None, None, None, None, include_out_of_scope, include_ineligible, program_ids)
    else:
        where, args = _target_where(None, None, platforms, tags, include_out_of_scope, include_ineligible)
        if tiers:
            ids = [p["id"] for p in leads(con, None, platforms, tags, None, 1, 5000, tiers=tiers)["items"]]
            where += (" AND p.id IN (%s)" % ",".join("?" * len(ids))) if ids else " AND 0"; args += ids
    total = con.execute(f"SELECT COUNT(*) FROM targets t JOIN programs p ON p.id=t.program_id WHERE {where}", args).fetchone()[0]
    rows = con.execute(f"SELECT t.*, p.id pid FROM targets t JOIN programs p ON p.id=t.program_id WHERE {where} ORDER BY p.id, t.id LIMIT ?", args + [limit]).fetchall()
    prow, cache = _prow_cache(con)
    items = [model.export_row(t, prow(t["pid"])) for t in rows]
    res = {"items": items, "format": fmt, "generated_at": config.iso(), "page": {"returned": len(items), "limit": limit, "total": total, "has_more": total > len(items), "programs": len(cache)}}
    if fmt == "csv":
        import csv as _csv, io
        buf = io.StringIO(); cols = ["program_id", "platform", "program_handle", "program_name", "program_status", "type", "identifier", "normalized", "in_scope", "bounty_eligible", "wildcard", "active", "tags", "languages", "source_url", "added_at"]
        w = _csv.writer(buf); w.writerow(cols)
        def cell(v):
            s = ",".join(str(x) for x in v) if isinstance(v, list) else ("" if v is None else str(v))
            return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s   # spreadsheet formula guard, applied after joining list columns
        for o in items:
            w.writerow([cell(o[c]) for c in cols])
        res["csv"] = buf.getvalue()
    return res

# ---- target changes ---------------------------------------------------------------------------
def target_changes(con, change_type=None, include_removed=False, include_ineligible=False, include_out_of_scope=False, search=None, platforms=None, tags=None, min_score=None, label=None,
                   competition=None, page=1, page_size=100, after_id=None, free_submission_only=False, since_ts=None, program_id=None):
    types = [change_type] if change_type else (["added", "readded", "updated"] + (["removed"] if include_removed else []))
    where = "c.change_type IN (%s)" % ",".join("?" * len(types)); args = list(types)
    if not include_out_of_scope: where += " AND t.in_scope=1"
    if not include_ineligible: where += " AND t.eligible_for_bounty=1"
    if search:
        s = f"%{search.lower()}%"; where += " AND (LOWER(t.identifier) LIKE ? OR LOWER(t.display_name) LIKE ? OR LOWER(p.name) LIKE ? OR LOWER(p.handle) LIKE ?)"; args += [s, s, s, s]
    c, a = _platform_clause(platforms); where += c; args += a
    c, a = _free_clause(free_submission_only); where += c; args += a
    c, a = _target_tags_clause(tags); where += c; args += a
    if since_ts: where += " AND c.changed_at>=?"; args.append(since_ts)
    if program_id: where += " AND c.program_id=?"; args.append(program_id)
    base = f"FROM target_changes c JOIN targets t ON t.id=c.target_id JOIN programs p ON p.id=c.program_id WHERE {where}"
    prow, _ = _prow_cache(con)
    def render(rows):
        out = []
        for c_ in rows:
            t = con.execute("SELECT * FROM targets WHERE id=?", (c_["target_id"],)).fetchone()
            if t is None: continue
            o = model.change_row(con, c_, t, prow(c_["program_id"]))
            sc = o["target"].get("score") or {}
            if min_score is not None and (sc.get("value") or 0) < min_score: continue
            if label and sc.get("label") != label: continue
            if competition and (o["target"].get("competition") or {}).get("level") != competition.lower(): continue
            out.append(o)
        return out
    filters = {"types": types, "include_removed": include_removed, "include_ineligible": include_ineligible, "include_out_of_scope": include_out_of_scope, "since": since_ts, "last_sync_at": db.kv_get(con, "last_sync_at")}
    if after_id is not None:
        rows = con.execute(f"SELECT c.* {base} AND c.id>? ORDER BY c.id ASC LIMIT ?", args + [int(after_id), page_size]).fetchall()
        items = render(rows)
        return {"items": items, "page": {"mode": "cursor", "after_id": int(after_id), "next_after_id": rows[-1]["id"] if rows else int(after_id), "has_more": len(rows) == page_size, "page_size": page_size,
                                         "returned": len(items), "scanned": len(rows), "as_of": config.iso(), **filters}, "facets": {"platforms": [], "tags": []}}
    total = con.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = con.execute(f"SELECT c.* {base} ORDER BY c.changed_at DESC, c.id DESC LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size]).fetchall()
    items = render(rows)
    fp = [{"value": r[0], "count": r[1]} for r in con.execute(f"SELECT p.platform, COUNT(*) {base} GROUP BY p.platform ORDER BY 2 DESC", args)]
    ft = [{"value": r[0], "count": r[1]} for r in con.execute(f"SELECT jf.value, COUNT(*) {base.replace('WHERE', ', json_each(t.scope_tags) jf WHERE', 1)} GROUP BY jf.value ORDER BY 2 DESC LIMIT 40", args)]
    return {"items": items, "page": {"mode": "page", **page_meta(page, page_size, total, len(items)), **filters}, "facets": {"platforms": fp, "tags": ft}}

# ---- repository pushes ---------------------------------------------------------------------------
def repo_changes(con, search=None, platforms=None, tags=None, free_submission_only=False, include_targets=True, page=1, page_size=50, after_id=None, since_ts=None, kinds=None, program_id=None):
    where = "1=1"; args = []
    extra = ""
    if platforms:
        c, a = _platform_clause(platforms); extra += c; args += a
    if free_submission_only:
        c, a = _free_clause(True); extra += c
    c, a = _target_tags_clause(tags); extra += c; args += a
    if program_id: extra += " AND p.id=?"; args.append(program_id)
    if search:
        s = f"%{search.lower()}%"; extra += " AND (LOWER(r.full_name) LIKE ? OR LOWER(p.name) LIKE ? OR LOWER(p.handle) LIKE ? OR LOWER(t.identifier) LIKE ? OR LOWER(rc.ref_name) LIKE ?)"; args += [s, s, s, s, s]
    where += " AND EXISTS (SELECT 1 FROM targets t JOIN programs p ON p.id=t.program_id WHERE t.repo_url=r.url AND t.is_active=1 AND p.listed=1" + extra + ")"
    if since_ts: where += " AND rc.observed_at>=?"; args.append(since_ts)
    if kinds: where += " AND rc.push_kind IN (%s)" % ",".join("?" * len(kinds)); args += list(kinds)
    base = f"FROM repo_changes rc JOIN repos r ON r.id=rc.repo_id WHERE {where}"
    def render(rows):
        return [model.push_row(con, rc, con.execute("SELECT * FROM repos WHERE id=?", (rc["repo_id"],)).fetchone(), include_targets) for rc in rows]
    if after_id is not None:
        rows = con.execute(f"SELECT rc.* {base} AND rc.id>? ORDER BY rc.id ASC LIMIT ?", args + [int(after_id), page_size]).fetchall()
        return {"items": render(rows), "page": {"mode": "cursor", "after_id": int(after_id), "next_after_id": rows[-1]["id"] if rows else int(after_id), "has_more": len(rows) == page_size,
                                                "page_size": page_size, "returned": len(rows), "include_targets": include_targets, "as_of": config.iso()}}
    total = con.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = con.execute(f"SELECT rc.* {base} ORDER BY rc.observed_at DESC, rc.id DESC LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size]).fetchall()
    return {"items": render(rows), "page": {"mode": "page", **page_meta(page, page_size, total, len(rows)), "include_targets": include_targets}}

# ---- leads --------------------------------------------------------------------------------------
def leads(con, tier, platforms=None, tags=None, search=None, page=1, page_size=100, updated_since=None, tiers=None, free_submission_only=False):
    tiers = tiers or ([tier] if tier else list(TIERS))
    where = "p.listed=1 AND p.is_active=1"; args = []
    c, a = _platform_clause(platforms); where += c; args += a
    c, a = _tags_clause(tags); where += c; args += a
    c, a = _free_clause(free_submission_only); where += c; args += a
    if search:
        s = f"%{search.lower()}%"; where += " AND (LOWER(p.name) LIKE ? OR LOWER(p.handle) LIKE ? OR LOWER(p.platform) LIKE ?)"; args += [s, s, s]
    if updated_since: where += " AND p.last_updated>=?"; args.append(updated_since)
    added30 = {r[0]: r[1] for r in con.execute("SELECT program_id, COUNT(*) FROM target_changes WHERE change_type IN ('added','readded') AND changed_at>=? GROUP BY program_id", (model.since("30d"),))}
    # Programs picked up by a platform's first listing were not "added" then: they predate tracking (no first_seen event), so only the launch date counts.
    seen_live = {r[0] for r in con.execute("SELECT DISTINCT program_id FROM program_events WHERE change_type='first_seen'")}
    scored = []
    for r in con.execute(f"SELECT * FROM programs p WHERE {where}", args):
        ld = intel.lead({"added_at": r["added_at"] if r["id"] in seen_live else None, "launch_at": r["launch_at"], "report_count": r["report_count"], "bounty_max": r["bounty_max"], "reward_status": r["reward_status"], "program_kind": r["program_kind"]}, added30.get(r["id"], 0))
        if ld["tier"] in tiers:
            scored.append((ld["score"], r, ld))
    scored.sort(key=lambda x: -x[0])
    total = len(scored)
    items = [{**model.program_obj(con, r, full=False), "lead": ld} for sc, r, ld in scored[(page - 1) * page_size: page * page_size]]
    return {"items": items, "page": {**page_meta(page, page_size, total, len(items)), "tier": tier, "tiers": tiers}}

# ---- capabilities / status ----------------------------------------------------------------------
def capabilities(con):
    """What this instance serves: routes, page-size limits, sync state."""
    endpoints = {"programs": ("GET", "/api/programs", 100), "program": ("GET", "/api/programs/{id}", None), "program_targets": ("GET", "/api/programs/{id}/targets", 500),
                 "program_payouts": ("GET", "/api/programs/{id}/payouts", None), "program_history": ("GET", "/api/programs/{id}/history", 200), "target_search": ("GET", "/api/targets/search", 500),
                 "target_export": ("POST", "/api/targets/export", 500), "target_changes": ("GET", "/api/changes/targets", 100), "repo_changes": ("GET", "/api/changes/repos", 100),
                 "leads": ("GET", "/api/leads/{tier}", 100), "rankings": ("GET", "/api/rankings/{view}", 100), "history": ("GET", "/api/history", 200), "status": ("GET", "/api/status", None)}
    return {"version": config.VERSION, "authentication": {"scheme": "none", "note": "local instance; no credentials required"},
            "endpoints": {k: {"method": m, "path": path, "enabled": True, **({"max_page_size": mx} if mx else {})} for k, (m, path, mx) in endpoints.items()},
            "vocabulary": {"tiers": TIERS, "score_labels": SCORE_LABELS, "competition": COMPETITION, "change_types": CHANGE_TYPES},
            "sync": {"last_sync_at": db.kv_get(con, "last_sync_at"), "tracking_since": db.kv_get(con, "tracking_since")}}

def status(con):
    from .collectors import load_all
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("programs", "targets", "target_changes", "repos", "repo_changes", "program_events", "payout_observations")}
    last = con.execute("SELECT started_at, finished_at, status, stats FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    live = set(load_all().keys())
    health = collector_health(con)
    names = [r[0] for r in con.execute("SELECT platform FROM programs WHERE listed=1 GROUP BY platform ORDER BY COUNT(*) DESC")]
    names += sorted(p for p in live if p not in names)
    counts_by = {r[0]: (r[1], r[2]) for r in con.execute("SELECT platform, COUNT(*), MAX(last_seen) FROM programs WHERE listed=1 GROUP BY platform")}
    plats = [{"platform": p, "programs": counts_by.get(p, (0, None))[0], "last_seen": counts_by.get(p, (0, None))[1], "collector": p in live, **health.get(p, {"last_ok_at": None, "last_error": None, "last_run_at": None, "stale": p in live})} for p in names]
    last_sync = db.row_to_dict(last) if last else None
    if last_sync and isinstance(last_sync.get("stats"), str):
        try: last_sync["stats"] = json.loads(last_sync["stats"])
        except ValueError: pass
    return {"version": config.VERSION, "counts": counts, "last_sync": last_sync, "platforms": plats, "collectors": sorted(live),
            "stale_platforms": [p["platform"] for p in plats if p["collector"] and p["stale"]],
            "tracking_since": db.kv_get(con, "tracking_since"), "last_sync_at": db.kv_get(con, "last_sync_at"), "db": config.DB_PATH}

def collector_health(con):
    """Per platform: last successful run, last error, stale flag (no success within STALE_AFTER_SECONDS)."""
    out = {}
    cutoff = config.iso(config.utcnow() - dt.timedelta(seconds=config.STALE_AFTER_SECONDS))
    for r in con.execute("SELECT platform, MAX(CASE WHEN ok=1 THEN finished_at END) last_ok, MAX(finished_at) last_run FROM collector_runs GROUP BY platform"):
        last = con.execute("SELECT ok, error FROM collector_runs WHERE platform=? ORDER BY id DESC LIMIT 1", (r["platform"],)).fetchone()
        out[r["platform"]] = {"last_ok_at": r["last_ok"], "last_run_at": r["last_run"], "last_error": None if last["ok"] else last["error"], "stale": not r["last_ok"] or r["last_ok"] < cutoff}
    return out
