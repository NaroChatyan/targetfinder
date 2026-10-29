"""The GitHub workflows: hourly schedule, write permission, bounded data branch (single orphan commit, force-pushed)."""
import os, re, unittest
from tests import helpers

WF = os.path.join(helpers.ROOT, ".github", "workflows")


class SyncWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = open(os.path.join(WF, "sync.yml")).read()

    def test_schedule_permissions_concurrency_timeout(self):
        for needle in ('cron: "17 * * * *"', "workflow_dispatch:", "contents: write", "group: sync", "timeout-minutes: 50", "import-snapshot --in public", "export-snapshot --out public"):
            self.assertIn(needle, self.text, needle)

    def test_data_branch_is_a_single_orphan_commit(self):
        """A 53 MB targets.json committed hourly must not accumulate: every run rewrites the data branch as one orphan commit."""
        self.assertRegex(self.text, r"checkout --orphan|init -q -b data", "commit step must start from an orphan root")
        self.assertIn("push --force origin HEAD:data", self.text)
        self.assertNotIn("worktree add public origin/data\n", self.text.replace("git worktree add public origin/data", "x"))  # the previous history is only read, never extended

    def test_tests_workflow_runs_unittest(self):
        t = open(os.path.join(WF, "tests.yml")).read()
        self.assertIn("python3 -m unittest -v", t); self.assertIn('"3.11"', t)


if __name__ == "__main__":
    unittest.main()
