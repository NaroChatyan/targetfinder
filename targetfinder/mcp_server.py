"""Dependency-free MCP server (stdio, JSON-RPC 2.0, newline-delimited) over the local database. Tools are listed in docs/API.md."""
import sys, json, traceback, datetime as dt
from . import db, config, queries, activity, model

PROTOCOL = "2024-11-05"
GUIDE = {
    "overview": "TargetFinder tracks public bug-bounty programs, their scope, scope changes, source-repository pushes and activity in a local SQLite database. No network calls are made by these tools.",
    "start_here": ["find_programs (filters: platforms, tags, free_submission_only, competition, min_bounty, wildcard_only, kind, recent_scope_days)", "get_program (id, handle or unambiguous name)",
                   "program_brief (one-screen summary)", "list_targets (scope of one program with scores)", "recent_target_changes (added/readded/updated/removed; after_id cursor for polling)",
                   "recent_repo_pushes (source pushes with compare links)", "leads (top/hot/solid/possible)", "rankings (scope/rewards/updates/status/payouts/response/all)",
                   "find_program_by_target (reverse lookup)", "watchlist_new_targets (new in-scope targets for a list of programs)", "run_sweep (the standing report)"],
    "ids": "Program ids are <platform-slug>:<handle>, e.g. hackerone:gitlab, bugcrowd:engagements/example, sherlock:56.",
    "filters": {"platforms": "platform names (HackerOne, Bugcrowd, Intigriti, YesWeHack, HackenProof, Sherlock, Immunefi when enabled)", "tags": "scope/language tags: domain, wildcard, api, mobile, source code, smart contract, solidity, rust, ...",
                "free_submission_only": "drop programs with a paid submission fee or deposit", "competition": "low|medium|high|unknown (heuristic, docs/SCORING.md)", "type": "added|readded|updated|removed"},
    "cursors": "recent_target_changes and recent_repo_pushes accept after_id (oldest-first) and return page.next_after_id; persist it between polls.",
    "scores": "score.value/label (top/strong/promising/normal) and competition.level are heuristics derived from public counts; always confirm scope on the program page.",
}

def _tool(name, desc, props, required=None):
    return {"name": name, "description": desc, "inputSchema": {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}}

S = lambda d="": {"type": "string", "description": d}
I = lambda d="": {"type": "integer", "description": d}
B = lambda d="": {"type": "boolean", "description": d}
A = lambda d="": {"type": "array", "items": {"type": "string"}, "description": d}

TOOLS = [
    _tool("guide", "How to choose among these tools; id format; filter vocabulary.", {}),
    _tool("status", "Database counts, last sync, collectors and their last successful run.", {}),
    _tool("find_programs", "Program discovery with combined filters.", {"search": S("name/handle/platform/target substring"), "platforms": A(), "tags": A(), "free_submission_only": B(), "competition": A("low|medium|high|unknown"),
                                                                   "min_bounty": {"type": "number"}, "wildcard_only": B(), "kind": {"type": "string", "enum": ["bounty", "vdp", "contest"]}, "recent_scope_days": I("only programs with scope changes in the last N days"),
                                                                   "sort": {"type": "string", "enum": ["name", "updated", "added", "bounty", "targets"]}, "limit": I("default 50"), "full": B("include payouts/activity blocks (default false)")}),
    _tool("get_program", "One program by id, handle or unambiguous name (full object).", {"program_id": S()}, ["program_id"]),
    _tool("program_brief", "One-screen summary: rewards, fee, competition, scope counts, recent activity, payouts, sample eligible targets.", {"program_id": S()}, ["program_id"]),
    _tool("list_targets", "Targets of one program with scores.", {"program_id": S(), "eligibility": {"type": "string", "enum": ["all", "eligible", "ineligible"]}, "type": S("domain|api|source code|smart contract|mobile|..."), "search": S(),
                                                            "min_score": I(), "label": {"type": "string", "enum": ["top", "strong", "promising", "normal"]}, "competition": S(), "limit": I("max 500"), "offset": I(), "compact": B("default true")}, ["program_id"]),
    _tool("search_targets", "Search targets across all programs.", {"q": S(), "match_mode": {"type": "string", "enum": ["contains", "exact", "suffix", "wildcard"]}, "platforms": A(), "tags": A(), "include_out_of_scope": B(), "include_ineligible": B(), "page": I(), "page_size": I("max 500")}),
    _tool("find_program_by_target", "Which program(s) list a domain / URL / repo / wildcard.", {"target": S(), "platforms": A()}, ["target"]),
    _tool("recent_target_changes", "Target changes (page mode or after_id cursor).", {"type": {"type": "string", "enum": ["added", "readded", "updated", "removed"]}, "platforms": A(), "tags": A(), "search": S(), "include_removed": B(), "include_ineligible": B(),
                                                                                "include_out_of_scope": B(), "min_score": I(), "competition": S(), "free_submission_only": B(), "since": S("ISO timestamp"), "program_id": S(), "page": I(), "page_size": I("max 100"), "after_id": I(), "compact": B("default true")}),
    _tool("recent_repo_pushes", "Source-repository pushes (page mode or after_id cursor). Compare links, no code.", {"search": S(), "platforms": A(), "tags": A(), "free_submission_only": B(), "kinds": A("code|docs|deps|ci|tests|mixed|unknown"), "include_targets": B("default false"),
                                                                                                      "since": S(), "program_id": S(), "page": I(), "page_size": I("max 100"), "after_id": I()}),
    _tool("leads", "Programs in one lead tier.", {"tier": {"type": "string", "enum": ["top", "hot", "solid", "possible"]}, "platforms": A(), "tags": A(), "search": S(), "free_submission_only": B(), "page": I(), "page_size": I()}, ["tier"]),
    _tool("rankings", "One ranked page of a view.", {"view": {"type": "string", "enum": list(activity.VIEWS)}, "period": S("24h|7d|30d|90d|all (per view)"), "sort": S(), "direction": {"type": "string", "enum": ["asc", "desc"]}, "limit": I("1-100"),
                                                 "platforms": A(), "tags": A(), "kinds": A(), "labels": A(), "cursor": S()}, ["view"]),
    _tool("program_payouts", "Payout facts, windows, signals and labels for one program.", {"program_id": S()}, ["program_id"]),
    _tool("program_history", "Event history (program/scope/rewards/fees/status/repositories) for one program, or all programs when program_id is omitted.", {"program_id": S(), "category": S(), "since": S(), "limit": I(), "cursor": S()}),
    _tool("watchlist_new_targets", "New in-scope targets for a list of programs since a timestamp.", {"program_ids": A(), "since": S("ISO timestamp; default 7 days ago")}, ["program_ids"]),
    _tool("export_targets", "Bounded export (max 500 rows) of current targets.", {"program_ids": A(), "platforms": A(), "tags": A(), "tiers": A(), "include_out_of_scope": B(), "include_ineligible": B(), "limit": I()}),
    _tool("list_filters", "Observed filter values (platforms, tags, target types) and the fixed vocabularies.", {}),
    _tool("run_sweep", "Run the standing sweep report and return the markdown.", {"since": S("ISO timestamp; default last sweep"), "write": B("write SWEEP_<date>.md to the sweep directory (default false)")}),
]

def _compact_program(p):
    return {k: p.get(k) for k in ("id", "name", "platform", "url", "kind", "status", "rewards", "submission_fee", "report_count", "competition", "tags", "scope_counts", "scope_activity", "first_seen_at", "updated_at")}

def _compact_target(t):
    sc = t.get("score") or {}
    return {"identifier": t["identifier"], "type": t["type"], "in_scope": t["in_scope"], "bounty_eligible": t["bounty_eligible"], "wildcard": t["wildcard"], "tags": t["tags"], "languages": t["languages"],
            "added_at": t["added_at"], "last_change": t.get("last_change"), "score": ({"value": sc.get("value"), "label": sc.get("label"), "summary": sc.get("summary")} if sc else None), "competition": t.get("competition")}

def _compact_change(r):
    p = r["program"]
    return {"id": r["id"], "type": r["type"], "at": r["at"], "program": {k: p.get(k) for k in ("id", "name", "platform", "rewards", "submission_fee", "competition")}, "target": _compact_target(r["target"]), "diff": r.get("diff")}

def _resolve(con, pid):
    prow = queries.program_row(con, pid)
    if prow is not None:
        return prow, None
    c = queries.resolve_program(con, pid, 5)
    if len(c) == 1:
        return queries.program_row(con, c[0]["id"]), None
    return None, {"error": "unknown or ambiguous program", "candidates": c}

def call(con, name, a):
    Q = queries
    a = a or {}
    if name == "guide":
        return {"guide": GUIDE, "tools": [t["name"] for t in TOOLS]}
    if name == "status":
        return Q.status(con)
    if name == "find_programs":
        res = Q.list_programs(con, a.get("platforms"), a.get("tags"), a.get("search"), None, 1, 5000, a.get("kind"), None, "active", False, bool(a.get("free_submission_only")), a.get("competition"), full=bool(a.get("full")))
        out = []
        since = config.iso(config.utcnow() - dt.timedelta(days=a["recent_scope_days"])) if a.get("recent_scope_days") else None
        for p in res["items"]:
            if a.get("min_bounty") and (p["rewards"]["max"] or 0) < a["min_bounty"]: continue
            if a.get("wildcard_only") and not p["scope_counts"]["wildcards"]: continue
            if since and not (p["scope_activity"]["last_change_at"] or "") >= since: continue
            out.append(p if a.get("full") else _compact_program(p))
        key = {"name": lambda p: (p["name"] or "").lower(), "updated": lambda p: p.get("updated_at") or "", "added": lambda p: p.get("first_seen_at") or "", "bounty": lambda p: p["rewards"]["max"] or 0, "targets": lambda p: p["scope_counts"]["in_scope"]}[a.get("sort") or "updated"]
        out.sort(key=key, reverse=(a.get("sort") or "updated") != "name")
        lim = a.get("limit") or 50
        return {"items": out[:lim], "total": len(out)}
    if name == "get_program":
        prow, err = _resolve(con, a["program_id"])
        return err or Q.get_program(con, prow["id"])
    if name == "program_brief":
        prow, err = _resolve(con, a["program_id"])
        if err: return err
        p = Q.get_program(con, prow["id"])
        tg = Q.program_targets(con, p["id"], "eligible", page_size=10)
        act = p["activity"]
        return {**_compact_program(p), "payouts": p["payouts"], "activity": {k: act.get(k) for k in ("last_changed_at", "changes_30d", "scope_added_30d", "scope_removed_30d", "labels")},
                "sample_eligible_targets": [_compact_target(t) for t in tg["items"]], "eligible_total": tg["page"]["total"]}
    if name == "list_targets":
        prow, err = _resolve(con, a["program_id"])
        if err: return err
        limit = min(500, a.get("limit") or 100); offset = a.get("offset") or 0
        res = Q.program_targets(con, prow["id"], a.get("eligibility") or "all", a.get("type"), a.get("search"), a.get("min_score"), a.get("label"), a.get("competition"), 1, 100000)
        rows = res["items"][offset:offset + limit]
        if a.get("compact", True): rows = [_compact_target(t) for t in rows]
        return {"program": res["program"], "items": rows, "page": {"total": res["page"]["total"], "offset": offset, "limit": limit, "has_more": offset + limit < res["page"]["total"], "target_types": res["page"]["target_types"]}}
    if name == "search_targets":
        return Q.search_targets(con, a.get("q"), a.get("match_mode") or "contains", a.get("platforms"), a.get("tags"), bool(a.get("include_out_of_scope")), bool(a.get("include_ineligible")), a.get("page") or 1, min(500, a.get("page_size") or 50))
    if name == "find_program_by_target":
        t = a["target"].strip()
        for mode in ("exact", "suffix", "contains"):
            res = Q.search_targets(con, t.lstrip("*."), mode, a.get("platforms"), None, True, True, 1, 50)
            if res["items"]: return {"match_mode": mode, **res}
        return {"match_mode": None, "items": [], "page": {"total": 0}}
    if name == "recent_target_changes":
        res = Q.target_changes(con, a.get("type"), bool(a.get("include_removed")), bool(a.get("include_ineligible")), bool(a.get("include_out_of_scope")), a.get("search"), a.get("platforms"), a.get("tags"),
                               a.get("min_score"), None, a.get("competition"), a.get("page") or 1, min(100, a.get("page_size") or 50), a.get("after_id"), bool(a.get("free_submission_only")), a.get("since"), a.get("program_id"))
        if a.get("compact", True): res["items"] = [_compact_change(r) for r in res["items"]]
        res.pop("facets", None); return res
    if name == "recent_repo_pushes":
        res = Q.repo_changes(con, a.get("search"), a.get("platforms"), a.get("tags"), bool(a.get("free_submission_only")), bool(a.get("include_targets")), a.get("page") or 1, min(100, a.get("page_size") or 50), a.get("after_id"), a.get("since"), a.get("kinds"), a.get("program_id"))
        if not a.get("include_targets"):
            for r in res["items"]: r.pop("targets", None)
        return res
    if name == "leads":
        res = Q.leads(con, a["tier"], a.get("platforms"), a.get("tags"), a.get("search"), a.get("page") or 1, min(100, a.get("page_size") or 25), free_submission_only=bool(a.get("free_submission_only")))
        res["items"] = [{**_compact_program(p), "lead": p["lead"]} for p in res["items"]]; return res
    if name == "rankings":
        res, err = activity.rankings(con, a["view"], a.get("period"), a.get("sort"), a.get("direction") or "desc", min(100, a.get("limit") or 25), a.get("platforms"), a.get("tags"), a.get("kinds"), a.get("labels"), int(a.get("cursor") or 0))
        return {"error": err} if err else res
    if name == "program_payouts":
        prow, err = _resolve(con, a["program_id"])
        return err or activity.program_payouts(con, prow)
    if name == "program_history":
        pid = None
        if a.get("program_id"):
            prow, err = _resolve(con, a["program_id"])
            if err: return err
            pid = prow["id"]
        return activity.history_changes(con, pid, min(200, a.get("limit") or 50), int(a.get("cursor") or 0), a.get("since"), a.get("category"))
    if name == "watchlist_new_targets":
        since = a.get("since") or config.iso(config.utcnow() - dt.timedelta(days=7))
        out = {}
        for pid in a.get("program_ids") or []:
            prow, err = _resolve(con, pid)
            if err: out[pid] = err; continue
            res = Q.target_changes(con, None, False, True, True, None, None, None, None, None, None, 1, 100, None, False, since, prow["id"])
            new = [r for r in res["items"] if r["type"] in ("added", "readded") and r["target"]["in_scope"]]
            out[prow["id"]] = {"new_in_scope_targets": len(new), "has_new": bool(new), "since": since, "changes": [_compact_change(r) for r in res["items"]]}
        return out
    if name == "export_targets":
        return Q.export_targets(con, a.get("program_ids"), a.get("platforms"), a.get("tags"), a.get("tiers"), bool(a.get("include_out_of_scope")), bool(a.get("include_ineligible")), "json", a.get("limit") or 500)
    if name == "list_filters":
        return {"platforms": [r[0] for r in con.execute("SELECT DISTINCT platform FROM programs ORDER BY 1")], "target_types": [r[0] for r in con.execute("SELECT DISTINCT target_type FROM targets WHERE is_active=1 ORDER BY 1")],
                "tags": [r[0] for r in con.execute("SELECT DISTINCT value FROM targets, json_each(targets.scope_tags) WHERE is_active=1 ORDER BY 1")], "languages": [r[0] for r in con.execute("SELECT DISTINCT value FROM targets, json_each(targets.language_tags) WHERE is_active=1 ORDER BY 1")],
                "change_types": list(Q.CHANGE_TYPES), "kinds": ["bounty", "vdp", "contest"], "competition": list(Q.COMPETITION), "tiers": list(Q.TIERS), "score_labels": list(Q.SCORE_LABELS), "views": list(activity.VIEWS)}
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
                       "instructions": "Start with guide. Use recent_target_changes / recent_repo_pushes with after_id cursors for polling. Scores are heuristics; confirm scope on the program page before testing."})
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
                reply({"resources": []} if method == "resources/list" else {"prompts": []})
            else:
                reply(error={"code": -32601, "message": f"method not found: {method}"})
        except Exception as ex:
            reply(error={"code": -32000, "message": str(ex), "data": traceback.format_exc(limit=3)})

if __name__ == "__main__":
    main()
