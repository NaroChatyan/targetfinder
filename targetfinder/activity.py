"""Rankings (scope / rewards / updates / status / payouts / response / all), per-program payouts and the history feed."""
import datetime as dt
from . import db, config, model

VIEWS = {
    "scope": {"periods": ["7d", "30d", "90d"], "sorts": ["net", "added", "removed", "last_change", "in_scope_now", "largest_update"], "default_sort": "net"},
    "rewards": {"periods": ["7d", "30d", "90d"], "sorts": ["changed", "change_pct", "last_change"], "default_sort": "change_pct"},
    "updates": {"periods": ["7d", "30d", "90d"], "sorts": ["last_change", "active_days", "changes"], "default_sort": "last_change"},
    "status": {"periods": ["7d", "30d", "90d"], "sorts": ["newest", "oldest"], "default_sort": "newest"},
    "payouts": {"periods": ["24h", "7d", "30d", "90d", "all"], "sorts": ["paid", "total", "per_report", "last_increase"], "default_sort": "paid"},
    "response": {"periods": ["90d"], "sorts": ["first_response", "last_resolved"], "default_sort": "first_response"},
    "all": {"periods": ["7d", "30d", "90d"], "sorts": ["newest", "changes"], "default_sort": "newest"},
}
LABELS = ["recently_updated", "high_activity", "large_scope_change", "scope_expanded", "scope_reduced", "rewards_increased", "rewards_decreased", "payout_increase", "fee_dropped", "fee_changed",
          "new_program", "program_returned", "program_paused", "program_resumed", "program_left"]

def capabilities(con):
    plats = [r[0] for r in con.execute("SELECT DISTINCT platform FROM programs ORDER BY 1")]
    tags = [r[0] for r in con.execute("SELECT DISTINCT value FROM targets, json_each(targets.scope_tags) WHERE is_active=1 ORDER BY 1")]
    return {"views": VIEWS, "labels": LABELS, "facets": {"platforms": plats, "tags": tags, "kinds": ["bounty", "vdp", "contest"]}, "as_of": config.iso()}

def _filters(platforms, tags, kinds):
    where = ["p.listed=1"]; args = []
    if platforms:
        where.append("LOWER(REPLACE(p.platform,' ','-')) IN (%s)" % ",".join("?" * len(platforms))); args += [x.lower().replace(" ", "-") for x in platforms]
    if kinds:
        where.append("p.program_kind IN (%s)" % ",".join("?" * len(kinds))); args += list(kinds)
    if tags:
        where.append("EXISTS (SELECT 1 FROM targets t, json_each(t.scope_tags) je WHERE t.program_id=p.id AND t.is_active=1 AND je.value IN (%s))" % ",".join("?" * len(tags))); args += tags
    return " AND ".join(where), args

def rankings(con, view, period=None, sort=None, direction="desc", limit=50, platforms=None, tags=None, kinds=None, labels=None, offset=0):
    """Returns (result, error)."""
    v = VIEWS.get(view)
    if v is None:
        return None, f"unknown view {view!r}; views: {', '.join(VIEWS)}"
    period = period or v["periods"][0]
    if period not in v["periods"]:
        return None, f"period {period} unavailable for {view} (periods: {', '.join(v['periods'])})"
    sort = sort or v["default_sort"]
    if sort not in v["sorts"]:
        return None, f"sort {sort} unavailable for {view} (sorts: {', '.join(v['sorts'])})"
    start = model.since(period)
    where, args = _filters(platforms, tags, kinds)
    rows = []
    if view == "scope":
        q = f"""SELECT p.*, SUM(c.change_type IN ('added','readded')) a, SUM(c.change_type='removed') r, SUM(c.change_type='updated') u, MAX(c.changed_at) last_change
                FROM target_changes c JOIN programs p ON p.id=c.program_id WHERE c.changed_at>=? AND {where} GROUP BY p.id"""
        for r in con.execute(q, [start] + args):
            big = con.execute("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM target_changes WHERE program_id=? AND changed_at>=? GROUP BY changed_at)", (r["id"], start)).fetchone()[0] or 0
            rows.append((r, {"added": r["a"] or 0, "removed": r["r"] or 0, "updated": r["u"] or 0, "net": (r["a"] or 0) - (r["r"] or 0), "in_scope_now": model.scope_counts(con, r["id"])["in_scope"], "largest_update": big, "last_change": r["last_change"]}))
        key = {"net": "net", "added": "added", "removed": "removed", "last_change": "last_change", "in_scope_now": "in_scope_now", "largest_update": "largest_update"}[sort]
    elif view == "rewards":
        q = f"""SELECT p.*, e.before, e.after, e.observed_at FROM program_events e JOIN programs p ON p.id=e.program_id WHERE e.change_type='rewards_changed' AND e.observed_at>=? AND {where} ORDER BY e.observed_at"""
        byp = {}
        for r in con.execute(q, [start] + args):
            b, a = db.uj(r["before"], {}), db.uj(r["after"], {})
            byp.setdefault(r["id"], [r, b, a, r["observed_at"]])
            byp[r["id"]][2] = a; byp[r["id"]][3] = r["observed_at"]
        for pid, (r, b, a, at) in byp.items():
            bm, am = float(b.get("maximum") or 0), float(a.get("maximum") or 0)
            rows.append((r, {"before": {"min": model._dec(b.get("minimum")), "max": model._dec(b.get("maximum")), "currency": b.get("currency")}, "after": {"min": model._dec(a.get("minimum")), "max": model._dec(a.get("maximum")), "currency": a.get("currency")},
                             "changed": model._dec(am - bm), "change_pct": round((am - bm) / bm * 100, 1) if bm else None, "last_change": at}))
        key = {"changed": "changed", "change_pct": "change_pct", "last_change": "last_change"}[sort]
    elif view == "updates":
        q = f"""SELECT p.*, COUNT(*) n, COUNT(DISTINCT substr(e.observed_at,1,10)) days, MAX(e.observed_at) last_change FROM program_events e JOIN programs p ON p.id=e.program_id WHERE e.observed_at>=? AND {where} GROUP BY p.id"""
        for r in con.execute(q, [start] + args):
            rows.append((r, {"changes": r["n"], "active_days": r["days"], "last_change": r["last_change"]}))
        key = {"last_change": "last_change", "active_days": "active_days", "changes": "changes"}[sort]
    elif view == "status":
        q = f"""SELECT p.*, e.id eid, e.change_type ct, e.observed_at, e.after FROM program_events e JOIN programs p ON p.id=e.program_id
                WHERE e.change_type IN ('first_seen','left_listing','returned','paused','resumed') AND e.observed_at>=? AND {where}"""
        for r in con.execute(q, [start] + args):
            rows.append((r, {"event_id": r["eid"], "at": r["observed_at"], "type": r["ct"], "status": (db.uj(r["after"], {}) or {}).get("status")}))
        key = "at"; direction = "desc" if sort == "newest" else "asc"
    elif view == "all":
        q = f"""SELECT p.*, COUNT(*) n, MAX(e.observed_at) last_change, SUM(e.category='scope') sc, SUM(e.category='rewards') rw, SUM(e.category='status') st, SUM(e.category='fees') fe FROM program_events e JOIN programs p ON p.id=e.program_id
                WHERE e.observed_at>=? AND {where} GROUP BY p.id"""
        for r in con.execute(q, [start] + args):
            rows.append((r, {"changes": r["n"], "scope_changes": r["sc"] or 0, "reward_changes": r["rw"] or 0, "status_changes": r["st"] or 0, "fee_changes": r["fe"] or 0, "last_change": r["last_change"]}))
        key = {"newest": "last_change", "changes": "changes"}[sort]
    elif view == "payouts":
        q = f"""SELECT DISTINCT p.* FROM payout_observations o JOIN programs p ON p.id=o.program_id WHERE o.metric='payout_total' AND {where}"""
        for r in con.execute(q, args):
            w = model.payout_window(con, r["id"], period)
            tot = model.latest_fact(con, r["id"], "payout_total")
            paid = float(w["amount"]) if w else 0.0
            if period != "all" and paid <= 0 and sort == "paid":
                continue
            sig = model.signals(con, r["id"], {"bounty_max": r["bounty_max"]})
            rows.append((r, {"paid": model._dec(paid), "currency": (w or {}).get("currency") or (tot or {}).get("currency"), "total": model._dec(tot["amount"]) if tot and tot["amount"] is not None else None,
                             "per_report": sig.get("per_resolved_report"), "last_increase": sig.get("last_increase_at"), "_per": float(sig["per_resolved_report"] or 0)}))
        key = {"paid": "paid", "total": "total", "per_report": "_per", "last_increase": "last_increase"}[sort]
    elif view == "response":
        q = f"""SELECT DISTINCT p.* FROM payout_observations o JOIN programs p ON p.id=o.program_id WHERE o.metric IN ('first_response_avg_native_90d','last_report_resolved_at') AND {where}"""
        for r in con.execute(q, args):
            fr = model.latest_fact(con, r["id"], "first_response_avg_native_90d"); lr = model.latest_fact(con, r["id"], "last_report_resolved_at")
            if fr is None and lr is None: continue
            rows.append((r, {"first_response_hours": model._dec(fr["amount"]) if fr and fr["amount"] is not None else None, "last_resolved_at": (config.iso(config.parse_ts(lr["amount"])) if lr and lr["amount"] else None),
                             "_fr": float(fr["amount"]) if fr and fr["amount"] is not None else 1e9, "_lr": float(lr["amount"]) if lr and lr["amount"] is not None else 0.0}))
        key = {"first_response": "_fr", "last_resolved": "_lr"}[sort]
        if sort == "first_response" and direction == "desc": direction = "asc"  # best responders first
    if labels:
        want = set(labels)
        rows = [(r, x) for r, x in rows if want & set(l["id"] for l in model.labels_for(con, r["id"]))]
    rows.sort(key=lambda rx: (rx[1].get(key) is None, rx[1].get(key) or 0) if not isinstance(rx[1].get(key), str) else (False, rx[1].get(key)), reverse=(direction == "desc"))
    total = len(rows)
    items = []
    for r, x in rows[offset:offset + limit]:
        lab = model.labels_for(con, r["id"])
        items.append({"program": {**model.program_ref(r), "kind": r["program_kind"], "status": r["program_status"] if r["listed"] else "unlisted", "tags": model.scope_tags_for(con, r["id"]), "in_scope_now": model.scope_counts(con, r["id"])["in_scope"]},
                      "metrics": {k: v for k, v in x.items() if not k.startswith("_")}, "labels": [l["id"] for l in lab]})
    return {"view": view, "period": period, "sort": sort, "direction": direction, "items": items,
            "page": {"total": total, "limit": limit, "offset": offset, "returned": len(items), "next_cursor": str(offset + limit) if offset + limit < total else None, "as_of": config.iso()}}, None

def program_payouts(con, prow):
    pid = prow["id"]
    facts = [model.fact_obj(f) for f in (model.latest_fact(con, pid, m) for m in ("first_response_avg_native_90d", "last_report_resolved_at", "payout_native_90d", "payout_total", "reports_received_native_90d", "reports_resolved_total", "last_submission_at")) if f]
    windows = {per: model.payout_window(con, pid, per) for per in ("24h", "7d", "30d", "90d", "all")}
    series = [{"at": r["observed_at"], "amount": model._dec(r["amount"]), "currency": r["currency"]} for r in con.execute("SELECT amount, observed_at, currency FROM payout_observations WHERE program_id=? AND metric='payout_total' ORDER BY observed_at DESC LIMIT 200", (pid,))]
    lab = model.labels_for(con, pid)
    fr = model.latest_fact(con, pid, "first_response_avg_native_90d"); lr = model.latest_fact(con, pid, "last_report_resolved_at"); rep = model.latest_fact(con, pid, "reports_received_native_90d")
    return {"program": model.program_ref(prow), "as_of": config.iso(), "signals": model.signals(con, pid, {"bounty_max": prow["bounty_max"]}), "labels": [l["id"] for l in lab], "label_details": lab,
            "facts": facts, "windows": windows, "series": series,
            "reports": {"received_90d": int(rep["amount"]) if rep and rep["amount"] is not None else None, "observed_at": rep["observed_at"] if rep else None},
            "response": {"first_response_hours": model._dec(fr["amount"]) if fr and fr["amount"] is not None else None, "last_resolved_at": (config.iso(config.parse_ts(lr["amount"])) if lr and lr["amount"] else None)}}

def history_changes(con, program_id=None, limit=50, offset=0, since_ts=None, category=None):
    where = []; args = []
    if program_id: where.append("e.program_id=?"); args.append(program_id)
    if since_ts: where.append("e.observed_at>=?"); args.append(since_ts)
    if category and category != "repositories": where.append("e.category=?"); args.append(category)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    items = []
    total = 0
    if category != "repositories":
        total = con.execute(f"SELECT COUNT(*) FROM program_events e{w}", args).fetchone()[0]
        for e in con.execute(f"SELECT e.*, p.name, p.platform, p.data, p.handle, p.id pid FROM program_events e JOIN programs p ON p.id=e.program_id{w} ORDER BY e.observed_at DESC, e.id DESC LIMIT ? OFFSET ?", args + [limit, offset]):
            counts = db.uj(e["counts"]) or {}
            summ = {"scope": f"Scope: {counts.get('added',0)} added, {counts.get('removed',0)} removed, {counts.get('updated',0)} updated", "rewards_changed": "Rewards changed", "fee_changed": "Submission fee changed",
                    "first_seen": "Program first seen", "left_listing": "Program left the listing", "returned": "Program returned", "paused": "Program paused", "resumed": "Program resumed", "updated": "Program details updated"}.get(e["change_type"], e["change_type"])
            items.append({"id": f"ev-{e['id']}", "program": model.program_ref(e), "at": e["observed_at"], "category": e["category"], "type": e["change_type"], "summary": summ, "counts": counts or None,
                          "before": db.uj(e["before"]), "after": db.uj(e["after"]), "source": e["source"]})
    if category in (None, "repositories"):
        rw = []; ra = []
        if program_id: rw.append("p.id=?"); ra.append(program_id)
        if since_ts: rw.append("rc.observed_at>=?"); ra.append(since_ts)
        rq = (" AND " + " AND ".join(rw)) if rw else ""
        for rc in con.execute(f"""SELECT rc.id, rc.observed_at, rc.ref_name, rc.before_sha, rc.after_sha, rc.push_kind, r.full_name, r.url, r.provider, p.id, p.name, p.platform, p.handle, p.data FROM repo_changes rc JOIN repos r ON r.id=rc.repo_id
                                  JOIN targets t ON t.repo_url=r.url AND t.is_active=1 JOIN programs p ON p.id=t.program_id WHERE 1=1{rq} GROUP BY rc.id, p.id ORDER BY rc.observed_at DESC LIMIT ?""", ra + [limit + offset]):
            items.append({"id": f"rc-{rc['id']}", "program": model.program_ref(rc), "at": rc["observed_at"], "category": "repositories", "type": "push", "summary": f"Push to {rc['full_name']} ({rc['push_kind'] or 'unclassified'})",
                          "counts": None, "before": {"sha": rc["before_sha"]}, "after": {"sha": rc["after_sha"], "ref": rc["ref_name"], "repo": rc["full_name"], "compare_url": repowatch_compare(rc)}, "source": "repository_monitor"})
        items.sort(key=lambda x: x["at"], reverse=True)
        items = items[:limit]
    return {"items": items, "page": {"total": total, "limit": limit, "offset": offset, "returned": len(items), "next_cursor": str(offset + limit) if offset + limit < total else None,
                                     "tracking_since": db.kv_get(con, "tracking_since"), "as_of": config.iso()}}

def repowatch_compare(rc):
    from . import repowatch
    return repowatch.compare_url(rc["url"], rc["provider"], rc["before_sha"], rc["after_sha"])
