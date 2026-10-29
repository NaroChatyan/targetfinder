"""Poll default-branch HEADs of in-scope source repositories with `git ls-remote` and record pushes."""
import subprocess, json, re, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from . import db, config

PERMANENT_FAILURE_MARKERS = ("repository not found", "invalid username or token", "could not read username", "authentication failed", "malformed input", "does not appear to be a git repository")
PERMANENT_FAILURE_LIMIT = 3     # consecutive not-found / auth rejections before the repo is retired
TRANSIENT_FAILURE_LIMIT = 30    # timeouts, DNS blips, 5xx

def timeout_for(url):
    """Per-host ls-remote timeout: big GitLab monorepos answer HEAD slowly."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return config.REPO_LS_REMOTE_TIMEOUT_BY_HOST.get(host, config.REPO_LS_REMOTE_TIMEOUT)

def is_permanent_failure(message):
    m = (message or "").lower()
    return any(k in m for k in PERMANENT_FAILURE_MARKERS)

def ls_remote(url, timeout=None):
    """Return (ref_name, sha) for HEAD or raise."""
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/echo", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}
    out = subprocess.run(["git", "ls-remote", "--symref", url, "HEAD"], capture_output=True, text=True, timeout=timeout or timeout_for(url), env=env)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "ls-remote failed").strip()[:200])
    ref = None; sha = None
    for line in out.stdout.splitlines():
        if line.startswith("ref:"):
            m = re.match(r"ref:\s+(\S+)\s+HEAD", line)
            if m: ref = m.group(1).replace("refs/heads/", "")
        else:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "HEAD":
                sha = parts[0]
    if not sha:
        raise RuntimeError("no HEAD in ls-remote output")
    return ref or "HEAD", sha

def active_repos(con):
    return [db.row_to_dict(r) for r in con.execute("""SELECT r.* FROM repos r WHERE r.active=1 AND r.url NOT LIKE '%*%' AND r.url NOT LIKE '%,%' AND EXISTS (
        SELECT 1 FROM targets t JOIN programs p ON p.id=t.program_id WHERE t.repo_url=r.url AND t.is_active=1 AND t.in_scope=1 AND p.listed=1 AND p.is_active=1)""")]

def poll(con, log=print, limit=None, workers=None):
    repos = active_repos(con)
    if limit:
        repos = repos[:limit]
    log(f"repo poll: {len(repos)} repositories")
    now = config.iso()
    results = {}
    def work(r):
        try:
            return r["id"], ls_remote(r["url"]), None
        except Exception as ex:
            return r["id"], None, str(ex)[:200]
    with ThreadPoolExecutor(max_workers=workers or config.REPO_POLL_WORKERS) as ex:
        for rid, res, err in ex.map(work, repos):
            results[rid] = (res, err)
    stats = {"polled": len(repos), "changed": 0, "errors": 0, "initialized": 0}
    for r in repos:
        res, err = results.get(r["id"], (None, "no result"))
        if err:
            stats["errors"] += 1
            fc = (r["fail_count"] or 0) + 1
            limit = PERMANENT_FAILURE_LIMIT if is_permanent_failure(err) else TRANSIENT_FAILURE_LIMIT
            if fc >= limit: stats["retired"] = stats.get("retired", 0) + 1
            con.execute("UPDATE repos SET last_polled_at=?, last_error=?, fail_count=?, active=? WHERE id=?", (now, err, fc, 0 if fc >= limit else 1, r["id"]))
            continue
        ref, sha = res
        if r["head_sha"] is None:
            stats["initialized"] += 1
            con.execute("UPDATE repos SET ref_name=?, head_sha=?, last_polled_at=?, last_error=NULL, fail_count=0 WHERE id=?", (ref, sha, now, r["id"]))
        elif sha != r["head_sha"]:
            stats["changed"] += 1
            comparison = {"status": "not_requested", "reason": "complete", "method": "head_only", "ancestry": "unknown", "commit_count": None, "matcher_version": 2}
            con.execute("INSERT INTO repo_changes(repo_id, event_type, source, ref_name, before_sha, after_sha, observed_at, commit_count, comparison) VALUES(?,?,?,?,?,?,?,?,?)",
                        (r["id"], "push", "poll", ref, r["head_sha"], sha, now, None, db.j(comparison)))
            con.execute("UPDATE repos SET ref_name=?, head_sha=?, last_polled_at=?, last_changed_at=?, last_error=NULL, fail_count=0 WHERE id=?", (ref, sha, now, now, r["id"]))
        else:
            con.execute("UPDATE repos SET ref_name=?, last_polled_at=?, last_error=NULL, fail_count=0 WHERE id=?", (ref, now, r["id"]))
    con.commit()
    try:
        stats["classified"] = classify_new_pushes(con, log)
    except Exception as ex:
        log(f"push classification failed: {ex}")
    log(f"repo poll done: {stats}")
    return stats

import json as _json, os as _os, urllib.request as _ur, urllib.error as _ue, time as _time

DOCS = (".md", ".mdx", ".rst", ".txt", ".adoc"); DOC_DIRS = ("docs/", "doc/", "content/", "website/", "examples/", "example/")
DEPS = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "go.sum", "go.mod", "Cargo.lock", "poetry.lock", "uv.lock", "Pipfile.lock", "Gemfile.lock", "composer.lock", "requirements.txt", "package.json", "Cargo.toml", "pyproject.toml", "renovate.json", ".renovaterc", "dependabot.yml")
CI = (".github/", ".gitlab-ci", ".circleci/", ".travis", "Jenkinsfile", ".buildkite/", "azure-pipelines", ".pre-commit")
TESTS = ("test/", "tests/", "__tests__/", "spec/", "_test.go", ".test.", ".spec.", "testdata/", "fixtures/", "e2e/")

def classify_paths(paths):
    """Bucket a push by its changed paths: code | docs | deps | ci | tests | mixed | unknown."""
    if paths is None:
        return "unknown"
    if not paths:
        return "unknown"
    kinds = set()
    for p in paths:
        low = p.lower(); base = low.rsplit("/", 1)[-1]
        if any(low.startswith(d) or ("/" + d) in low for d in CI) or base in ("jenkinsfile",):
            kinds.add("ci")
        elif base in {d.lower() for d in DEPS} or base.endswith((".lock",)):
            kinds.add("deps")
        elif low.endswith(DOCS) or any(low.startswith(d) or ("/" + d) in low for d in DOC_DIRS) or base in ("changelog", "license", "readme"):
            kinds.add("docs")
        elif any(t in low for t in TESTS):
            kinds.add("tests")
        else:
            kinds.add("code")
    if kinds == {"code"} or ("code" in kinds and len(kinds) > 1 and "code" in kinds):
        return "code" if kinds == {"code"} else "mixed"
    return next(iter(kinds)) if len(kinds) == 1 else "mixed"

_gh_budget = {"window": 0, "used": 0}

def _github_token():
    tok = _os.environ.get("GITHUB_TOKEN") or _os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None

def changed_files(provider, host, full_name, before, after, token=None):
    """Return the list of changed paths for a push, or None when it cannot be determined."""
    try:
        if provider == "github":
            token = token or _github_token()
            if not token:
                hour = int(_time.time() // 3600)
                if _gh_budget["window"] != hour: _gh_budget.update(window=hour, used=0)
                if _gh_budget["used"] >= 50: return None
                _gh_budget["used"] += 1
            url = f"https://api.github.com/repos/{full_name}/compare/{before}...{after}?per_page=250"
            h = {"User-Agent": config.USER_AGENT, "Accept": "application/vnd.github+json"}
            if token: h["Authorization"] = f"Bearer {token}"
            with _ur.urlopen(_ur.Request(url, headers=h), timeout=40) as r:
                d = _json.loads(r.read())
            return [f["filename"] for f in d.get("files") or []]
        if provider == "gitlab":
            pid = _ur.quote(full_name, safe="")
            url = f"https://{host}/api/v4/projects/{pid}/repository/compare?from={before}&to={after}"
            with _ur.urlopen(_ur.Request(url, headers={"User-Agent": config.USER_AGENT}), timeout=40) as r:
                d = _json.loads(r.read())
            return [x.get("new_path") or x.get("old_path") for x in d.get("diffs") or []]
    except Exception:
        return None
    return None

def classify_new_pushes(con, log=print, limit=200):
    """Classify pushes recorded by poll() that have no push_kind yet."""
    rows = con.execute("""SELECT rc.id, rc.before_sha, rc.after_sha, r.provider, r.provider_host, r.full_name FROM repo_changes rc JOIN repos r ON r.id=rc.repo_id
                          WHERE rc.push_kind IS NULL AND rc.source='poll' ORDER BY rc.id DESC LIMIT ?""", (limit,)).fetchall()
    done = 0
    for rc in rows:
        files = changed_files(rc["provider"], rc["provider_host"], rc["full_name"], rc["before_sha"], rc["after_sha"])
        kind = classify_paths(files)
        con.execute("UPDATE repo_changes SET files=?, push_kind=? WHERE id=?", (db.j(files[:300]) if files is not None else None, kind, rc["id"]))
        done += 1
    con.commit()
    if rows:
        log(f"classified {done} pushes")
    return done

def compare_url(repo_url, provider, before, after):
    if provider == "gitlab":
        return f"{repo_url}/-/compare/{before}...{after}"
    if provider == "bitbucket":
        return f"{repo_url}/branches/compare/{after}..{before}"
    return f"{repo_url}/compare/{before}...{after}"
