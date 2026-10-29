import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ["TARGETFINDER_DB"] = tempfile.mktemp(suffix=".db", dir=tempfile.mkdtemp())
from targetfinder import config; config.DB_PATH = os.environ["TARGETFINDER_DB"]
from targetfinder import db, sync
con = db.connect()
def prog(targets, **kw):
    p = {"id": "T:acme", "platform": "T", "handle": "acme", "name": "Acme", "link": "https://x", "program_kind": "bounty", "program_status": "active", "is_active": True, "reward_status": "paid",
         "bounty_min": 100, "bounty_max": 5000, "reward_currency": "USD", "launch_at": "2025-01-01T00:00:00Z", "fee": None, "detail_marker": "m", "targets": targets, "payout_facts": []}
    p.update(kw); return p
def tg(ident, **kw):
    t = {"identifier": ident, "hint_type": "URL", "in_scope": True, "eligible_for_bounty": True}; t.update(kw); return t
def counts(): return {r[0]: r[1] for r in con.execute("select change_type, count(*) from target_changes group by 1")}
def events(): return [r[0] for r in con.execute("select change_type from program_events order by id")]
res = lambda ts, **kw: {"platform": "T", "programs": [prog(ts, **kw)], "complete": True}
# 1. first sighting = baseline
sync.apply_platform(con, "T", res([tg("a.example.com"), tg("b.example.com"), tg("*.c.example.com")]), print)
assert counts() == {}, counts(); assert events() == [], events()   # first listing of a platform: no first_seen noise
assert con.execute("select count(*) from targets where is_active=1").fetchone()[0] == 3
# 2. second run: one added, one removed, one updated (out of scope)
sync.apply_platform(con, "T", res([tg("a.example.com", in_scope=False), tg("*.c.example.com"), tg("d.example.com")]), print)
assert counts() == {"added": 1, "removed": 1, "updated": 1}, counts()
# 3. third run: b comes back -> readded
sync.apply_platform(con, "T", res([tg("a.example.com", in_scope=False), tg("*.c.example.com"), tg("d.example.com"), tg("b.example.com")]), print)
assert counts()["readded"] == 1, counts()
# 4. empty scope guard: collector returns [] for a program with >=3 active targets -> nothing removed
sync.apply_platform(con, "T", res([]), print)
assert con.execute("select count(*) from targets where is_active=1").fetchone()[0] == 4
assert counts().get("removed") == 1, counts()
# 5. targets None = not fetched -> untouched
sync.apply_platform(con, "T", res(None), print)
assert con.execute("select count(*) from targets where is_active=1").fetchone()[0] == 4
# 6. rewards change -> event; same values -> no event
sync.apply_platform(con, "T", res(None, bounty_max=10000), print)
assert events()[-1] == "rewards_changed", events()
n = len(events()); sync.apply_platform(con, "T", res(None, bounty_max=10000), print); assert len(events()) == n
# 7. program disappears from a complete listing -> left_listing; reappears -> returned
sync.apply_platform(con, "T", {"platform": "T", "programs": [prog(None, id="T:other", handle="other")], "complete": True}, print)
assert "left_listing" in events() and events()[-2:] == ["first_seen", "left_listing"], events()   # a genuinely new program after the first listing IS an event
n = len(events()); sync.apply_platform(con, "T", res(None), print); new_ev = events()[n:]; assert "returned" in new_ev and "resumed" not in new_ev and "left_listing" in new_ev, new_ev
# 8. paused/resumed are debounced: one observation is not an event, two consecutive ones are; a flip-flop emits nothing
n = len(events())
sync.apply_platform(con, "T", res(None, program_status="paused", is_active=False), print); assert len(events()) == n, events()[n:]
sync.apply_platform(con, "T", res(None, program_status="active"), print); assert len(events()) == n, events()[n:]       # flip-flop swallowed
sync.apply_platform(con, "T", res(None, program_status="paused", is_active=False), print)
sync.apply_platform(con, "T", res(None, program_status="paused", is_active=False), print); assert events()[-1] == "paused", events()[-3:]
sync.apply_platform(con, "T", res(None, program_status="active"), print)
sync.apply_platform(con, "T", res(None, program_status="active"), print); assert events()[-1] == "resumed", events()[-3:]
# 8b. fee change event
sync.apply_platform(con, "T", res(None, fee={"amount": 0, "unit": "USD", "kind": "fee", "source": "t_catalog"}), print)
n = len(events()); sync.apply_platform(con, "T", res(None, fee={"amount": 25, "unit": "USD", "kind": "fee", "source": "t_catalog"}), print)
assert events()[-1] == "fee_changed", events()[-3:]
sync.apply_platform(con, "T", res(None, fee=None), print); assert len(events()) == n + 1, events()[-3:]   # unknown fee keeps the old one, no event
# 9. program known without scope (e.g. imported listing): first scope fetch is a silent baseline
con.execute("UPDATE programs SET detail_fetched_at=NULL WHERE id='T:acme'"); con.commit()
n = len(events()); c0 = counts()
sync.apply_platform(con, "T", res([tg("zzz.example.com")], bounty_max=777, program_status="paused", is_active=False), print)
assert "scope" not in events()[n:], (events()[n:],); assert counts() == c0, (counts(), c0)   # scope is baselined; program-level changes are still real events
assert con.execute("select source from programs where id='T:acme'").fetchone()[0] == "T"
# 10. after reconciliation, real changes are reported again
sync.apply_platform(con, "T", res([tg("zzz.example.com"), tg("new.example.com")], bounty_max=777, program_status="active"), print)
assert counts()["added"] == c0["added"] + 1, counts(); assert events()[-1] in ("scope", "resumed"), events()[-3:]
print("ALL SYNC SEMANTICS OK", counts(), events())
