"""A failing collector never touches stored data and is reported as stale; an empty scope never removes targets."""
import datetime as dt, unittest
from tests import helpers

helpers.temp_db()
from targetfinder import db, config, sync, queries, sweep, collectors  # noqa: E402


class Resilience(unittest.TestCase):
    def setUp(self):
        self.con = db.connect()
        self.addCleanup(self.con.close)   # close even when setUp fails, or the next test blocks on the write lock
        for t in ("programs", "targets", "target_changes", "program_events", "collector_runs", "sync_runs", "kv"):
            self.con.execute(f"DELETE FROM {t}")
        self.con.commit()
        self._registry = dict(collectors.REGISTRY)

    def tearDown(self):
        collectors.REGISTRY.clear(); collectors.REGISTRY.update(self._registry)

    def _seed(self):
        helpers.apply(self.con, [helpers.program("testplat:acme", [helpers.target("a.acme.test"), helpers.target("b.acme.test"), helpers.target("c.acme.test")])])
        helpers.apply(self.con, [helpers.program("testplat:acme", [helpers.target("a.acme.test"), helpers.target("b.acme.test"), helpers.target("c.acme.test"), helpers.target("d.acme.test")])])

    def test_failed_collector_keeps_targets_and_marks_stale(self):
        self._seed()
        def boom(existing, log):
            raise RuntimeError("payload changed: no programs array")
        collectors.REGISTRY.clear(); collectors.REGISTRY["TestPlat"] = boom
        sync.load_all = lambda: collectors.REGISTRY
        st = sync.run(platforms=["TestPlat"], log=lambda m: None, repo_poll=False, con=self.con)
        self.assertIn("error", st["TestPlat"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM targets WHERE is_active=1").fetchone()[0], 4)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM target_changes WHERE change_type='removed'").fetchone()[0], 0)
        status = queries.status(self.con)
        plat = next(p for p in status["platforms"] if p["platform"] == "TestPlat")
        self.assertTrue(plat["stale"]); self.assertIn("payload changed", plat["last_error"]); self.assertIsNone(plat["last_ok_at"])
        self.assertIn("TestPlat", status["stale_platforms"])   # real collectors that never ran are stale too

    def test_successful_collector_is_not_stale(self):
        self._seed()
        ok = lambda existing, log: {"platform": "TestPlat", "programs": [helpers.program("testplat:acme", None)], "complete": True}
        collectors.REGISTRY.clear(); collectors.REGISTRY["TestPlat"] = ok
        sync.load_all = lambda: collectors.REGISTRY
        sync.run(platforms=["TestPlat"], log=lambda m: None, repo_poll=False, con=self.con)
        plat = next(p for p in queries.status(self.con)["platforms"] if p["platform"] == "TestPlat")
        self.assertFalse(plat["stale"]); self.assertIsNotNone(plat["last_ok_at"]); self.assertIsNone(plat["last_error"])
        self.assertEqual(self.con.execute("SELECT ok, programs FROM collector_runs ORDER BY id DESC LIMIT 1").fetchone()[:], (1, 1))

    def test_old_success_becomes_stale(self):
        self._seed()
        old = config.iso(config.utcnow() - dt.timedelta(seconds=config.STALE_AFTER_SECONDS + 60))
        self.con.execute("INSERT INTO collector_runs(platform, started_at, finished_at, ok, programs, error) VALUES('TestPlat',?,?,1,1,NULL)", (old, old)); self.con.commit()
        plat = next(p for p in queries.status(self.con)["platforms"] if p["platform"] == "TestPlat")
        self.assertTrue(plat["stale"])

    def test_empty_scope_guard(self):
        self._seed()
        c = sync.apply_targets(self.con, "testplat:acme", [], config.iso(), "TestPlat")
        self.assertEqual(c["removed"], 0); self.assertEqual(c["skipped"], "empty-scope-guard")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM targets WHERE is_active=1").fetchone()[0], 4)

    def test_sweep_header_names_stale_collectors(self):
        self._seed()
        self.con.execute("INSERT INTO collector_runs(platform, started_at, finished_at, ok, programs, error) VALUES('TestPlat',?,?,0,0,'boom')", (config.iso(), config.iso())); self.con.commit()
        collectors.REGISTRY.clear(); collectors.REGISTRY["TestPlat"] = lambda e, l: None
        text, raw = sweep.run(write=False, log=lambda m: None)
        self.assertIn("Stale collectors: TestPlat", text)


if __name__ == "__main__":
    unittest.main()
