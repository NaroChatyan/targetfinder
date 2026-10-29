"""A fresh clone's first sync records everything as a baseline: no target is reported as added, programs are first_seen only."""
import unittest
from tests import helpers
from targetfinder import db, sync, collectors


class FirstRun(unittest.TestCase):
    def test_first_sync_is_baseline(self):
        helpers.temp_db(); con = db.connect(); self.addCleanup(con.close)
        stub = lambda existing, log: {"platform": "TestPlat", "programs": [helpers.program("testplat:acme", [helpers.target(f"t{i}.acme.test") for i in range(4)])], "complete": True}
        registry = dict(collectors.REGISTRY); orig = sync.load_all
        collectors.REGISTRY.clear(); collectors.REGISTRY["TestPlat"] = stub; sync.load_all = lambda: collectors.REGISTRY
        self.addCleanup(lambda: (collectors.REGISTRY.clear(), collectors.REGISTRY.update(registry), setattr(sync, "load_all", orig)))
        st = sync.run(platforms=["TestPlat"], log=lambda m: None, repo_poll=False, con=con)
        self.assertEqual(st["TestPlat"]["baseline"], 4)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM target_changes").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM targets WHERE is_active=1").fetchone()[0], 4)
        self.assertEqual([r[0] for r in con.execute("SELECT change_type FROM program_events")], [])   # first listing of a platform: known, not new
        self.assertIsNotNone(db.kv_get(con, "tracking_since"))
        # second run with one more target: exactly one 'added'
        collectors.REGISTRY["TestPlat"] = lambda existing, log: {"platform": "TestPlat", "programs": [helpers.program("testplat:acme", [helpers.target(f"t{i}.acme.test") for i in range(5)])], "complete": True}
        sync.run(platforms=["TestPlat"], log=lambda m: None, repo_poll=False, con=con)
        self.assertEqual([r[0] for r in con.execute("SELECT change_type FROM target_changes")], ["added"])


if __name__ == "__main__":
    unittest.main()
