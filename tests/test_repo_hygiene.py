"""Repository hygiene: the public tree carries no private data, no third-party dumps and no vendor strings."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Strings that must never appear in the public repository (stored hex-encoded so this file passes its own check).
FORBIDDEN = [bytes.fromhex(h).decode() for h in ("62627261646172", "6262725f6c697665")]
SKIP_DIRS = {".git", ".superpowers", "__pycache__", "data", "sweeps"}
PRIVATE_TOP_LEVEL = {"seed", "data", "compat"}


def repo_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield os.path.join(dirpath, name)


class RepoHygiene(unittest.TestCase):
    def test_no_private_strings_or_dirs(self):
        self.assertTrue(os.path.isdir(os.path.join(ROOT, "targetfinder")), "package dir targetfinder/ missing")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "bin", "targetfinder")), "bin/targetfinder missing")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "LICENSE")), "LICENSE missing")
        for top in PRIVATE_TOP_LEVEL:
            self.assertFalse(os.path.exists(os.path.join(ROOT, top)), f"private directory {top}/ present")
        offenders = []
        for path in repo_files():
            try:
                text = open(path, "rb").read().decode("utf-8", "replace").lower()
            except OSError:
                continue
            for word in FORBIDDEN:
                if word in text:
                    offenders.append(f"{os.path.relpath(path, ROOT)}: {word}")
        self.assertEqual(offenders, [])

    def test_readme_has_heuristic_notice(self):
        readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        self.assertIn(
            "competitionRisk and opportunity scores are comparative estimates derived from public counts, "
            "not measured probabilities",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
