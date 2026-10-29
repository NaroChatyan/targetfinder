"""The standing sweep, run against the local database.

Sections: New programs · Fee changes · Added / Re-added / Updated targets · Commits · Your watchlist · Activity rankings ·
Stale platforms. Standing rule (2026-10-21): a paid submission fee is the only deprioritizer. Everything else is shown.
Noise controls: bulk scope updates are collapsed, non-code pushes are hidden unless --all-pushes, platforms without a
collector can be hidden with --live-only. Writes SWEEP_<date>.md + sweep_<stamp>_raw.json into the sweep directory."""
import json, os, datetime as dt, collections
from . import db, config, queries, activity, model, tools
from .collectors import load_all

def live_platforms():
    return tuple(load_all().keys())
NON_CODE = ("docs", "deps", "ci")

def _free(prog):
    sc = prog.get("submission_fee") or {}
    return (sc.get("amount") or 0) == 0

def _fee_str(prog):
    sc = prog.get("submission_fee") or {}
    return f"{sc.get('amount', 0):g} {sc.get('unit', 'USD')}" if sc else "n/a"

def _h1_stats(con, pid):
    """Better competition signals than report_count, where a platform publishes them (HackerOne does)."""
    parts = []
    for metric, fmt in (("reports_received_native_90d", "{:,.0f} reports/90d"), ("payout_native_90d", "${:,.0f} paid/90d"), ("first_response_avg_native_90d", "{:.0f}h first response")):
        f = model.latest_fact(con, pid, metric)
        if f and f["amount"] is not None:
            parts.append(fmt.format(f["amount"]))
    return ", ".join(parts)

def _brief(con, prow):
    p = model.program_summary(con, prow)
    counts = model.scope_counts(con, prow["id"])
    bits = [p.get("kind") or "?", f"max ${p['rewards']['max']:,.0f}" if p["rewards"].get("max") else "no disclosed max", f"fee {_fee_str(p)}", f"{counts['in_scope']} in scope"]
    st = _h1_stats(con, prow["id"])
    if st: bits.append(st)
    if prow["report_count"] is not None: bits.append(f"{prow['report_count']} public reports")
    return " · ".join(bits)

def run(since=None, out_dir=None, write=True, log=print, max_per_program=6, bulk_threshold=20, all_pushes=False, live_only=False):
    con = db.connect()
    out_dir = out_dir or config.CACHE_DIR
    now = config.utcnow()
    stamp = now.strftime("%Y-%m-%dT%H%M")
    prev = since or db.kv_get(con, "last_sweep_at") or config.iso(now - dt.timedelta(hours=28))
    raw = {"fetched_at": config.iso(now), "window_since": prev, "mode": "local", "options": {"max_per_program": max_per_program, "bulk_threshold": bulk_threshold, "all_pushes": all_pushes, "live_only": live_only}}
    L = [f"# TargetFinder sweep — {now.strftime('%Y-%m-%d %H:%M')} UTC", "",
         f"Window: since **{prev}**. Filter rule: **free-submission only** is the only deprioritizer; nothing else is filtered.", ""]
    live = live_platforms()
    hide = lambda plat: live_only and plat not in live
    briefed = set()
    def brief_line(prow):
        if prow is None or prow["id"] in briefed: return None
        briefed.add(prow["id"]); return f"    - brief: {_brief(con, prow)}"

    # ---- New programs -----------------------------------------------------------------------
    newp = [dict(r) for r in con.execute("SELECT e.program_id, e.observed_at, p.* FROM program_events e JOIN programs p ON p.id=e.program_id WHERE e.change_type='first_seen' AND e.observed_at>=? ORDER BY e.observed_at DESC", (prev,))]
    newp = [r for r in newp if not hide(r["platform"])]
    raw["new_programs"] = [r["program_id"] for r in newp]
    L.append(f"## New programs — {len(newp)} in window")
    L += [f"- **[{'FREE' if (r['fee_amount'] or 0) == 0 else 'PAID-FEE'}] [{r['platform']}] {r['name']}** (`{r['id']}`) {r['program_kind']} · max {r['bounty_max'] or '?'} · first seen {r['observed_at']}" for r in newp] or ["- none"]
    L.append("")
    # ---- Fee changes ------------------------------------------------------------------------
    fees = [dict(r) for r in con.execute("SELECT e.program_id, e.observed_at, e.before, e.after, p.name, p.platform FROM program_events e JOIN programs p ON p.id=e.program_id WHERE e.change_type='fee_changed' AND e.observed_at>=? ORDER BY e.observed_at DESC", (prev,))]
    raw["fee_changes"] = fees
    L.append(f"## Submission fee changes — {len(fees)} in window")
    for r in fees:
        b, a = db.uj(r["before"], {}), db.uj(r["after"], {})
        tag = "NOW FREE" if (a.get("amount") or 0) == 0 else ("NOW PAID" if (b.get("amount") or 0) == 0 else "CHANGED")
        L.append(f"- **[{tag}] [{r['platform']}] {r['name']}** (`{r['program_id']}`): {b.get('amount')} {b.get('unit') or ''} → {a.get('amount')} {a.get('unit') or ''} ({r['observed_at']})")
    if not fees: L.append("- none")
    L.append("")
    # ---- target tabs ------------------------------------------------------------------------
    for ct, label in (("added", "Added"), ("readded", "Re-added"), ("updated", "Updated")):
        rows = []; page = 1
        while page <= 10:
            res = queries.target_changes(con, ct, page=page, page_size=100, since_ts=prev)
            rows += res["items"]
            if len(rows) >= res["page"]["total"] or not res["items"]: break
            page += 1
        rows = [r for r in rows if not hide(r["program"]["platform"])]
        raw[ct] = {"fetched": len(rows), "rows": rows}
        passed = [r for r in rows if _free(r["program"])]; failed = [r for r in rows if not _free(r["program"])]
        L.append(f"## {label} — {len(rows)} in window, {len(passed)} free-submission")
        if not rows: L.append("- none (clean negative)")
        for tag, group in (("PASS", passed), ("FEE-DEPRIORITIZED", failed)):
            by = collections.defaultdict(list)
            for r in group:
                p = r["program"]; by[(p["name"], p["platform"], (p.get("competition") or {}).get("level"), _fee_str(p), p["id"])].append(r)
            for (name, plat, risk, fee, pid), rs in sorted(by.items(), key=lambda x: -len(x[1])):
                bulk = len(rs) >= bulk_threshold
                L.append(f"- **[{tag}] [{plat}] {name}** (`{pid}`, risk={risk}, fee={fee}) ×{len(rs)}" + (f" — **bulk scope update**, {len(rs)} targets in one go; types: " + ", ".join(f"{k} {v}" for k, v in collections.Counter(r['target'].get('type') for r in rs).most_common(4)) if bulk else ""))
                bl = brief_line(queries.program_row(con, pid)) if tag == "PASS" else None
                if bl: L.append(bl)
                if not bulk:
                    for r in rs[:max_per_program]:
                        t = r["target"]
                        L.append(f"    - `{t['identifier']}` ({t.get('type')}, score {(t.get('score') or {}).get('value')}, {r['at']})")
                    if len(rs) > max_per_program: L.append(f"    - … +{len(rs) - max_per_program} more")
                else:
                    for r in rs[:3]:
                        L.append(f"    - e.g. `{r['target']['identifier'][:100]}`")
        L.append("")
    # ---- commits ----------------------------------------------------------------------------
    rows = []; page = 1
    while page <= 15:
        res = queries.repo_changes(con, page=page, page_size=100, since_ts=prev, include_targets=True)
        rows += res["items"]
        if len(rows) >= res["page"]["total"] or not res["items"]: break
        page += 1
    rows = [r for r in rows if not (r.get("programs") and all(hide(p["platform"]) for p in r["programs"]))]
    hidden = [r for r in rows if (r.get("kind") in NON_CODE) and not all_pushes]
    shown = [r for r in rows if r not in hidden]
    raw["commits"] = {"fetched": len(rows), "rows": rows, "hidden_non_code": len(hidden)}
    byp = collections.defaultdict(list)
    for r in shown:
        for p in (r.get("programs") or [{"id": "?", "name": "(unlinked)", "platform": "?"}]): byp[p["id"]].append(r)
    L.append(f"## Commits — {len(shown)} pushes in window across {len(byp)} programs" + (f" ({len(hidden)} docs/deps/ci-only pushes hidden; --all-pushes shows them)" if hidden else ""))
    if not shown: L.append("- none")
    for pid, rs in sorted(byp.items(), key=lambda x: -len(x[1])):
        prow = queries.program_row(con, pid)
        pobj = model.program_summary(con, prow) if prow else {"name": pid, "platform": "?", "submission_fee": None, "competition": {"level": "?"}}
        risk = pobj["competition"]["level"]
        tag = "FREE" if _free(pobj) else "PAID-FEE"
        repos = collections.defaultdict(list)
        for r in rs: repos[r["repo"]["name"]].append(r)
        L.append(f"- **[{tag}] [{pobj.get('platform')}] {pobj.get('name')}** (`{pid}`, fee={_fee_str(pobj)}, risk={risk}) ×{len(rs)} pushes")
        bl = brief_line(prow) if tag == "FREE" else None
        if bl: L.append(bl)
        for repo, rr in sorted(repos.items(), key=lambda x: -len(x[1])):
            latest = max(rr, key=lambda r: r["at"])
            kinds = collections.Counter(r.get("kind") or "unclassified" for r in rr)
            files = latest.get("files") or []
            L.append(f"    - {repo} ({len(rr)}: {', '.join(f'{k} {v}' for k, v in kinds.most_common())}) latest {latest['at']} {latest['compare_url']}" + (f" — {len(files)} files, e.g. {', '.join(files[:3])}" if files else ""))
    L.append("")
    # ---- watchlist -------------------------------------------------------------------------
    wl = tools.watch_report(con, prev)
    raw["watchlist"] = [{"program_id": w["program_id"], "changes": len(w["changes"]), "pushes": len(w["pushes"]), "events": len(w["events"])} for w in wl]
    L.append(f"## Your watchlist — {len(wl)} programs")
    if not wl: L.append("- empty (`targetfinder watch add <program>`)")
    for w in wl:
        L.append(f"- **{w['name'] or w['program_id']}** (`{w['program_id']}`): {len(w['changes'])} target changes, {len(w['pushes'])} pushes, {len(w['events'])} events" + (f" — {w['note']}" if w['note'] else ""))
        for c in w["changes"][:max_per_program]: L.append(f"    - {c['at']} {c['type']} `{c['target']['identifier'][:90]}`")
        for r in w["pushes"][:max_per_program]: L.append(f"    - {r['at']} push [{r.get('kind') or '?'}] {r['repo']['name']} {r['compare_url']}")
        for e in w["events"][:max_per_program]: L.append(f"    - {e['observed_at']} {e['change_type']} {db.uj(e['before']) or ''} → {db.uj(e['after']) or ''}")
    L.append("")
    # ---- activity -----------------------------------------------------------------------------
    raw["activity"] = {}
    L.append("## Activity rankings (program-level, unfiltered)")
    for tab, period, sort in (("scope", "7d", "net"), ("rewards", "30d", "change_pct"), ("status", "7d", "newest"), ("payouts", "7d", "paid")):
        res, err = activity.rankings(con, tab, period, sort, "desc", 50)
        if err:
            raw["activity"][tab] = {"rows": [], "error": err}; L.append(f"- **{tab} ({period})**: unavailable ({err})"); continue
        rows_ = [r for r in res["items"] if not hide(r["program"]["platform"])]
        raw["activity"][tab] = {"page": res["page"], "rows": rows_}
        items = []
        for r in rows_[:15]:
            name = r["program"]["name"]; m = r["metrics"]
            if tab == "scope": s = f"{name} +{m['added']}/-{m['removed']} net {m['net']} (in scope {m['in_scope_now']})"
            elif tab == "rewards": s = f"{name} {m['before']['max']}→{m['after']['max']} ({m['change_pct']}%)"
            elif tab == "status": s = f"{name} {m['type']} {m['at'][:10]}"
            else: s = f"{name} +${float(m.get('paid') or 0):,.0f} (total ${float(m.get('total') or 0):,.0f})"
            if r.get("labels"): s += f" [{', '.join(r['labels'])}]"
            items.append(s)
        note = " — only platforms that publish payout counters appear here (HackerOne, HackenProof)" if tab == "payouts" else ""
        L.append(f"- **{tab} ({period}, {len(rows_)})**{note}: " + ("; ".join(items) if items else "none"))
    L.append("")
    # ---- stale platforms ------------------------------------------------------------------------
    stale = [dict(r) for r in con.execute("SELECT platform, COUNT(*) n, MAX(last_seen) seen FROM programs WHERE listed=1 GROUP BY platform ORDER BY n DESC") if r["platform"] not in live]
    raw["stale_platforms"] = stale
    if stale and not live_only:
        ages = []
        for s_ in stale:
            d = config.parse_ts(s_["seen"]); days = (now - d).days if d else "?"
            ages.append(f"{s_['platform']} ({s_['n']}, {days}d)")
        L.append("## Platforms without a collector (data age in days)"); L.append("- " + ", ".join(ages)); L.append("")
    # ---- bottom line ------------------------------------------------------------------------------
    npass = sum(len([r for r in raw[k]["rows"] if _free(r["program"])]) for k in ("added", "readded", "updated"))
    nfree = sum(1 for pid in byp if (lambda pr: pr and ((pr["fee_amount"] or 0) == 0))(queries.program_row(con, pid)))
    L.append("## Bottom line")
    L.append(f"{len(newp)} new programs, {len(fees)} fee changes, {npass} free-submission target rows across Added/Re-added/Updated, {len(shown)} code pushes on {len(byp)} programs ({nfree} free-submission), {len(wl)} watched programs. Raw: `sweep_{stamp}_raw.json`.")
    text = "\n".join(L)
    if write:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"sweep_{stamp}_raw.json"), "w") as f: json.dump(raw, f, default=str)
        md = os.path.join(out_dir, f"SWEEP_{now.strftime('%Y-%m-%d')}.md")
        with open(md, "a" if os.path.exists(md) else "w") as f: f.write(("\n\n---\n\n" if os.path.exists(md) else "") + text + "\n")
        db.kv_set(con, "last_sweep_at", config.iso(now)); con.commit()
        log(f"wrote {md}")
    con.close()
    return text, raw
