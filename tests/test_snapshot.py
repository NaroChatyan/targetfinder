"""JSON snapshots: export then import into an empty database reproduces the tracked state the hourly diff depends on."""
import json, os, tempfile, unittest
from tests import helpers
from targetfinder import db, config, sync, repowatch, snapshot, queries


def populate(con):
    helpers.apply(con, [helpers.program("testplat:acme", [helpers.target("a.acme.test"), helpers.target("https://github.com/acme/app")]),
                        helpers.program("testplat:beta", [helpers.target("b.beta.test")], fee={"amount": 30, "unit": "USD", "kind": "deposit", "source": "test"})])
    helpers.apply(con, [helpers.program("testplat:acme", [helpers.target("a.acme.test"), helpers.target("https://github.com/acme/app"), helpers.target("api.acme.test")]),
                        helpers.program("testplat:beta", [], bounty_max=9000)])
    repowatch.ls_remote = lambda url, timeout=None: ("main", "1" * 40)
    repowatch.classify_new_pushes = lambda con, log=print, limit=200: 0
    repowatch.poll(con, lambda m: None)
    repowatch.ls_remote = lambda url, timeout=None: ("main", "2" * 40)
    repowatch.poll(con, lambda m: None)
    sync.record_payout_facts(con, "testplat:acme", [{"metric": "payout_total", "amount": 1000, "currency": "USD", "unit": "currency", "source_label": "t", "source_field": "t", "period": "all_time", "basis": "reported"}], config.iso())
    con.execute("INSERT INTO collector_runs(platform, started_at, finished_at, ok, programs, error) VALUES('TestPlat',?,?,1,2,NULL)", (config.iso(), config.iso()))
    db.kv_set(con, "platform_synced:TestPlat", config.iso()); db.kv_set(con, "tracking_since", "2026-10-01T00:00:00Z")
    con.commit()


def state(con):
    return {"targets": [tuple(r) for r in con.execute("SELECT program_id, norm, identifier, added_at, last_changed_at, last_change_type, is_active, removed_at FROM targets ORDER BY program_id, norm")],
            "programs": [tuple(r) for r in con.execute("SELECT id, platform, handle, name, program_status, listed, bounty_max, fee_amount, fee_source, detail_fetched_at, detail_marker, added_at, source FROM programs ORDER BY id")],
            "repos": [tuple(r) for r in con.execute("SELECT url, provider, provider_host, full_name, ref_name, head_sha, active FROM repos ORDER BY url")],
            "changes": [tuple(r) for r in con.execute("SELECT program_id, change_type, changed_at FROM target_changes ORDER BY id")],
            "events": [tuple(r) for r in con.execute("SELECT program_id, change_type, category, observed_at FROM program_events ORDER BY id")],
            "pushes": [tuple(r) for r in con.execute("SELECT before_sha, after_sha, observed_at FROM repo_changes ORDER BY id")],
            "payouts": con.execute("SELECT COUNT(*) FROM payout_observations").fetchone()[0],
            "kv": sorted((k, v) for k, v in con.execute("SELECT k, v FROM kv WHERE k LIKE 'platform_synced:%' OR k='tracking_since'"))}


class Snapshot(unittest.TestCase):
    def test_round_trip_preserves_state(self):
        helpers.temp_db(); con = db.connect(); self.addCleanup(con.close)
        populate(con)
        before = state(con)
        out = tempfile.mkdtemp(prefix="tf-snap-")
        files = snapshot.export_snapshot(con, out)
        for name in ("programs.json", "targets.json", "repos.json", "target_changes_90d.json", "program_events_90d.json", "repo_changes_90d.json", "payouts_90d.json", "meta.json"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        meta = json.load(open(os.path.join(out, "meta.json")))
        self.assertEqual(meta["version"], config.VERSION); self.assertIn("exported_at", meta)
        self.assertEqual(json.load(open(os.path.join(out, "programs.json")))[0]["id"], "testplat:acme")
        self.assertTrue(any(not t["is_active"] for t in json.load(open(os.path.join(out, "targets.json")))))   # inactive rows are kept
        helpers.temp_db(); con2 = db.connect(); self.addCleanup(con2.close)
        snapshot.import_snapshot(con2, out)
        self.assertEqual(state(con2), before)
        self.assertEqual(con2.execute("SELECT head_sha FROM repos WHERE url='https://github.com/acme/app'").fetchone()[0], "2" * 40)
        self.assertEqual(con2.execute("SELECT stale FROM (SELECT 0 stale)").fetchone()[0], 0)
        self.assertFalse(next(p for p in queries.status(con2)["platforms"] if p["platform"] == "TestPlat")["stale"])
        snapshot.import_snapshot(con2, out)   # idempotent
        self.assertEqual(state(con2), before)

    def test_next_sync_after_import_reports_only_real_changes(self):
        helpers.temp_db(); con = db.connect(); self.addCleanup(con.close)
        populate(con); out = tempfile.mkdtemp(prefix="tf-snap-"); snapshot.export_snapshot(con, out)
        helpers.temp_db(); con2 = db.connect(); self.addCleanup(con2.close)
        snapshot.import_snapshot(con2, out)
        n = con2.execute("SELECT COUNT(*) FROM target_changes").fetchone()[0]
        helpers.apply(con2, [helpers.program("testplat:acme", [helpers.target("a.acme.test"), helpers.target("https://github.com/acme/app"), helpers.target("api.acme.test"), helpers.target("new.acme.test")]),
                             helpers.program("testplat:beta", [helpers.target("b.beta.test")], bounty_max=9000)])
        # exactly two real changes: acme gained new.acme.test, beta re-listed b.beta.test (removed in the snapshot's history)
        self.assertEqual(con2.execute("SELECT COUNT(*) FROM target_changes").fetchone()[0], n + 2)
        self.assertEqual(sorted(tuple(r) for r in con2.execute("SELECT change_type, program_id FROM target_changes ORDER BY id DESC LIMIT 2")), [("added", "testplat:acme"), ("readded", "testplat:beta")])


class ExcludePlatforms(unittest.TestCase):
    def test_export_can_leave_a_platform_out(self):
        helpers.temp_db(); con = db.connect(); self.addCleanup(con.close)
        populate(con)
        helpers.apply(con, [helpers.program("otherplat:zeta", [helpers.target("z.zeta.test")])], platform="OtherPlat")
        out = tempfile.mkdtemp(prefix="tf-snap-")
        snapshot.export_snapshot(con, out, exclude_platforms=["OtherPlat"])
        progs = json.load(open(os.path.join(out, "programs.json")))
        self.assertEqual({p["platform"] for p in progs}, {"TestPlat"})
        self.assertFalse(any(t["program_id"].startswith("otherplat:") for t in json.load(open(os.path.join(out, "targets.json")))))
        self.assertEqual(json.load(open(os.path.join(out, "meta.json")))["excluded_platforms"], ["otherplat"])


if __name__ == "__main__":
    unittest.main()
