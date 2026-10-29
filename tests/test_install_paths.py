"""Installed with pip, the package lives in site-packages: data must go to a per-user directory, never next to the code."""
import os, sys, subprocess, tempfile, unittest
from tests import helpers

ROOT = helpers.ROOT


class InstallPaths(unittest.TestCase):
    def test_checkout_detection(self):
        from targetfinder import config
        self.assertTrue(config.in_checkout(ROOT))
        self.assertFalse(config.in_checkout(tempfile.mkdtemp(prefix="tf-site-")))

    def test_user_data_dir_per_os(self):
        from targetfinder import config
        env = {"XDG_DATA_HOME": "/x/share", "LOCALAPPDATA": r"C:\Users\u\AppData\Local", "HOME": "/home/u"}
        self.assertEqual(config.user_data_dir("Linux", env), "/x/share/targetfinder")
        self.assertEqual(config.user_data_dir("Linux", {"HOME": "/home/u"}), "/home/u/.local/share/targetfinder")
        self.assertEqual(config.user_data_dir("Darwin", {"HOME": "/Users/u"}), "/Users/u/Library/Application Support/targetfinder")
        self.assertEqual(config.user_data_dir("Windows", env), r"C:\Users\u\AppData\Local\targetfinder")

    def test_defaults_outside_a_checkout_use_the_user_dir(self):
        from targetfinder import config
        d = config.default_dirs(root="/usr/lib/python3/site-packages", system="Linux", env={"HOME": "/home/u"})
        self.assertEqual(d["home"], "/home/u/.local/share/targetfinder")
        self.assertEqual(d["data"], "/home/u/.local/share/targetfinder")
        self.assertEqual(d["sweeps"], "/home/u/.local/share/targetfinder/sweeps")
        d = config.default_dirs(root=ROOT, system="Linux", env={"HOME": "/home/u"})
        self.assertEqual(d["home"], ROOT); self.assertEqual(d["data"], os.path.join(ROOT, "data")); self.assertEqual(d["sweeps"], os.path.join(ROOT, "sweeps"))

    def test_module_entry_point(self):
        out = subprocess.run([sys.executable, "-m", "targetfinder", "--version"], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr); self.assertIn("targetfinder 0.", out.stdout)

    def test_scheduler_uses_the_installed_command_outside_a_checkout(self):
        from targetfinder import schedule
        self.assertEqual(schedule.bin_path(root=ROOT), os.path.join(ROOT, "bin", "targetfinder"))
        self.assertEqual(schedule.bin_path(root="/nowhere", which=lambda n: "/usr/local/bin/targetfinder"), "/usr/local/bin/targetfinder")


if __name__ == "__main__":
    unittest.main()
