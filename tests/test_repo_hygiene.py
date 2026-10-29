"""Repository hygiene: the public tree carries no private data, no third-party dumps and no vendor strings."""
import os, re, subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Strings that must never appear in the public repository (stored hex-encoded so this file passes its own check).
FORBIDDEN = [bytes.fromhex(h).decode() for h in ("62627261646172", "6262725f6c697665")]
# Anything shaped like a credential is refused even as a test value: secret scanners read tests too.
SECRET_SHAPES = [("an AWS key id", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), ("a GitHub token", r"\b(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
                 ("a Discord webhook", r"discord(?:app)?\.com/api/webhooks/\d{8,}/[A-Za-z0-9_-]{20,}"), ("a Telegram bot token", r"\b\d{8,}:[A-Za-z0-9_-]{30,}\b"),
                 ("a private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----")]
SKIP_DIRS = {".git", ".superpowers", "__pycache__", "data", "sweeps"}
PRIVATE_TOP_LEVEL = {"seed", "compat"}
RUNTIME_DIRS = {"data", "sweeps"}   # created at run time; must be git-ignored, never tracked


def repo_files():
    """Tracked files only: what a push publishes. Ignored runtime folders (data/, sweeps/, public/) are checked separately."""
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"], capture_output=True)
    if out.returncode == 0 and out.stdout:
        for rel in out.stdout.decode().split("\0"):
            if rel: yield os.path.join(ROOT, rel)
        return
    for dirpath, dirnames, filenames in os.walk(ROOT):   # not a git checkout (e.g. an sdist): walk, skipping runtime dirs
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
        ignored = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read().split()
        for d in RUNTIME_DIRS:
            self.assertIn(d + "/", ignored, f"{d}/ must be git-ignored")
            tracked = subprocess.run(["git", "-C", ROOT, "ls-files", d], capture_output=True, text=True).stdout.strip()
            self.assertEqual(tracked, "", f"{d}/ has tracked files: {tracked[:200]}")
        offenders = []
        for path in repo_files():
            try:
                text = open(path, "rb").read().decode("utf-8", "replace").lower()
            except OSError:
                continue
            for word in FORBIDDEN:
                if word in text:
                    offenders.append(f"{os.path.relpath(path, ROOT)}: {word}")
            raw = open(path, "rb").read().decode("utf-8", "replace")
            for name, pat in SECRET_SHAPES:
                if re.search(pat, raw):
                    offenders.append(f"{os.path.relpath(path, ROOT)}: looks like {name}")
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
