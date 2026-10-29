"""Dependency-free MCP (stdio, JSON-RPC 2.0, newline-delimited) server over the local database."""
import sys, json, traceback, datetime as dt
from . import db, config, queries, activity, model

PROTOCOL = "2024-11-05"
GUIDE = {
    "overview": "TargetFinder tracks public bug-bounty programs, their scope, scope changes, source-repository pushes and activity in a local SQLite database.",
    "start_here": ["search_programs (name/handle/platform search)", "get_program (exact platform:handle)", "get_program_targets (scope list)", "get_recent_changes (added/readded/updated/removed targets)",
                   "get_repository_changes (source pushes)", "get_opportunities (elite/hot/strong/potential)", "get_activity_rankings (scope/rewards/updates/status/payouts/response/all_changes)",
                   "find_programs (combined filters incl. free_submission_only)", "find_programs_by_target (reverse lookup)", "check_watchlist_new_targets"],
    "filters": {"platforms": "csv of platform names (HackerOne, BugCrowd, Intigriti, YesWeHack, HackenProof, Immunefi, Sherlock, Cantina, Self-Hosted, ...)", "tags": "scope/language tags: domain, wildcard, api, mobile, source code, smart contract, solidity, rust, ...",
                "free_submission_only": "drop programs with a paid submission fee/deposit (standing rule in this workspace)", "competition_risks": "Low/Medium/High/Unknown", "change_type": "added|readded|updated|removed"},
    "cursors": "get_recent_changes and get_repository_changes accept after_id (oldest-first) and return meta.next_after_id; persist it between polls.",
    "coverage": "Only platforms with a collector are tracked; see status for the list and their last successful run.",
}

def _tool(name, desc, props, required=None):
    return {"name": name, "description": desc, "inputSchema": {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}}

S = lambda d="": {"type": "string", "description": d}
I = lambda d="": {"type": "integer", "description": d}
B = lambda d="": {"type": "boolean", "description": d}
A = lambda d="": {"type": "array", "items": {"type": "string"}, "description": d}

TOOLS = [
    _tool("get_mcp_status", "Status: database counts, last sync, platforms with live collectors.", {}),
    _tool("guide", "Quick-start guide for choosing tools (no network).", {}),
    _tool("search_programs", "Search or list programs. Server-side name/handle/platform/target search.", {"search": S(), "platforms": A(), "tags": A(), "program_kind": {"type": "string", "enum": ["bounty", "vdp", "contest", "challenge"]}, "reward_status": {"type": "string", "enum": ["paid", "no_reward", "unknown"]}, "free_submission_only": B(), "page": I(), "page_size": I("max 100"), "compact": B("default true")}),
    _tool("get_program", "One program by platform:handle, handle or unambiguous name.", {"program_id": S()}, ["program_id"]),
    _tool("resolve_program", "Resolve a name fragment to candidate program_ids.", {"query": S()}, ["query"]),
    _tool("get_program_targets", "Active targets for one program with Target Intelligence.", {"program_id": S(), "eligibility": {"type": "string", "enum": ["all", "eligible", "ineligible"]}, "target_type": S(), "search": S(), "min_target_score": I(), "target_opportunity_label": S(), "competition_risk": S(), "limit": I("max 500"), "offset": I(), "compact": B("default true")}, ["program_id"]),
    _tool("get_program_eligible_targets_by_name", "Resolve a program name then return in-scope bounty-eligible targets.", {"name": S(), "limit": I()}, ["name"]),
    _tool("search_targets", "Reverse target search / inventory paging.", {"q": S(), "match_mode": {"type": "string", "enum": ["contains", "exact", "suffix", "wildcard"]}, "platforms": A(), "tags": A(), "include_out_of_scope": B(), "include_ineligible": B(), "page": I(), "page_size": I("max 500")}),
    _tool("find_programs_by_target", "Which program(s) own a domain / URL / repo / wildcard.", {"target": S(), "platforms": A()}, ["target"]),
    _tool("get_recent_changes", "Recent target changes (page mode or after_id cursor).", {"change_type": {"type": "string", "enum": ["added", "readded", "updated", "removed"]}, "platforms": A(), "tags": A(), "search": S(), "include_removed": B(), "include_ineligible": B(), "include_out_of_scope": B(), "min_target_score": I(), "competition_risk": S(), "free_submission_only": B(), "since": S("ISO timestamp"), "page": I(), "page_size": I("max 100"), "after_id": I(), "compact": B("default true")}),
    _tool("get_repository_changes", "Repository push feed (page mode or after_id cursor). No diffs or code.", {"search": S(), "platforms": A(), "tags": A(), "free_submission_only": B(), "include_targets": B("default false"), "since": S(), "page": I(), "page_size": I("max 100"), "after_id": I()}),
    _tool("get_opportunities", "Programs in one opportunity tier.", {"level": {"type": "string", "enum": ["elite", "hot", "strong", "potential"]}, "platforms": A(), "tags": A(), "search": S(), "page": I(), "page_size": I()}, ["level"]),
    _tool("find_programs", "Program discovery with combined local filters (platforms, tags, free_submission_only, competition_risks, min_bounty_max, wildcard_only, program_kind, recent_scope_days).", {"platforms": A(), "tags": A(), "free_submission_only": B(), "competition_risks": A(), "min_bounty_max": {"type": "number"}, "wildcard_only": B(), "program_kind": S(), "recent_scope_days": I("only programs with scope changes in the last N days"), "search": S(), "limit": I(), "sort": {"type": "string", "enum": ["name", "updated", "added", "bounty", "targets"]}}),
    _tool("get_latest_added_targets", "Latest added targets, optionally by type/platform.", {"target_type": S("e.g. wildcard, domain, source code, smart contract, mobile, api"), "platforms": A(), "min_bounty_max": {"type": "number"}, "free_submission_only": B(), "limit": I()}),
    _tool("check_program_new_targets", "Yes/no: new in-scope targets for one program since a timestamp.", {"program_id": S(), "since": S("ISO timestamp; default 7 days ago")}, ["program_id"]),
    _tool("check_watchlist_new_targets", "New in-scope targets across many program_ids since a timestamp.", {"program_ids": A(), "since": S()}, ["program_ids"]),
    _tool("get_program_scope_delta", "Recent target changes for one program.", {"program_id": S(), "since": S(), "limit": I()}, ["program_id"]),
    _tool("get_program_brief", "Compact worth-hunting brief for one program (rewards, fee, risk, scope counts, recent activity, payouts).", {"program_id": S()}, ["program_id"]),
    _tool("export_targets", "Bounded export (max 500) of current targets.", {"program_ids": A(), "platforms": A(), "tags": A(), "opportunity_levels": A(), "include_out_of_scope": B(), "include_ineligible": B(), "limit": I()}),
    _tool("list_filters", "Observed filter values (platforms, tags, target types, change types).", {}),
    _tool("get_activity_capabilities", "Enabled Activity tabs/periods/sorts.", {}),
    _tool("get_activity_snapshot", "Current Activity snapshot identity.", {}),
    _tool("get_activity_rankings", "One ranked page of an Activity tab.", {"tab": {"type": "string", "enum": ["payouts", "scope", "rewards", "updates", "status", "response", "all_changes"]}, "period": S(), "sort": S(), "direction": {"type": "string", "enum": ["asc", "desc"]}, "limit": I("1-100"), "platforms": A(), "tags": A(), "program_types": A(), "labels": A(), "next_cursor": S()}, ["tab"]),
    _tool("get_program_payouts", "Payout facts, windows, signals and labels for one program.", {"program_id": S()}, ["program_id"]),
    _tool("get_history_changes", "Semantic program history feed (all programs).", {"program_id": S(), "category": S(), "since": S(), "limit": I(), "cursor": S()}),
    _tool("get_program_history", "History for one program.", {"program_id": S(), "limit": I(), "cursor": S()}, ["program_id"]),
    _tool("get_program_activity", "activity_summary block for one program.", {"program_id": S()}, ["program_id"]),
    _tool("get_activity_change_detail", "Before/after detail of one history/activity event id.", {"event_id": S()}, ["event_id"]),
    _tool("run_sweep", "Run the standing 4-tab + commits + activity sweep (free-submission rule) and return the markdown.", {"since": S("ISO timestamp; default last sweep"), "write": B("write SWEEP_<date>.md to the sweep directory (default false)")}),
]

def _compact_program(p):
    sc = p.get("submission_cost") or {}
    return {"id": p["id"], "name": p["name"], "platform": p["platform"], "link": p.get("link"), "program_kind": p.get("program_kind"), "program_status": p.get("program_status"), "reward_status": p.get("reward_status"),
            "bounty_min": p.get("bounty_min"), "bounty_max": p.get("bounty_max"), "submission_cost": {"amount": sc.get("amount"), "unit": sc.get("unit"), "kind": sc.get("kind")} if sc else None,
            "report_count": p.get("report_count"), "competitionRisk": p.get("competitionRisk"), "scope_tags": p.get("scope_tags"), "target_counts": p.get("target_counts"), "target_activity": p.get("target_activity"),
            "added_at": p.get("added_at"), "last_updated": p.get("last_updated"), "labels": (p.get("activity_summary") or {}).get("labels")}

def _compact_target(t):
    return {k: t.get(k) for k in ("identifier", "target_type", "type", "in_scope", "eligible_for_bounty", "wildcard", "scope_tags", "language_tags", "added_at", "last_changed_at", "targetOpportunityScore", "targetOpportunityLabel", "competitionRisk", "whyThisTarget") if k in t}

def _compact_change(r):
    return {"id": r["id"], "change_type": r["change_type"], "changed_at": r["changed_at"], "program": {k: r["program"].get(k) for k in ("id", "name", "platform", "bounty_max", "submission_cost", "competitionRisk")}, "target": _compact_target(r["target"])}

def call(con, name, a):
    Q = queries
    a = a or {}
    if name == "get_mcp_status":
        return {"server": "targetfinder", "version": config.VERSION, **Q.status(con)}
    if name == "guide":
        return {"guide": GUIDE, "tools": [t["name"] for t in TOOLS]}
    if name == "search_programs":
        res = Q.list_programs(con, a.get("platforms"), a.get("tags"), a.get("search"), None, a.get("page") or 1, min(100, a.get("page_size") or 25), a.get("program_kind"), a.get("reward_status"), None, False, bool(a.get("free_submission_only")), full=not a.get("compact", True))
        if a.get("compact", True): res["programs"] = [_compact_program(p) for p in res["programs"]]
        return res
    if name == "get_program":
        p = Q.get_program(con, a["program_id"])
        if p is None:
            return {"error": "not found", "candidates": Q.resolve_program(con, a["program_id"])}
        return p
    if name == "resolve_program":
        return {"candidates": Q.resolve_program(con, a["query"], 15)}
    if name in ("get_program_targets", "get_program_eligible_targets_by_name"):
        pid = a.get("program_id") or a.get("name")
        prow = Q.program_row(con, pid)
        if prow is None:
            c = Q.resolve_program(con, pid, 5)
            if len(c) == 1: pid = c[0]["id"]
            else: return {"error": "ambiguous or unknown program", "candidates": c}
        elig = "eligible" if name == "get_program_eligible_targets_by_name" else (a.get("eligibility") or "all")
        limit = min(500, a.get("limit") or 100); offset = a.get("offset") or 0
        res = Q.program_targets(con, pid, elig, a.get("target_type"), a.get("search"), a.get("min_target_score"), a.get("target_opportunity_label"), a.get("competition_risk"), 1, 100000)
        rows = res["targets"][offset:offset + limit]
        if a.get("compact", True): rows = [_compact_target(t) for t in rows]
        return {"program": res["program"], "program_id": pid, "targets": rows, "total": res["meta"]["total"], "offset": offset, "limit": limit, "has_more": offset + limit < res["meta"]["total"]}
    if name == "search_targets":
        return Q.search_targets(con, a.get("q"), a.get("match_mode") or "contains", a.get("platforms"), a.get("tags"), bool(a.get("include_out_of_scope")), bool(a.get("include_ineligible")), a.get("page") or 1, min(500, a.get("page_size") or 50))
    if name == "find_programs_by_target":
        t = a["target"].strip()
        for mode in ("exact", "suffix", "contains"):
            res = Q.search_targets(con, t.lstrip("*."), mode, a.get("platforms"), None, True, True, 1, 50)
            if res["results"]: return {"match_mode": mode, **res}
        return {"match_mode": None, "results": [], "meta": {"total": 0}}
    if name == "get_recent_changes":
        res = Q.target_changes(con, a.get("change_type"), bool(a.get("include_removed")), bool(a.get("include_ineligible")), bool(a.get("include_out_of_scope")), a.get("search"), a.get("platforms"), a.get("tags"),
                               a.get("min_target_score"), None, a.get("competition_risk"), a.get("page") or 1, min(100, a.get("page_size") or 50), a.get("after_id"), bool(a.get("free_submission_only")), a.get("since"))
        if a.get("compact", True): res["results"] = [_compact_change(r) for r in res["results"]]
        res.pop("facets", None); return res
    if name == "get_repository_changes":
        res = Q.repo_changes(con, a.get("search"), a.get("platforms"), a.get("tags"), bool(a.get("free_submission_only")), bool(a.get("include_targets")), a.get("page") or 1, min(100, a.get("page_size") or 50), a.get("after_id"), a.get("since"))
        if not a.get("include_targets"):
            for r in res["results"]: r.pop("targets", None)
        return res
    if name == "get_opportunities":
        res = Q.opportunities(con, a["level"], a.get("platforms"), a.get("tags"), a.get("search"), a.get("page") or 1, min(100, a.get("page_size") or 25))
        res["programs"] = [{**_compact_program(p), "opportunity_tag": p["opportunity_tag"]} for p in res["programs"]]; return res
    if name == "find_programs":
        res = Q.list_programs(con, a.get("platforms"), a.get("tags"), a.get("search"), None, 1, 5000, a.get("program_kind"), None, "active", False, bool(a.get("free_submission_only")), a.get("competition_risks"), full=False)
        out = []
        since = config.iso(config.utcnow() - dt.timedelta(days=a["recent_scope_days"])) if a.get("recent_scope_days") else None
        for p in res["programs"]:
            if a.get("min_bounty_max") and (p.get("bounty_max") or 0) < a["min_bounty_max"]: continue
            if a.get("wildcard_only") and not p["target_counts"]["wildcard"]: continue
            if since and not (p["target_activity"]["latest_change_at"] or "") >= since: continue
            out.append(_compact_program(p))
        key = {"name": lambda p: (p["name"] or "").lower(), "updated": lambda p: p.get("last_updated") or "", "added": lambda p: p.get("added_at") or "", "bounty": lambda p: p.get("bounty_max") or 0, "targets": lambda p: p["target_counts"]["in_scope"]}[a.get("sort") or "updated"]
        out.sort(key=key, reverse=(a.get("sort") or "updated") != "name")
        lim = a.get("limit") or 50
        return {"programs": out[:lim], "total": len(out), "scan": {"complete": True}}
    if name == "get_latest_added_targets":
        tt = a.get("target_type"); tags = ["wildcard"] if tt == "wildcard" else None
        res = Q.target_changes(con, "added", False, False, False, None, a.get("platforms"), tags, None, None, None, 1, 100, None, bool(a.get("free_submission_only")))
        rows = [r for r in res["results"] if (not tt or tt == "wildcard" or r["target"].get("type") == tt) and (not a.get("min_bounty_max") or (r["program"].get("bounty_max") or 0) >= a["min_bounty_max"])]
        return {"targets": [_compact_change(r) for r in rows[: a.get("limit") or 50]], "total_scanned": len(res["results"])}
    if name in ("check_program_new_targets", "check_watchlist_new_targets", "get_program_scope_delta"):
        since = a.get("since") or config.iso(config.utcnow() - dt.timedelta(days=7))
        ids = a.get("program_ids") or [a.get("program_id")]
        out = {}
        for pid in ids:
            prow = Q.program_row(con, pid)
            if prow is None: out[pid] = {"error": "unknown program"}; continue
            res = Q.target_changes(con, None, name == "get_program_scope_delta", True, True, None, None, None, None, None, None, 1, a.get("limit") or 100, None, False, since, prow["id"])
            new = [r for r in res["results"] if r["change_type"] in ("added", "readded") and r["target"]["in_scope"]]
            out[prow["id"]] = {"new_in_scope_targets": len(new), "has_new": bool(new), "since": since, "changes": [_compact_change(r) for r in res["results"]]}
        return out if name == "check_watchlist_new_targets" else out[list(out)[0]]
    if name == "get_program_brief":
        p = Q.get_program(con, a["program_id"])
        if p is None: return {"error": "not found", "candidates": Q.resolve_program(con, a["program_id"])}
        tg = Q.program_targets(con, p["id"], "eligible", page_size=10)
        return {**_compact_program(p), "competitionRiskReason": p.get("competitionRiskReason"), "payout_summary": p.get("payout_summary"), "activity_summary": {k: p["activity_summary"].get(k) for k in ("last_changed_at", "changes_30d", "scope_added_30d", "scope_removed_30d", "labels")},
                "sample_eligible_targets": [_compact_target(t) for t in tg["targets"]], "eligible_total": tg["meta"]["total"]}
    if name == "export_targets":
        return Q.export_targets(con, a.get("program_ids"), a.get("platforms"), a.get("tags"), a.get("opportunity_levels"), bool(a.get("include_out_of_scope")), bool(a.get("include_ineligible")), "json", a.get("limit") or 500)
    if name == "list_filters":
        return {"platforms": [r[0] for r in con.execute("SELECT DISTINCT platform FROM programs ORDER BY 1")], "target_types": [r[0] for r in con.execute("SELECT DISTINCT target_type FROM targets WHERE is_active=1 ORDER BY 1")],
                "scope_tags": [r[0] for r in con.execute("SELECT DISTINCT value FROM targets, json_each(targets.scope_tags) WHERE is_active=1 ORDER BY 1")], "language_tags": [r[0] for r in con.execute("SELECT DISTINCT value FROM targets, json_each(targets.language_tags) WHERE is_active=1 ORDER BY 1")],
                "change_types": ["added", "readded", "updated", "removed"], "program_kinds": ["bounty", "vdp", "contest", "challenge"], "competition_risks": ["Low", "Medium", "High", "Unknown"], "opportunity_levels": ["elite", "hot", "strong", "potential"]}
    if name == "get_activity_capabilities":
        return activity.capabilities(con)
    if name == "get_activity_snapshot":
        return activity.snapshot()
    if name == "get_activity_rankings":
        res, err = activity.rankings(con, a["tab"], a.get("period"), a.get("sort"), a.get("direction") or "desc", min(100, a.get("limit") or 25), a.get("platforms"), a.get("tags"), a.get("program_types"), a.get("labels"), int(a.get("next_cursor") or 0))
        if err: return {"error": err, "status": 422}
        for r in res["rows"]: r.pop("label_details", None); r.pop("signals", None)
        return res
    if name == "get_program_payouts":
        prow = Q.program_row(con, a["program_id"])
        return activity.program_payouts(con, prow) if prow else {"error": "not found"}
    if name in ("get_history_changes", "get_program_history"):
        return activity.history_changes(con, a.get("program_id"), min(200, a.get("limit") or 50), int(a.get("cursor") or 0), a.get("since"), a.get("category"))
    if name == "get_program_activity":
        prow = Q.program_row(con, a["program_id"])
        return {"program_id": prow["id"], "activity_summary": model.activity_summary(con, prow["id"])} if prow else {"error": "not found"}
    if name == "get_activity_change_detail":
        e = con.execute("SELECT * FROM program_events WHERE id=?", (str(a["event_id"]).replace("ev-", ""),)).fetchone()
        return {"event": {"id": f"ev-{e['id']}", "program_id": e["program_id"], "observed_at": e["observed_at"], "change_type": e["change_type"], "before": db.uj(e["before"]), "after": db.uj(e["after"]), "counts": db.uj(e["counts"])}} if e else {"error": "not found"}
    if name == "run_sweep":
        from . import sweep
        text, raw = sweep.run(a.get("since"), None, write=bool(a.get("write")))
        return {"markdown": text}
    return {"error": f"unknown tool {name}"}

def main():
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get("id"); method = msg.get("method")
        def reply(result=None, error=None):
            if mid is None: return
            resp = {"jsonrpc": "2.0", "id": mid}
            if error is not None: resp["error"] = error
            else: resp["result"] = result
            out.write(json.dumps(resp, ensure_ascii=False) + "\n"); out.flush()
        try:
            if method == "initialize":
                reply({"protocolVersion": msg.get("params", {}).get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "targetfinder", "version": config.VERSION},
                       "instructions": "Start with guide; use get_recent_changes/get_repository_changes with after_id cursors for polling; apply free_submission_only per the standing sweep rule."})
            elif method == "notifications/initialized":
                pass
            elif method == "ping":
                reply({})
            elif method == "tools/list":
                reply({"tools": TOOLS})
            elif method == "tools/call":
                p = msg.get("params", {}); name = p.get("name"); args = p.get("arguments") or {}
                con = db.connect()
                try:
                    res = call(con, name, args)
                finally:
                    con.close()
                is_err = isinstance(res, dict) and "error" in res and len(res) <= 3
                reply({"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}], "structuredContent": res if isinstance(res, dict) else {"result": res}, "isError": bool(is_err)})
            elif method in ("resources/list", "prompts/list"):
                reply({"resources": [], "prompts": []} if method == "resources/list" else {"prompts": []})
            else:
                reply(error={"code": -32601, "message": f"method not found: {method}"})
        except Exception as ex:
            reply(error={"code": -32000, "message": str(ex), "data": traceback.format_exc(limit=3)})

if __name__ == "__main__":
    main()
