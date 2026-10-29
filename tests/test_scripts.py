"""Runs the script-style behaviour checks under tests/scripts/ (each asserts its own invariants and prints an OK line)."""
import os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = sorted(f for f in os.listdir(os.path.join(HERE, "scripts")) if f.endswith(".py"))


class ScriptChecks(unittest.TestCase):
    def test_scripts(self):
        for name in SCRIPTS:
            with self.subTest(script=name):
                r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "scripts", name)], capture_output=True, text=True, timeout=300)
                self.assertEqual(r.returncode, 0, f"{name} failed:\n{r.stdout[-2000:]}\n{r.stderr[-3000:]}")
                self.assertIn("OK", r.stdout.splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
