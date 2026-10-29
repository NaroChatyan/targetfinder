"""Local scheduling: systemd user units / launchd plist text, and `sweep --sync` ordering."""
import os, sys, unittest
from unittest import mock
from tests import helpers  # noqa: F401
from targetfinder import schedule, cli


class UnitFiles(unittest.TestCase):
    def test_unit_files_contain_bin_and_interval(self):
        files = schedule.unit_files(2, "/opt/tf/bin/targetfinder", system="Linux")
        self.assertEqual(set(files), {"targetfinder-sync.service", "targetfinder-sync.timer"})
        self.assertIn("OnCalendar=*-*-* 00/2:07:00", files["targetfinder-sync.timer"])
        self.assertIn("Persistent=true", files["targetfinder-sync.timer"])
        exec_line = next(l for l in files["targetfinder-sync.service"].splitlines() if l.startswith("ExecStart="))
        self.assertTrue(exec_line.endswith("/opt/tf/bin/targetfinder sync"), exec_line)

    def test_hourly_calendar(self):
        self.assertIn("OnCalendar=*-*-* *:07:00", schedule.unit_files(1, "/x/targetfinder", system="Linux")["targetfinder-sync.timer"])

    def test_launchd_plist(self):
        files = schedule.unit_files(3, "/opt/tf/bin/targetfinder", system="Darwin")
        self.assertEqual(list(files), ["io.targetfinder.sync.plist"])
        self.assertIn("<integer>10800</integer>", files["io.targetfinder.sync.plist"])
        self.assertIn("<string>/opt/tf/bin/targetfinder</string>", files["io.targetfinder.sync.plist"])

    def test_windows_prints_schtasks(self):
        text = schedule.windows_command(1, r"C:\tf\bin\targetfinder")
        self.assertTrue(text.startswith("schtasks /Create"), text); self.assertIn("/MO 1", text)


class SweepSync(unittest.TestCase):
    def test_sweep_sync_flag_runs_sync_first(self):
        calls = []
        with mock.patch("targetfinder.sync.run", side_effect=lambda **kw: calls.append("sync") or {}), \
             mock.patch("targetfinder.sweep.run", side_effect=lambda *a, **kw: calls.append("sweep") or ("report", {})):
            cli.main(["sweep", "--sync", "--no-write"])
        self.assertEqual(calls, ["sync", "sweep"])

    def test_watch_loops_sync_then_sweep_until_interrupted(self):
        calls = []
        sleeps = []
        def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 2: raise KeyboardInterrupt
        with mock.patch("targetfinder.sync.run", side_effect=lambda **kw: calls.append("sync") or {}), \
             mock.patch("targetfinder.sweep.run", side_effect=lambda *a, **kw: calls.append("sweep") or ("report", {})), \
             mock.patch("time.sleep", side_effect=fake_sleep):
            cli.main(["watch", "--every", "2"])
        self.assertEqual(calls, ["sync", "sweep", "sync", "sweep"]); self.assertEqual(sleeps, [7200, 7200])


if __name__ == "__main__":
    unittest.main()
