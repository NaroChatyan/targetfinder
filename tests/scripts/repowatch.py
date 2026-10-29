import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from targetfinder import config; config.DB_PATH = tempfile.mktemp(suffix=".db", dir=tempfile.mkdtemp())
from targetfinder import db, sync, repowatch, queries
con = db.connect()
p = {"id": "T:acme", "platform": "T", "handle": "acme", "name": "Acme", "link": "https://x", "program_kind": "bounty", "program_status": "active", "is_active": True, "reward_status": "paid",
     "bounty_min": 100, "bounty_max": 5000, "reward_currency": "USD", "launch_at": "2025-01-01T00:00:00Z", "fee": None, "detail_marker": "m", "payout_facts": [],
     "targets": [{"identifier": "https://github.com/acme/app", "hint_type": "SOURCE_CODE", "in_scope": True, "eligible_for_bounty": True},
                 {"identifier": "https://github.com/acme/lib/blob/main/src/x.sol", "hint_type": "smart_contract", "in_scope": True, "eligible_for_bounty": True},
                 {"identifier": "https://gitlab.com/acme/grp/sub/-/tree/main/pkg", "hint_type": "SOURCE_CODE", "in_scope": False, "eligible_for_bounty": False}]}
sync.apply_platform(con, "T", {"platform": "T", "programs": [p], "complete": True}, print)
repos = [tuple(r) for r in con.execute("select url, full_name from repos order by url")]
assert repos == [("https://github.com/acme/app", "acme/app"), ("https://github.com/acme/lib", "acme/lib"), ("https://gitlab.com/acme/grp/sub", "acme/grp/sub")], repos
active = [r["url"] for r in repowatch.active_repos(con)]
assert active == ["https://github.com/acme/app", "https://github.com/acme/lib"], active   # out-of-scope repo not polled
heads = {"https://github.com/acme/app": ("main", "a" * 40), "https://github.com/acme/lib": ("master", "b" * 40)}
repowatch.ls_remote = lambda url, timeout=None: heads[url] if url in heads else (_ for _ in ()).throw(RuntimeError("boom"))
st = repowatch.poll(con, print); st.pop("classified", None); assert st == {"polled": 2, "changed": 0, "errors": 0, "initialized": 2}, st
heads["https://github.com/acme/app"] = ("main", "c" * 40); del heads["https://github.com/acme/lib"]
st = repowatch.poll(con, print); assert st["changed"] == 1 and st["errors"] == 1, st
rc = queries.repo_changes(con, page_size=5)
r = rc["items"][0]; assert r["before"] == "a" * 40 and r["after"] == "c" * 40 and r["compare_url"].endswith("/compare/" + "a" * 40 + "..." + "c" * 40), r
assert r["programs"][0]["id"] == "T:acme" and r["targets"][0]["identifier"] == "https://github.com/acme/app", r
cur = queries.repo_changes(con, page_size=5, after_id=0); assert cur["page"]["next_after_id"] == r["id"] and cur["page"]["mode"] == "cursor", cur["page"]
# target_changes cursor + free filter smoke
tc = queries.target_changes(con, "added", page_size=10, after_id=0); assert tc["page"]["mode"] == "cursor", tc["page"]
# 30 consecutive failures deactivates the repo
for _ in range(30): repowatch.poll(con, lambda m: None)
assert con.execute("select active from repos where url='https://github.com/acme/lib'").fetchone()[0] == 0
print("REPOWATCH OK")
