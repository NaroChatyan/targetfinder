import sys, os, tempfile, subprocess, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCR = tempfile.mkdtemp()
from targetfinder import config; config.DB_PATH = tempfile.mktemp(suffix=".db", dir=SCR); config.CACHE_DIR = tempfile.mkdtemp(dir=SCR)
from targetfinder import db, sync, tools, repowatch, sweep
con = db.connect()
def prog(pid, targets, **kw):
    plat, handle = pid.split(":", 1)
    p = {"id": pid, "platform": plat, "handle": handle, "name": handle.title(), "link": "https://x/" + handle, "program_kind": "bounty", "program_status": "active", "is_active": True, "reward_status": "paid",
         "bounty_min": 100, "bounty_max": 5000, "reward_currency": "USD", "launch_at": "2025-01-01T00:00:00Z", "fee": {"amount": 0, "unit": "USD", "kind": "fee", "source": "t"}, "detail_marker": "m", "targets": targets, "payout_facts": []}
    p.update(kw); return p
tg = lambda i, **kw: {"identifier": i, "hint_type": "URL", "in_scope": True, "eligible_for_bounty": True, **kw}
run = lambda progs: sync.apply_platform(con, "T", {"platform": "T", "programs": progs, "complete": True}, lambda m: None)
# baseline listing
run([prog("T:acme", [tg("a.acme.test"), tg("https://github.com/acme/app")]), prog("T:beta", [tg("b.beta.test")], fee={"amount": 25, "unit": "USD", "kind": "fee", "source": "t"})])
# second run: new program, bulk scope dump on beta, fee drop on beta, acme adds one
bulk = [tg(f"https://etherscan.io/address/0x{i:040x}") for i in range(25)]
run([prog("T:acme", [tg("a.acme.test"), tg("https://github.com/acme/app"), tg("api.acme.test")]), prog("T:beta", [tg("b.beta.test")] + bulk, fee={"amount": 0, "unit": "USD", "kind": "fee", "source": "t"}), prog("T:gamma", [tg("g.gamma.test")])])
# pushes: one code, one docs
repowatch.ls_remote = lambda url, timeout=None: ("main", "1" * 40)
repowatch.classify_new_pushes = lambda con, log=print, limit=200: 0
repowatch.poll(con, lambda m: None)
repowatch.ls_remote = lambda url, timeout=None: ("main", "2" * 40)
repowatch.poll(con, lambda m: None)
con.execute("UPDATE repo_changes SET push_kind='docs', files='[\"README.md\"]'"); con.commit()
repowatch.ls_remote = lambda url, timeout=None: ("main", "3" * 40)
repowatch.poll(con, lambda m: None)
con.execute("UPDATE repo_changes SET push_kind='code', files='[\"src/a.ts\"]' WHERE push_kind IS NULL"); con.commit()
# watchlist
pid, c = tools.watch_add(con, "Acme"); assert pid == "T:acme", (pid, c)
assert [w["program_id"] for w in tools.watch_list(con)] == ["T:acme"]
rep = tools.watch_report(con, "2000-01-01T00:00:00Z"); assert rep[0]["changes"] and rep[0]["pushes"], rep
# clones: make a temp clone-like repo whose origin is acme/app and whose HEAD equals the first poll sha? use a real git repo with origin set
d = tempfile.mkdtemp(dir=SCR); sub = os.path.join(d, "acme-app"); os.makedirs(sub)
subprocess.run(["git", "init", "-q", sub], check=True); subprocess.run(["git", "-C", sub, "remote", "add", "origin", "git@github.com:acme/app.git"], check=True)
subprocess.run(["git", "-C", sub, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"], check=True, env={**os.environ, "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z", "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z"})
cl = tools.clones_report(con, d); assert cl and cl[0]["tracked"] and cl[0]["stale"] and len(cl[0]["pushes_since_local"]) == 2, cl   # local HEAD unknown to us -> pushes after local commit date... both pushes are newer
# sweep rendering
text, raw = sweep.run(since="2000-01-01T00:00:00Z", write=True, log=lambda m: None, max_per_program=2, bulk_threshold=20)
for h in ("## New programs — 1", "## Submission fee changes — 1", "[NOW FREE]", "bulk scope update", "## Commits — 1 pushes", "docs/deps/ci-only pushes hidden", "## Your watchlist — 1", "brief:", "## Bottom line"):
    assert h in text, h
text2, _ = sweep.run(since="2000-01-01T00:00:00Z", write=False, log=lambda m: None, all_pushes=True)
assert "## Commits — 2 pushes" in text2, [l for l in text2.splitlines() if l.startswith("## Commits")]
assert os.path.exists(os.path.join(config.CACHE_DIR, "SWEEP_" + config.utcnow().date().isoformat() + ".md"))   # the report is named by UTC date
# debounce + first-listing baseline already covered by the semantics test; classify_paths covered earlier
# notifications: stub sender, cursor semantics
from targetfinder import notify
sent = []
stub = lambda url, payload, headers=None: sent.append((url, payload)) or 200
cfg = {"discord_webhook": "https://discord.test/hook", "telegram": {"bot_token": "t", "chat_id": "1"}, "rules": {"watchlist": True, "new_programs": True, "new_targets": {"enabled": True, "free_only": True, "min_score": 0, "platforms": [], "max_per_program": 5}}}
notify.load_config = lambda: cfg
r = notify.run(con, lambda m: None, sender=stub); assert r.get("initialised") and not sent, r         # first run only sets cursors
run([prog("T:acme", [tg("a.acme.test"), tg("https://github.com/acme/app"), tg("api.acme.test"), tg("new.acme.test")]), prog("T:beta", [tg("b.beta.test")] + bulk), prog("T:gamma", [tg("g.gamma.test")]), prog("T:delta", [tg("d.delta.test")])])
repowatch.ls_remote = lambda url, timeout=None: ("main", "4" * 40); repowatch.poll(con, lambda m: None)
con.execute("UPDATE repo_changes SET push_kind='code' WHERE push_kind IS NULL"); con.commit()
r = notify.run(con, lambda m: None, sender=stub); assert r["sent"] >= 2, r
texts = "\n".join(p.get("content") or p.get("text") for _, p in sent)
assert "New programs (1)" in texts and "Delta" in texts, texts
assert "Watchlist (1 target changes" in texts and "new.acme.test" in texts and "push [code] acme/app" in texts, texts
assert "d.delta.test" in texts.split("Watchlist")[0], texts          # a brand-new program lists its scope under New programs
assert "Fresh targets" not in texts or "new.acme.test" not in texts.split("Fresh targets")[1], texts   # watched programs are not repeated under Fresh targets
n = len(sent); r = notify.run(con, lambda m: None, sender=stub); assert r["sent"] == 0 and len(sent) == n, r   # nothing new → nothing sent
print("FEATURES OK")
