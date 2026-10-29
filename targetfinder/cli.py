"""TargetFinder command line.

Exit codes: 0 ok, 1 not found, 2 usage error, 3 collector or network failure.
Every lookup prints a readable view by default and JSON with --json.
"""
import sys, json, argparse, os, datetime as dt
from . import config

EXIT_OK, EXIT_NOT_FOUND, EXIT_USAGE, EXIT_FAILURE = 0, 1, 2, 3

COMMAND_HELP = {
    "init": "first run: create the database, collect every platform, write public/ snapshots",
    "schedule": "install, remove or show the hourly OS scheduler job (systemd, launchd, schtasks)",
    "notify": "Discord / Telegram alerts: setup, show, test, preview, run",
    "serve": "run the local HTTP API (default http://127.0.0.1:8787/api/)",
    "mcp": "run the MCP server over stdio for AI assistants",
    "sweep": "the report: new programs, fee changes, added/updated targets, commits, watchlist, rankings",
    "sync": "collect every platform once, record changes, poll repositories, send alerts",
    "loop": "foreground loop: sync then sweep every N hours until Ctrl-C",
    "watch": "your watchlist: add, rm, list, report",
    "changes": "target changes (added, readded, updated, removed) with scores",
    "commits": "pushes to in-scope repositories, classified code / docs / deps / ci / tests",
    "leads": "programs worth a look, by tier: top, hot, solid, possible",
    "clones": "which of your local git clones are behind the tracked HEAD",
    "diff": "save the compare diff of one push",
    "programs": "list programs with rewards, fee, competition and scope size",
    "program": "one program as a card: rewards, fee, scope, activity, best targets",
    "targets": "the scope of one program with scores",
    "search": "which programs list an asset (domain, repo, package, contract)",
    "resolve": "find a program id from a name or handle",
    "rankings": "activity views: scope, rewards, updates, status, payouts, response, all",
    "payouts": "payout signals and platform-published facts for one program",
    "status": "database counts, collector health, last run per platform",
    "repos": "poll repository HEADs only",
    "export-snapshot": "write JSON snapshots of the tracked state (what the GitHub Action publishes)",
    "import-snapshot": "load JSON snapshots into the database (start from published data)",
}
GROUPS = [("Setup", ["init", "schedule", "notify", "serve", "mcp"]),
          ("Daily use", ["sweep", "sync", "loop", "watch", "changes", "commits", "leads", "clones", "diff"]),
          ("Lookups", ["programs", "program", "targets", "search", "resolve", "rankings", "payouts", "status"]),
          ("Data", ["repos", "export-snapshot", "import-snapshot"])]
QUICKSTART = [("targetfinder init", "first run: collect every platform (10–30 min)"),
              ("targetfinder sweep", "today's report; add --sync to collect fresh data first"),
              ("targetfinder schedule install --every 1", "keep the data fresh with the OS scheduler"),
              ("targetfinder leads top --free", "programs worth a look, free to submit")]

# ---- presentation helpers ---------------------------------------------------------------------
COLORS = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}

def use_color():
    return os.environ.get("NO_COLOR") is None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

def paint(text, color):
    return f"\x1b[{COLORS[color]}m{text}\x1b[0m" if use_color() else str(text)

def money(amount, currency="USD"):
    if amount is None:
        return "-"
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    s = f"{a:,.0f}" if a == int(a) else f"{a:,.2f}".rstrip("0").rstrip(".")
    sym = SYMBOLS.get((currency or "USD").upper())
    return f"{sym}{s}" if sym else f"{s} {currency or ''}".strip()

def num(n):
    return "-" if n is None else (f"{n:,}" if isinstance(n, int) else str(n))

def dash(v, width=0):
    s = "-" if v in (None, "", [], {}) else (", ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v))
    return s[:width] if width else s

def when(ts, length=16):
    """2026-10-29T14:35:50Z -> 2026-10-29 14:35"""
    if not ts: return "-"
    return str(ts).replace("T", " ").rstrip("Z")[:length]

def dur(seconds):
    if seconds is None: return "-"
    s = int(round(float(seconds)))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"

def table(rows, headers=None, right=()):
    rows = [[dash(c) if not isinstance(c, str) else c for c in r] for r in rows]
    all_rows = ([list(headers)] if headers else []) + rows
    if not all_rows: return ""
    widths = [max(len(r[i]) for r in all_rows) for i in range(len(all_rows[0]))]
    out = []
    for n, r in enumerate(all_rows):
        cells = [(c.rjust(w) if i in right else c.ljust(w)) for i, (c, w) in enumerate(zip(r, widths))]
        out.append("  ".join(cells).rstrip())
        if headers and n == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)

def reward_label(rw):
    rw = rw or {}
    if rw.get("status") == "no_reward" or rw.get("offers_bounty") is False:
        return "none"
    cur = rw.get("currency") or "USD"
    if rw.get("max"): return f"up to {money(rw['max'], cur)}"
    if rw.get("min"): return f"from {money(rw['min'], cur)}"
    return "-"

def fee_label(fee):
    if not fee or fee.get("amount") is None: return "-"
    if float(fee["amount"] or 0) == 0: return "free"
    return f"{dash(fee['amount'])} {fee.get('unit') or ''}".strip() + (" deposit" if fee.get("kind") == "deposit" else "")

def emit(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False))

def footer(page, flag_page="--page", flag_cursor="--after-id"):
    """'showing 50 of 1,204 (page 1 of 25), next: --page 2' or the cursor equivalent."""
    if page.get("mode") == "cursor":
        s = f"showing {num(page.get('returned'))} of {num(page.get('total'))}"
        return s + (f", next: {flag_cursor} {page['next_after_id']}" if page.get("has_more") and page.get("next_after_id") is not None else "")
    s = f"showing {num(page.get('returned'))} of {num(page.get('total'))}"
    if page.get("total_pages", 1) > 1:
        s += f" (page {page.get('page')} of {page.get('total_pages')})"
        if page.get("has_more"): s += f", next: {flag_page} {page.get('page', 1) + 1}"
    return s

def help_text():
    lines = [paint(f"targetfinder {config.VERSION}", "bold") + " — find bug-bounty targets worth your time, from public data, on your own machine", "",
             "Quickstart:"]
    w = max(len(c) for c, _ in QUICKSTART)
    lines += [f"  {c.ljust(w)}   {d}" for c, d in QUICKSTART]
    for title, names in GROUPS:
        lines += ["", paint(title, "bold")]
        w = max(len(n) for n in names)
        lines += [f"  {n.ljust(w)}   {COMMAND_HELP[n]}" for n in names]
    lines += ["", "Options: -h / --help, --version. `targetfinder <command> -h` shows that command's options.",
              "Lookups print a readable view; add --json for machine-readable output. Nothing is hidden by default:",
              "a paid submission fee is the only thing that lowers a program's priority, and --free hides those."]
    return "\n".join(lines) + "\n"

class Parser(argparse.ArgumentParser):
    def format_help(self):
        return help_text()

# ---- parser -----------------------------------------------------------------------------------
def build_parser():
    ap = Parser(prog="targetfinder", description="public bug-bounty scope collector and change tracker")
    ap.add_argument("--version", action="version", version=f"targetfinder {config.VERSION}")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>", parser_class=argparse.ArgumentParser)
    def cmd(name, **kw):
        return sub.add_parser(name, help=COMMAND_HELP[name], description=COMMAND_HELP[name], **kw)
    def jsonflag(p): p.add_argument("--json", action="store_true", help="print JSON instead of the readable view")

    s = cmd("init"); s.add_argument("--out", default="public", help="snapshot directory (default: public/)"); s.add_argument("--no-repos", action="store_true", help="skip the repository poll"); s.add_argument("--verbose", action="store_true", help="show every collector log line"); jsonflag(s)
    s = cmd("schedule"); s.add_argument("action", choices=["install", "remove", "status"]); s.add_argument("--every", type=int, default=1, help="hours between runs (default 1)"); jsonflag(s)
    s = cmd("notify"); s.add_argument("action", choices=["setup", "show", "test", "run", "preview"], help="setup saves channels and rules; preview shows what run would send")
    s.add_argument("--discord", metavar="WEBHOOK_URL", help="Discord webhook URL"); s.add_argument("--telegram-token", metavar="BOT_TOKEN"); s.add_argument("--telegram-chat", metavar="CHAT_ID")
    s.add_argument("--new-targets", choices=["on", "off"], help="alert on fresh targets"); s.add_argument("--free-only", choices=["on", "off"], help="fresh-target alerts only for free-submission programs")
    s.add_argument("--min-score", type=int, help="fresh-target alerts only at or above this score"); s.add_argument("--platforms", help="csv of platforms for fresh-target alerts, empty = all")
    s.add_argument("--new-programs", choices=["on", "off"], help="alert on brand-new programs"); s.add_argument("--watchlist", choices=["on", "off"], help="alert on watchlist changes"); jsonflag(s)
    s = cmd("serve"); s.add_argument("--host", help=f"bind address (default {config.API_HOST})"); s.add_argument("--port", type=int, help=f"port (default {config.API_PORT})")
    cmd("mcp")
    s = cmd("sweep"); s.add_argument("--since", metavar="ISO", help="window start (default: last 28 h)"); s.add_argument("--sync", action="store_true", help="collect fresh data first")
    s.add_argument("--no-write", action="store_true", help="print only, do not save the report under sweeps/"); s.add_argument("--out-dir", help="where to save the report (default sweeps/)")
    s.add_argument("--max-per-program", type=int, default=6, help="target rows shown per program (default 6)"); s.add_argument("--bulk-threshold", type=int, default=20, help="collapse programs with more changes than this (default 20)")
    s.add_argument("--all-pushes", action="store_true", help="also list docs/deps/ci-only pushes"); s.add_argument("--live-only", action="store_true", help="hide platforms without a collector")
    s = cmd("sync"); s.add_argument("--platforms", help="csv subset of platforms"); s.add_argument("--no-repos", action="store_true", help="skip the repository poll"); s.add_argument("--verbose", action="store_true", help="show every collector log line"); jsonflag(s)
    s = cmd("loop"); s.add_argument("--every", type=float, default=1.0, help="hours between runs (default 1)")
    s = cmd("watch"); s.add_argument("action", choices=["add", "rm", "list", "report"]); s.add_argument("program_id", nargs="?", help="program id, handle or name"); s.add_argument("--note", help="free text stored with the entry"); s.add_argument("--since", metavar="ISO", help="report window start (default 7 days)"); jsonflag(s)
    for name in ("changes", "commits"):
        s = cmd(name)
        if name == "changes": s.add_argument("--type", choices=["added", "readded", "updated", "removed"])
        s.add_argument("--since", metavar="ISO"); s.add_argument("--platforms", help="csv of platforms"); s.add_argument("--free", action="store_true", help="free-submission programs only")
        s.add_argument("--page-size", type=int, default=50); s.add_argument("--after-id", type=int, help="cursor mode: rows after this id"); jsonflag(s)
    s = cmd("leads"); s.add_argument("tier", choices=["top", "hot", "solid", "possible"]); s.add_argument("--free", action="store_true", help="free-submission programs only"); jsonflag(s)
    s = cmd("clones"); s.add_argument("--dir", default=".", help="directory holding your git clones (two levels deep)"); jsonflag(s)
    s = cmd("diff"); s.add_argument("push_id", type=int, help="id from `commits`"); s.add_argument("--out-dir")
    s = cmd("programs"); s.add_argument("--search", help="name or handle contains"); s.add_argument("--platforms", help="csv of platforms"); s.add_argument("--tags", help="csv of scope tags (api, wildcard, mobile, ...)"); s.add_argument("--kind", choices=["bounty", "vdp", "contest", "challenge"])
    s.add_argument("--free", action="store_true", help="free-submission programs only"); s.add_argument("--page", type=int, default=1); s.add_argument("--page-size", type=int, default=50); jsonflag(s)
    s = cmd("program"); s.add_argument("id", help="program id, handle or name"); jsonflag(s)
    s = cmd("targets"); s.add_argument("id", help="program id, handle or name"); s.add_argument("--eligibility", default="all", choices=["all", "eligible", "ineligible"], help="eligible = in scope and bounty-eligible"); s.add_argument("--type", help="target type (domain, api, source code, ...)"); jsonflag(s)
    s = cmd("search"); s.add_argument("q", help="asset text"); s.add_argument("--mode", default="contains", choices=["contains", "exact", "suffix", "wildcard"], help="suffix matches subdomains of a domain; wildcard takes * patterns"); jsonflag(s)
    s = cmd("resolve"); s.add_argument("text", help="name, handle or id fragment"); jsonflag(s)
    s = cmd("rankings"); s.add_argument("view", choices=["scope", "rewards", "updates", "status", "payouts", "response", "all"]); s.add_argument("--period", help="24h, 7d, 30d, 90d, all (depends on the view)"); s.add_argument("--sort"); s.add_argument("--direction", default="desc", choices=["asc", "desc"]); s.add_argument("--limit", type=int, default=50); jsonflag(s)
    s = cmd("payouts"); s.add_argument("id", help="program id, handle or name"); jsonflag(s)
    s = cmd("status"); jsonflag(s)
    s = cmd("repos"); s.add_argument("--limit", type=int, help="poll at most N repositories"); jsonflag(s)
    s = cmd("export-snapshot"); s.add_argument("--out", default="public", help="output directory (default public/)"); s.add_argument("--exclude-platforms", help="csv of platforms to leave out of the published files"); jsonflag(s)
    s = cmd("import-snapshot"); s.add_argument("--in", dest="indir", default="public", help="snapshot directory (default public/)"); jsonflag(s)
    return ap

# ---- command bodies ---------------------------------------------------------------------------
def _con():
    from . import db
    return db.connect()

def _resolve_or_exit(con, text):
    from . import queries
    row = queries.program_row(con, text)
    if row is None:
        cands = queries.resolve_program(con, text, limit=5)
        if len(cands) == 1:
            row = queries.program_row(con, cands[0]["id"])
        else:
            print(f"program not found: {text}" + ("\ndid you mean: " + ", ".join(c["id"] for c in cands) if cands else ""))
            return None
    return row

def print_sync_stats(st):
    """Per-platform table from sync.run's stats. Returns the number of failed collectors."""
    rows = []; failed = 0; total = 0
    for name, s in st.items():
        if name in ("repos", "notify") or not isinstance(s, dict):
            continue
        total += 1
        if "error" in s:
            failed += 1; rows.append([paint("✗", "red"), name, "-", "-", "-", "-", "-", s["error"][:80]])
        else:
            rows.append([paint("✓", "green"), name, num(s.get("programs")), num(s.get("added")), num(s.get("readded")), num(s.get("updated")), num(s.get("removed")), dur(s.get("seconds"))])
    if rows:
        print(table(rows, ["", "platform", "programs", "added", "readded", "updated", "removed", "time"], right=(2, 3, 4, 5, 6)))
    r = st.get("repos")
    if isinstance(r, dict):
        print("repositories: " + ("poll failed: " + r["error"] if "error" in r else f"{num(r.get('polled'))} polled, {num(r.get('changed'))} pushed, {num(r.get('initialized'))} new, {num(r.get('errors'))} errors" + (f", {r['retired']} retired" if r.get("retired") else "")))
    n = st.get("notify")
    if isinstance(n, dict) and n.get("configured"):
        print(f"alerts: {num(n.get('sent'))} sent")
    return failed, total

def cmd_sync(a):
    from . import sync
    st = sync.run(platforms=a.platforms.split(",") if a.platforms else None, repo_poll=not a.no_repos, log=print if a.verbose else (lambda m: None))
    if a.json: emit(st)
    else:
        failed, total = print_sync_stats(st)
        return EXIT_FAILURE if total and failed == total else EXIT_OK
    return EXIT_OK

def cmd_init(a):
    from . import sync, snapshot, queries
    from .collectors import load_all
    config.ensure_dirs()
    names = list(load_all())
    out = a.out if os.path.isabs(a.out) else os.path.join(config.HOME_DIR, a.out)
    print(f"database:   {config.DB_PATH}\ncollectors: {', '.join(names)}\nsnapshot:   {out}\n")
    print("Collecting. The first run records a baseline, so expect no change events yet.")
    def on_platform(name, st):
        if "error" in st: print(f"  {paint('✗', 'red')} {name:12s} failed: {st['error'][:100]}", flush=True)
        else: print(f"  {paint('✓', 'green')} {name:12s} {num(st.get('programs')):>5} programs  {num(st.get('baseline') or st.get('added')):>7} targets  {dur(st.get('seconds')):>7}", flush=True)
    st = sync.run(repo_poll=not a.no_repos, log=print if a.verbose else (lambda m: None), on_platform=on_platform)
    r = st.get("repos")
    if isinstance(r, dict) and "error" not in r:
        print(f"  {paint('✓', 'green')} {'repos':12s} {num(r.get('polled')):>5} polled   {num(r.get('errors')):>7} errors")
    con = _con(); snapshot.export_snapshot(con, out); status = queries.status(con); con.close()
    if a.json:
        emit({"stats": st, "snapshot": out, "stale_platforms": status["stale_platforms"]}); return EXIT_OK
    print(f"\nsnapshot written to {out}")
    stale = status["stale_platforms"]
    print(("stale collectors: " + ", ".join(stale)) if stale else "all collectors healthy")
    print("\nnext:\n  targetfinder sweep                       the report\n  targetfinder leads top --free            where to start\n  targetfinder schedule install --every 1  keep it fresh")
    failed = [k for k, v in st.items() if k not in ("repos", "notify") and isinstance(v, dict) and "error" in v]
    return EXIT_FAILURE if failed and len(failed) == len(names) else EXIT_OK

def cmd_status(a, con):
    from . import queries
    st = queries.status(con)
    if a.json: emit(st); return EXIT_OK
    c = st["counts"]
    print(paint(f"TargetFinder {st['version']}", "bold") + f"  {st['db']}")
    print(f"programs {num(c['programs'])}   targets {num(c['targets'])}   target changes {num(c['target_changes'])}   repos {num(c['repos'])}   pushes {num(c['repo_changes'])}   program events {num(c['program_events'])}")
    ls = st.get("last_sync") or {}
    print(f"tracking since {when(st.get('tracking_since'))}   last sync {when(ls.get('finished_at') or ls.get('started_at'))} ({ls.get('status') or 'never'})")
    rows = []
    for p in st["platforms"]:
        if not p["collector"]: state = paint("no collector", "dim")
        elif p["stale"]: state = paint("stale", "yellow") + (f": {p['last_error'][:50]}" if p.get("last_error") else "")
        else: state = paint("ok", "green")
        rows.append([p["platform"], num(p["programs"]), "yes" if p["collector"] else "off", when(p.get("last_ok_at")), state])
    print("\n" + table(rows, ["platform", "programs", "collector", "last ok", "state"], right=(1,)))
    return EXIT_OK

def cmd_programs(a, con):
    from . import queries
    res = queries.list_programs(con, queries.csv(a.platforms), queries.csv(a.tags), a.search, None, a.page, a.page_size, a.kind, free_submission_only=a.free, full=False)
    if a.json: emit(res); return EXIT_OK
    rows = [[p["id"], dash(p.get("name"), 28), p.get("kind"), p.get("status"), reward_label(p.get("rewards")), fee_label(p.get("submission_fee")), (p.get("competition") or {}).get("level"), num((p.get("scope_counts") or {}).get("in_scope")), dash(p.get("tags"), 40)] for p in res["items"]]
    if not rows: print("no programs match"); return EXIT_OK
    print(table(rows, ["id", "name", "kind", "status", "reward", "fee", "competition", "targets", "tags"], right=(7,)))
    print(footer(res["page"]))
    return EXIT_OK

def cmd_program(a, con):
    from . import queries
    row = _resolve_or_exit(con, a.id)
    if row is None: return EXIT_NOT_FOUND
    res = queries.get_program(con, row["id"])
    if a.json: emit(res); return EXIT_OK
    rw = res.get("rewards") or {}; fee = res.get("submission_fee") or {}; comp = res.get("competition") or {}; rc = res.get("report_count") or {}
    print(paint(res["name"] or res["id"], "bold") + f"  ({res['platform']}, {res.get('kind') or '?'}, {res.get('status') or '?'})")
    kv = [("id", res["id"]), ("link", res.get("url")), ("rewards", reward_label(rw) + (f" (min {money(rw.get('min'), rw.get('currency'))})" if rw.get("min") and rw.get("max") else "")),
          ("fee", fee_label(fee)), ("competition", f"{comp.get('level', '-')} — {comp.get('reason', '')}".rstrip(" —")),
          ("reports", f"{num(rc.get('value'))} {rc.get('label') or ''}".strip() if rc.get("value") is not None else "-"), ("tags", dash(res.get("tags")))]
    print(table([[k, v] for k, v in kv]))
    sc = res.get("scope_counts") or {}; sa = res.get("scope_activity") or {}; po = res.get("payouts") or {}
    print(f"\nScope: {num(sc.get('in_scope'))} in scope, {num(sc.get('out_of_scope'))} out, {num(sc.get('bounty_eligible'))} bounty-eligible, {num(sc.get('wildcards'))} wildcards"
          f"; last change {when(sa.get('last_change_at'))}, added 7d {num(sa.get('added_7d'))}, removed 7d {num(sa.get('removed_7d'))}")
    if any(po.get(k) for k in ("total", "last_30d", "last_7d")):
        print(f"Payouts: total {money(po.get('total'))}, last 30d {money(po.get('last_30d'))}, last 7d {money(po.get('last_7d'))}")
    tg = queries.program_targets(con, res["id"], "all", None, page_size=500) or {"items": []}
    best = sorted([t for t in tg["items"] if t.get("in_scope")], key=lambda t: -((t.get("score") or {}).get("value") or 0))[:5]
    if best:
        print("\nBest targets by score:")
        print(table([[num((t.get("score") or {}).get("value")), dash(t.get("type")), (t.get("competition") or {}).get("level"), t["identifier"][:90]] for t in best], right=(0,)))
        print(f"targets {res['id']} shows all {num(sc.get('total'))}")
    return EXIT_OK

def cmd_targets(a, con):
    from . import queries
    row = _resolve_or_exit(con, a.id)
    if row is None: return EXIT_NOT_FOUND
    res = queries.program_targets(con, row["id"], a.eligibility, a.type, page_size=500)
    if a.json: emit(res); return EXIT_OK
    rows = [["in" if t["in_scope"] else "out", "$" if t["bounty_eligible"] else "", dash(t.get("type")), num((t.get("score") or {}).get("value")), (t.get("competition") or {}).get("level"), t["identifier"][:100]] for t in res["items"]]
    if not rows: print("no targets match"); return EXIT_OK
    print(table(rows, ["scope", "$", "type", "score", "competition", "target"], right=(3,)))
    print(footer(res["page"]))
    return EXIT_OK

def cmd_search(a, con):
    from . import queries
    res = queries.search_targets(con, a.q, a.mode, page_size=100)
    if a.json: emit(res); return EXIT_OK
    rows = [[r["program"]["id"], dash(r["program"].get("name"), 28), dash(r["target"].get("type")), r["target"]["identifier"][:90]] for r in res["items"]]
    if not rows: print(f"no program lists {a.q!r}"); return EXIT_OK
    print(table(rows, ["program", "name", "type", "target"])); print(footer(res["page"]))
    return EXIT_OK

def cmd_changes(a, con):
    from . import queries
    res = queries.target_changes(con, a.type, page_size=a.page_size, after_id=a.after_id, platforms=queries.csv(a.platforms), free_submission_only=a.free, since_ts=a.since)
    if a.json: emit(res); return EXIT_OK
    rows = []
    for r in res["items"]:
        t = r["target"]; p = r["program"]
        rows.append([when(r["at"]), r["type"], p["platform"], dash(p.get("name"), 28), fee_label(p.get("submission_fee")), dash(t.get("type")), num((t.get("score") or {}).get("value")), t["identifier"][:80]])
    if not rows:
        print("no target changes" + (" in this window" if a.since else " yet: changes appear from the second sync on")); return EXIT_OK
    print(table(rows, ["when", "type", "platform", "program", "fee", "target type", "score", "target"], right=(6,))); print(footer(res["page"]))
    return EXIT_OK

def cmd_commits(a, con):
    from . import queries
    res = queries.repo_changes(con, platforms=queries.csv(a.platforms), free_submission_only=a.free, page_size=a.page_size, after_id=a.after_id, since_ts=a.since, include_targets=False)
    if a.json: emit(res); return EXIT_OK
    rows = [[num(r.get("id")), when(r["at"]), dash(r.get("kind")), ", ".join(p["id"] for p in r["programs"])[:45] or "-", r["repo"]["name"][:40], r["compare_url"]] for r in res["items"]]
    if not rows: print("no pushes recorded" + (" in this window" if a.since else " yet: the first poll only records HEADs")); return EXIT_OK
    print(table(rows, ["id", "when", "kind", "programs", "repository", "compare"])); print(footer(res["page"]))
    return EXIT_OK

def cmd_rankings(a, con):
    from . import activity
    res, err = activity.rankings(con, a.view, a.period, a.sort, a.direction, a.limit)
    if err: print("error:", err); return EXIT_USAGE
    if a.json: emit(res); return EXIT_OK
    if not res["items"]:
        print(f"no ranked programs for {res['view']} over {res['period']}: rankings need change history, run sync a few more times (or wait for the scheduler)"); return EXIT_OK
    rows = [[dash(r["program"].get("name"), 36), r["program"]["id"], " ".join(f"{k}={v}" for k, v in (r.get("metrics") or {}).items() if v not in (None, 0, 0.0, ""))[:70], dash(r.get("labels"))] for r in res["items"]]
    print(f"{res['view']} over {res['period']}, sorted by {res.get('sort')} {res.get('direction')}")
    print(table(rows, ["program", "id", "metrics", "labels"]))
    return EXIT_OK

def cmd_leads(a, con):
    from . import queries
    res = queries.leads(con, a.tier, page_size=100, free_submission_only=a.free)
    if a.json: emit(res); return EXIT_OK
    if not res["items"]:
        print(f"no {a.tier} leads" + (" among free-submission programs" if a.free else "") + (". Freshness counts from a program's launch date, or from the day TargetFinder first sees it appear after your first run; try `leads hot` or `leads solid`." if a.tier == "top" else ""))
        return EXIT_OK
    rows = [[f"{p['lead']['score']:.1f}", p["id"], dash(p.get("name"), 28), reward_label(p.get("rewards")), fee_label(p.get("submission_fee")), p["lead"]["reason"][:70]] for p in res["items"]]
    print(table(rows, ["score", "id", "name", "reward", "fee", "why"], right=(0,))); print(footer(res["page"]))
    return EXIT_OK

def cmd_resolve(a, con):
    from . import queries
    res = queries.resolve_program(con, a.text)
    if a.json: emit(res); return EXIT_OK
    if not res: print(f"nothing matches {a.text!r}"); return EXIT_NOT_FOUND
    print(table([[r["id"], dash(r.get("name")), r["platform"], r.get("status")] for r in res], ["id", "name", "platform", "status"]))
    return EXIT_OK

def cmd_payouts(a, con):
    from . import queries, activity
    row = _resolve_or_exit(con, a.id)
    if row is None: return EXIT_NOT_FOUND
    res = activity.program_payouts(con, row)
    if a.json: emit(res); return EXIT_OK
    print(paint(res["program"]["name"] or res["program"]["id"], "bold") + f"  ({res['program']['platform']})")
    print(table([[k, dash(v)] for k, v in (res.get("signals") or {}).items()]))
    if res.get("labels"): print("labels: " + ", ".join(res["labels"]))
    facts = res.get("facts") or []
    if facts:
        print("\nPlatform-published facts:")
        def value(f):
            if f.get("unit") == "epoch_seconds" and f.get("amount"):
                return when(dt.datetime.fromtimestamp(float(f["amount"]), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            if f.get("currency"): return money(f.get("amount"), f["currency"])
            return f"{dash(f.get('amount'))} {f.get('unit') or ''}".strip()
        print(table([[f["metric"], value(f), dash(f.get("period")), when(f.get("observed_at"))] for f in facts], ["metric", "value", "period", "observed"]))
    return EXIT_OK

def cmd_watch(a, con):
    from . import tools
    if a.action == "add":
        if not a.program_id: print("usage: targetfinder watch add <program id, handle or name>"); return EXIT_USAGE
        pid, cands = tools.watch_add(con, a.program_id, a.note)
        if pid: print(f"watching {pid}"); return EXIT_OK
        print("ambiguous, pick one id:"); print(table([[c["id"], dash(c.get("name")), c.get("platform")] for c in cands])); return EXIT_NOT_FOUND
    if a.action == "rm":
        if not a.program_id: print("usage: targetfinder watch rm <program id>"); return EXIT_USAGE
        print(f"removed {tools.watch_rm(con, a.program_id)}"); return EXIT_OK
    if a.action == "list":
        items = tools.watch_list(con)
        if a.json: emit(items); return EXIT_OK
        if not items: print('watchlist is empty. add one: targetfinder watch add "Program name"'); return EXIT_OK
        print(table([[w["program_id"], dash(w.get("name")), w.get("platform"), dash(w.get("note"))] for w in items], ["id", "name", "platform", "note"])); return EXIT_OK
    since = a.since or config.iso(config.utcnow() - dt.timedelta(days=7))
    rep = tools.watch_report(con, since)
    if a.json: emit({"since": since, "items": rep}); return EXIT_OK
    if not rep: print('watchlist is empty. add one: targetfinder watch add "Program name"'); return EXIT_OK
    for w in rep:
        print(paint(f"{w['name'] or w['program_id']}", "bold") + f"  ({w['program_id']}) since {when(since)}: {len(w['changes'])} target changes, {len(w['pushes'])} pushes, {len(w['events'])} events")
        for c in w["changes"][:10]: print(f"  {when(c['at'])}  {c['type']:8s} {c['target']['identifier'][:90]}")
        for r in w["pushes"][:10]: print(f"  {when(r['at'])}  push [{r.get('kind') or '?'}] {r['repo']['name']} {r['compare_url']}")
        for e in w["events"]: print(f"  {when(e['observed_at'])}  {e['change_type']} {e['before'] or ''} -> {e['after'] or ''}")
    return EXIT_OK

def cmd_clones(a, con):
    from . import tools
    rep = tools.clones_report(con, a.dir)
    if a.json: emit(rep); return EXIT_OK
    if not rep: print(f"no git clones found under {a.dir} (looks two levels deep)"); return EXIT_OK
    for c in rep:
        if not c["tracked"]: print(f"  {c['path']}: {c['repo']} is not in any tracked program scope"); continue
        state = paint("BEHIND", "yellow") if c["stale"] else paint("up to date", "green")
        print(f"  {c['path']}: {c['repo']} [{', '.join(c['programs'])}] {state}, {len(c['pushes_since_local'])} pushes since your checkout")
        for p in c["pushes_since_local"][:5]: print(f"      {when(p['at'])} [{p['kind'] or '?'}] {p['compare_url']}")
    return EXIT_OK

def cmd_notify(a):
    from . import notify
    cfg = notify.load_config()
    if a.action == "setup":
        if a.discord: cfg["discord_webhook"] = a.discord
        if a.telegram_token and a.telegram_chat: cfg["telegram"] = {"bot_token": a.telegram_token, "chat_id": a.telegram_chat}
        elif a.telegram_token or a.telegram_chat: print("telegram needs both --telegram-token and --telegram-chat"); return EXIT_USAGE
        r = cfg["rules"]
        if a.new_targets: r["new_targets"]["enabled"] = a.new_targets == "on"
        if a.free_only: r["new_targets"]["free_only"] = a.free_only == "on"
        if a.min_score is not None: r["new_targets"]["min_score"] = a.min_score
        if a.platforms is not None: r["new_targets"]["platforms"] = [x for x in a.platforms.split(",") if x]
        if a.new_programs: r["new_programs"] = a.new_programs == "on"
        if a.watchlist: r["watchlist"] = a.watchlist == "on"
        notify.save_config(cfg); print(f"saved {notify.CONFIG_PATH}")
    if a.action in ("setup", "show"):
        shown = dict(cfg); hook = shown.get("discord_webhook") or ""
        shown["discord_webhook"] = (hook[:40] + "…") if hook else None
        if shown.get("telegram"): shown["telegram"] = {"bot_token": "set", "chat_id": shown["telegram"].get("chat_id")}
        if a.json: emit(shown); return EXIT_OK
        r = cfg["rules"]; nt = r["new_targets"]
        print(table([["Discord", shown["discord_webhook"] or "not set"], ["Telegram", f"chat {shown['telegram']['chat_id']}" if shown.get("telegram") else "not set"],
                     ["watchlist alerts", "on" if r.get("watchlist") else "off"], ["new programs", "on" if r.get("new_programs") else "off"],
                     ["fresh targets", ("on" if nt.get("enabled") else "off") + (f", free only" if nt.get("free_only") else "") + f", min score {nt.get('min_score', 0)}" + (f", platforms {', '.join(nt['platforms'])}" if nt.get("platforms") else "")]]))
        if not (hook or cfg.get("telegram")): print("\nset a channel: targetfinder notify setup --discord <webhook url>   or   --telegram-token <token> --telegram-chat <chat id>")
        return EXIT_OK
    if a.action == "test":
        res = notify.send(cfg, "TargetFinder: test message — notifications are working.")
        if a.json: emit(res); return EXIT_OK
        if not res: print("no channel configured"); return EXIT_USAGE
        for ch, ok, info in res: print(f"{ch}: {'sent' if ok else 'failed'} {info or ''}".rstrip())
        return EXIT_OK if all(ok for _, ok, _ in res) else EXIT_FAILURE
    res = notify.run(_con(), dry_run=(a.action == "preview"))
    if a.json: emit(res); return EXIT_OK
    for k, v in (res or {}).items():
        print(f"{k}: {json.dumps(v) if isinstance(v, (dict, list)) else v}")
    return EXIT_OK

def cmd_schedule(a):
    from . import schedule
    res = {"install": lambda: schedule.install(a.every), "remove": schedule.remove, "status": schedule.status}[a.action]()
    if a.json: emit(res); return EXIT_OK
    if a.action == "install":
        if res.get("installed") is False and res.get("command"):
            print("run this once in an elevated prompt:\n  " + res["command"]); return EXIT_OK
        print(f"installed on {res.get('system')}: sync + alerts every {a.every} h")
        for f in res.get("files") or []: print(f"  {f}")
        print("check with: targetfinder schedule status")
    elif a.action == "remove":
        print(res.get("command") and f"run once:\n  {res['command']}" or f"removed from {res.get('system')}")
    else:
        print(f"{res.get('system')}: " + (paint("active", "green") if res.get("active") else paint("not installed", "yellow")))
        if res.get("output"): print(res["output"].strip())
    return EXIT_OK

# ---- entry ------------------------------------------------------------------------------------
def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        import signal; signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `targetfinder leads top | head` ends quietly
    except (ImportError, AttributeError, ValueError):
        pass
    ap = build_parser()
    if not argv:
        print(help_text(), end=""); return EXIT_OK
    a = ap.parse_args(argv)
    if a.cmd == "sync": return cmd_sync(a)
    if a.cmd == "init": return cmd_init(a)
    if a.cmd == "notify": return cmd_notify(a)
    if a.cmd == "schedule": return cmd_schedule(a)
    if a.cmd == "serve":
        from . import api
        api.serve(a.host, a.port); return EXIT_OK
    if a.cmd == "mcp":
        from . import mcp_server
        mcp_server.main(); return EXIT_OK
    if a.cmd == "sweep":
        from . import sweep
        if a.sync:
            from . import sync
            sync.run(log=lambda m: None)
        text, raw = sweep.run(a.since, a.out_dir, write=not a.no_write, max_per_program=a.max_per_program, bulk_threshold=a.bulk_threshold, all_pushes=a.all_pushes, live_only=a.live_only)
        print(text); return EXIT_OK
    if a.cmd == "loop":
        import time
        from . import sync, sweep
        print(f"loop: sync + sweep every {a.every:g} h (Ctrl-C to stop)", flush=True)
        try:
            while True:
                sync.run(log=lambda m: None)
                text, raw = sweep.run(); print(text, flush=True)
                time.sleep(a.every * 3600)
        except KeyboardInterrupt:
            print("stopped")
        return EXIT_OK
    if a.cmd == "repos":
        from . import repowatch
        st = repowatch.poll(_con(), limit=a.limit, log=lambda m: None)
        if a.json: emit(st)
        else: print(f"{num(st.get('polled'))} polled, {num(st.get('changed'))} pushed, {num(st.get('initialized'))} new, {num(st.get('errors'))} errors")
        return EXIT_OK
    if a.cmd == "export-snapshot":
        from . import snapshot
        out = a.out if os.path.isabs(a.out) else os.path.join(config.HOME_DIR, a.out)
        res = snapshot.export_snapshot(_con(), out, [x.strip() for x in (a.exclude_platforms or "").split(",") if x.strip()])
        if a.json: emit({"out": out, "files": res})
        else: print(f"snapshot written to {out} ({len(res)} files)")
        return EXIT_OK
    if a.cmd == "import-snapshot":
        from . import snapshot
        src = a.indir if os.path.isabs(a.indir) else os.path.join(config.HOME_DIR, a.indir)
        if not os.path.exists(os.path.join(src, "meta.json")): print(f"no snapshot at {src} (meta.json missing)"); return EXIT_NOT_FOUND
        res = snapshot.import_snapshot(_con(), src)
        if a.json: emit(res)
        else: print(f"imported from {src}: " + (", ".join(f"{k} {num(v)}" for k, v in res.items() if isinstance(v, int)) if isinstance(res, dict) else str(res)))
        return EXIT_OK
    if a.cmd == "diff":
        from . import tools
        path, n, files = tools.pull_diff(_con(), a.push_id, a.out_dir)
        print(f"saved {path} ({num(n)} bytes, {len(files)} files)"); [print("   ", f) for f in files[:40]]; return EXIT_OK
    con = _con()
    try:
        fn = {"status": cmd_status, "programs": cmd_programs, "program": cmd_program, "targets": cmd_targets, "search": cmd_search, "changes": cmd_changes, "commits": cmd_commits,
              "rankings": cmd_rankings, "leads": cmd_leads, "resolve": cmd_resolve, "payouts": cmd_payouts, "watch": cmd_watch, "clones": cmd_clones}[a.cmd]
        return fn(a, con)
    finally:
        con.close()

if __name__ == "__main__":
    sys.exit(main())
