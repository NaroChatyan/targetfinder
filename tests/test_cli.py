"""CLI behaviour: help, exit codes, human vs JSON output, empty-result messages."""
import io, json, os, unittest, contextlib
from unittest import mock
from tests import helpers


def run_cli(argv):
    """(exit_code, stdout) for one CLI invocation."""
    from targetfinder import cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            code = cli.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return (code or 0), buf.getvalue()


class HelpAndVersion(unittest.TestCase):
    def test_version(self):
        from targetfinder import config
        code, out = run_cli(["--version"])
        self.assertEqual(code, 0); self.assertIn(f"targetfinder {config.VERSION}", out)

    def test_no_arguments_prints_grouped_help_with_quickstart(self):
        code, out = run_cli([])
        self.assertEqual(code, 0)
        for word in ("Setup", "Daily", "Lookups", "Data", "init", "sweep", "schedule install"):
            self.assertIn(word, out)

    def test_every_command_has_a_description(self):
        from targetfinder import cli
        _, out = run_cli(["-h"])
        for name, help_text in cli.COMMAND_HELP.items():
            self.assertIn(name, out); self.assertIn(help_text, out)

    def test_sweep_options_are_documented(self):
        _, out = run_cli(["sweep", "-h"])
        self.assertNotIn("4-tab", out)
        for opt in ("--since", "--no-write", "--max-per-program", "--bulk-threshold"):
            idx = out.index(opt); line = out[idx:].splitlines()[0]
            self.assertTrue(len(line.strip()) > len(opt) + 10 or out[idx:].splitlines()[1].strip(), f"{opt} has no help text")


class Formatting(unittest.TestCase):
    def test_money(self):
        from targetfinder import cli
        self.assertEqual(cli.money(4000, "USD"), "$4,000")
        self.assertEqual(cli.money(500, "EUR"), "€500")
        self.assertEqual(cli.money(None, "USD"), "-")
        self.assertEqual(cli.money(2500.5, "XYZ"), "2,500.5 XYZ")

    def test_no_color_outside_a_terminal(self):
        from targetfinder import cli
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.paint("x", "green"), "x")
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), mock.patch("sys.stdout.isatty", return_value=True):
            self.assertEqual(cli.paint("x", "green"), "x")


class WithData(unittest.TestCase):
    def setUp(self):
        helpers.temp_db()
        from targetfinder import db
        self.con = db.connect(); self.addCleanup(self.con.close)
        helpers.apply(self.con, [helpers.program("testplat:acme", [helpers.target("api.acme.example"), helpers.target("*.acme.example")], name="Acme Corp", bounty_max=None, bounty_min=None),
                                 helpers.program("testplat:beta", [helpers.target("beta.example")], name="Beta VDP", program_kind="vdp", reward_status="no_reward", bounty_max=0)])
        helpers.apply(self.con, [helpers.program("testplat:acme", [helpers.target("api.acme.example"), helpers.target("*.acme.example"), helpers.target("new.acme.example")], name="Acme Corp", bounty_max=None, bounty_min=None),
                                 helpers.program("testplat:beta", [helpers.target("beta.example")], name="Beta VDP", program_kind="vdp", reward_status="no_reward", bounty_max=0)])

    def test_programs_table_never_prints_none(self):
        code, out = run_cli(["programs"])
        self.assertEqual(code, 0); self.assertNotIn("None", out); self.assertIn("Acme Corp", out); self.assertIn("vdp", out)

    def test_program_card_is_human_and_json_on_request(self):
        code, out = run_cli(["program", "testplat:acme"])
        self.assertEqual(code, 0); self.assertFalse(out.lstrip().startswith("{"))
        for word in ("Acme Corp", "TestPlat", "https://example.test/acme", "Scope", "new.acme.example"):
            self.assertIn(word, out)
        code, out = run_cli(["program", "testplat:acme", "--json"])
        self.assertEqual(json.loads(out)["id"], "testplat:acme")

    def test_program_not_found_exits_1(self):
        code, out = run_cli(["program", "nope:nothing"])
        self.assertEqual(code, 1); self.assertIn("not found", out)

    def test_changes_footer_and_json(self):
        code, out = run_cli(["changes"])
        self.assertEqual(code, 0); self.assertIn("new.acme.example", out); self.assertIn("1 of 1", out); self.assertNotIn('"page"', out)
        code, out = run_cli(["changes", "--json"])
        self.assertEqual(json.loads(out)["page"]["total"], 1)

    def test_rankings_empty_explains_why(self):
        code, out = run_cli(["rankings", "rewards"])
        self.assertEqual(code, 0); self.assertIn("no ranked programs", out.lower())

    def test_rankings_bad_period_exits_2(self):
        code, out = run_cli(["rankings", "scope", "--period", "3d"])
        self.assertEqual(code, 2)

    def test_watch_list_empty_hint(self):
        code, out = run_cli(["watch", "list"])
        self.assertEqual(code, 0); self.assertIn("watchlist is empty", out); self.assertIn("watch add", out)

    def test_status_human_and_json(self):
        code, out = run_cli(["status"])
        self.assertEqual(code, 0); self.assertFalse(out.lstrip().startswith("{")); self.assertIn("TestPlat", out); self.assertIn("programs", out)
        code, out = run_cli(["status", "--json"])
        self.assertEqual(json.loads(out)["counts"]["programs"], 2)

    def test_resolve_and_payouts_have_json_flags(self):
        code, out = run_cli(["resolve", "acme"])
        self.assertEqual(code, 0); self.assertIn("testplat:acme", out); self.assertFalse(out.lstrip().startswith("["))
        code, out = run_cli(["resolve", "acme", "--json"])
        self.assertEqual(json.loads(out)[0]["id"], "testplat:acme")
        code, out = run_cli(["payouts", "testplat:acme", "--json"])
        self.assertEqual(json.loads(out)["program"]["id"], "testplat:acme")


class LoopAndSync(unittest.TestCase):
    def test_loop_runs_sync_then_sweep_until_interrupted(self):
        calls, sleeps = [], []
        def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 2: raise KeyboardInterrupt
        with mock.patch("targetfinder.sync.run", side_effect=lambda **kw: calls.append("sync") or {}), \
             mock.patch("targetfinder.sweep.run", side_effect=lambda *a, **kw: calls.append("sweep") or ("report", {})), \
             mock.patch("time.sleep", side_effect=fake_sleep):
            code, out = run_cli(["loop", "--every", "2"])
        self.assertEqual(code, 0); self.assertEqual(calls, ["sync", "sweep", "sync", "sweep"]); self.assertEqual(sleeps, [7200, 7200])

    def test_watch_no_longer_takes_every(self):
        code, out = run_cli(["watch", "--every", "2"])
        self.assertEqual(code, 2)

    def test_sync_failure_exits_3(self):
        with mock.patch("targetfinder.sync.run", return_value={"TestPlat": {"error": "boom"}, "repos": {"polled": 0}}):
            code, out = run_cli(["sync"])
        self.assertEqual(code, 3); self.assertIn("boom", out)
        with mock.patch("targetfinder.sync.run", return_value={"TestPlat": {"programs": 3, "added": 1, "removed": 0, "updated": 0, "readded": 0, "seconds": 1.0}}):
            code, out = run_cli(["sync"])
        self.assertEqual(code, 0); self.assertIn("TestPlat", out)


if __name__ == "__main__":
    unittest.main()
