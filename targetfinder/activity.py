"""Activity rankings (scope / rewards / updates / status / payouts / response / all_changes) computed from stored events."""
import datetime as dt, json
from . import db, config, model

TABS = {
    "payouts": {"periods": ["24h", "7d", "30d", "90d", "all_time"], "sorts": ["paid", "total", "generosity", "last_increase"], "default_sort": "paid"},
    "value": {"periods": [], "sorts": [], "default_sort": None},
    "scope": {"periods": ["7d", "30d", "90d"], "sorts": ["net", "added", "removed", "last_change", "in_scope_now", "largest_update"], "default_sort": "net"},
    "rewards": {"periods": ["7d", "30d", "90d"], "sorts": ["changed", "change_pct", "last_change"], "default_sort": "change_pct"},
    "updates": {"periods": ["7d", "30d", "90d"], "sorts": ["last_change", "active_days", "changes"], "default_sort": "last_change"},
    "reports": {"periods": [], "sorts": [], "default_sort": None},
    "response": {"periods": ["30d", "90d"], "sorts": ["first_response", "last_resolved"], "default_sort": "first_response"},
    "status": {"periods": ["7d", "30d", "90d"], "sorts": ["newest", "oldest"], "default_sort": "newest"},
    "all_changes": {"periods": ["7d", "30d", "90d"], "sorts": ["newest", "changes"], "default_sort": "newest"},
}
LABELS = ["recently_updated", "high_activity", "large_scope_change", "scope_expanded", "scope_reduced", "rewards_increased", "rewards_decreased", "payout_increase_observed", "new_program", "program_returned", "program_paused", "program_resumed", "program_left"]

def capabilities(con):
    tabs = []
    for tid, t in TABS.items():
        views = []
        for per in t["periods"]:
            views.append({"basis": "tracked" if tid == "payouts" else None, "sorts": t["sorts"], "period": per, "precision": "exact" if tid == "payouts" else None, "default_sort": t["default_sort"],
                          "provider_count": 7, "provider_counts": {s: 7 for s in t["sorts"]}, "sort_directions": {s: ["asc", "desc"] for s in t["sorts"]}, "default_direction": "desc",
                          "provider_counts_by_direction": {s: {"asc": 7, "desc": 7} for s in t["sorts"]}})
        tabs.append({"id": tid, "views": views, "default_view": views[0] if views else None})
    plats = [r[0] for r in con.execute("SELECT DISTINCT platform FROM programs ORDER BY 1")]
    tags = [r[0] for r in con.execute("SELECT DISTINCT value FROM targets, json_each(targets.scope_tags) WHERE is_active=1 ORDER BY 1")]
    return {"tabs": tabs, "facets": {"tags": tags, "platforms": plats, "program_types": ["paid", "vdp"], "opportunity_tiers": ["elite", "hot", "strong", "potential"]},
            "labels": LABELS, "listing": {"enabled": True}, "version": config.VERSION, "derived_signals_enabled": True, "listing_payouts_enabled": True,
            "snapshot": snapshot()}

def snapshot():
    now = config.utcnow()
    return {"snapshot_id": now.strftime("%Y%m%d%H") + "0" * 54, "as_of": config.iso(now), "expires_at": config.iso(now + dt.timedelta(minutes=15)), "policy_version": "1", "poll_seconds": 900}

def _filters(platforms, tags, program_types):
    where = ["p.listed=1"]; args = []
    if platforms:
        where.append("LOWER(p.platform) IN (%s)" % ",".join("?" * len(platforms))); args += [x.lower() for x in platforms]
    if program_types:
        pts = []
        for pt in program_types:
            if pt == "vdp": pts.append("p.program_kind='vdp'")
            elif pt == "paid": pts.append("p.reward_status='paid'")
        if pts: where.append("(" + " OR ".join(pts) + ")")
    if tags:
        where.append("EXISTS (SELECT 1 FROM targets t, json_each(t.scope_tags) je WHERE t.program_id=p.id AND t.is_active=1 AND je.value IN (%s))" % ",".join("?" * len(tags))); args += tags
    return " AND ".join(where), args

def _row(con, prow, extra, labels_needed=True):
    lab = model.labels_for(con, prow["id"]) if labels_needed else []
    out = dict(extra)
    out["program"] = model.program_activity_block(con, prow)
    out["labels"] = [l["id"] for l in lab]; out["label_details"] = lab
    return out

def rankings(con, tab, period=None, sort=None, direction="desc", limit=50, platforms=None, tags=None, program_types=None, labels=None, offset=0, basis="tracked"):
    t = TABS.get(tab)
    if t is None or not t["periods"]:
        return None, "view unavailable"
    period = period or t["periods"][0]
    if period not in t["periods"]:
        return None, f"period {period} unavailable for {tab}"
    sort = sort or t["default_sort"]
    if sort not in t["sorts"]:
        return None, f"sort {sort} unavailable for {tab}"
    start = model.since(period)
    where, args = _filters(platforms, tags, program_types)
    rows = []
    if tab == "scope":
        q = f"""SELECT p.*, SUM(c.change_type IN ('added','readded')) a, SUM(c.change_type='removed') r, SUM(c.change_type='updated') u, MAX(c.changed_at) last_change
                FROM target_changes c JOIN programs p ON p.id=c.program_id WHERE c.changed_at>=? AND {where} GROUP BY p.id"""
        for r in con.execute(q, [start] + args):
            big = con.execute("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM target_changes WHERE program_id=? AND changed_at>=? GROUP BY changed_at)", (r["id"], start)).fetchone()[0] or 0
            rows.append((r, {"added": r["a"] or 0, "removed": r["r"] or 0, "updated": r["u"] or 0, "net": (r["a"] or 0) - (r["r"] or 0), "in_scope_now": model.target_counts(con, r["id"])["in_scope"], "largest_update": big, "last_changed_at": r["last_change"]}))
        key = {"net": "net", "added": "added", "removed": "removed", "last_change": "last_changed_at", "in_scope_now": "in_scope_now", "largest_update": "largest_update"}[sort]
    elif tab == "rewards":
        q = f"""SELECT p.*, e.before, e.after, e.observed_at FROM program_events e JOIN programs p ON p.id=e.program_id WHERE e.change_type='rewards_changed' AND e.observed_at>=? AND {where} ORDER BY e.observed_at"""
        byp = {}
        for r in con.execute(q, [start] + args):
            b, a = db.uj(r["before"], {}), db.uj(r["after"], {})
            byp.setdefault(r["id"], [r, b, a, r["observed_at"]])
            byp[r["id"]][2] = a; byp[r["id"]][3] = r["observed_at"]
        for pid, (r, b, a, at) in byp.items():
            bm, am = float(b.get("maximum") or 0), float(a.get("maximum") or 0)
            pct = round((am - bm) / bm * 100, 1) if bm else None
            fmt = lambda x: {"currency": x.get("currency") or "USD", "maximum": model._dec(x.get("maximum")), "maximum_usd": model._dec(x.get("maximum")), "minimum": model._dec(x.get("minimum")), "minimum_usd": model._dec(x.get("minimum")), "status": x.get("status"), "version": 1}
            rows.append((r, {"before": fmt(b), "after": fmt(a), "latest_before": fmt(b), "latest_after": fmt(a), "changed": am - bm, "change_pct": pct, "changed_at": at}))
        key = {"changed": "changed", "change_pct": "change_pct", "last_change": "changed_at"}[sort]
    elif tab == "updates":
        q = f"""SELECT p.*, COUNT(*) n, COUNT(DISTINCT substr(e.observed_at,1,10)) days, MAX(e.observed_at) last_change FROM program_events e JOIN programs p ON p.id=e.program_id
                WHERE e.observed_at>=? AND {where} GROUP BY p.id"""
        for r in con.execute(q, [start] + args):
            rows.append((r, {"changes": r["n"], "active_days": r["days"], "last_changed_at": r["last_change"]}))
        key = {"last_change": "last_changed_at", "active_days": "active_days", "changes": "changes"}[sort]
    elif tab == "status":
        q = f"""SELECT p.*, e.id eid, e.change_type ct, e.observed_at, e.before, e.after FROM program_events e JOIN programs p ON p.id=e.program_id
                WHERE e.change_type IN ('first_seen','left_listing','returned','paused','resumed') AND e.observed_at>=? AND {where}"""
        for r in con.execute(q, [start] + args):
            rows.append((r, {"event_id": f"ev-{r['eid']}", "observed_at": r["observed_at"], "sequence": r["eid"], "category": "program", "categories": ["program"], "source": "catalog",
                             "update_eligible": True, "update_categories": ["program"], "provider_revision": False, "counts": {"added": 0, "removed": 0, "updated": 1},
                             "source_url": (db.uj(r["data"], {}) or {}).get("link"), "change_type": r["ct"], "status": (db.uj(r["after"], {}) or {}).get("status"), "signals": {}}))
        key = "observed_at"; direction = "desc" if sort == "newest" else "asc"
    elif tab == "all_changes":
        q = f"""SELECT p.*, COUNT(*) n, MAX(e.observed_at) last_change, SUM(e.category='scope') sc, SUM(e.category='rewards') rw, SUM(e.category='status') st FROM program_events e JOIN programs p ON p.id=e.program_id
                WHERE e.observed_at>=? AND {where} GROUP BY p.id"""
        for r in con.execute(q, [start] + args):
            rows.append((r, {"changes": r["n"], "scope_changes": r["sc"] or 0, "reward_changes": r["rw"] or 0, "status_changes": r["st"] or 0, "last_changed_at": r["last_change"]}))
        key = {"newest": "last_changed_at", "changes": "changes"}[sort]
    elif tab == "payouts":
        q = f"""SELECT DISTINCT p.* FROM payout_observations o JOIN programs p ON p.id=o.program_id WHERE o.metric='payout_total' AND {where}"""
        for r in con.execute(q, args):
            w = model.payout_window(con, r["id"], period if period != "all_time" else "all_time")
            tot = model.fact_obj(model.latest_fact(con, r["id"], "payout_total"), "all_time", "reported")
            paid = float(w["amount"]) if w else 0.0
            if period != "all_time" and paid <= 0 and sort == "paid":
                continue
            sig = model.signals(con, r["id"], {"bounty_max": r["bounty_max"], "reward_currency": r["reward_currency"]})
            rows.append((r, {"paid": w, "total": tot, "generosity": sig.get("generosity_usd"), "last_increase_at": sig.get("last_increase_at"), "signals": sig, "_paid": paid,
                             "_total": float(tot["amount"]) if tot and tot["amount"] else 0.0, "_gen": float(sig["generosity_usd"]) if sig.get("generosity_usd") else 0.0}))
        key = {"paid": "_paid", "total": "_total", "generosity": "_gen", "last_increase": "last_increase_at"}[sort]
    elif tab == "response":
        q = f"""SELECT DISTINCT p.* FROM payout_observations o JOIN programs p ON p.id=o.program_id WHERE o.metric IN ('first_response_avg_native_90d','last_report_resolved_at') AND {where}"""
        for r in con.execute(q, args):
            fr = model.latest_fact(con, r["id"], "first_response_avg_native_90d"); lr = model.latest_fact(con, r["id"], "last_report_resolved_at")
            if fr is None and lr is None: continue
            rows.append((r, {"first_response": model.fact_obj(fr, "native_90d", "reported"), "last_resolved": model.fact_obj(lr, "current", "reported"),
                             "_fr": float(fr["amount"]) if fr and fr["amount"] is not None else 1e9, "_lr": float(lr["amount"]) if lr and lr["amount"] is not None else 0.0}))
        key = {"first_response": "_fr", "last_resolved": "_lr"}[sort]
        if sort == "first_response" and direction == "desc": direction = "asc"  # best responders first
    else:
        return None, "unavailable"
    if labels:
        want = set(labels)
        rows = [(r, x) for r, x in rows if want & set(l["id"] for l in model.labels_for(con, r["id"]))]
    rows.sort(key=lambda rx: (rx[1].get(key) is None, rx[1].get(key) or 0) if not isinstance(rx[1].get(key), str) else (False, rx[1].get(key)), reverse=(direction == "desc"))
    total = len(rows)
    page = rows[offset:offset + limit]
    out = []
    for r, x in page:
        x = {k: v for k, v in x.items() if not k.startswith("_")}
        out.append(_row(con, r, x))
    snap = snapshot()
    meta = {"as_of": snap["as_of"], "snapshot_id": snap["snapshot_id"], "total": total, "limit": limit, "next_cursor": str(offset + limit) if offset + limit < total else None, "returned": len(out)}
    caps = capabilities(con); caps.pop("snapshot", None)
    return {"tab": tab, "period": period, "basis": basis if tab == "payouts" else None, "precision": "exact" if tab == "payouts" else None, "sort": sort, "direction": direction, "rows": out,
            "capabilities": caps, "cohort_median": None, "meta": meta}, None

def program_payouts(con, prow):
    pid = prow["id"]
    p = {"bounty_max": prow["bounty_max"], "reward_currency": prow["reward_currency"]}
    snap = snapshot()
    facts = []
    for metric in ("first_response_avg_native_90d", "last_report_resolved_at", "payout_native_90d", "payout_total", "reports_received_native_90d", "reports_resolved_total", "last_submission_at"):
        f = model.latest_fact(con, pid, metric)
        if f: facts.append(model.fact_obj(f))
    windows = {per: model.payout_window(con, pid, per) for per in ("24h", "7d", "30d", "90d")}
    series = [{"observed_at": r["observed_at"], "amount": model._dec(r["amount"]), "currency": r["currency"], "metric": "payout_total", "basis": "reported"}
              for r in con.execute("SELECT amount, observed_at, currency FROM payout_observations WHERE program_id=? AND metric='payout_total' ORDER BY observed_at DESC LIMIT 200", (pid,))]
    rep = model.latest_fact(con, pid, "reports_received_native_90d")
    lab = model.labels_for(con, pid)
    fr = model.latest_fact(con, pid, "first_response_avg_native_90d"); lr = model.latest_fact(con, pid, "last_report_resolved_at")
    def resp(per):
        if per not in ("90d", "all_time"):   # the only facts we hold are native 90-day / current values
            return {"change_pct": None, "first_response_label": None, "first_response_precision": None, "first_response_seconds": None, "first_response_text": None, "first_response_window": None,
                    "last_increase_at": None, "last_resolution_basis": None, "last_resolution_label": None, "last_resolved_at": None, "observation": None}
        secs = float(fr["amount"]) * 3600 if fr and fr["amount"] is not None else None
        return {"change_pct": None, "first_response_label": "Average time to first response" if fr else None, "first_response_precision": "exact" if fr else None, "first_response_seconds": secs,
                "first_response_text": (f"{float(fr['amount']):g} hours" if fr and fr["amount"] is not None else None), "first_response_window": "native_90d" if fr else None, "last_increase_at": None,
                "last_resolution_basis": "reported" if lr else None, "last_resolution_label": "Last report resolved" if lr else None,
                "last_resolved_at": (config.iso(config.parse_ts(lr["amount"])) if lr and lr["amount"] else None), "observation": model.fact_obj(fr) if fr else None}
    return {"program": model.program_activity_block(con, prow), "as_of": snap["as_of"], "snapshot_id": snap["snapshot_id"], "expires_at": snap["expires_at"], "signals": model.signals(con, pid, p),
            "label_details": lab, "labels": [l["id"] for l in lab], "value_points": [], "payout_summary": model.payout_summary(con, pid), "facts": facts, "windows": windows,
            "last_increase_at": model.signals(con, pid, p).get("last_increase_at"), "series": series,
            "reports": {per: ({"reports": int(rep["amount"]) if (rep and per == "90d") else None, "previous": None, "change_pct": None, "total_reports": None, "last_count_change": None,
                               "basis": "reported" if (rep and per == "90d") else None, "observation": model.fact_obj(rep) if (rep and per == "90d") else None, "previous_observation": None}) for per in ("24h", "7d", "30d", "90d", "all_time")},
            "response": {per: resp(per) for per in ("24h", "7d", "30d", "90d", "all_time")}}

def history_changes(con, program_id=None, limit=50, offset=0, since_ts=None, category=None):
    where = []; args = []
    if program_id: where.append("e.program_id=?"); args.append(program_id)
    if since_ts: where.append("e.observed_at>=?"); args.append(since_ts)
    if category: where.append("e.category=?"); args.append(category)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = con.execute(f"SELECT COUNT(*) FROM program_events e{w}", args).fetchone()[0]
    out = []
    for e in con.execute(f"SELECT e.*, p.name, p.platform, p.data, p.id pid FROM program_events e JOIN programs p ON p.id=e.program_id{w} ORDER BY e.observed_at DESC, e.id DESC LIMIT ? OFFSET ?", args + [limit, offset]):
        counts = db.uj(e["counts"]) or {"added": 0, "removed": 0, "updated": 1 if e["change_type"] != "scope" else 0}
        summ = {"scope": f"Scope: {counts.get('added',0)} added, {counts.get('removed',0)} removed, {counts.get('updated',0)} updated", "rewards_changed": "Rewards: range changed",
                "first_seen": "Program: first seen", "left_listing": "Program: left listing", "returned": "Program: returned", "paused": "Program: paused", "resumed": "Program: resumed", "updated": "Program: 1 updated"}.get(e["change_type"], e["change_type"])
        out.append({"id": f"ev-{e['id']}", "program": {"id": e["pid"], "name": e["name"], "platform": e["platform"], "url": (db.uj(e["data"], {}) or {}).get("link"), "catalog_id": e["pid"]},
                    "observed_at": e["observed_at"], "published_at": e["observed_at"], "category": e["category"], "change_type": e["change_type"], "summary": summ, "counts": counts,
                    "before": db.uj(e["before"]), "after": db.uj(e["after"]), "source": e["source"], "provider_revision": False})
    if category in (None, "repositories"):
        rw = []; ra = []
        if program_id: rw.append("p.id=?"); ra.append(program_id)
        if since_ts: rw.append("rc.observed_at>=?"); ra.append(since_ts)
        rq = (" AND " + " AND ".join(rw)) if rw else ""
        for rc in con.execute(f"""SELECT rc.id, rc.observed_at, rc.ref_name, rc.before_sha, rc.after_sha, r.full_name, r.url, p.id pid, p.name, p.platform, p.data FROM repo_changes rc JOIN repos r ON r.id=rc.repo_id
                                  JOIN targets t ON t.repo_url=r.url AND t.is_active=1 JOIN programs p ON p.id=t.program_id WHERE 1=1{rq} GROUP BY rc.id, p.id ORDER BY rc.observed_at DESC LIMIT ?""", ra + [limit + offset]):
            out.append({"id": f"rc-{rc['id']}", "program": {"id": rc["pid"], "name": rc["name"], "platform": rc["platform"], "url": (db.uj(rc["data"], {}) or {}).get("link"), "catalog_id": rc["pid"]},
                        "observed_at": rc["observed_at"], "published_at": rc["observed_at"], "category": "repositories", "change_type": "push", "summary": "Repository: 1 updated",
                        "counts": {"added": 0, "removed": 0, "updated": 1}, "before": {"sha": rc["before_sha"]}, "after": {"sha": rc["after_sha"], "ref": rc["ref_name"], "repository": rc["full_name"]},
                        "source": "repository_monitor", "source_url": rc["url"], "provider_revision": False})
        out.sort(key=lambda x: x["observed_at"], reverse=True)
        out = out[:limit]
    return {"changes": out, "meta": {"has_more": offset + limit < total, "next_cursor": str(offset + limit) if offset + limit < total else None, "total": total, "tracking_since": db.kv_get(con, "tracking_since"), "as_of": config.iso(), "feed_revision": None},
            "coverage": {"partial": True, "capture_enabled": True, "message": "Coverage depends on which platforms have a collector and since when this instance has been syncing."}}
