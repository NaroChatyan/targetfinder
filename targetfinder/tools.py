"""Hunter tools: watchlist, local clone staleness, diff pull."""
import os, re, subprocess, datetime as dt, urllib.request
from . import db, config, queries, repowatch, normalize

# ---- watchlist ---------------------------------------------------------------------------------
def watch_add(con, program_id, note=None):
    prow = queries.program_row(con, program_id)
    if prow is None:
        cands = queries.resolve_program(con, program_id, 5)
        if len(cands) == 1:
            prow = queries.program_row(con, cands[0]["id"])
        else:
            return None, cands
    con.execute("INSERT INTO watchlist(program_id, added_at, note) VALUES(?,?,?) ON CONFLICT(program_id) DO UPDATE SET note=COALESCE(excluded.note, note)", (prow["id"], config.iso(), note))
    con.commit(); return prow["id"], []

def watch_rm(con, program_id):
    n = con.execute("DELETE FROM watchlist WHERE program_id=? OR LOWER(program_id)=LOWER(?)", (program_id, program_id)).rowcount
    con.commit(); return n

def watch_list(con):
    return [dict(r) for r in con.execute("SELECT w.program_id, w.added_at, w.note, p.name, p.platform FROM watchlist w LEFT JOIN programs p ON p.id=w.program_id ORDER BY w.added_at")]

def watch_report(con, since_ts):
    """Changes, pushes and events since `since_ts` for every watched program."""
    out = []
    for w in watch_list(con):
        pid = w["program_id"]
        changes = queries.target_changes(con, None, True, True, True, None, None, None, None, None, None, 1, 100, None, False, since_ts, pid)["items"]
        pushes = queries.repo_changes(con, page_size=100, since_ts=since_ts, include_targets=False, program_id=pid)["items"]
        events = [dict(r) for r in con.execute("SELECT change_type, observed_at, before, after FROM program_events WHERE program_id=? AND observed_at>=? AND change_type!='scope' ORDER BY observed_at", (pid, since_ts))]
        out.append({"program_id": pid, "name": w["name"], "platform": w["platform"], "note": w["note"], "changes": changes, "pushes": pushes, "events": events})
    return out

# ---- local clones -------------------------------------------------------------------------------
def _git(args, cwd):
    out = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=30, env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin", "HOME": "/tmp"})
    return out.stdout.strip() if out.returncode == 0 else None

def find_clones(root):
    """Yield (path, origin_url, head_sha, head_date) for git clones up to two levels below root."""
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return
    for d1 in sorted(os.listdir(root)):
        p1 = os.path.join(root, d1)
        if not os.path.isdir(p1):
            continue
        cands = [p1] + [os.path.join(p1, d2) for d2 in sorted(os.listdir(p1)) if os.path.isdir(os.path.join(p1, d2))]
        for c in cands:
            if not os.path.isdir(os.path.join(c, ".git")):
                continue
            origin = _git(["remote", "get-url", "origin"], c)
            head = _git(["rev-parse", "HEAD"], c)
            date = _git(["log", "-1", "--format=%cI"], c)
            if origin and head:
                yield c, origin, head, date

def canonical_repo_url(origin):
    o = origin.strip()
    o = re.sub(r"^git@([^:]+):", r"https://\1/", o)
    o = re.sub(r"^ssh://git@", "https://", o)
    o = re.sub(r"\.git$", "", o).rstrip("/")
    r = normalize.repo_from_url(o)
    return r[0] if r else o

def clones_report(con, root):
    out = []
    for path, origin, head, date in find_clones(root):
        url = canonical_repo_url(origin)
        repo = con.execute("SELECT * FROM repos WHERE url=?", (url,)).fetchone()
        if repo is None:
            out.append({"path": path, "repo": url, "tracked": False, "local_head": head[:12], "local_date": date}); continue
        progs = [r[0] for r in con.execute("SELECT DISTINCT p.id FROM targets t JOIN programs p ON p.id=t.program_id WHERE t.repo_url=? AND t.is_active=1", (url,))]
        pushes = [dict(r) for r in con.execute("SELECT id, before_sha, after_sha, observed_at, push_kind FROM repo_changes WHERE repo_id=? ORDER BY observed_at DESC LIMIT 200", (repo["id"],))]
        since = []
        if repo["head_sha"] != head:
            matched = False
            for p in pushes:                      # newest first: everything after the push that produced the local HEAD
                if p["after_sha"] == head:
                    matched = True; break
                since.append(p)
                if p["before_sha"] == head:
                    matched = True; break
            if not matched:                       # local HEAD not in our push history: fall back to pushes observed after the local commit date
                ld = config.parse_ts(date)
                since = [p for p in pushes if ld and (config.parse_ts(p["observed_at"]) or ld) > ld]
        stale = bool(repo["head_sha"]) and repo["head_sha"] != head
        out.append({"path": path, "repo": url, "tracked": True, "programs": progs, "local_head": head[:12], "local_date": date, "remote_head": (repo["head_sha"] or "")[:12],
                    "stale": stale, "pushes_since_local": [{"id": p["id"], "at": p["observed_at"], "kind": p["push_kind"], "compare_url": repowatch.compare_url(url, repo["provider"], p["before_sha"], p["after_sha"])} for p in since]})
    return out

# ---- diff pull -----------------------------------------------------------------------------------
def pull_diff(con, push_id, out_dir=None):
    rc = con.execute("SELECT rc.*, r.url, r.provider, r.full_name FROM repo_changes rc JOIN repos r ON r.id=rc.repo_id WHERE rc.id=?", (push_id,)).fetchone()
    if rc is None:
        raise ValueError(f"push {push_id} not found")
    url = repowatch.compare_url(rc["url"], rc["provider"], rc["before_sha"], rc["after_sha"]) + ".diff"
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT, "Accept": "text/plain"})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    out_dir = out_dir or os.path.join(config.CACHE_DIR, f"diffs_{dt.date.today().isoformat()}")
    os.makedirs(out_dir, exist_ok=True)
    name = f"{rc['full_name'].replace('/', '_')}_{rc['before_sha'][:7]}_{rc['after_sha'][:7]}.diff"
    path = os.path.join(out_dir, name)
    open(path, "wb").write(body)
    files = [l[6:] for l in body.decode("utf-8", "replace").splitlines() if l.startswith("+++ b/")]
    if rc["push_kind"] is None:
        con.execute("UPDATE repo_changes SET files=?, push_kind=? WHERE id=?", (db.j(files[:300]), repowatch.classify_paths(files), push_id)); con.commit()
    return path, len(body), files
