"""The public HTTP/MCP contract: routes, top-level keys, object shapes, id format, tool names (docs/API.md)."""
import json, os, re, unittest
from tests import helpers

from targetfinder import db, config, repowatch, mcp_server, collectors  # noqa: E402

ID_RE = re.compile(r"^[a-z0-9-]+:[a-z0-9._/-]+$")
PROGRAM_KEYS = {"id", "platform", "handle", "name", "url", "logo", "kind", "status", "rewards", "submission_fee", "report_count", "launched_at", "first_seen_at", "updated_at",
                "scope_updated_at", "tags", "scope_counts", "scope_activity", "competition"}
TARGET_KEYS = {"identifier", "display_name", "normalized", "type", "in_scope", "bounty_eligible", "wildcard", "tags", "languages", "severity", "source_url", "added_at", "last_seen_at",
               "last_change", "active"}
OLD_KEYS = {"targetOpportunityScore", "targetOpportunityLabel", "competitionRisk", "competitionRiskReason", "whyThisTarget", "submission_cost", "changes_url", "target_counts", "program_kind"}
MCP_TOOLS = ["guide", "status", "find_programs", "get_program", "program_brief", "list_targets", "search_targets", "find_program_by_target", "recent_target_changes", "recent_repo_pushes",
             "leads", "rankings", "program_payouts", "program_history", "watchlist_new_targets", "export_targets", "list_filters", "run_sweep"]


def seed():
    con = db.connect()
    helpers.apply(con, [helpers.program("testplat:acme", [helpers.target("a.acme.test"), helpers.target("*.acme.test"), helpers.target("https://github.com/acme/app")]),
                        helpers.program("testplat:beta", [helpers.target("b.beta.test")], fee={"amount": 30, "unit": "USD", "kind": "deposit", "source": "test"})])
    helpers.apply(con, [helpers.program("testplat:acme", [helpers.target("a.acme.test"), helpers.target("*.acme.test"), helpers.target("https://github.com/acme/app"), helpers.target("api.acme.test")]),
                        helpers.program("testplat:beta", [helpers.target("b.beta.test")], bounty_max=9000)])
    repowatch.ls_remote = lambda url, timeout=None: ("main", "1" * 40)
    repowatch.classify_new_pushes = lambda con, log=print, limit=200: 0
    repowatch.poll(con, lambda m: None)
    repowatch.ls_remote = lambda url, timeout=None: ("main", "2" * 40)
    repowatch.poll(con, lambda m: None)
    con.execute("INSERT INTO payout_observations(program_id, metric, amount, currency, unit, source_label, source_field, source_url, period, basis, observed_at) VALUES('testplat:acme','payout_total',1000,'USD','currency','t','t','https://x',NULL,'reported','2026-01-01T00:00:00Z')")
    con.execute("INSERT INTO payout_observations(program_id, metric, amount, currency, unit, source_label, source_field, source_url, period, basis, observed_at) VALUES('testplat:acme','payout_total',1500,'USD','currency','t','t','https://x',NULL,'reported',?)", (config.iso(),))
    con.commit(); con.close()


class ApiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        helpers.temp_db()   # per class, not at import: unittest imports every module before running any, so module-level setup would be shared
        seed()
        cls.srv, cls.base = helpers.start_api()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def get(self, path, body=None, expect=200):
        st, ct, raw = helpers.http(self.base, path, body)
        self.assertEqual(st, expect, f"{path}: {raw[:300]}")
        return json.loads(raw) if ct.startswith("application/json") else raw

    def test_list_routes_return_items_and_page(self):
        for path in ("/api/programs", "/api/programs/testplat:acme/targets", "/api/programs/testplat:acme/history", "/api/targets/search?q=acme", "/api/changes/targets",
                     "/api/changes/repos", "/api/leads/top", "/api/leads/solid", "/api/rankings/scope", "/api/history"):
            with self.subTest(path=path):
                res = self.get(path)
                self.assertIn("items", res); self.assertIn("page", res)
                self.assertIsInstance(res["items"], list)

    def test_health_status_capabilities(self):
        self.assertEqual(self.get("/api/health")["service"], "targetfinder")
        st = self.get("/api/status")
        for k in ("counts", "platforms", "last_sync"): self.assertIn(k, st)
        self.assertIn("endpoints", self.get("/api/capabilities"))

    def test_program_object_shape(self):
        p = self.get("/api/programs/testplat:acme")
        self.assertTrue(PROGRAM_KEYS <= set(p), PROGRAM_KEYS - set(p))
        for k in ("payouts", "activity"): self.assertIn(k, p)
        self.assertEqual(p["rewards"]["max"], 5000); self.assertEqual(p["submission_fee"]["amount"], 0)
        self.assertEqual(p["kind"], "bounty"); self.assertEqual(p["status"], "active")
        self.assertIn(p["competition"]["level"], ("low", "medium", "high", "unknown"))
        self.assertEqual(p["scope_counts"]["in_scope"], 4)
        self.assertEqual(p["report_count"]["value"], 12)
        self.assertFalse(OLD_KEYS & helpers.keys_in(p), OLD_KEYS & helpers.keys_in(p))
        compact = self.get("/api/programs?page_size=1")["items"][0]
        self.assertNotIn("activity", compact); self.assertTrue(PROGRAM_KEYS <= set(compact), PROGRAM_KEYS - set(compact))

    def test_target_object_shape(self):
        res = self.get("/api/programs/testplat:acme/targets")
        t = next(x for x in res["items"] if x["identifier"] == "api.acme.test")
        self.assertTrue(TARGET_KEYS <= set(t), TARGET_KEYS - set(t))
        self.assertIn("freshness", t["score"]["factors"]); self.assertIn(t["score"]["label"], ("top", "strong", "promising", "normal"))
        self.assertEqual(t["last_change"]["type"], "added"); self.assertTrue(t["bounty_eligible"])
        self.assertFalse(OLD_KEYS & helpers.keys_in(res), OLD_KEYS & helpers.keys_in(res))
        self.assertEqual(res["program"]["id"], "testplat:acme")

    def test_change_rows_and_cursor(self):
        res = self.get("/api/changes/targets")
        row = res["items"][0]
        for k in ("id", "type", "at", "program", "target", "diff"): self.assertIn(k, row)
        for k in ("id", "name", "platform", "rewards", "submission_fee", "competition"): self.assertIn(k, row["program"])
        self.assertIn("facets", res)
        cur = self.get("/api/changes/targets?after_id=0&page_size=1")
        self.assertEqual(len(cur["items"]), 1); self.assertIn("next_after_id", cur["page"]); self.assertTrue(cur["page"]["has_more"])
        self.assertFalse(OLD_KEYS & helpers.keys_in(res))

    def test_repo_push_rows(self):
        res = self.get("/api/changes/repos")
        self.assertEqual(len(res["items"]), 1)
        row = res["items"][0]
        for k in ("id", "at", "repo", "ref", "before", "after", "compare_url", "programs", "targets", "kind", "files"): self.assertIn(k, row)
        for k in ("provider", "host", "name", "url"): self.assertIn(k, row["repo"])
        self.assertEqual(row["programs"][0]["id"], "testplat:acme")
        self.assertTrue(row["compare_url"].startswith("https://github.com/acme/app/compare/"))

    def test_leads_and_rankings(self):
        res = self.get("/api/leads/top")   # both fixtures are fresh, lightly reported and pay: top tier
        self.assertTrue(res["items"]); lead = res["items"][0]["lead"]
        self.assertIn(lead["tier"], ("top", "hot", "solid", "possible"))
        for k in ("freshness", "competition", "reward", "scope_bonus"): self.assertIn(k, lead["factors"])
        self.get("/api/leads/elite", expect=400)
        rk = self.get("/api/rankings/scope?period=7d")
        self.assertEqual(rk["view"], "scope"); self.assertEqual(rk["period"], "7d")
        self.assertTrue(rk["items"]); row = rk["items"][0]
        for k in ("program", "metrics", "labels"): self.assertIn(k, row)
        self.assertEqual(row["metrics"]["added"], 1)
        self.assertEqual(self.get("/api/rankings/nope", expect=400)["error"]["code"], "bad_request")
        pay = self.get("/api/rankings/payouts?period=all")
        self.assertEqual(pay["period"], "all")

    def test_payouts_history_search_export(self):
        pay = self.get("/api/programs/testplat:acme/payouts")
        for k in ("program", "facts", "windows", "signals", "series"): self.assertIn(k, pay)
        self.assertEqual(pay["windows"]["all"]["amount"], 500)
        srch = self.get("/api/targets/search?q=acme.test")
        self.assertTrue(srch["items"]); self.assertIn("program", srch["items"][0]); self.assertIn("target", srch["items"][0])
        exp = self.get("/api/targets/export", body={"program_ids": ["testplat:acme"]})
        self.assertEqual(len(exp["items"]), 4); self.assertEqual(exp["items"][0]["program_id"], "testplat:acme")
        csv = self.get("/api/targets/export", body={"program_ids": ["testplat:acme"], "format": "csv"})
        self.assertTrue(csv.startswith("program_id,"))
        hist = self.get("/api/history?category=scope")
        self.assertTrue(hist["items"]); self.assertEqual(hist["items"][0]["category"], "scope")

    def test_bad_integer_params_are_400_without_traceback(self):
        for path in ("/api/changes/targets?after_id=x", "/api/changes/repos?after_id=1.5", "/api/programs/testplat:acme/targets?min_score=abc", "/api/changes/targets?min_score=-"):
            with self.subTest(path=path):
                err = self.get(path, expect=400)
                self.assertEqual(err["error"]["code"], "bad_request"); self.assertNotIn("trace", err)
        err = self.get("/api/targets/export", body={"program_ids": ["testplat:acme"], "limit": "abc"}, expect=400)
        self.assertEqual(err["error"]["code"], "bad_request")

    def test_csv_export_escapes_formula_cells_including_list_columns(self):
        con = db.connect()
        con.execute("UPDATE targets SET scope_tags=? WHERE identifier='api.acme.test'", ('["=evil()","api"]',)); con.commit(); con.close()
        self.addCleanup(lambda: (lambda c: (c.execute("UPDATE targets SET scope_tags=? WHERE identifier='api.acme.test'", ('["api","domain"]',)), c.commit(), c.close()))(db.connect()))
        csv = self.get("/api/targets/export", body={"program_ids": ["testplat:acme"], "format": "csv"})
        row = next(l for l in csv.splitlines() if ",api.acme.test," in l)
        self.assertIn("'=evil()", row); self.assertNotIn(",=evil()", row)

    def test_errors_are_structured(self):
        err = self.get("/api/programs/nope:nope", expect=404)
        self.assertEqual(set(err["error"]), {"code", "message"}); self.assertEqual(err["error"]["code"], "not_found")
        self.assertEqual(self.get("/api/nothing", expect=404)["error"]["code"], "not_found")
        self.assertEqual(self.get("/api/targets/search?q=a", expect=400)["error"]["code"], "bad_request")

    def test_program_id_format(self):
        self.assertEqual(collectors.program_id("HackerOne", "GitLab"), "hackerone:gitlab")
        self.assertEqual(collectors.program_id("Bugcrowd", "bitso-mbb-og"), "bugcrowd:bitso-mbb-og")
        self.assertEqual(collectors.program_id("Sherlock", 56), "sherlock:56")
        cdir = os.path.join(helpers.ROOT, "targetfinder", "collectors")
        for name in ("hackerone", "bugcrowd", "intigriti", "yeswehack", "hackenproof", "immunefi", "sherlock"):
            with open(os.path.join(cdir, name + ".py")) as f:
                src = f.read()
            self.assertIn("program_id(", src, f"{name} must build ids with collectors.program_id")
            self.assertNotRegex(src, r'f"[A-Za-z]+:\{', f"{name} builds a hand-made id")
        con = db.connect()
        for (pid,) in con.execute("SELECT id FROM programs"):
            self.assertRegex(pid, ID_RE)
        con.close()

    def test_mcp_tool_names(self):
        self.assertEqual([t["name"] for t in mcp_server.TOOLS], MCP_TOOLS)
        con = db.connect()
        res = mcp_server.call(con, "get_program", {"program_id": "testplat:acme"})
        self.assertEqual(res["id"], "testplat:acme"); self.assertIn("scope_counts", res)
        res = mcp_server.call(con, "recent_target_changes", {"page_size": 5})
        self.assertIn("items", res); self.assertFalse(OLD_KEYS & helpers.keys_in(res))
        res = mcp_server.call(con, "leads", {"tier": "solid"})
        self.assertIn("items", res)
        res = mcp_server.call(con, "watchlist_new_targets", {"program_ids": ["testplat:acme"], "since": "2020-01-01T00:00:00Z"})
        self.assertTrue(res["testplat:acme"]["has_new"])
        con.close()


if __name__ == "__main__":
    unittest.main()
