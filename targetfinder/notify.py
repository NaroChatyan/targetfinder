"""Discord / Telegram notifications after each sync.

Config file data/notify.json (or env DISCORD_WEBHOOK_URL / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID):
{
  "discord_webhook": "https://discord.com/api/webhooks/...",
  "telegram": {"bot_token": "123:abc", "chat_id": "123456"},
  "rules": {
    "watchlist": true,                                  # target changes, fee/reward changes, code pushes on watched programs
    "new_programs": true,                               # programs first seen on any platform
    "new_targets": {"enabled": true, "free_only": true, "min_score": 0, "platforms": [], "max_per_program": 5}
  }
}
Cursors (last notified row ids) live in the kv table, so nothing is sent twice."""
import json, os, time, urllib.request, urllib.error, datetime as dt
from . import db, config, queries, tools, model, http

CONFIG_PATH = os.path.join(config.DATA_DIR, "notify.json")
CODE_KINDS = (None, "code", "mixed", "tests", "unknown")

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            cfg = json.load(open(CONFIG_PATH))
        except Exception:
            cfg = {}
    if os.environ.get("DISCORD_WEBHOOK_URL"): cfg["discord_webhook"] = os.environ["DISCORD_WEBHOOK_URL"]
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram"] = {"bot_token": os.environ["TELEGRAM_BOT_TOKEN"], "chat_id": os.environ["TELEGRAM_CHAT_ID"]}
    rules = cfg.setdefault("rules", {})
    rules.setdefault("watchlist", True); rules.setdefault("new_programs", True)
    nt = rules.setdefault("new_targets", {}); nt.setdefault("enabled", True); nt.setdefault("free_only", True); nt.setdefault("min_score", 0); nt.setdefault("platforms", []); nt.setdefault("max_per_program", 5)
    return cfg

def save_config(cfg):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(CONFIG_PATH, 0o600)

def configured(cfg):
    return bool(cfg.get("discord_webhook") or (cfg.get("telegram") or {}).get("bot_token"))

# ---- senders ------------------------------------------------------------------------------------
def _post(url, payload, headers=None):
    for attempt in range(2):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "User-Agent": config.USER_AGENT, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:   # Discord / Telegram rate limit: wait as told (capped), retry once
                time.sleep(min(10.0, http.retry_after_seconds(e.headers.get("Retry-After")) or 2.0)); continue
            raise

def channels(cfg):
    out = []
    if cfg.get("discord_webhook"): out.append("discord")
    tg = cfg.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"): out.append("telegram")
    return out

def send_to(cfg, channel, text, sender=None):
    """Send one message (chunked) to one channel. Returns list of (channel, ok, info)."""
    post = sender or _post; results = []
    if channel == "discord":
        for chunk in _chunks(text, 1900):
            try:
                post(cfg["discord_webhook"], {"content": chunk, "allowed_mentions": {"parse": []}}); results.append(("discord", True, len(chunk)))
            except Exception as ex:
                results.append(("discord", False, str(ex)[:200]))
    elif channel == "telegram":
        tg = cfg["telegram"]
        for chunk in _chunks(text, 3900):
            try:
                post(f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage", {"chat_id": tg["chat_id"], "text": chunk, "disable_web_page_preview": True}); results.append(("telegram", True, len(chunk)))
            except Exception as ex:
                results.append(("telegram", False, str(ex)[:200]))
    return results

def _chunks(text, limit):
    out, cur = [], ""
    for line in text.splitlines():
        if len(cur) + len(line) + 1 > limit:
            out.append(cur); cur = ""
        cur += line + "\n"
    if cur.strip(): out.append(cur)
    return out

def send(cfg, text, sender=None):
    """Send one message (chunked) to every configured channel. Returns list of (channel, ok, info)."""
    return [r for ch in channels(cfg) for r in send_to(cfg, ch, text, sender)]

def _current_ids(con):
    return {"event": con.execute("SELECT COALESCE(MAX(id),0) FROM program_events").fetchone()[0], "change": con.execute("SELECT COALESCE(MAX(id),0) FROM target_changes").fetchone()[0],
            "push": con.execute("SELECT COALESCE(MAX(id),0) FROM repo_changes").fetchone()[0]}

# ---- digest ---------------------------------------------------------------------------------------
def _fee(prow):
    return "free" if (prow["fee_amount"] or 0) == 0 else f"fee {prow['fee_amount']:g} {prow['fee_unit'] or ''}".strip()

def build_digest(con, cfg, cursors):
    """Collect everything newer than the cursors. Returns (text, new_cursors). Empty text = nothing to send."""
    rules = cfg["rules"]; lines = []; new = dict(cursors)
    watched = {w["program_id"] for w in tools.watch_list(con)}
    # program events (first_seen / fee_changed / rewards_changed / paused / resumed / returned / left_listing)
    evs = con.execute("SELECT e.*, p.name, p.platform, p.program_kind, p.bounty_max, p.fee_amount, p.fee_unit, p.data FROM program_events e JOIN programs p ON p.id=e.program_id WHERE e.id>? AND e.change_type!='scope' ORDER BY e.id", (cursors.get("event", 0),)).fetchall()
    if evs: new["event"] = evs[-1]["id"]
    newp = [e for e in evs if e["change_type"] == "first_seen" and rules.get("new_programs")]
    if newp:
        lines.append(f"🆕 New programs ({len(newp)})")
        for e in newp[:25]:
            lines.append(f"• [{e['platform']}] {e['name']} — {e['program_kind']}, max {e['bounty_max'] or '?'}, {_fee(e)}  {(db.uj(e['data'], {}) or {}).get('link') or ''}")
            scope = con.execute("SELECT identifier, target_type FROM targets WHERE program_id=? AND is_active=1 AND in_scope=1 ORDER BY wildcard DESC, id LIMIT 6", (e["program_id"],)).fetchall()
            for t in scope[:5]:
                lines.append(f"    – {t['target_type']}: {t['identifier'][:100]}")
            if len(scope) > 5: lines.append("    – …")
        if len(newp) > 25: lines.append(f"• … +{len(newp) - 25} more")
        lines.append("")
    wev = [e for e in evs if e["program_id"] in watched and e["change_type"] in ("fee_changed", "rewards_changed", "paused", "resumed", "returned", "left_listing")] if rules.get("watchlist") else []
    # target changes
    tcs = con.execute("SELECT c.*, t.identifier, t.target_type, t.in_scope, t.eligible_for_bounty, t.wildcard, t.scope_tags, t.added_at, t.last_changed_at, p.name pname, p.platform, p.fee_amount, p.fee_unit, p.bounty_max, p.reward_status, p.report_count, p.launch_at, p.data FROM target_changes c JOIN targets t ON t.id=c.target_id JOIN programs p ON p.id=c.program_id WHERE c.id>? ORDER BY c.id", (cursors.get("change", 0),)).fetchall()
    if tcs: new["change"] = tcs[-1]["id"]
    wtc = [c for c in tcs if c["program_id"] in watched] if rules.get("watchlist") else []
    # pushes
    pushes = con.execute("SELECT rc.*, r.url, r.provider, r.full_name FROM repo_changes rc JOIN repos r ON r.id=rc.repo_id WHERE rc.id>? AND rc.source='poll' ORDER BY rc.id", (cursors.get("push", 0),)).fetchall()
    if pushes: new["push"] = pushes[-1]["id"]
    wpush = []
    if rules.get("watchlist") and pushes:
        for rc in pushes:
            if rc["push_kind"] not in CODE_KINDS: continue
            progs = [r[0] for r in con.execute("SELECT DISTINCT p.id FROM targets t JOIN programs p ON p.id=t.program_id WHERE t.repo_url=? AND t.is_active=1", (rc["url"],))]
            if watched & set(progs): wpush.append((rc, sorted(watched & set(progs))))
    if wev or wtc or wpush:
        lines.append(f"👁 Watchlist ({len(wtc)} target changes, {len(wev)} program events, {len(wpush)} code pushes)")
        for e in wev:
            b, a = db.uj(e["before"], {}) or {}, db.uj(e["after"], {}) or {}
            lines.append(f"• {e['name']}: {e['change_type']} {json.dumps(b)} → {json.dumps(a)}")
        for c in wtc[:30]:
            lines.append(f"• {c['pname']}: {c['change_type']} {c['target_type']} {c['identifier'][:100]}")
        if len(wtc) > 30: lines.append(f"• … +{len(wtc) - 30} more target changes")
        for rc, progs in wpush[:20]:
            from . import repowatch
            lines.append(f"• {', '.join(progs)}: push [{rc['push_kind'] or '?'}] {rc['full_name']} {repowatch.compare_url(rc['url'], rc['provider'], rc['before_sha'], rc['after_sha'])}")
        lines.append("")
    # fresh targets everywhere
    nt = rules.get("new_targets") or {}
    if nt.get("enabled"):
        fresh = [c for c in tcs if c["change_type"] in ("added", "readded") and c["in_scope"] and c["eligible_for_bounty"] and c["program_id"] not in watched]
        if nt.get("free_only"): fresh = [c for c in fresh if (c["fee_amount"] or 0) == 0]
        if nt.get("platforms"): fresh = [c for c in fresh if c["platform"].lower() in [x.lower() for x in nt["platforms"]]]
        if nt.get("min_score"):
            def score(c):
                prog = {"bounty_max": c["bounty_max"], "reward_status": c["reward_status"], "report_count": c["report_count"], "launch_at": c["launch_at"], "platform": c["platform"]}
                from . import intel
                return intel.score_target({"target_type": c["target_type"], "wildcard": c["wildcard"], "scope_tags": db.uj(c["scope_tags"], []), "in_scope": 1, "eligible_for_bounty": 1, "last_changed_at": c["last_changed_at"], "added_at": c["added_at"]}, prog)[0]["value"]
            fresh = [c for c in fresh if score(c) >= nt["min_score"]]
        if fresh:
            byp = {}
            for c in fresh: byp.setdefault((c["program_id"], c["pname"], c["platform"]), []).append(c)
            lines.append(f"✨ Fresh targets ({len(fresh)} in {len(byp)} programs" + (", free-submission only" if nt.get("free_only") else "") + ")")
            for (pid, name, plat), cs in sorted(byp.items(), key=lambda x: -len(x[1]))[:20]:
                link = (db.uj(cs[0]["data"], {}) or {}).get("link") or ""
                lines.append(f"• [{plat}] {name} +{len(cs)}  {link}")
                for c in cs[: nt.get("max_per_program", 5)]:
                    lines.append(f"    – {c['target_type']}: {c['identifier'][:100]}")
                if len(cs) > nt.get("max_per_program", 5): lines.append(f"    – … +{len(cs) - nt.get('max_per_program', 5)} more")
            lines.append("")
    text = "\n".join(lines).strip()
    return text, new

def run(con=None, log=print, sender=None, dry_run=False):
    cfg = load_config()
    own = con is None; con = con or db.connect()
    if not configured(cfg) and not dry_run:
        log("notify: no channel configured (targetfinder notify setup ...)"); return {"sent": 0, "configured": False}
    chans = channels(cfg) or (["preview"] if dry_run else [])
    stored = db.kv_get(con, "notify_cursors", {}) or {}
    if stored and "event" in stored:   # legacy single cursor set: every channel continues from it
        stored = {ch: dict(stored) for ch in chans}
    if not stored:   # first run: start from now, do not replay history
        stored = {ch: _current_ids(con) for ch in chans}
        db.kv_set(con, "notify_cursors", stored); con.commit(); log("notify: cursors initialised at current state; alerts start with the next sync")
        return {"sent": 0, "configured": configured(cfg), "initialised": True}
    header = f"TargetFinder {config.iso()[:16]}Z\n\n"
    results = []; texts = {}
    for ch in chans:
        cursors = stored.get(ch)
        if cursors is None:   # channel added later: start it from now
            stored[ch] = _current_ids(con); log(f"notify: {ch} cursors initialised; alerts start with the next sync"); continue
        text, new = build_digest(con, cfg, cursors)
        if not text:
            continue
        if dry_run:
            texts[ch] = header + text; continue
        res = send_to(cfg, ch, header + text, sender); results += res
        if res and all(ok for _, ok, _ in res):   # a failed chunk keeps the cursor, so the whole digest is resent next run
            stored[ch] = new
    db.kv_set(con, "notify_cursors", stored); con.commit()
    if dry_run:
        return {"sent": 0, "dry_run": True, "text": next(iter(texts.values()), ""), "cursors": stored}
    if not results:
        log("notify: nothing new"); return {"sent": 0, "configured": True}
    log(f"notify: {results}")
    if own: con.close()
    return {"sent": sum(1 for _, ok, _ in results if ok), "results": results, "chars": sum(n for _, ok, n in results if ok and isinstance(n, int))}
