"""Read model: build the JSON objects served by the API, MCP server and CLI."""
import json, datetime as dt
from . import db, config, intel, repowatch

PERIODS = {"24h": 1, "7d": 7, "30d": 30, "90d": 90, "all_time": 36500}

def since(period):
    days = PERIODS.get(period, 7)
    return config.iso(config.utcnow() - dt.timedelta(days=days))

def platform_label(p):
    return p

# ---- programs -------------------------------------------------------------------------------
def scope_tags_for(con, pid):
    tags = set()
    for r in con.execute("SELECT scope_tags, language_tags, target_type, wildcard FROM targets WHERE program_id=? AND is_active=1 AND in_scope=1", (pid,)):
        tags |= set(db.uj(r["scope_tags"], [])) | set(db.uj(r["language_tags"], []))
        if r["wildcard"]: tags.add("wildcard")
    return sorted(tags)

def target_counts(con, pid):
    r = con.execute("""SELECT COUNT(*) total, SUM(in_scope=1) ins, SUM(in_scope=0) outs, SUM(in_scope=1 AND eligible_for_bounty=1) elig, SUM(wildcard=1) wc
                       FROM targets WHERE program_id=? AND is_active=1""", (pid,)).fetchone()
    return {"total": r["total"] or 0, "in_scope": r["ins"] or 0, "out_of_scope": r["outs"] or 0, "eligible_for_bounty": r["elig"] or 0, "wildcard": r["wc"] or 0}

def target_activity(con, pid):
    now = config.utcnow()
    d1, d7 = config.iso(now - dt.timedelta(days=1)), config.iso(now - dt.timedelta(days=7))
    r = con.execute("""SELECT MAX(changed_at) latest, MAX(CASE WHEN change_type IN ('added','readded') THEN changed_at END) latest_added,
                       SUM(change_type IN ('added','readded') AND changed_at>=?) a24, SUM(change_type IN ('added','readded') AND changed_at>=?) a7,
                       SUM(change_type='removed' AND changed_at>=?) r7 FROM target_changes WHERE program_id=?""", (d1, d7, d7, pid)).fetchone()
    return {"latest_change_at": r["latest"], "latest_added_at": r["latest_added"], "added_24h": r["a24"] or 0, "added_7d": r["a7"] or 0, "removed_7d": r["r7"] or 0}

def payout_summary(con, pid):
    out = {"7d": None, "30d": None, "listing_enabled": False, "sort_ranks": {"payout_7d_desc": None, "payout_7d_asc": None, "payout_30d_desc": None, "payout_30d_asc": None}}
    for per in ("7d", "30d"):
        w = payout_window(con, pid, per)
        if w: out[per] = w; out["listing_enabled"] = True
    return out

def payout_window(con, pid, period, metric="payout_total"):
    """Tracked increase of a reported counter over the window ('tracked' basis)."""
    start = since(period)
    first = con.execute("SELECT amount, observed_at, currency, source_label, source_field, source_url FROM payout_observations WHERE program_id=? AND metric=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (pid, metric, start)).fetchone()
    last = con.execute("SELECT amount, observed_at, currency, source_label, source_field, source_url FROM payout_observations WHERE program_id=? AND metric=? ORDER BY observed_at DESC LIMIT 1", (pid, metric)).fetchone()
    if last is None:
        return None
    if first is None:
        first = con.execute("SELECT amount, observed_at, currency, source_label, source_field, source_url FROM payout_observations WHERE program_id=? AND metric=? ORDER BY observed_at ASC LIMIT 1", (pid, metric)).fetchone()
    if first is None or first["amount"] is None or last["amount"] is None:
        return None
    amt = max(0.0, float(last["amount"]) - float(first["amount"]))
    return {"metric": metric, "measure": "paid_amount", "period": period, "basis": "tracked", "precision": "exact", "amount": _dec(amt), "lower_bound": None, "upper_bound": None,
            "currency": last["currency"] or "USD", "unit": "currency", "source_label": last["source_label"], "source_url": last["source_url"], "source_field": last["source_field"],
            "observed_at": last["observed_at"], "window_start": start, "window_end": config.iso(), "start_observed_at": first["observed_at"], "parser_version": 1,
            "amount_usd": _dec(amt), "fx_rate": "1"}

def _dec(v):
    if v is None: return None
    return str(int(v)) if float(v).is_integer() else str(round(float(v), 2))

def latest_fact(con, pid, metric):
    r = con.execute("SELECT * FROM payout_observations WHERE program_id=? AND metric=? ORDER BY observed_at DESC LIMIT 1", (pid, metric)).fetchone()
    return db.row_to_dict(r)

def fact_obj(r, period=None, basis=None, precision="exact"):
    if not r: return None
    return {"metric": r["metric"], "measure": {"payout_total": "paid_amount", "payout_native_90d": "paid_amount", "reports_received_native_90d": "received_count", "reports_resolved_total": "resolved_count",
                                               "first_response_avg_native_90d": "average_duration", "last_report_resolved_at": "resolved_at", "last_submission_at": "submitted_at"}.get(r["metric"], "value"),
            "period": period or r["period"], "basis": basis or r["basis"], "precision": precision, "amount": _dec(r["amount"]), "lower_bound": None, "upper_bound": None,
            "currency": r["currency"], "unit": r["unit"], "source_label": r["source_label"], "source_url": r["source_url"], "source_field": r["source_field"], "qualifiers": {},
            "observed_at": r["observed_at"], "window_start": None, "window_end": None, "start_observed_at": None, "parser_version": 1}

def signals(con, pid, p=None):
    s = {k: None for k in ("last_increase_at", "days_since_increase", "active_payout_weeks_12", "payout_weeks_12", "average_increase_usd", "increase_count_90d", "realization_ratio", "realization_pct",
                            "top_reward_usd", "top_reward_native", "top_reward_currency", "generosity_usd", "resolution_term", "lifetime_processed", "volume_90d", "volume_basis", "processed_90d",
                            "processed_basis", "processed_received_ratio", "processed_received_pct", "payout_change_pct", "observed_increase_30d_usd", "previous_increase_30d_usd", "received_7d",
                            "prior_weekly_received", "scope_net_7d", "generosity_percentile", "generosity_population", "volume_percentile", "volume_population", "realization_percentile",
                            "realization_population", "processed_received_percentile", "processed_received_population")}
    s["quiet_90d"] = False; s["generosity_population"] = 0; s["volume_population"] = 0; s["realization_population"] = 0; s["processed_received_population"] = 0
    rows = con.execute("SELECT amount, observed_at FROM payout_observations WHERE program_id=? AND metric='payout_total' ORDER BY observed_at", (pid,)).fetchall()
    incs = []
    for a, b in zip(rows, rows[1:]):
        if a["amount"] is not None and b["amount"] is not None and b["amount"] > a["amount"]:
            incs.append((b["observed_at"], b["amount"] - a["amount"]))
    if incs:
        s["last_increase_at"] = incs[-1][0]
        d = config.parse_ts(incs[-1][0]); s["days_since_increase"] = (config.utcnow() - d).days if d else None
        s90 = since("90d"); i90 = [x for x in incs if x[0] >= s90]
        s["increase_count_90d"] = len(i90); s["average_increase_usd"] = _dec(sum(x[1] for x in i90) / len(i90)) if i90 else None
        s30 = since("30d"); s["observed_increase_30d_usd"] = _dec(sum(x[1] for x in incs if x[0] >= s30))
        wk = set(config.parse_ts(x[0]).isocalendar()[:2] for x in incs if x[0] >= config.iso(config.utcnow() - dt.timedelta(weeks=12)))
        s["active_payout_weeks_12"] = len(wk); s["payout_weeks_12"] = 12
    total = latest_fact(con, pid, "payout_total"); res = latest_fact(con, pid, "reports_resolved_total"); vol = latest_fact(con, pid, "reports_received_native_90d")
    if res and res["amount"]:
        s["lifetime_processed"] = int(res["amount"]); s["resolution_term"] = "resolved"
        if total and total["amount"]:
            s["generosity_usd"] = _dec(total["amount"] / res["amount"])
    if vol: s["volume_90d"] = int(vol["amount"]); s["volume_basis"] = "reported"
    if p and p.get("bounty_max"): s["top_reward_usd"] = _dec(p["bounty_max"]); s["top_reward_native"] = _dec(p["bounty_max"]); s["top_reward_currency"] = p.get("reward_currency") or "USD"
    sn = con.execute("SELECT SUM(change_type IN ('added','readded')) - SUM(change_type='removed') FROM target_changes WHERE program_id=? AND changed_at>=?", (pid, since("7d"))).fetchone()[0]
    s["scope_net_7d"] = sn or 0
    s["quiet_90d"] = not con.execute("SELECT 1 FROM program_events WHERE program_id=? AND observed_at>=? LIMIT 1", (pid, since("90d"))).fetchone() and not incs
    return s

def labels_for(con, pid, p=None):
    out = []
    ev7 = con.execute("SELECT COUNT(*) FROM program_events WHERE program_id=? AND observed_at>=? AND change_type!='scope'", (pid, since("7d"))).fetchone()[0]
    days30 = con.execute("SELECT COUNT(DISTINCT substr(observed_at,1,10)) FROM program_events WHERE program_id=? AND observed_at>=?", (pid, since("30d"))).fetchone()[0]
    sc = con.execute("SELECT SUM(change_type IN ('added','readded')) a, SUM(change_type='removed') r, MAX(cnt) big FROM (SELECT change_type, COUNT(*) OVER (PARTITION BY changed_at) cnt FROM target_changes WHERE program_id=? AND changed_at>=?)", (pid, since("7d"))).fetchone()
    a, r, big = sc["a"] or 0, sc["r"] or 0, sc["big"] or 0
    def L(id_, family, tone, prio, chip, evidence, tab, period, sort):
        out.append({"id": id_, "family": family, "tone": tone, "priority": prio, "chip": chip, "title": chip, "evidence": evidence, "evidence_values": {},
                    "destination": {"kind": "activity", "direction": "desc", "tab": tab, "period": period, "sort": sort}, "detail_only": False})
    if ev7: L("recently_updated", "updates", "neutral", 3, "Recently updated", "Program details changed in the last 7 days.", "updates", "7d", "last_change")
    if days30 >= 4: L("high_activity", "updates", "neutral", 2, "High activity", f"Program changes were observed on {days30} days in the last 30 days.", "updates", "30d", "active_days")
    if big >= 10: L("large_scope_change", "scope", "positive", 1, "Large scope change", f"The largest scope update changed {big} entries in the last 7 days.", "scope", "7d", "largest_update")
    if a > r and a > 0: L("scope_expanded", "scope", "positive", 1, "Scope expanded", f"{a} target{'s' if a != 1 else ''} added in the last 7 days.", "scope", "7d", "net")
    if r > a: L("scope_reduced", "scope", "negative", 2, "Scope reduced", f"{r} target{'s' if r != 1 else ''} removed in the last 7 days.", "scope", "7d", "removed")
    rw = con.execute("SELECT before, after FROM program_events WHERE program_id=? AND change_type='rewards_changed' AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (pid, since("30d"))).fetchone()
    if rw:
        b, af = db.uj(rw["before"], {}), db.uj(rw["after"], {})
        if (af.get("maximum") or 0) > (b.get("maximum") or 0): L("rewards_increased", "rewards", "positive", 1, "Rewards increased", f"Maximum reward rose from {b.get('maximum')} to {af.get('maximum')} in the last 30 days.", "rewards", "30d", "change_pct")
        elif (af.get("maximum") or 0) < (b.get("maximum") or 0): L("rewards_decreased", "rewards", "negative", 2, "Rewards decreased", f"Maximum reward fell from {b.get('maximum')} to {af.get('maximum')} in the last 30 days.", "rewards", "30d", "change_pct")
    w7 = payout_window(con, pid, "7d")
    if w7 and float(w7["amount"]) > 0: L("payout_increase_observed", "payouts", "positive", 1, "Payout increase observed", f"Reported bounties paid rose by ${float(w7['amount']):,.0f} in the last 7 days.", "payouts", "7d", "paid")
    st = con.execute("SELECT change_type FROM program_events WHERE program_id=? AND change_type IN ('first_seen','returned','paused','resumed','left_listing') AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (pid, since("7d"))).fetchone()
    if st:
        m = {"first_seen": ("new_program", "New program", "Program first seen in the last 7 days."), "returned": ("program_returned", "Program returned", "Program returned to the listing in the last 7 days."),
             "paused": ("program_paused", "Program paused", "Program paused in the last 7 days."), "resumed": ("program_resumed", "Program resumed", "Program resumed in the last 7 days."),
             "left_listing": ("program_left", "Left listing", "Program left the listing in the last 7 days.")}[st["change_type"]]
        L(m[0], "status", "neutral", 2, m[1], m[2], "status", "7d", "newest")
    return out

def activity_summary(con, pid, p=None):
    s30 = since("30d")
    ev = con.execute("SELECT MAX(observed_at) last, COUNT(*) n, COUNT(DISTINCT substr(observed_at,1,10)) days FROM program_events WHERE program_id=? AND observed_at>=?", (pid, s30)).fetchone()
    sc = con.execute("SELECT SUM(change_type IN ('added','readded')) a, SUM(change_type='removed') r, SUM(change_type='updated') u FROM target_changes WHERE program_id=? AND changed_at>=?", (pid, s30)).fetchone()
    sc7 = con.execute("SELECT SUM(change_type IN ('added','readded')) - SUM(change_type='removed') net, MAX(cnt) big FROM (SELECT change_type, COUNT(*) OVER (PARTITION BY changed_at) cnt FROM target_changes WHERE program_id=? AND changed_at>=?)", (pid, since("7d"))).fetchone()
    lab = labels_for(con, pid, p)
    return {"available": True, "window_days": 30, "recent_days": 7, "last_changed_at": ev["last"], "active_days_30d": ev["days"] or 0, "changes_30d": ev["n"] or 0,
            "scope_added_30d": sc["a"] or 0, "scope_removed_30d": sc["r"] or 0, "scope_updated_30d": sc["u"] or 0, "largest_scope_change_7d": sc7["big"] or 0, "in_scope_delta_7d": sc7["net"] or 0,
            "labels": [l["id"] for l in lab], "as_of": config.iso(), "label_details": lab, "signals": signals(con, pid, p),
            "payouts": {"amount": (payout_window(con, pid, "30d") or {}).get("amount"), "basis": "tracked", "period": "30d", "last_observed_at": (latest_fact(con, pid, "payout_total") or {}).get("observed_at"), "fact": None}}

def program_core(row):
    d = db.uj(row["data"], {})
    p = {"handle": row["handle"], "name": row["name"], "date_launched": (row["launch_at"] or "")[:10] or None, "launch_at": row["launch_at"], "ends_at": d.get("ends_at"), "ends_on": (d.get("ends_at") or "")[:10] or None,
         "program_kind": row["program_kind"], "program_status": row["program_status"], "is_active": bool(row["is_active"]), "profile_picture": d.get("profile_picture"), "platform": row["platform"],
         "link": d.get("link"), "report_count": row["report_count"], "last_updated": row["last_updated"], "targets_updated_at": row["targets_updated_at"], "listed_at": (d.get("extra") or {}).get("listed_at"),
         "reward_status": row["reward_status"], "bounty_min": row["bounty_min"], "bounty_max": row["bounty_max"], "up_votes": 0, "down_votes": 0, "catalog": row["catalog"],
         "launch_at_precision": d.get("launch_at_precision"), "id": row["id"], "platform_label": row["platform"], "added_at": row["added_at"],
         "submission_cost": intel.submission_cost(row["platform"], {"amount": row["fee_amount"], "unit": row["fee_unit"], "kind": row["fee_kind"], "source": row["fee_source"]} if row["fee_source"] else None, row["fee_updated_at"] or row["last_updated"]),
         "total_votes": 0, "vote_ratio": 0.0, "net_score": 0, "star_rating": None, "review_signal": 0.0, "weighted_sentiment": 0.0, "reward_currency": row["reward_currency"]}
    if row["report_count"] is not None or d.get("report_count_source"):
        p["extra_data"] = {"report_count": row["report_count"], "report_count_source": d.get("report_count_source"), "report_count_label": d.get("report_count_label")}
    else:
        p["extra_data"] = None
    if row["catalog"] == "self_hosted" or row["platform"] == "Self-Hosted":
        ex = d.get("extra") or {}
        p.update({k: ex.get(k) for k in ("policy_urls", "policy_host", "reporting_routes", "reporting_url", "reporting_email", "verification", "verified_at", "reward_markers", "reward_types", "scope_status", "program_type", "aliases")})
        p["policy_url"] = d.get("policy_url"); p["safe_harbor"] = d.get("safe_harbor")
        p["data_capabilities"] = ex.get("data_capabilities") or {"target_inventory": True, "target_changes": False, "opportunity_scoring": False, "public_report_count": False, "submission_cost": False, "repository_changes": False}
        p["submission_cost"] = None
    else:
        p["program_type"] = (d.get("extra") or {}).get("program_type") or ("vdp" if row["program_kind"] == "vdp" else ("paid" if row["reward_status"] == "paid" else None))
    if not row["listed"]:
        p["program_status"] = "unavailable"; p["is_active"] = False
    return p

def program_obj(con, row, full=True):
    pid = row["id"]
    p = program_core(row)
    tags = scope_tags_for(con, pid)
    p["scope_tags"] = tags
    p["target_counts"] = target_counts(con, pid)
    p["target_activity"] = target_activity(con, pid)
    risk, reason = intel.program_competition_risk({"report_count": row["report_count"], "launch_at": row["launch_at"], "platform": row["platform"]}, tags)
    p["competitionRisk"], p["competitionRiskReason"] = risk, reason
    if full:
        p["payout_summary"] = payout_summary(con, pid)
        p["activity_summary"] = activity_summary(con, pid, p)
    return p

def program_summary(row):
    """Compact program block used inside target-change rows."""
    p = program_core(row)
    return {k: p.get(k) for k in ("handle", "platform", "name", "link", "bounty_min", "bounty_max", "submission_cost", "profile_picture", "date_launched", "targets_updated_at", "id")}

def program_activity_block(con, row):
    p = program_core(row)
    return {"id": row["id"], "program_key": None, "namespace": "platform" if row["catalog"] != "self_hosted" else "self_hosted", "slug": None, "platform": row["platform"], "catalog_visible": bool(row["listed"]),
            "handle": row["handle"], "name": row["name"], "url": p.get("link"), "logo": p.get("profile_picture"), "program_kind": row["program_kind"], "program_type": p.get("program_type"),
            "program_status": p["program_status"], "launch_at": row["launch_at"], "launch_precision": p.get("launch_at_precision"), "launch_date": p.get("date_launched"),
            "tags": scope_tags_for(con, row["id"]), "in_scope_now": target_counts(con, row["id"])["in_scope"], "opportunity_tier": None, "history_available": True}

# ---- targets --------------------------------------------------------------------------------
def target_base(t):
    return {"target_type": t["target_type"], "identifier": t["identifier"], "display_name": t["display_name"], "normalized_identifier": t["normalized_identifier"],
            "in_scope": bool(t["in_scope"]), "wildcard": bool(t["wildcard"]), "scope_tags": db.uj(t["scope_tags"], []), "language_tags": db.uj(t["language_tags"], []),
            "metadata": db.uj(t["metadata"], {}), "source_url": t["source_url"], "added_at": t["added_at"], "source_added_at": t["source_added_at"], "last_seen": t["last_seen"],
            "eligible_for_bounty": bool(t["eligible_for_bounty"]), "severity_score": t["severity_score"], "severity_label": t["severity_label"],
            "last_change_type": t["last_change_type"], "last_changed_at": t["last_changed_at"], "is_active": bool(t["is_active"])}

def target_with_intel(con, t, prow, pobj=None):
    o = target_base(t)
    if prow["platform"] == "Self-Hosted" or not o["in_scope"] or not o["eligible_for_bounty"]:
        return o
    prog = {"bounty_max": prow["bounty_max"], "reward_status": prow["reward_status"], "report_count": prow["report_count"], "launch_at": prow["launch_at"], "platform": prow["platform"]}
    o.update(intel.score_target({"target_type": t["target_type"], "wildcard": t["wildcard"], "scope_tags": o["scope_tags"], "in_scope": t["in_scope"], "eligible_for_bounty": t["eligible_for_bounty"],
                                 "last_changed_at": t["last_changed_at"], "added_at": t["added_at"]}, prog))
    return o

def change_target_obj(con, t, prow):
    o = target_with_intel(con, t, prow)
    o["type"] = o.pop("target_type")
    for k in ("last_change_type", "last_changed_at"):
        o.pop(k, None)
    return o

def change_row(con, c, t, prow):
    diff = db.uj(c["diff"], {}) or {}
    meta = dict(db.uj(t["metadata"], {}) or {})
    meta.setdefault("eligible_for_bounty", bool(t["eligible_for_bounty"]))
    if c["change_type"] == "readded":
        meta["reactivated"] = True
    return {"id": c["id"], "change_type": c["change_type"], "changed_at": c["changed_at"], "program": program_summary_with_risk(con, prow), "target": change_target_obj(con, t, prow),
            "metadata": meta, "diff": {k: diff.get(k) for k in ("before", "after")} if ("before" in diff or "after" in diff) else {}}

def program_summary_with_risk(con, prow):
    s = program_summary(prow)
    s["scope_tags"] = scope_tags_for(con, prow["id"])
    s["competitionRisk"], s["competitionRiskReason"] = intel.program_competition_risk({"report_count": prow["report_count"], "launch_at": prow["launch_at"], "platform": prow["platform"]}, s["scope_tags"])
    return s

def search_row(con, t, prow):
    p = program_core(prow)
    return {"program": {"id": prow["id"], "handle": prow["handle"], "name": prow["name"], "platform": prow["platform"], "catalog": prow["catalog"], "program_type": p.get("program_type"), "link": p.get("link"),
                        "policy_url": p.get("policy_url"), "scope_tags": scope_tags_for(con, prow["id"]), "added_at": prow["added_at"], "last_updated": prow["last_updated"],
                        "bounty_min": prow["bounty_min"], "bounty_max": prow["bounty_max"], "reward_status": prow["reward_status"], "reward_currency": prow["reward_currency"], "report_count": prow["report_count"],
                        **({"data_capabilities": p.get("data_capabilities")} if prow["platform"] == "Self-Hosted" else {})},
            "target": {k: v for k, v in target_base(t).items() if k not in ("last_change_type", "last_changed_at")}}

def export_row(t, prow):
    return {"program_id": prow["id"], "platform": prow["platform"], "program_handle": prow["handle"], "program_name": prow["name"], "program_status": prow["program_status"], "program_is_active": bool(prow["is_active"]),
            "catalog": prow["catalog"], "target_type": t["target_type"], "identifier": t["identifier"], "display_name": t["display_name"], "normalized_identifier": t["normalized_identifier"],
            "scope_tags": db.uj(t["scope_tags"], []), "language_tags": db.uj(t["language_tags"], []), "in_scope": bool(t["in_scope"]), "eligible_for_bounty": bool(t["eligible_for_bounty"]),
            "wildcard": bool(t["wildcard"]), "metadata": db.uj(t["metadata"], {}), "source_url": t["source_url"], "added_at": t["added_at"], "last_seen": t["last_seen"], "is_active": bool(t["is_active"])}

# ---- repositories ---------------------------------------------------------------------------
def repo_change_row(con, rc, repo, include_targets=True):
    targets = con.execute("""SELECT t.*, p.id pid, p.platform, p.handle, p.name pname, p.bounty_max, p.reward_status, p.report_count, p.launch_at FROM targets t JOIN programs p ON p.id=t.program_id
                             WHERE t.repo_url=? AND t.is_active=1 AND p.listed=1""", (repo["url"],)).fetchall()
    progs = {}
    tgt = []
    for t in targets:
        progs.setdefault(t["pid"], {"id": t["pid"], "platform": t["platform"], "handle": t["handle"], "name": t["pname"], "match_qualities": ["confirmed"]})
        if include_targets:
            o = target_with_intel(con, t, t)
            o["id"] = t["id"]; o["type"] = o.pop("target_type"); o["path_kind"] = t["path_kind"]; o["path_prefix"] = t["path_prefix"]
            o["match_quality"] = "confirmed"; o["match_reason"] = "repository_head_changed"
            tgt.append(o)
    return {"id": rc["id"], "event_type": rc["event_type"], "comparison": db.uj(rc["comparison"], {"status": "not_requested", "reason": "complete", "method": "head_only", "ancestry": "unknown", "commit_count": rc["commit_count"], "matcher_version": 2}),
            "source": rc["source"], "ref_name": rc["ref_name"], "before_sha": rc["before_sha"], "after_sha": rc["after_sha"], "observed_at": rc["observed_at"],
            "repository": {"provider": repo["provider"], "provider_host": repo["provider_host"], "full_name": repo["full_name"], "url": repo["url"]},
            "changes_url": repowatch.compare_url(repo["url"], repo["provider"], rc["before_sha"], rc["after_sha"]), "link_type": "compare",
            "push_kind": rc["push_kind"], "files_changed": db.uj(rc["files"]),
            "programs": list(progs.values()), "targets": tgt if include_targets else []}
