"""TargetFinder command line."""
import sys, json, argparse, os
from . import config

def _con():
    from . import db
    return db.connect()

def main(argv=None):
    ap = argparse.ArgumentParser(prog="targetfinder", description="public bug-bounty scope collector and change tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="collect all platforms, diff, poll repos"); s.add_argument("--platforms", help="csv subset"); s.add_argument("--no-repos", action="store_true")
    s = sub.add_parser("repos", help="poll repository HEADs only"); s.add_argument("--limit", type=int)
    s = sub.add_parser("serve", help="run the local HTTP API"); s.add_argument("--host"); s.add_argument("--port", type=int)
    s = sub.add_parser("sweep", help="standing 4-tab + commits + activity sweep"); s.add_argument("--since"); s.add_argument("--no-write", action="store_true"); s.add_argument("--out-dir")
    s.add_argument("--max-per-program", type=int, default=6); s.add_argument("--bulk-threshold", type=int, default=20); s.add_argument("--all-pushes", action="store_true", help="also list docs/deps/ci-only pushes"); s.add_argument("--live-only", action="store_true", help="hide platforms without a collector"); s.add_argument("--sync", action="store_true", help="collect fresh data first")
    s = sub.add_parser("watch", help="watchlist of programs"); s.add_argument("action", choices=["add", "rm", "list", "report"]); s.add_argument("program_id", nargs="?"); s.add_argument("--note"); s.add_argument("--since")
    s = sub.add_parser("clones", help="local git clones vs tracked pushes"); s.add_argument("--dir", default=".", help="directory holding your git clones (two levels deep)"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("diff", help="save the compare diff of a push"); s.add_argument("push_id", type=int); s.add_argument("--out-dir")
    s = sub.add_parser("notify", help="Discord/Telegram alerts"); s.add_argument("action", choices=["setup", "show", "test", "run", "preview"]); s.add_argument("--discord"); s.add_argument("--telegram-token"); s.add_argument("--telegram-chat")
    s.add_argument("--new-targets", choices=["on", "off"]); s.add_argument("--free-only", choices=["on", "off"]); s.add_argument("--min-score", type=int); s.add_argument("--platforms", help="csv, empty = all"); s.add_argument("--new-programs", choices=["on", "off"]); s.add_argument("--watchlist", choices=["on", "off"])
    sub.add_parser("status", help="database counts and collector state")
    s = sub.add_parser("programs", help="list programs"); s.add_argument("--search"); s.add_argument("--platforms"); s.add_argument("--tags"); s.add_argument("--kind"); s.add_argument("--free", action="store_true"); s.add_argument("--page", type=int, default=1); s.add_argument("--page-size", type=int, default=50); s.add_argument("--json", action="store_true")
    s = sub.add_parser("program"); s.add_argument("id"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("targets"); s.add_argument("id"); s.add_argument("--eligibility", default="all"); s.add_argument("--type"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("search"); s.add_argument("q"); s.add_argument("--mode", default="contains"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("changes"); s.add_argument("--type", choices=["added", "readded", "updated", "removed"]); s.add_argument("--since"); s.add_argument("--platforms"); s.add_argument("--free", action="store_true"); s.add_argument("--page-size", type=int, default=50); s.add_argument("--after-id", type=int); s.add_argument("--json", action="store_true")
    s = sub.add_parser("commits"); s.add_argument("--since"); s.add_argument("--platforms"); s.add_argument("--free", action="store_true"); s.add_argument("--page-size", type=int, default=50); s.add_argument("--after-id", type=int); s.add_argument("--json", action="store_true")
    s = sub.add_parser("rankings", help="ranked programs per view"); s.add_argument("view", choices=["scope", "rewards", "updates", "status", "payouts", "response", "all"]); s.add_argument("--period"); s.add_argument("--sort"); s.add_argument("--direction", default="desc"); s.add_argument("--limit", type=int, default=50); s.add_argument("--json", action="store_true")
    s = sub.add_parser("leads", help="programs by lead tier"); s.add_argument("tier", choices=["top", "hot", "solid", "possible"]); s.add_argument("--free", action="store_true"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("resolve"); s.add_argument("text")
    s = sub.add_parser("payouts"); s.add_argument("id")
    sub.add_parser("mcp", help="run the stdio MCP server")
    s = sub.add_parser("init", help="first run: create the database, collect every platform, write public/ snapshots"); s.add_argument("--out", default="public"); s.add_argument("--no-repos", action="store_true")
    s = sub.add_parser("export-snapshot", help="write JSON snapshots of the tracked state"); s.add_argument("--out", default="public")
    s = sub.add_parser("import-snapshot", help="load JSON snapshots into the database"); s.add_argument("--in", dest="indir", default="public")
    a = ap.parse_args(argv)
    if a.cmd == "sync":
        from . import sync
        st = sync.run(platforms=a.platforms.split(",") if a.platforms else None, repo_poll=not a.no_repos)
        print(json.dumps(st, indent=1)); return
    if a.cmd == "repos":
        from . import repowatch
        con = _con(); print(json.dumps(repowatch.poll(con, limit=a.limit))); return
    if a.cmd == "serve":
        from . import api
        api.serve(a.host, a.port); return
    if a.cmd == "sweep":
        from . import sweep
        if a.sync:
            from . import sync
            sync.run()
        text, raw = sweep.run(a.since, a.out_dir, write=not a.no_write, max_per_program=a.max_per_program, bulk_threshold=a.bulk_threshold, all_pushes=a.all_pushes, live_only=a.live_only); print(text); return
    if a.cmd == "notify":
        from . import notify
        cfg = notify.load_config()
        if a.action == "setup":
            if a.discord: cfg["discord_webhook"] = a.discord
            if a.telegram_token and a.telegram_chat: cfg["telegram"] = {"bot_token": a.telegram_token, "chat_id": a.telegram_chat}
            r = cfg["rules"]
            if a.new_targets: r["new_targets"]["enabled"] = a.new_targets == "on"
            if a.free_only: r["new_targets"]["free_only"] = a.free_only == "on"
            if a.min_score is not None: r["new_targets"]["min_score"] = a.min_score
            if a.platforms is not None: r["new_targets"]["platforms"] = [x for x in a.platforms.split(",") if x]
            if a.new_programs: r["new_programs"] = a.new_programs == "on"
            if a.watchlist: r["watchlist"] = a.watchlist == "on"
            notify.save_config(cfg); print(f"saved {notify.CONFIG_PATH}")
        if a.action in ("setup", "show"):
            shown = dict(cfg); shown["discord_webhook"] = (shown.get("discord_webhook") or "")[:40] + ("…" if shown.get("discord_webhook") else "(not set)")
            if shown.get("telegram"): shown["telegram"] = {"bot_token": "set", "chat_id": shown["telegram"].get("chat_id")}
            print(json.dumps(shown, indent=1)); return
        if a.action == "test":
            print(notify.send(cfg, "TargetFinder: test message — notifications are working.")); return
        if a.action == "preview":
            print(json.dumps(notify.run(_con(), dry_run=True), indent=1)); return
        if a.action == "run":
            print(json.dumps(notify.run(_con()), indent=1)); return
    if a.cmd in ("watch", "clones", "diff"):
        from . import tools
        con = _con()
        if a.cmd == "watch":
            if a.action == "add":
                pid, cands = tools.watch_add(con, a.program_id, a.note)
                print(f"watching {pid}" if pid else f"ambiguous: {json.dumps(cands, indent=1)}"); return
            if a.action == "rm": print(f"removed {tools.watch_rm(con, a.program_id)}"); return
            if a.action == "list":
                for w in tools.watch_list(con): print(f"  {w['program_id']:50s} {w['name'] or '?'} ({w['platform']}) {w['note'] or ''}")
                return
            import datetime as dt
            since = a.since or config.iso(config.utcnow() - dt.timedelta(days=7))
            for w in tools.watch_report(con, since):
                print(f"== {w['program_id']} ({w['name']}) since {since}: {len(w['changes'])} target changes, {len(w['pushes'])} pushes, {len(w['events'])} events")
                for c in w["changes"][:10]: print(f"    {c['at']} {c['type']:8s} {c['target']['identifier'][:90]}")
                for r in w["pushes"][:10]: print(f"    {r['at']} push [{r.get('kind') or '?'}] {r['repo']['name']} {r['compare_url']}")
                for e in w["events"]: print(f"    {e['observed_at']} {e['change_type']} {e['before'] or ''} -> {e['after'] or ''}")
            return
        if a.cmd == "clones":
            rep = tools.clones_report(con, a.dir)
            if a.json: print(json.dumps(rep, indent=1)); return
            for c in rep:
                if not c["tracked"]: print(f"  {c['path']}: {c['repo']} not in any tracked program scope"); continue
                print(f"  {c['path']}: {c['repo']} programs={','.join(c['programs'])} local={c['local_head']} remote={c['remote_head']} {'STALE' if c['stale'] else 'up to date'} pushes_since_local={len(c['pushes_since_local'])}")
                for p in c["pushes_since_local"][:5]: print(f"      {p['at']} [{p['kind'] or '?'}] {p['compare_url']}")
            return
        if a.cmd == "diff":
            path, n, files = tools.pull_diff(con, a.push_id, a.out_dir); print(f"saved {path} ({n} bytes, {len(files)} files)"); [print("   ", f) for f in files[:40]]; return
    if a.cmd == "mcp":
        from . import mcp_server
        mcp_server.main(); return
    if a.cmd == "init":
        from . import sync, snapshot, queries
        from .collectors import load_all
        config.ensure_dirs()
        print(f"database: {config.DB_PATH}\ncollectors: {', '.join(load_all())}\ncollecting (first run records a baseline; expect no change events)...", flush=True)
        st = sync.run(repo_poll=not a.no_repos)
        print(json.dumps({k: (v.get("programs") if isinstance(v, dict) and "programs" in v else v) for k, v in st.items()}, indent=1))
        con = _con(); out = a.out if os.path.isabs(a.out) else os.path.join(config.ROOT, a.out)
        snapshot.export_snapshot(con, out); st = queries.status(con); con.close()
        print(f"snapshot written to {out}\nstale collectors: {st['stale_platforms'] or 'none'}\nnext: `targetfinder sweep` for a report, `targetfinder schedule install --every 1` to keep it fresh"); return
    if a.cmd == "export-snapshot":
        from . import snapshot
        out = a.out if os.path.isabs(a.out) else os.path.join(config.ROOT, a.out)
        print(json.dumps(snapshot.export_snapshot(_con(), out))); print(out); return
    if a.cmd == "import-snapshot":
        from . import snapshot
        src = a.indir if os.path.isabs(a.indir) else os.path.join(config.ROOT, a.indir)
        if not os.path.exists(os.path.join(src, "meta.json")): print(f"no snapshot at {src} (meta.json missing)"); sys.exit(1)
        print(json.dumps(snapshot.import_snapshot(_con(), src))); return
    from . import queries, activity
    con = _con()
    if a.cmd == "status":
        print(json.dumps(queries.status(con), indent=1)); return
    if a.cmd == "programs":
        res = queries.list_programs(con, queries.csv(a.platforms), queries.csv(a.tags), a.search, None, a.page, a.page_size, a.kind, free_submission_only=a.free, full=False)
        if a.json: print(json.dumps(res, indent=1)); return
        print(f"{res['page']['total']} programs (page {a.page})")
        for p in res["items"]:
            fee = p.get("submission_fee") or {}
            print(f"  {p['id']:50s} {p.get('kind') or '':7s} {p.get('status') or '':8s} max={p['rewards'].get('max')} fee={fee.get('amount', '?')} {fee.get('unit') or ''} competition={p['competition']['level']} targets={p['scope_counts']['in_scope']} tags={','.join(p.get('tags') or [])}")
        return
    if a.cmd == "program":
        res = queries.get_program(con, a.id)
        if res is None: print("not found"); sys.exit(1)
        print(json.dumps(res if a.json else {k: res[k] for k in ("id", "name", "platform", "url", "kind", "status", "rewards", "submission_fee", "report_count", "competition", "tags", "scope_counts", "scope_activity", "payouts")}, indent=1)); return
    if a.cmd == "targets":
        res = queries.program_targets(con, a.id, a.eligibility, a.type, page_size=500)
        if res is None: print("not found"); sys.exit(1)
        if a.json: print(json.dumps(res, indent=1)); return
        for t in res["items"]:
            sc = t.get("score") or {}; cp = t.get("competition") or {}
            print(f"  {'IN ' if t['in_scope'] else 'OUT'} {'$' if t['bounty_eligible'] else ' '} {t['type'] or '':14s} {sc.get('value') or '':>3} {cp.get('level') or '':7s} {t['identifier']}")
        return
    if a.cmd == "search":
        res = queries.search_targets(con, a.q, a.mode, page_size=100)
        if a.json: print(json.dumps(res, indent=1)); return
        for r in res["items"]: print(f"  {r['program']['id']:45s} {r['target']['type'] or '':14s} {r['target']['identifier']}")
        print(f"{res['page']['total']} results"); return
    if a.cmd == "changes":
        res = queries.target_changes(con, a.type, page_size=a.page_size, after_id=a.after_id, platforms=queries.csv(a.platforms), free_submission_only=a.free, since_ts=a.since)
        if a.json: print(json.dumps(res, indent=1)); return
        for r in res["items"]:
            t = r["target"]; p = r["program"]; fee = p.get("submission_fee") or {}
            print(f"  {r['at']} {r['type']:8s} [{p['platform']}] {(p['name'] or '')[:30]:30s} fee={fee.get('amount', '?')} {t.get('type') or '':13s} {(t.get('score') or {}).get('value') or '':>3} {t['identifier'][:90]}")
        print(json.dumps(res["page"])); return
    if a.cmd == "commits":
        res = queries.repo_changes(con, platforms=queries.csv(a.platforms), free_submission_only=a.free, page_size=a.page_size, after_id=a.after_id, since_ts=a.since, include_targets=False)
        if a.json: print(json.dumps(res, indent=1)); return
        for r in res["items"]:
            print(f"  {r['at']} [{r.get('kind') or '?':6s}] {', '.join(p['id'] for p in r['programs']) or '-':45s} {r['repo']['name']:40s} {r['compare_url']}")
        print(json.dumps(res["page"])); return
    if a.cmd == "rankings":
        res, err = activity.rankings(con, a.view, a.period, a.sort, a.direction, a.limit)
        if err: print("error:", err); sys.exit(2)
        if a.json: print(json.dumps(res, indent=1)); return
        for r in res["items"]:
            print(f"  {(r['program']['name'] or '')[:40]:40s} {json.dumps(r['metrics'])[:120]} {r['labels']}")
        return
    if a.cmd == "leads":
        res = queries.leads(con, a.tier, page_size=100, free_submission_only=a.free)
        if a.json: print(json.dumps(res, indent=1)); return
        for p in res["items"]: print(f"  {p['lead']['score']:5.1f} {p['id']:50s} {p['lead']['reason']}")
        return
    if a.cmd == "resolve":
        print(json.dumps(queries.resolve_program(con, a.text), indent=1)); return
    if a.cmd == "payouts":
        prow = queries.program_row(con, a.id)
        if not prow: print("not found"); sys.exit(1)
        print(json.dumps(activity.program_payouts(con, prow), indent=1)); return

if __name__ == "__main__":
    main()
