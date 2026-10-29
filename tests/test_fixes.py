"""Regression tests for bugs found by running the CLI against a full first-run database."""
import json, unittest
from tests import helpers


class ReportCountTypes(unittest.TestCase):
    def test_lead_tolerates_non_numeric_report_count(self):
        from targetfinder import intel
        ld = intel.lead({"added_at": "2025-01-01T00:00:00Z", "report_count": "Private", "bounty_max": 1000, "reward_status": "paid", "program_kind": "bounty"})
        self.assertEqual(ld["factors"]["competition"], 50)   # unknown, same as None

    def test_hackenproof_private_report_count_becomes_null(self):
        from targetfinder.collectors import hackenproof
        data = [{"data": 1}, {"programs-api-bounty": 2}, {"programs": 3, "next_page": 4}, [5], None,
                {"slug": 6, "name": 7, "submittedReports": 8, "state": 9, "status": 10}, "zest", "Zest", "Private", "published", "active"]
        html = '<html><script type="application/json" id="__NUXT_DATA__">' + json.dumps(data) + "</script></html>"
        progs = hackenproof.parse_listing(html)
        self.assertEqual(len(progs), 1)
        self.assertIsNone(progs[0]["report_count"])

    def test_sync_stores_report_count_as_integer_or_null(self):
        helpers.temp_db()
        from targetfinder import db
        con = db.connect(); self.addCleanup(con.close)
        helpers.apply(con, [helpers.program("testplat:a", [helpers.target("a.example")], report_count="Private"),
                            helpers.program("testplat:b", [helpers.target("b.example")], report_count="12")])
        rows = {r[0]: r[1] for r in con.execute("SELECT id, report_count FROM programs")}
        self.assertIsNone(rows["testplat:a"]); self.assertEqual(rows["testplat:b"], 12)


class ProgramNames(unittest.TestCase):
    def test_names_are_stripped(self):
        helpers.temp_db()
        from targetfinder import db
        con = db.connect(); self.addCleanup(con.close)
        helpers.apply(con, [helpers.program("testplat:a", [helpers.target("a.example")], name="Nado Smart Contracts  ")])
        self.assertEqual(con.execute("SELECT name FROM programs").fetchone()[0], "Nado Smart Contracts")


class RepoUrls(unittest.TestCase):
    def test_wildcard_org_is_not_a_repo(self):
        from targetfinder import normalize
        self.assertIsNone(normalize.repo_from_url("https://github.com/awslabs/*"))
        self.assertIsNone(normalize.repo_from_url("https://github.com/brave/*, https:"))
        self.assertIsNone(normalize.repo_from_url("https://gitlab.com/gitlab-org/*"))
        self.assertIsNotNone(normalize.repo_from_url("https://github.com/awslabs/aws-sdk"))

    def test_gitlab_gets_longer_ls_remote_timeout(self):
        from targetfinder import repowatch
        self.assertGreaterEqual(repowatch.timeout_for("https://gitlab.com/gitlab-org/gitlab"), 60)
        self.assertEqual(repowatch.timeout_for("https://github.com/a/b"), 25)


class RepoFailures(unittest.TestCase):
    def setUp(self):
        helpers.temp_db()
        from targetfinder import db, repowatch
        self.con = db.connect(); self.addCleanup(self.con.close)
        helpers.apply(self.con, [helpers.program("testplat:a", [helpers.target("https://github.com/acme/gone", hint_type="source code")])])
        self.repowatch = repowatch
        self.orig = repowatch.ls_remote

    def tearDown(self):
        self.repowatch.ls_remote = self.orig

    def _poll_with(self, message, times):
        def boom(url, timeout=None): raise RuntimeError(message)
        self.repowatch.ls_remote = boom
        for _ in range(times):
            self.repowatch.poll(self.con, log=lambda m: None, workers=1)
        return self.con.execute("SELECT active, fail_count FROM repos").fetchone()

    def test_not_found_deactivates_after_three_failures(self):
        active, fc = self._poll_with("remote: Repository not found.", 3)
        self.assertEqual((active, fc), (0, 3))

    def test_auth_rejection_deactivates_after_three_failures(self):
        active, fc = self._poll_with("remote: Invalid username or token. Password authentication is not supported", 3)
        self.assertEqual(active, 0)

    def test_timeout_keeps_retrying(self):
        active, fc = self._poll_with("Command timed out after 25 seconds", 3)
        self.assertEqual((active, fc), (1, 3))


class StatusShape(unittest.TestCase):
    def test_last_sync_stats_is_decoded(self):
        helpers.temp_db()
        from targetfinder import db, queries
        con = db.connect(); self.addCleanup(con.close)
        con.execute("INSERT INTO sync_runs(started_at, finished_at, kind, status, stats) VALUES(?,?,?,?,?)",
                    ("2026-10-29T00:00:00Z", "2026-10-29T00:01:00Z", "full", "ok", json.dumps({"TestPlat": {"programs": 3}})))
        con.commit()
        st = queries.status(con)
        self.assertEqual(st["last_sync"]["stats"]["TestPlat"]["programs"], 3)


if __name__ == "__main__":
    unittest.main()
