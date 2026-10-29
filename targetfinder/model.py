"""Read model: build the JSON objects served by the API, MCP server and CLI (shapes in docs/API.md)."""
import json, datetime as dt
from . import db, config, intel, repowatch

PERIODS = {"24h": 1, "7d": 7, "30d": 30, "90d": 90, "all": 36500}

def since(period):
    return config.iso(config.utcnow() - dt.timedelta(days=PERIODS.get(period, 7)))

def _dec(v):
    if v is None: return None
    return int(v) if float(v).is_integer() else round(float(v), 2)

# ---- program building blocks -------------------------------------------------------------------
def scope_tags_for(con, pid):
    tags = set()
    for r in con.execute("SELECT scope_tags, language_tags, wildcard FROM targets WHERE program_id=? AND is_active=1 AND in_scope=1", (pid,)):
        tags |= set(db.uj(r["scope_tags"], [])) | set(db.uj(r["language_tags"], []))
        if r["wildcard"]: tags.add("wildcard")
    return sorted(tags)

def scope_counts(con, pid):
    r = con.execute("""SELECT COUNT(*) total, SUM(in_scope=1) ins, SUM(in_scope=0) outs, SUM(in_scope=1 AND eligible_for_bounty=1) elig, SUM(wildcard=1) wc
                       FROM targets WHERE program_id=? AND is_active=1""", (pid,)).fetchone()
    return {"total": r["total"] or 0, "in_scope": r["ins"] or 0, "out_of_scope": r["outs"] or 0, "bounty_eligible": r["elig"] or 0, "wildcards": r["wc"] or 0}

def scope_activity(con, pid):
    now = config.utcnow()
    d1, d7 = config.iso(now - dt.timedelta(days=1)), config.iso(now - dt.timedelta(days=7))
    r = con.execute("""SELECT MAX(changed_at) latest, MAX(CASE WHEN change_type IN ('added','readded') THEN changed_at END) latest_added,
                       SUM(change_type IN ('added','readded') AND changed_at>=?) a24, SUM(change_type IN ('added','readded') AND changed_at>=?) a7,
                       SUM(change_type='removed' AND changed_at>=?) r7 FROM target_changes WHERE program_id=?""", (d1, d7, d7, pid)).fetchone()
    return {"last_change_at": r["latest"], "last_added_at": r["latest_added"], "added_24h": r["a24"] or 0, "added_7d": r["a7"] or 0, "removed_7d": r["r7"] or 0}

def competition_for(row, tags):
    level, reason = intel.program_competition_risk({"report_count": row["report_count"], "launch_at": row["launch_at"], "platform": row["platform"]}, tags)
    return {"level": level, "reason": reason}

def rewards_block(row):
    return {"offers_bounty": row["reward_status"] == "paid" or bool(row["bounty_max"]), "min": _dec(row["bounty_min"]), "max": _dec(row["bounty_max"]), "currency": row["reward_currency"] or ("USD" if row["bounty_max"] else None),
            "status": row["reward_status"]}

def fee_block(row):
    observed = {"amount": row["fee_amount"], "unit": row["fee_unit"], "kind": row["fee_kind"], "source": row["fee_source"]} if row["fee_source"] else None
    return intel.submission_fee(row["platform"], observed, row["fee_updated_at"] or row["last_updated"])

def report_block(row):
    d = db.uj(row["data"], {})
    if row["report_count"] is None:
        return None
    return {"value": row["report_count"], "source": d.get("report_count_source"), "label": d.get("report_count_label")}

def program_ref(row):
    d = db.uj(row["data"], {})
    return {"id": row["id"], "name": row["name"], "platform": row["platform"], "handle": row["handle"], "url": d.get("link")}

def program_obj(con, row, full=True):
    pid = row["id"]; d = db.uj(row["data"], {})
    tags = scope_tags_for(con, pid)
    status = row["program_status"] if row["listed"] else "unlisted"
    p = {"id": pid, "platform": row["platform"], "handle": row["handle"], "name": row["name"], "url": d.get("link"), "logo": d.get("profile_picture"),
         "kind": row["program_kind"], "status": status, "rewards": rewards_block(row), "submission_fee": fee_block(row), "report_count": report_block(row),
         "launched_at": row["launch_at"], "ends_at": d.get("ends_at"), "first_seen_at": row["added_at"], "updated_at": row["last_updated"], "scope_updated_at": row["targets_updated_at"],
         "last_seen_at": row["last_seen"], "tags": tags, "scope_counts": scope_counts(con, pid), "scope_activity": scope_activity(con, pid), "competition": competition_for(row, tags),
         "policy_url": d.get("policy_url"), "source": row["source"]}
    if full:
        p["payouts"] = {"last_7d": (payout_window(con, pid, "7d") or {}).get("amount"), "last_30d": (payout_window(con, pid, "30d") or {}).get("amount"), "total": (latest_fact(con, pid, "payout_total") or {}).get("amount")}
        p["activity"] = activity_summary(con, pid)
    return p

def program_summary(con, row):
    """Compact program block embedded in change rows and leads."""
    tags = scope_tags_for(con, row["id"])
    return {**program_ref(row), "kind": row["program_kind"], "status": row["program_status"] if row["listed"] else "unlisted", "rewards": rewards_block(row), "submission_fee": fee_block(row),
            "report_count": report_block(row), "competition": competition_for(row, tags), "tags": tags, "scope_updated_at": row["targets_updated_at"]}

# ---- payouts ----------------------------------------------------------------------------------
def latest_fact(con, pid, metric):
    r = con.execute("SELECT * FROM payout_observations WHERE program_id=? AND metric=? ORDER BY observed_at DESC LIMIT 1", (pid, metric)).fetchone()
    return db.row_to_dict(r)

def fact_obj(r):
    if not r: return None
    return {"metric": r["metric"], "amount": _dec(r["amount"]), "currency": r["currency"], "unit": r["unit"], "source_label": r["source_label"], "source_field": r["source_field"],
            "source_url": r["source_url"], "period": r["period"], "basis": r["basis"], "observed_at": r["observed_at"]}

def payout_window(con, pid, period, metric="payout_total"):
    """Increase of a platform-published paid-out counter across the window (first and last observation inside it)."""
    start = since(period)
    q = "SELECT amount, observed_at, currency FROM payout_observations WHERE program_id=? AND metric=? {} ORDER BY observed_at {} LIMIT 1"
    first = con.execute(q.format("AND observed_at<=?", "DESC"), (pid, metric, start)).fetchone()
    last = con.execute(q.format("", "DESC"), (pid, metric)).fetchone()
    if last is None:
        return None
    if first is None:
        first = con.execute(q.format("", "ASC"), (pid, metric)).fetchone()
    if first is None or first["amount"] is None or last["amount"] is None:
        return None
    amt = max(0.0, float(last["amount"]) - float(first["amount"]))
    return {"period": period, "amount": _dec(amt), "currency": last["currency"] or "USD", "from": first["observed_at"], "to": last["observed_at"], "basis": "tracked"}

def signals(con, pid, p=None):
    s = {"last_increase_at": None, "days_since_increase": None, "increase_count_90d": 0, "average_increase": None, "increase_30d": None, "active_weeks_12": 0,
         "lifetime_resolved": None, "per_resolved_report": None, "reports_90d": None, "top_reward": None, "scope_net_7d": 0, "quiet_90d": False}
    rows = con.execute("SELECT amount, observed_at FROM payout_observations WHERE program_id=? AND metric='payout_total' ORDER BY observed_at", (pid,)).fetchall()
    incs = [(b["observed_at"], b["amount"] - a["amount"]) for a, b in zip(rows, rows[1:]) if a["amount"] is not None and b["amount"] is not None and b["amount"] > a["amount"]]
    if incs:
        s["last_increase_at"] = incs[-1][0]
        d = config.parse_ts(incs[-1][0]); s["days_since_increase"] = (config.utcnow() - d).days if d else None
        i90 = [x for x in incs if x[0] >= since("90d")]
        s["increase_count_90d"] = len(i90); s["average_increase"] = _dec(sum(x[1] for x in i90) / len(i90)) if i90 else None
        s["increase_30d"] = _dec(sum(x[1] for x in incs if x[0] >= since("30d")))
        s["active_weeks_12"] = len(set(config.parse_ts(x[0]).isocalendar()[:2] for x in incs if x[0] >= config.iso(config.utcnow() - dt.timedelta(weeks=12))))
    total = latest_fact(con, pid, "payout_total"); res = latest_fact(con, pid, "reports_resolved_total"); vol = latest_fact(con, pid, "reports_received_native_90d")
    if res and res["amount"]:
        s["lifetime_resolved"] = int(res["amount"])
        if total and total["amount"]: s["per_resolved_report"] = _dec(total["amount"] / res["amount"])
    if vol: s["reports_90d"] = int(vol["amount"])
    if p and p.get("bounty_max"): s["top_reward"] = _dec(p["bounty_max"])
    sn = con.execute("SELECT SUM(change_type IN ('added','readded')) - SUM(change_type='removed') FROM target_changes WHERE program_id=? AND changed_at>=?", (pid, since("7d"))).fetchone()[0]
    s["scope_net_7d"] = sn or 0
    s["quiet_90d"] = not con.execute("SELECT 1 FROM program_events WHERE program_id=? AND observed_at>=? LIMIT 1", (pid, since("90d"))).fetchone() and not incs
    return s

def labels_for(con, pid):
    out = []
    ev7 = con.execute("SELECT COUNT(*) FROM program_events WHERE program_id=? AND observed_at>=? AND change_type!='scope'", (pid, since("7d"))).fetchone()[0]
    days30 = con.execute("SELECT COUNT(DISTINCT substr(observed_at,1,10)) FROM program_events WHERE program_id=? AND observed_at>=?", (pid, since("30d"))).fetchone()[0]
    sc = con.execute("SELECT SUM(change_type IN ('added','readded')) a, SUM(change_type='removed') r, MAX(cnt) big FROM (SELECT change_type, COUNT(*) OVER (PARTITION BY changed_at) cnt FROM target_changes WHERE program_id=? AND changed_at>=?)", (pid, since("7d"))).fetchone()
    a, r, big = sc["a"] or 0, sc["r"] or 0, sc["big"] or 0
    def L(id_, title, evidence, view, period):
        out.append({"id": id_, "title": title, "evidence": evidence, "view": view, "period": period})
    if ev7: L("recently_updated", "Recently updated", "Program details changed in the last 7 days.", "updates", "7d")
    if days30 >= 4: L("high_activity", "High activity", f"Program changes were observed on {days30} days in the last 30 days.", "updates", "30d")
    if big >= 10: L("large_scope_change", "Large scope change", f"The largest scope update changed {big} entries in the last 7 days.", "scope", "7d")
    if a > r and a > 0: L("scope_expanded", "Scope expanded", f"{a} target{'s' if a != 1 else ''} added in the last 7 days.", "scope", "7d")
    if r > a: L("scope_reduced", "Scope reduced", f"{r} target{'s' if r != 1 else ''} removed in the last 7 days.", "scope", "7d")
    rw = con.execute("SELECT before, after FROM program_events WHERE program_id=? AND change_type='rewards_changed' AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (pid, since("30d"))).fetchone()
    if rw:
        b, af = db.uj(rw["before"], {}), db.uj(rw["after"], {})
        if (af.get("maximum") or 0) > (b.get("maximum") or 0): L("rewards_increased", "Rewards increased", f"Maximum reward rose from {b.get('maximum')} to {af.get('maximum')} in the last 30 days.", "rewards", "30d")
        elif (af.get("maximum") or 0) < (b.get("maximum") or 0): L("rewards_decreased", "Rewards decreased", f"Maximum reward fell from {b.get('maximum')} to {af.get('maximum')} in the last 30 days.", "rewards", "30d")
    w7 = payout_window(con, pid, "7d")
    if w7 and float(w7["amount"]) > 0: L("payout_increase", "Payout increase observed", f"Reported bounties paid rose by ${float(w7['amount']):,.0f} in the last 7 days.", "payouts", "7d")
    fee = con.execute("SELECT after FROM program_events WHERE program_id=? AND change_type='fee_changed' AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (pid, since("30d"))).fetchone()
    if fee:
        af = db.uj(fee["after"], {}) or {}
        L("fee_dropped" if not af.get("amount") else "fee_changed", "Submission fee dropped" if not af.get("amount") else "Submission fee changed", "Submission fee changed in the last 30 days.", "updates", "30d")
    st = con.execute("SELECT change_type FROM program_events WHERE program_id=? AND change_type IN ('first_seen','returned','paused','resumed','left_listing') AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (pid, since("7d"))).fetchone()
    if st:
        m = {"first_seen": ("new_program", "New program", "Program first seen in the last 7 days."), "returned": ("program_returned", "Program returned", "Program returned to the listing in the last 7 days."),
             "paused": ("program_paused", "Program paused", "Program paused in the last 7 days."), "resumed": ("program_resumed", "Program resumed", "Program resumed in the last 7 days."),
             "left_listing": ("program_left", "Left listing", "Program left the listing in the last 7 days.")}[st["change_type"]]
        L(m[0], m[1], m[2], "status", "7d")
    return out

def activity_summary(con, pid):
    s30 = since("30d")
    ev = con.execute("SELECT MAX(observed_at) last, COUNT(*) n, COUNT(DISTINCT substr(observed_at,1,10)) days FROM program_events WHERE program_id=? AND observed_at>=?", (pid, s30)).fetchone()
    sc = con.execute("SELECT SUM(change_type IN ('added','readded')) a, SUM(change_type='removed') r, SUM(change_type='updated') u FROM target_changes WHERE program_id=? AND changed_at>=?", (pid, s30)).fetchone()
    sc7 = con.execute("SELECT SUM(change_type IN ('added','readded')) - SUM(change_type='removed') net, MAX(cnt) big FROM (SELECT change_type, COUNT(*) OVER (PARTITION BY changed_at) cnt FROM target_changes WHERE program_id=? AND changed_at>=?)", (pid, since("7d"))).fetchone()
    lab = labels_for(con, pid)
    return {"last_changed_at": ev["last"], "active_days_30d": ev["days"] or 0, "changes_30d": ev["n"] or 0, "scope_added_30d": sc["a"] or 0, "scope_removed_30d": sc["r"] or 0,
            "scope_updated_30d": sc["u"] or 0, "largest_scope_change_7d": sc7["big"] or 0, "scope_net_7d": sc7["net"] or 0, "labels": [l["id"] for l in lab], "label_details": lab,
            "signals": signals(con, pid), "as_of": config.iso()}

# ---- targets ------------------------------------------------------------------------------------
def target_obj(con, t, prow):
    o = {"id": t["id"], "identifier": t["identifier"], "display_name": t["display_name"], "normalized": t["normalized_identifier"], "type": t["target_type"], "in_scope": bool(t["in_scope"]),
         "bounty_eligible": bool(t["eligible_for_bounty"]), "wildcard": bool(t["wildcard"]), "tags": db.uj(t["scope_tags"], []), "languages": db.uj(t["language_tags"], []),
         "severity": ({"label": t["severity_label"], "score": t["severity_score"]} if (t["severity_label"] or t["severity_score"] is not None) else None),
         "source_url": t["source_url"], "added_at": t["added_at"], "last_seen_at": t["last_seen"],
         "last_change": ({"type": t["last_change_type"], "at": t["last_changed_at"]} if t["last_change_type"] else None), "active": bool(t["is_active"]),
         "metadata": db.uj(t["metadata"], {}), "repo": ({"url": t["repo_url"], "path_kind": t["path_kind"], "path_prefix": t["path_prefix"]} if t["repo_url"] else None)}
    if o["in_scope"] and o["bounty_eligible"]:
        prog = {"bounty_max": prow["bounty_max"], "reward_status": prow["reward_status"], "report_count": prow["report_count"], "launch_at": prow["launch_at"], "platform": prow["platform"]}
        o["score"], o["competition"] = intel.score_target({"target_type": t["target_type"], "wildcard": t["wildcard"], "scope_tags": o["tags"], "in_scope": t["in_scope"], "eligible_for_bounty": t["eligible_for_bounty"],
                                                           "last_changed_at": t["last_changed_at"], "added_at": t["added_at"]}, prog)
    else:
        o["score"] = None; o["competition"] = None
    return o

def change_row(con, c, t, prow):
    diff = db.uj(c["diff"], {}) or {}
    return {"id": c["id"], "type": c["change_type"], "at": c["changed_at"], "program": program_summary(con, prow), "target": target_obj(con, t, prow),
            "diff": ({"before": diff.get("before"), "after": diff.get("after")} if ("before" in diff or "after" in diff) else None), "source": c["source"]}

def search_row(con, t, prow):
    return {"program": program_summary(con, prow), "target": target_obj(con, t, prow)}

def export_row(t, prow):
    return {"program_id": prow["id"], "platform": prow["platform"], "program_handle": prow["handle"], "program_name": prow["name"], "program_status": prow["program_status"] if prow["listed"] else "unlisted",
            "type": t["target_type"], "identifier": t["identifier"], "display_name": t["display_name"], "normalized": t["normalized_identifier"], "tags": db.uj(t["scope_tags"], []),
            "languages": db.uj(t["language_tags"], []), "in_scope": bool(t["in_scope"]), "bounty_eligible": bool(t["eligible_for_bounty"]), "wildcard": bool(t["wildcard"]),
            "source_url": t["source_url"], "added_at": t["added_at"], "last_seen_at": t["last_seen"], "active": bool(t["is_active"])}

# ---- repositories ---------------------------------------------------------------------------------
def push_row(con, rc, repo, include_targets=True):
    rows = con.execute("""SELECT t.*, p.id pid, p.platform, p.handle, p.name pname, p.bounty_max, p.reward_status, p.report_count, p.launch_at, p.data pdata FROM targets t JOIN programs p ON p.id=t.program_id
                          WHERE t.repo_url=? AND t.is_active=1 AND p.listed=1""", (repo["url"],)).fetchall()
    progs = {}; tgt = []
    for t in rows:
        progs.setdefault(t["pid"], {"id": t["pid"], "name": t["pname"], "platform": t["platform"], "url": (db.uj(t["pdata"], {}) or {}).get("link")})
        if include_targets:
            tgt.append(target_obj(con, t, t))
    return {"id": rc["id"], "at": rc["observed_at"], "repo": {"provider": repo["provider"], "host": repo["provider_host"], "name": repo["full_name"], "url": repo["url"]},
            "ref": rc["ref_name"], "before": rc["before_sha"], "after": rc["after_sha"], "compare_url": repowatch.compare_url(repo["url"], repo["provider"], rc["before_sha"], rc["after_sha"]),
            "kind": rc["push_kind"], "files": db.uj(rc["files"]), "source": rc["source"], "programs": list(progs.values()), "targets": tgt if include_targets else []}
