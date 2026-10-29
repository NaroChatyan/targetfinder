"""Query layer shared by the HTTP API, the MCP server, the CLI and the sweep."""
import json, math, re, datetime as dt
from . import db, config, model, intel, activity

LEVELS = ("elite", "hot", "strong", "potential")
LABELS = ("Elite Target", "Strong Target", "Potential Target", "Normal")

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

def _platform_clause(platforms, col="p.platform"):
    if not platforms: return "", []
    norm = []
    for x in platforms:
        lx = x.lower().replace("_", "-")
        norm.append({"bugcrowd": "bugcrowd", "self-hosted": "self-hosted", "selfhosted": "self-hosted"}.get(lx, lx))
    return f" AND LOWER(REPLACE({col},' ','-')) IN (%s)" % ",".join("?" * len(norm)), norm

def _tags_clause(tags, alias="p"):
    if not tags: return "", []
    return f" AND EXISTS (SELECT 1 FROM targets tt, json_each(tt.scope_tags) je WHERE tt.program_id={alias}.id AND tt.is_active=1 AND (je.value IN (%s) OR EXISTS (SELECT 1 FROM json_each(tt.language_tags) jl WHERE jl.value IN (%s))))" % (",".join("?" * len(tags)), ",".join("?" * len(tags))), [t.lower() for t in tags] * 2

def _free_clause(free_only):
    if not free_only: return "", []
    return " AND (p.fee_amount IS NULL OR p.fee_amount=0) AND p.platform!='Self-Hosted'", []

# ---- programs -------------------------------------------------------------------------------
def list_programs(con, platforms=None, tags=None, search=None, updated_since=None, page=1, page_size=100, program_kind=None, reward_status=None, program_status=None,
                  include_unlisted=False, free_submission_only=False, competition_risks=None, full=False, order="name"):
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
    if program_kind:
        where += " AND p.program_kind=?"; args.append(program_kind)
    if reward_status:
        where += " AND p.reward_status=?"; args.append(reward_status)
    if program_status:
        where += " AND p.program_status=?"; args.append(program_status)
    total = con.execute(f"SELECT COUNT(*) FROM programs p WHERE {where}", args).fetchone()[0]
    ob = {"name": "LOWER(p.name)", "updated": "p.last_updated DESC", "added": "p.added_at DESC", "launch": "p.launch_at DESC"}.get(order, "LOWER(p.name)")
    rows = con.execute(f"SELECT p.* FROM programs p WHERE {where} ORDER BY {ob} LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size]).fetchall()
    progs = [model.program_obj(con, r, full=full) for r in rows]
    if competition_risks:
        progs = [p for p in progs if p.get("competitionRisk") in competition_risks]
    return {"programs": progs, "meta": {"page": page, "page_size": page_size, "total": total, "total_pages": max(1, math.ceil(total / page_size)), "as_of": config.iso()}}

def program_row(con, pid):
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
    return [{"id": r["id"], "name": r["name"], "handle": r["handle"], "platform": r["platform"], "program_status": r["program_status"], "listed": bool(r["listed"])} for r in rows]

def get_program(con, pid):
    r = program_row(con, pid)
    return model.program_obj(con, r, full=True) if r else None

# ---- program targets -----------------------------------------------------------------------
def program_targets(con, pid, eligibility="all", target_type=None, search=None, min_score=None, label=None, risk=None, page=1, page_size=250, include_inactive=False):
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
    out = [model.target_with_intel(con, t, prow) for t in rows]
    if min_score is not None: out = [o for o in out if (o.get("targetOpportunityScore") or 0) >= min_score]
    if label: out = [o for o in out if o.get("targetOpportunityLabel") == label]
    if risk: out = [o for o in out if o.get("competitionRisk") == risk]
    total = len(out)
    pageo = out[(page - 1) * page_size: page * page_size]
    types = sorted(set(r["target_type"] for r in rows if r["target_type"]))
    return {"program": {"platform": prow["platform"], "handle": prow["handle"], "program_status": prow["program_status"] if prow["listed"] else "unavailable", "is_active": bool(prow["is_active"])},
            "targets": pageo, "meta": {"page": page, "page_size": page_size, "total": total, "total_pages": max(1, math.ceil(total / page_size)), "returned": len(pageo), "has_more": page * page_size < total,
                                       "eligibility": eligibility, "target_type": target_type, "search": search, "target_types": types, "target_types_complete": True, "last_updated": prow["targets_updated_at"]}}

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
    if tags:
        where += " AND (EXISTS (SELECT 1 FROM json_each(t.scope_tags) je WHERE je.value IN (%s)) OR EXISTS (SELECT 1 FROM json_each(t.language_tags) jl WHERE jl.value IN (%s)))" % (",".join("?" * len(tags)), ",".join("?" * len(tags)))
        args += [x.lower() for x in tags] * 2
    if program_ids:
        where += " AND p.id IN (%s)" % ",".join("?" * len(program_ids)); args += program_ids
    return where, args

def search_targets(con, q=None, match_mode="contains", platforms=None, tags=None, include_out_of_scope=False, include_ineligible=False, page=1, page_size=50):
    where, args = _target_where(q, match_mode, platforms, tags, include_out_of_scope, include_ineligible)
    total = con.execute(f"SELECT COUNT(*) FROM targets t JOIN programs p ON p.id=t.program_id WHERE {where}", args).fetchone()[0]
    rows = con.execute(f"SELECT t.*, p.id AS pid FROM targets t JOIN programs p ON p.id=t.program_id WHERE {where} ORDER BY t.added_at DESC, t.id LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size]).fetchall()
    cache = {}
    out = []
    for t in rows:
        prow = cache.get(t["pid"]) or con.execute("SELECT * FROM programs WHERE id=?", (t["pid"],)).fetchone(); cache[t["pid"]] = prow
        out.append(model.search_row(con, t, prow))
    return {"results": out, "meta": {"q": q, "match_mode": match_mode, "page": page, "page_size": page_size, "total": total, "total_pages": max(1, math.ceil(total / page_size)), "returned": len(out),
                                     "has_more": page * page_size < total, "include_out_of_scope": include_out_of_scope, "include_ineligible": include_ineligible, "as_of": config.iso()}}

def export_targets(con, program_ids=None, platforms=None, tags=None, opportunity_levels=None, include_out_of_scope=False, include_ineligible=False, fmt="json", limit=500):
    limit = min(500, limit or 500)
    if program_ids:
        where, args = _target_where(None, None, None, None, include_out_of_scope, include_ineligible, program_ids)
        where = where.replace("p.listed=1", "1=1") if include_ineligible else where
    else:
        where, args = _target_where(None, None, platforms, tags, include_out_of_scope, include_ineligible)
        if opportunity_levels:
            ids = [p["id"] for p in opportunities(con, None, platforms, tags, None, 1, 5000, levels=opportunity_levels)["programs"]]
            where += " AND p.id IN (%s)" % ",".join("?" * len(ids)) if ids else " AND 0"; args += ids
    total = con.execute(f"SELECT COUNT(*) FROM targets t JOIN programs p ON p.id=t.program_id WHERE {where}", args).fetchone()[0]
    rows = con.execute(f"SELECT t.*, p.id pid FROM targets t JOIN programs p ON p.id=t.program_id WHERE {where} ORDER BY p.id, t.id LIMIT ?", args + [limit]).fetchall()
    cache = {}; out = []
    for t in rows:
        prow = cache.get(t["pid"]) or con.execute("SELECT * FROM programs WHERE id=?", (t["pid"],)).fetchone(); cache[t["pid"]] = prow
        out.append(model.export_row(t, prow))
    res = {"targets": out, "format": fmt, "total": total, "generated_at": config.iso(), "meta": {"returned": len(out), "limit": limit, "has_more": total > len(out), "truncated": total > len(out), "programs_returned": len(cache)}}
    if fmt == "csv":
        import csv as _csv, io
        buf = io.StringIO(); cols = ["program_id", "platform", "program_handle", "program_name", "program_status", "catalog", "target_type", "identifier", "normalized_identifier", "in_scope", "eligible_for_bounty", "wildcard", "is_active", "scope_tags", "language_tags", "source_url", "added_at"]
        w = _csv.writer(buf); w.writerow(cols)
        for o in out:
            w.writerow([("'" + str(o[c])) if str(o[c])[:1] in ("=", "+", "-", "@") else (",".join(o[c]) if isinstance(o[c], list) else o[c]) for c in cols])
        res["csv"] = buf.getvalue()
    return res

# ---- target changes ---------------------------------------------------------------------------
def target_changes(con, change_type=None, include_removed=False, include_ineligible=False, include_out_of_scope=False, search=None, platforms=None, tags=None, min_score=None, label=None,
                   risk=None, page=1, page_size=100, after_id=None, free_submission_only=False, since_ts=None, program_id=None):
    types = [change_type] if change_type else (["added", "readded", "updated"] + (["removed"] if include_removed else []))
    where = "c.change_type IN (%s)" % ",".join("?" * len(types)); args = list(types)
    where += " AND p.platform!='Self-Hosted'"
    if not include_out_of_scope: where += " AND t.in_scope=1"
    if not include_ineligible: where += " AND t.eligible_for_bounty=1"
    if search:
        s = f"%{search.lower()}%"; where += " AND (LOWER(t.identifier) LIKE ? OR LOWER(t.display_name) LIKE ? OR LOWER(p.name) LIKE ? OR LOWER(p.handle) LIKE ?)"; args += [s, s, s, s]
    c, a = _platform_clause(platforms); where += c; args += a
    c, a = _free_clause(free_submission_only); where += c; args += a
    if tags:
        where += " AND (EXISTS (SELECT 1 FROM json_each(t.scope_tags) je WHERE je.value IN (%s)) OR EXISTS (SELECT 1 FROM json_each(t.language_tags) jl WHERE jl.value IN (%s)))" % (",".join("?" * len(tags)), ",".join("?" * len(tags)))
        args += [x.lower() for x in tags] * 2
    if since_ts: where += " AND c.changed_at>=?"; args.append(since_ts)
    if program_id: where += " AND c.program_id=?"; args.append(program_id)
    base = f"FROM target_changes c JOIN targets t ON t.id=c.target_id JOIN programs p ON p.id=c.program_id WHERE {where}"
    cache = {}
    def render(rows):
        out = []
        for c_ in rows:
            prow = cache.get(c_["program_id"]) or con.execute("SELECT * FROM programs WHERE id=?", (c_["program_id"],)).fetchone(); cache[c_["program_id"]] = prow
            t = con.execute("SELECT * FROM targets WHERE id=?", (c_["target_id"],)).fetchone()
            if t is None: continue
            o = model.change_row(con, c_, t, prow)
            tg = o["target"]
            if min_score is not None and (tg.get("targetOpportunityScore") or 0) < min_score: continue
            if label and tg.get("targetOpportunityLabel") != label: continue
            if risk and tg.get("competitionRisk") != risk: continue
            out.append(o)
        return out
    if after_id is not None:
        rows = con.execute(f"SELECT c.* {base} AND c.id>? ORDER BY c.id ASC LIMIT ?", args + [int(after_id), page_size]).fetchall()
        out = render(rows)
        nxt = rows[-1]["id"] if rows else int(after_id)
        return {"results": out, "meta": {"pagination_mode": "cursor", "page_size": page_size, "returned": len(out), "scanned": len(rows), "after_id": int(after_id), "consistency": "latest_state_cursor",
                                         "resync_required_after_retention": True, "next_after_id": nxt, "has_more": len(rows) == page_size, "change_type": change_type, "included_change_types": types,
                                         "include_removed": include_removed, "include_ineligible": include_ineligible, "include_out_of_scope": include_out_of_scope, "min_target_score": min_score,
                                         "target_opportunity_label": label, "competition_risk": risk, "sort": "date_asc", "last_sync": db.kv_get(con, "last_sync_at"), "previous_sync": None, "applied_since": since_ts},
                "facets": {"platforms": [], "tags": []}}
    total = con.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = con.execute(f"SELECT c.* {base} ORDER BY c.changed_at DESC, c.id DESC LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size]).fetchall()
    out = render(rows)
    fp = [{"value": r[0].lower().replace(" ", "-"), "label": r[0], "count": r[1]} for r in con.execute(f"SELECT p.platform, COUNT(*) {base} GROUP BY p.platform ORDER BY 2 DESC", args)]
    ft = [{"value": r[0], "label": r[0], "count": r[1]} for r in con.execute(f"SELECT jf.value, COUNT(*) {base.replace('WHERE', ', json_each(t.scope_tags) jf WHERE', 1)} GROUP BY jf.value ORDER BY 2 DESC LIMIT 40", args)]
    return {"results": out, "meta": {"page": page, "page_size": page_size, "total": total, "total_pages": max(1, math.ceil(total / page_size)), "change_type": change_type, "included_change_types": types,
                                     "include_removed": include_removed, "include_ineligible": include_ineligible, "include_out_of_scope": include_out_of_scope, "last_sync": db.kv_get(con, "last_sync_at"),
                                     "previous_sync": None, "applied_since": since_ts, "as_of": config.iso()}, "facets": {"platforms": fp, "tags": ft}}

# ---- repository changes ---------------------------------------------------------------------------
def repo_changes(con, search=None, platforms=None, tags=None, free_submission_only=False, include_targets=True, page=1, page_size=50, after_id=None, since_ts=None, include_removed=False):
    where = "1=1"; args = []
    assoc = "EXISTS (SELECT 1 FROM targets t JOIN programs p ON p.id=t.program_id WHERE t.repo_url=r.url AND t.is_active=1 AND p.listed=1{extra})"
    extra = ""
    if platforms:
        c, a = _platform_clause(platforms); extra += c; args += a
    if free_submission_only:
        c, a = _free_clause(True); extra += c
    if tags:
        extra += " AND (EXISTS (SELECT 1 FROM json_each(t.scope_tags) je WHERE je.value IN (%s)) OR EXISTS (SELECT 1 FROM json_each(t.language_tags) jl WHERE jl.value IN (%s)))" % (",".join("?" * len(tags)), ",".join("?" * len(tags)))
        args += [x.lower() for x in tags] * 2
    if search:
        s = f"%{search.lower()}%"; extra += " AND (LOWER(r.full_name) LIKE ? OR LOWER(p.name) LIKE ? OR LOWER(p.handle) LIKE ? OR LOWER(t.identifier) LIKE ? OR LOWER(rc.ref_name) LIKE ?)"; args += [s, s, s, s, s]
    where += " AND " + assoc.format(extra=extra)
    if since_ts: where += " AND rc.observed_at>=?"; args.append(since_ts)
    base = f"FROM repo_changes rc JOIN repos r ON r.id=rc.repo_id WHERE {where}"
    def render(rows):
        return [model.repo_change_row(con, rc, con.execute("SELECT * FROM repos WHERE id=?", (rc["repo_id"],)).fetchone(), include_targets) for rc in rows]
    if after_id is not None:
        rows = con.execute(f"SELECT rc.* {base} AND rc.id>? ORDER BY rc.id ASC LIMIT ?", args + [int(after_id), page_size]).fetchall()
        return {"results": render(rows), "facets": {"platforms": [], "tags": []}, "available_facets": {"platforms": [], "tags": []},
                "meta": {"pagination_mode": "cursor", "page_size": page_size, "returned": len(rows), "after_id": int(after_id), "consistency": "live_cursor", "resync_required_after_retention": True,
                         "next_after_id": rows[-1]["id"] if rows else int(after_id), "has_more": len(rows) == page_size, "include_targets": include_targets, "include_removed": include_removed,
                         "include_ineligible": False, "include_out_of_scope": False}}
    total = con.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = con.execute(f"SELECT rc.* {base} ORDER BY rc.observed_at DESC, rc.id DESC LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size]).fetchall()
    return {"results": render(rows), "facets": {"platforms": [], "tags": []}, "available_facets": {"platforms": [], "tags": []},
            "meta": {"pagination_mode": "page", "page": page, "page_size": page_size, "total": total, "total_pages": max(1, math.ceil(total / page_size)), "returned": len(rows), "has_more": page * page_size < total,
                     "include_targets": include_targets, "include_removed": include_removed, "include_ineligible": False, "include_out_of_scope": False, "as_of": config.iso()}}

# ---- opportunities --------------------------------------------------------------------------------
def opportunities(con, level, platforms=None, tags=None, search=None, page=1, page_size=100, updated_since=None, levels=None, program_types=None):
    levels = levels or ([level] if level else list(LEVELS))
    where = "p.listed=1 AND p.is_active=1 AND p.platform!='Self-Hosted'"; args = []
    c, a = _platform_clause(platforms); where += c; args += a
    c, a = _tags_clause(tags); where += c; args += a
    if search:
        s = f"%{search.lower()}%"; where += " AND (LOWER(p.name) LIKE ? OR LOWER(p.handle) LIKE ? OR LOWER(p.platform) LIKE ?)"; args += [s, s, s]
    if updated_since: where += " AND p.last_updated>=?"; args.append(updated_since)
    s30 = model.since("30d")
    added30 = {r[0]: r[1] for r in con.execute("SELECT program_id, COUNT(*) FROM target_changes WHERE change_type IN ('added','readded') AND changed_at>=? AND program_id IN (SELECT id FROM programs) GROUP BY program_id", (s30,))}
    scored = []
    for r in con.execute(f"SELECT * FROM programs p WHERE {where}", args):
        tag = intel.opportunity_tag({"added_at": r["added_at"], "launch_at": r["launch_at"], "report_count": r["report_count"], "bounty_max": r["bounty_max"], "reward_status": r["reward_status"], "program_kind": r["program_kind"]}, added30.get(r["id"], 0))
        if tag["tier"] in levels:
            scored.append((tag["opportunity_score"], r, tag))
    scored.sort(key=lambda x: -x[0])
    total = len(scored)
    pg = scored[(page - 1) * page_size: page * page_size]
    out = []
    for sc, r, tag in pg:
        o = model.program_obj(con, r, full=True); o["opportunity_tag"] = tag; out.append(o)
    return {"programs": out, "meta": {"level": level, "page": page, "page_size": page_size, "total": total, "total_pages": max(1, math.ceil(total / page_size)), "activity_snapshot_id": activity.snapshot()["snapshot_id"]}}

# ---- capabilities ----------------------------------------------------------------------------------
def capabilities(con):
    """What this instance serves: routes, page-size limits, sync state."""
    endpoints = {"programs": ("GET", 100), "program_detail": ("GET", None), "program_targets": ("GET", 500), "target_search": ("GET", 500), "target_export": ("POST", 500),
                 "target_changes": ("GET", 100), "repository_changes": ("GET", 100), "opportunities": ("GET", 100), "activity_capabilities": ("GET", None), "activity_snapshot": ("GET", None),
                 "activity_rankings": ("GET", 100), "program_payouts": ("GET", None), "history_changes": ("GET", 200)}
    return {"version": config.VERSION, "authentication": {"scheme": "none", "note": "local instance; no credentials required"},
            "endpoints": {k: {"method": m, "enabled": True, **({"max_page_size": mx} if mx else {})} for k, (m, mx) in endpoints.items()},
            "sync": {"last_sync_at": db.kv_get(con, "last_sync_at"), "tracking_since": db.kv_get(con, "tracking_since")}}

def status(con):
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("programs", "targets", "target_changes", "repos", "repo_changes", "program_events", "payout_observations")}
    last = con.execute("SELECT started_at, finished_at, status, stats FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    plats = [{"platform": r[0], "programs": r[1], "live_collector": r[0] in ("HackerOne", "BugCrowd", "Intigriti", "YesWeHack", "HackenProof", "Sherlock") or (r[0] == "Immunefi" and config.ENABLE_IMMUNEFI), "last_seen": r[2]}
             for r in con.execute("SELECT platform, COUNT(*), MAX(last_seen) FROM programs WHERE listed=1 GROUP BY platform ORDER BY 2 DESC")]
    return {"counts": counts, "last_sync": db.row_to_dict(last) if last else None, "platforms": plats, "tracking_since": db.kv_get(con, "tracking_since"), "db": config.DB_PATH}
