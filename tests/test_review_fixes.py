"""Regression tests for the ten findings of the whole-branch cloud review (2026-10-29)."""
import io, os, json, time, unittest, contextlib, tempfile
from unittest import mock
from tests import helpers

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WorkflowFetch(unittest.TestCase):
    """1. A transient fetch error must not look like 'no data branch yet' and wipe the published history."""
    def test_fetch_failure_fails_the_job(self):
        text = open(os.path.join(ROOT, ".github", "workflows", "sync.yml")).read()
        self.assertIn("ls-remote --exit-code --heads origin data", text)
        fetch_lines = [l for l in text.splitlines() if "git fetch" in l]
        self.assertTrue(fetch_lines)
        for l in fetch_lines:
            self.assertNotIn("2>/dev/null", l); self.assertNotIn("||", l)
            self.assertFalse(l.strip().startswith("if "), "fetch must not be the condition that decides 'first run'")


class SnapshotExclusion(unittest.TestCase):
    """2. --exclude-platforms must accept slugs and display names alike."""
    def test_slug_excludes_platform(self):
        helpers.temp_db()
        from targetfinder import db, snapshot
        con = db.connect(); self.addCleanup(con.close)
        helpers.apply(con, [helpers.program("testplat:a", [helpers.target("a.example")])])
        helpers.apply(con, [helpers.program("otherplat:b", [helpers.target("b.example")])], platform="OtherPlat")
        out = tempfile.mkdtemp(prefix="tf-snap-")
        snapshot.export_snapshot(con, out, exclude_platforms=["testplat"])
        progs = json.load(open(os.path.join(out, "programs.json")))
        self.assertEqual([p["id"] for p in progs], ["otherplat:b"])
        self.assertEqual(json.load(open(os.path.join(out, "meta.json")))["excluded_platforms"], ["testplat"])


class RepoRetirement(unittest.TestCase):
    """3. The next sync must not reactivate a repository that polling just retired."""
    def test_retired_repo_stays_retired_after_resync(self):
        helpers.temp_db()
        from targetfinder import db, repowatch
        con = db.connect(); self.addCleanup(con.close)
        progs = [helpers.program("testplat:a", [helpers.target("https://github.com/acme/gone", hint_type="source code")])]
        helpers.apply(con, progs)
        def boom(url, timeout=None): raise RuntimeError("remote: Repository not found.")
        with mock.patch.object(repowatch, "ls_remote", boom):
            for _ in range(3): repowatch.poll(con, log=lambda m: None, workers=1)
        self.assertEqual(con.execute("SELECT active FROM repos").fetchone()[0], 0)
        helpers.apply(con, progs)
        self.assertEqual(con.execute("SELECT active FROM repos").fetchone()[0], 0)
        self.assertEqual(repowatch.active_repos(con), [])


class HackerOneScopePaging(unittest.TestCase):
    """4. A failed second page must leave the stored scope alone, not shrink it to page one."""
    def test_paging_failure_yields_no_targets(self):
        from targetfinder.collectors import hackerone
        p = {"handle": "big", "targets": [{"identifier": "a"}], "_scope_cursor": "c1"}
        def more_fail(handle, after, log): raise RuntimeError("500")
        hackerone.finish_scopes([p], lambda m: None, more=more_fail)
        self.assertIsNone(p["targets"]); self.assertNotIn("_scope_cursor", p)

    def test_paging_success_appends(self):
        from targetfinder.collectors import hackerone
        p = {"handle": "big", "targets": [{"identifier": "a"}], "_scope_cursor": "c1"}
        hackerone.finish_scopes([p], lambda m: None, more=lambda h, a, l: [{"identifier": "b"}])
        self.assertEqual([t["identifier"] for t in p["targets"]], ["a", "b"])


class HistoryProgramIds(unittest.TestCase):
    """5. History rows must carry the program id, not the event id."""
    def setUp(self):
        helpers.temp_db()
        from targetfinder import db
        self.con = db.connect(); self.addCleanup(self.con.close)
        helpers.apply(self.con, [helpers.program("testplat:acme", [helpers.target("https://github.com/acme/app", hint_type="source code")])])
        self.con.execute("INSERT INTO program_events(program_id, observed_at, change_type, category, before, after, counts, source) VALUES(?,?,?,?,?,?,?,?)",
                         ("testplat:acme", "2026-10-29T10:00:00Z", "fee_changed", "program", "0", "30", None, "test"))
        rid = self.con.execute("SELECT id FROM repos").fetchone()[0]
        self.con.execute("INSERT INTO repo_changes(repo_id, event_type, source, ref_name, before_sha, after_sha, observed_at) VALUES(?,?,?,?,?,?,?)",
                         (rid, "push", "poll", "main", "a" * 40, "b" * 40, "2026-10-29T11:00:00Z"))
        self.con.commit()

    def test_event_and_push_rows_reference_the_program(self):
        from targetfinder import activity
        res = activity.history_changes(self.con)
        kinds = {i["id"][:3]: i for i in res["items"]}
        self.assertIn("ev-", kinds); self.assertIn("rc-", kinds)
        for i in res["items"]:
            self.assertEqual(i["program"]["id"], "testplat:acme", i)


class CompetitionPaging(unittest.TestCase):
    """6. A competition filter must page and count the filtered set."""
    def test_total_counts_filtered_rows(self):
        helpers.temp_db()
        from targetfinder import db, queries
        con = db.connect(); self.addCleanup(con.close)
        helpers.apply(con, [helpers.program("testplat:a", [helpers.target("a.example")], report_count=900, launch_at="2018-01-01T00:00:00Z"),
                            helpers.program("testplat:b", [helpers.target("b.example")], report_count=900, launch_at="2018-01-01T00:00:00Z"),
                            helpers.program("testplat:c", [helpers.target("c.example")], report_count=0, launch_at="2026-10-20T00:00:00Z")])
        everything = queries.list_programs(con, page_size=50)["items"]
        levels = [p["competition"]["level"] for p in everything]
        self.assertGreater(len(set(levels)), 1, levels)   # the filter must have something to exclude
        level = levels[0]; n = levels.count(level)
        res = queries.list_programs(con, competition=[level], page_size=1)
        self.assertEqual(res["page"]["total"], n)
        self.assertEqual(len(res["items"]), 1); self.assertEqual(res["items"][0]["competition"]["level"], level)
        self.assertEqual(res["page"]["has_more"], n > 1)


class NotifyCursors(unittest.TestCase):
    """7. A channel that failed must get the digest again next run; a channel that succeeded must not."""
    def setUp(self):
        helpers.temp_db()
        from targetfinder import db, notify
        self.con = db.connect(); self.addCleanup(self.con.close)
        self.notify = notify
        self.cfg = {"discord_webhook": "https://discord.test/hook", "telegram": {"bot_token": "t", "chat_id": "1"},
                    "rules": {"watchlist": True, "new_programs": True, "new_targets": {"enabled": True, "free_only": False, "min_score": 0, "platforms": [], "max_per_program": 5}}}
        self.orig = notify.load_config; notify.load_config = lambda: self.cfg
        self.addCleanup(lambda: setattr(notify, "load_config", self.orig))
        helpers.apply(self.con, [helpers.program("testplat:a", [helpers.target("a.example")])])
        self.notify.run(self.con, lambda m: None, sender=lambda u, p, h=None: None)   # initialise cursors
        helpers.apply(self.con, [helpers.program("testplat:a", [helpers.target("a.example")]), helpers.program("testplat:fresh", [helpers.target("fresh.example")])])

    def test_failed_channel_is_retried_and_successful_one_is_not(self):
        calls = []
        def sender(url, payload, headers=None):
            calls.append("discord" if "discord" in url else "telegram")
            if "discord" in url: raise RuntimeError("429")
        r = self.notify.run(self.con, lambda m: None, sender=sender)
        self.assertIn("telegram", calls); self.assertIn("discord", calls)
        calls.clear()
        self.notify.run(self.con, lambda m: None, sender=sender)
        self.assertIn("discord", calls); self.assertNotIn("telegram", calls)

    def test_both_succeed_then_nothing_resent(self):
        calls = []
        self.notify.run(self.con, lambda m: None, sender=lambda u, p, h=None: calls.append(u))
        self.assertEqual(len(calls), 2)
        calls.clear()
        r = self.notify.run(self.con, lambda m: None, sender=lambda u, p, h=None: calls.append(u))
        self.assertEqual(calls, []); self.assertEqual(r["sent"], 0)


class RetryAfter(unittest.TestCase):
    """8. Retry-After may be an HTTP date; never let it raise out of fetch()."""
    def test_seconds_date_and_garbage(self):
        from targetfinder import http
        self.assertEqual(http.retry_after_seconds("120"), 120.0)
        self.assertEqual(http.retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)
        self.assertIsNone(http.retry_after_seconds("soon")); self.assertIsNone(http.retry_after_seconds(None))


class MirrorCache(unittest.TestCase):
    """9. The scope mirror must be re-downloaded on every sync run, not cached for the life of the process."""
    def test_cache_cleared_per_run(self):
        from targetfinder.collectors import btd
        from targetfinder import http
        n = []
        with mock.patch.object(http, "get_json", side_effect=lambda url, **kw: n.append(url) or {"ok": 1}):
            btd.clear(); btd.load("bugcrowd"); btd.load("bugcrowd")
            self.assertEqual(len(n), 1)
            btd.clear(); btd.load("bugcrowd")
            self.assertEqual(len(n), 2)
        btd.clear()


class CliVocabulary(unittest.TestCase):
    """10. The CLI must only offer values the query layer implements."""
    def test_unsupported_values_are_usage_errors(self):
        from tests.test_cli import run_cli
        helpers.temp_db()
        from targetfinder import db
        con = db.connect(); self.addCleanup(con.close)
        helpers.apply(con, [helpers.program("testplat:a", [helpers.target("a.example")])])
        self.assertEqual(run_cli(["targets", "testplat:a", "--eligibility", "bounty"])[0], 2)
        self.assertEqual(run_cli(["search", "a.example", "--mode", "prefix"])[0], 2)
        self.assertEqual(run_cli(["targets", "testplat:a", "--eligibility", "eligible"])[0], 0)
        code, out = run_cli(["search", "example", "--mode", "suffix"])
        self.assertEqual(code, 0); self.assertIn("a.example", out)


if __name__ == "__main__":
    unittest.main()
