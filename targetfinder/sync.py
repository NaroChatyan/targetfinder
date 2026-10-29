"""Collect → diff → persist.

mode="live" emits events; mode="baseline" records state silently (first listing of a platform)."""
import json, traceback, time
from . import db, config, normalize, intel
from .collectors import load_all

MATERIAL_TARGET_FIELDS = ("in_scope", "eligible_for_bounty", "target_type", "wildcard", "scope_tags", "language_tags", "severity_label", "display_name")

def load_existing(con, platform):
    out = {}
    for r in con.execute("SELECT id, detail_marker, detail_fetched_at, fee_amount, fee_unit, fee_kind, fee_source, (SELECT COUNT(*) FROM targets t WHERE t.program_id=p.id AND t.is_active=1) AS nt FROM programs p WHERE platform=?", (platform,)):
        out[r["id"]] = {"detail_marker": r["detail_marker"], "detail_fetched_at": r["detail_fetched_at"], "has_targets": (r["nt"] or 0) > 0,
                        "fee": ({"amount": r["fee_amount"], "unit": r["fee_unit"], "kind": r["fee_kind"], "source": r["fee_source"]} if r["fee_source"] else None)}
    return out

def _event(con, pid, now, change_type, category, before=None, after=None, counts=None, source=None):
    con.execute("INSERT INTO program_events(program_id, observed_at, change_type, category, before, after, counts, source) VALUES(?,?,?,?,?,?,?,?)",
                (pid, now, change_type, category, db.j(before), db.j(after), db.j(counts), source))

def _reward_state(p):
    return {"minimum": p.get("bounty_min"), "maximum": p.get("bounty_max"), "currency": p.get("reward_currency"), "status": p.get("reward_status")}

def apply_program(con, p, now, source, mode="live"):
    """Upsert one program. Returns list of event names emitted."""
    pid = p["id"]
    old = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
    fee = p.get("fee")
    if fee is None and old is not None and old["fee_source"]:
        fee = {"amount": old["fee_amount"], "unit": old["fee_unit"], "kind": old["fee_kind"], "source": old["fee_source"]}
        fee_updated = old["fee_updated_at"]
    else:
        fee_updated = now if (fee is not None and (old is None or old["fee_amount"] != (fee or {}).get("amount"))) else (old["fee_updated_at"] if old else None)
    data = {k: p.get(k) for k in ("name", "link", "profile_picture", "program_kind", "program_status", "is_active", "reward_status", "bounty_min", "bounty_max",
                                   "reward_currency", "launch_at", "launch_at_precision", "ends_at", "report_count", "report_count_source", "report_count_label",
                                   "policy_url", "safe_harbor", "raw")}
    if p.get("extra"):
        data["extra"] = p["extra"]
    events = []
    detail_fetched = now if p.get("targets") is not None else (old["detail_fetched_at"] if old else None)
    marker = p.get("detail_marker") if p.get("targets") is not None else (old["detail_marker"] if old else p.get("detail_marker"))
    if old is None:
        added_at = p.get("added_at") or now
        con.execute("""INSERT INTO programs(id, platform, handle, name, catalog, program_kind, program_status, is_active, listed, reward_status, bounty_min, bounty_max,
                       reward_currency, report_count, launch_at, added_at, last_updated, targets_updated_at, last_seen, left_at, fee_amount, fee_unit, fee_kind, fee_source,
                       fee_updated_at, source, detail_fetched_at, detail_marker, data)
                       VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?)""",
                    (pid, p["platform"], p["handle"], p.get("name"), p.get("catalog") or "platform", p.get("program_kind"), p.get("program_status"), 1 if p.get("is_active", True) else 0,
                     p.get("reward_status"), p.get("bounty_min"), p.get("bounty_max"), p.get("reward_currency"), p.get("report_count"), p.get("launch_at"), added_at,
                     p.get("last_updated") or now, p.get("targets_updated_at"), now, (fee or {}).get("amount"), (fee or {}).get("unit"), (fee or {}).get("kind"), (fee or {}).get("source"),
                     fee_updated, source, detail_fetched, marker, db.j(data)))
        if mode == "live":
            _event(con, pid, now, "first_seen", "program", None, {"name": p.get("name"), "status": p.get("program_status")}, None, source); events.append("first_seen")
        return events
    olddata = db.uj(old["data"], {})
    material = False
    if old["listed"] == 0 and mode == "live":
        _event(con, pid, now, "returned", "program", {"status": "unavailable"}, {"status": p.get("program_status")}, None, source); events.append("returned"); material = True
    new_status = p.get("program_status")
    if new_status and old["program_status"] != new_status and old["listed"] != 0:
        # debounce: a pause/resume must be observed on two consecutive runs before it becomes an event (platform listings flip-flop)
        if mode == "live" and old["pending_status"] != new_status:
            con.execute("UPDATE programs SET pending_status=?, pending_status_at=? WHERE id=?", (new_status, now, pid))
            new_status = old["program_status"]   # keep the old status for now
            p = dict(p); p["program_status"] = new_status; p["is_active"] = new_status == "active"
        else:
            ev = "paused" if new_status == "paused" else "resumed"
            if mode == "live":
                _event(con, pid, now, ev, "status", {"status": old["program_status"]}, {"status": new_status}, None, source); events.append(ev)
            con.execute("UPDATE programs SET pending_status=NULL, pending_status_at=NULL WHERE id=?", (pid,))
            material = True
    elif old["pending_status"]:
        con.execute("UPDATE programs SET pending_status=NULL, pending_status_at=NULL WHERE id=?", (pid,))
    if fee is not None and old["fee_source"] and (old["fee_amount"] or 0) != (fee.get("amount") or 0) and p.get("fee") is not None:
        if mode == "live":
            _event(con, pid, now, "fee_changed", "fees", {"amount": old["fee_amount"], "unit": old["fee_unit"], "kind": old["fee_kind"]}, {"amount": fee.get("amount"), "unit": fee.get("unit"), "kind": fee.get("kind")}, None, source)
            events.append("fee_changed")
        material = True
    before_r = {"minimum": old["bounty_min"], "maximum": old["bounty_max"], "currency": old["reward_currency"], "status": old["reward_status"]}
    after_r = _reward_state(p)
    for k, ok in (("minimum", "bounty_min"), ("maximum", "bounty_max"), ("currency", "reward_currency"), ("status", "reward_status")):
        if after_r[k] is None:
            after_r[k] = before_r[k]
    supplied = p.get("bounty_max") is not None or p.get("bounty_min") is not None or (p.get("reward_status") not in (None, "unknown"))
    if supplied and (before_r["minimum"], before_r["maximum"], before_r["status"]) != (after_r["minimum"], after_r["maximum"], after_r["status"]):
        if mode == "live":
            _event(con, pid, now, "rewards_changed", "rewards", before_r, after_r, None, source); events.append("rewards_changed")
        material = True
    diffs = {}
    norm = lambda v: " ".join(str(v).split()).casefold() if v is not None else None
    for k in ("name", "program_kind", "ends_at"):
        if p.get(k) is not None and norm(olddata.get(k)) != norm(p.get(k)):
            diffs[k] = {"before": olddata.get(k), "after": p.get(k)}
    if diffs:
        if mode == "live":
            _event(con, pid, now, "updated", "program", {k: v["before"] for k, v in diffs.items()}, {k: v["after"] for k, v in diffs.items()}, None, source); events.append("updated")
        material = True
    # keep previously known fields when this run has no value for them
    for k, v in olddata.items():
        if data.get(k) is None and v is not None:
            data[k] = v
    con.execute("""UPDATE programs SET platform=?, handle=?, name=?, program_kind=?, program_status=?, is_active=?, listed=1, left_at=NULL, reward_status=?, bounty_min=?, bounty_max=?,
                   reward_currency=?, report_count=?, launch_at=COALESCE(?, launch_at), last_updated=?, last_seen=?, fee_amount=?, fee_unit=?, fee_kind=?, fee_source=?, fee_updated_at=?,
                   source=?, detail_fetched_at=?, detail_marker=?, data=?, targets_updated_at=COALESCE(targets_updated_at, ?) WHERE id=?""",
                (p["platform"], p["handle"], p.get("name") or old["name"], p.get("program_kind") or old["program_kind"], p.get("program_status") or old["program_status"],
                 1 if p.get("is_active", True) else 0, p.get("reward_status") or old["reward_status"], p.get("bounty_min") if p.get("bounty_min") is not None else old["bounty_min"],
                 p.get("bounty_max") if p.get("bounty_max") is not None else old["bounty_max"], p.get("reward_currency") or old["reward_currency"],
                 p.get("report_count") if p.get("report_count") is not None else old["report_count"], p.get("launch_at"),
                 now if material else old["last_updated"], now, (fee or {}).get("amount"), (fee or {}).get("unit"), (fee or {}).get("kind"), (fee or {}).get("source"), fee_updated,
                 source, detail_fetched, marker, db.j(data), p.get("targets_updated_at"), pid))
    return events

def _target_row(t):
    ident = (t.get("identifier") or "").strip()
    ttype, wildcard, tags, langs = normalize.classify(ident, t.get("hint_type"), t.get("display_name"), t.get("description"))
    extra_langs = [normalize.norm_lang(x) for x in (t.get("languages") or [])]
    langs = sorted(set(langs) | {x for x in extra_langs if x})
    repo = normalize.repo_from_url(ident) if ttype in ("source code", "smart contract", "other") else None
    return {"identifier": ident, "norm": normalize.norm_key(ident), "normalized_identifier": normalize.normalize_identifier(ident, ttype, wildcard),
            "display_name": t.get("display_name") or ident, "target_type": ttype, "in_scope": 1 if t.get("in_scope", True) else 0,
            "eligible_for_bounty": 1 if t.get("eligible_for_bounty", True) else 0, "wildcard": 1 if wildcard else 0, "scope_tags": tags, "language_tags": langs,
            "severity_score": t.get("severity_score"), "severity_label": t.get("severity_label"), "source_url": t.get("source_url"), "source_added_at": t.get("source_added_at"),
            "metadata": t.get("metadata") or {}, "repo_url": repo[0] if repo else None, "path_kind": repo[1] if repo else None, "path_prefix": repo[2] if repo else None,
            "_repo": repo, "_added_at": t.get("added_at"), "_last_changed_at": t.get("last_changed_at"), "_last_change_type": t.get("last_change_type")}

def _ensure_repo(con, repo):
    if not repo:
        return
    url, kind, prefix, provider, host, full = repo
    con.execute("INSERT INTO repos(url, provider, provider_host, full_name, active) VALUES(?,?,?,?,1) ON CONFLICT(url) DO UPDATE SET active=1", (url, provider, host, full))

def apply_targets(con, pid, targets, now, source, mode="live", changed_at=None, insert_missing=True, hint_ts=None, force_baseline=False):
    """Diff a program's full target list against the stored one. Returns counts dict."""
    changed_at = changed_at or now
    old_rows = {r["norm"]: r for r in con.execute("SELECT * FROM targets WHERE program_id=?", (pid,))}
    old_active = {k for k, r in old_rows.items() if r["is_active"]}
    new = {}
    for t in targets:
        row = _target_row(t)
        if not row["identifier"]:
            continue
        if row["norm"] in new:
            prev = new[row["norm"]]
            if row["in_scope"] and not prev["in_scope"]:
                new[row["norm"]] = row
            continue
        new[row["norm"]] = row
    if not new and len(old_active) >= 3 and mode == "live":
        return {"added": 0, "removed": 0, "updated": 0, "readded": 0, "skipped": "empty-scope-guard"}
    counts = {"added": 0, "removed": 0, "updated": 0, "readded": 0}
    # first observation of a program's scope is a baseline, not an "added" event
    pr = con.execute("SELECT targets_updated_at, added_at, detail_fetched_at, source FROM programs WHERE id=?", (pid,)).fetchone()
    baseline = mode == "live" and (force_baseline or not old_rows or (pr is not None and pr["detail_fetched_at"] is None))
    base_ts = None
    if baseline and pr:
        base_ts = pr["targets_updated_at"] or hint_ts
        if not base_ts:
            la = con.execute("SELECT launch_at, added_at FROM programs WHERE id=?", (pid,)).fetchone()
            base_ts = (la["launch_at"] if la and la["launch_at"] and la["launch_at"] < now else None) or (la["added_at"] if la and la["added_at"] and la["added_at"] < now[:10] else None)
    for norm, row in new.items():
        old = old_rows.get(norm)
        _ensure_repo(con, row["_repo"])
        if old is None:
            if not insert_missing:
                continue
            if baseline:
                sa = config.parse_ts(row.get("source_added_at"))
                row["_added_at"] = config.iso(sa) if sa else (base_ts or changed_at)
                row["_last_changed_at"] = row["_added_at"]
            t_added = row.get("_added_at") or changed_at
            t_changed = row.get("_last_changed_at") or t_added
            cur = con.execute("""INSERT INTO targets(program_id, norm, identifier, normalized_identifier, display_name, target_type, in_scope, eligible_for_bounty, wildcard, scope_tags,
                                 language_tags, severity_score, severity_label, source_url, source_added_at, added_at, last_seen, last_change_type, last_changed_at, is_active, metadata,
                                 repo_url, path_kind, path_prefix) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                              (pid, norm, row["identifier"], row["normalized_identifier"], row["display_name"], row["target_type"], row["in_scope"], row["eligible_for_bounty"], row["wildcard"],
                               db.j(row["scope_tags"]), db.j(row["language_tags"]), row["severity_score"], row["severity_label"], row["source_url"], row["source_added_at"],
                               t_added, now, row.get("_last_change_type") or "added", t_changed, db.j(row["metadata"]), row["repo_url"], row["path_kind"], row["path_prefix"]))
            if mode == "live" and not baseline:
                con.execute("INSERT INTO target_changes(target_id, program_id, change_type, changed_at, diff, source) VALUES(?,?,?,?,?,?)", (cur.lastrowid, pid, "added", changed_at, None, source))
            if baseline:
                counts["baseline"] = counts.get("baseline", 0) + 1
            else:
                counts["added"] += 1
            continue
        tid = old["id"]
        if not old["is_active"]:
            meta = dict(row["metadata"]); meta["reactivated"] = True
            con.execute("""UPDATE targets SET identifier=?, normalized_identifier=?, display_name=?, target_type=?, in_scope=?, eligible_for_bounty=?, wildcard=?, scope_tags=?, language_tags=?,
                           severity_score=?, severity_label=?, source_url=?, source_added_at=COALESCE(?, source_added_at), last_seen=?, last_change_type='readded', last_changed_at=?, is_active=1,
                           removed_at=NULL, metadata=?, repo_url=?, path_kind=?, path_prefix=? WHERE id=?""",
                        (row["identifier"], row["normalized_identifier"], row["display_name"], row["target_type"], row["in_scope"], row["eligible_for_bounty"], row["wildcard"],
                         db.j(row["scope_tags"]), db.j(row["language_tags"]), row["severity_score"], row["severity_label"], row["source_url"], row["source_added_at"], now, changed_at,
                         db.j(meta), row["repo_url"], row["path_kind"], row["path_prefix"], tid))
            if mode == "live" and not baseline:
                con.execute("INSERT INTO target_changes(target_id, program_id, change_type, changed_at, diff, source) VALUES(?,?,?,?,?,?)",
                            (tid, pid, "readded", changed_at, db.j({"before": {"is_active": False, "removed_at": old["removed_at"]}, "after": {"is_active": True}}), source))
            counts["readded"] += 0 if baseline else 1
            continue
        before, after = {}, {}
        for k in MATERIAL_TARGET_FIELDS:
            ov = old[k]; nv = row[k]
            if k in ("scope_tags", "language_tags"):
                ov = db.uj(ov, [])
            if k in ("in_scope", "eligible_for_bounty", "wildcard"):
                ov = 1 if ov else 0
            if ov != nv and not (k == "display_name" and (not nv or nv == row["identifier"]) ):
                before[k] = ov; after[k] = nv
        if before:
            con.execute("""UPDATE targets SET identifier=?, normalized_identifier=?, display_name=?, target_type=?, in_scope=?, eligible_for_bounty=?, wildcard=?, scope_tags=?, language_tags=?,
                           severity_score=?, severity_label=?, source_url=?, last_seen=?, last_change_type='updated', last_changed_at=?, metadata=?, repo_url=?, path_kind=?, path_prefix=? WHERE id=?""",
                        (row["identifier"], row["normalized_identifier"], row["display_name"], row["target_type"], row["in_scope"], row["eligible_for_bounty"], row["wildcard"],
                         db.j(row["scope_tags"]), db.j(row["language_tags"]), row["severity_score"], row["severity_label"], row["source_url"], now, changed_at, db.j(row["metadata"]),
                         row["repo_url"], row["path_kind"], row["path_prefix"], tid))
            if mode == "live" and not baseline:
                con.execute("INSERT INTO target_changes(target_id, program_id, change_type, changed_at, diff, source) VALUES(?,?,?,?,?,?)", (tid, pid, "updated", changed_at, db.j({"before": before, "after": after}), source))
            counts["updated"] += 0 if baseline else 1
        else:
            con.execute("UPDATE targets SET last_seen=?, metadata=?, severity_score=COALESCE(?, severity_score), source_url=COALESCE(?, source_url) WHERE id=?",
                        (now, db.j(row["metadata"]) if row["metadata"] else old["metadata"], row["severity_score"], row["source_url"], tid))
    for norm in (old_active - set(new.keys())) if mode == "live" else set():
        old = old_rows[norm]
        con.execute("UPDATE targets SET is_active=0, removed_at=?, last_change_type='removed', last_changed_at=? WHERE id=?", (changed_at, changed_at, old["id"]))
        if mode == "live" and not baseline:
            con.execute("INSERT INTO target_changes(target_id, program_id, change_type, changed_at, diff, source) VALUES(?,?,?,?,?,?)",
                        (old["id"], pid, "removed", changed_at, db.j({"before": {"is_active": True}, "after": {"is_active": False}}), source))
        counts["removed"] += 0 if baseline else 1
    if any(counts[k] for k in ("added", "removed", "updated", "readded")):
        con.execute("UPDATE programs SET targets_updated_at=? WHERE id=?", (changed_at, pid))
        if mode == "live" and not baseline:
            _event(con, pid, now, "scope", "scope", None, None, counts, source)
    elif baseline and counts.get("baseline"):
        con.execute("UPDATE programs SET targets_updated_at=COALESCE(targets_updated_at, ?) WHERE id=?", (changed_at, pid))
    return counts

def record_payout_facts(con, pid, facts, now):
    for f in facts or []:
        last = con.execute("SELECT amount, observed_at FROM payout_observations WHERE program_id=? AND metric=? ORDER BY observed_at DESC LIMIT 1", (pid, f["metric"])).fetchone()
        if last is not None and last["amount"] == f.get("amount"):
            # same value: refresh at most daily so series stay compact
            lt = config.parse_ts(last["observed_at"])
            if lt and (config.utcnow() - lt).total_seconds() < 86400:
                continue
        con.execute("INSERT INTO payout_observations(program_id, metric, amount, currency, unit, source_label, source_field, source_url, period, basis, observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, f["metric"], f.get("amount"), f.get("currency"), f.get("unit"), f.get("source_label"), f.get("source_field"), f.get("source_url"), f.get("period"), f.get("basis"), now))

def apply_platform(con, platform, res, log, source=None):
    now = config.iso()
    source = source or platform
    first_listing = not db.kv_get(con, f"platform_synced:{platform}")   # the first live listing of a platform is a baseline: programs are "known", not "new"
    seen = set(); stats = {"programs": 0, "events": 0, "added": 0, "removed": 0, "updated": 0, "readded": 0, "scoped": 0}
    for p in res.get("programs") or []:
        seen.add(p["id"])
        try:
            prev = con.execute("SELECT source, detail_fetched_at FROM programs WHERE id=?", (p["id"],)).fetchone()
            reconcile = prev is not None and prev["detail_fetched_at"] is None   # decide before apply_program rewrites the row
            evs = apply_program(con, p, now, source, mode="baseline" if (prev is None and first_listing) else "live")
            stats["events"] += len(evs); stats["programs"] += 1
            if p.get("targets") is not None:
                c = apply_targets(con, p["id"], p["targets"], now, source, hint_ts=p.get("targets_updated_at") or p.get("launch_at"), force_baseline=reconcile)
                for k in ("added", "removed", "updated", "readded", "baseline"):
                    stats[k] = stats.get(k, 0) + c.get(k, 0)
                stats["scoped"] += 1
            record_payout_facts(con, p["id"], p.get("payout_facts"), now)
        except Exception:
            log(f"{platform} apply {p.get('id')}: {traceback.format_exc(limit=2)}")
    if res.get("complete") and seen:
        gone = [r["id"] for r in con.execute("SELECT id FROM programs WHERE platform=? AND listed=1", (platform,)) if r["id"] not in seen]
        for pid in gone:
            con.execute("UPDATE programs SET listed=0, left_at=?, is_active=0, program_status='unavailable' WHERE id=?", (now, pid))
            _event(con, pid, now, "left_listing", "program", {"status": "active"}, {"status": "unavailable"}, None, source)
        stats["left_listing"] = len(gone)
    if res.get("complete") and seen:
        db.kv_set(con, f"platform_synced:{platform}", now)
    con.commit()
    return stats

def run(platforms=None, log=print, repo_poll=True, con=None):
    config.ensure_dirs()
    own = con is None
    con = con or db.connect()
    started = config.iso()
    cur = con.execute("INSERT INTO sync_runs(started_at, kind, status) VALUES(?,?,?)", (started, "sync", "running")); run_id = cur.lastrowid; con.commit()
    reg = load_all(); allstats = {}; lines = []
    def L(msg):
        lines.append(f"{config.iso()} {msg}"); log(msg)
    for name, fn in reg.items():
        if platforms and name not in platforms:
            continue
        t0 = time.time(); t0_iso = config.iso()
        try:
            existing = load_existing(con, name)
            res = fn(existing, L)
            st = apply_platform(con, name, res, L)
            st["seconds"] = round(time.time() - t0, 1)
            allstats[name] = st
            con.execute("INSERT INTO collector_runs(platform, started_at, finished_at, ok, programs, error) VALUES(?,?,?,1,?,NULL)", (name, t0_iso, config.iso(), st.get("programs", 0)))
            con.commit()
            L(f"{name}: {st}")
        except Exception as ex:
            con.rollback()   # nothing from this platform is kept: stored programs and targets stay as they were
            allstats[name] = {"error": str(ex)[:300]}
            con.execute("INSERT INTO collector_runs(platform, started_at, finished_at, ok, programs, error) VALUES(?,?,?,0,0,?)", (name, t0_iso, config.iso(), str(ex)[:500]))
            con.commit()
            L(f"{name} FAILED: {ex}\n{traceback.format_exc(limit=3)}")
    if repo_poll:
        try:
            from . import repowatch
            allstats["repos"] = repowatch.poll(con, L)
        except Exception as ex:
            allstats["repos"] = {"error": str(ex)[:300]}; L(f"repo poll FAILED: {ex}")
    try:
        from . import notify
        allstats["notify"] = notify.run(con, L)
    except Exception as ex:
        allstats["notify"] = {"error": str(ex)[:200]}; L(f"notify FAILED: {ex}")
    con.execute("UPDATE sync_runs SET finished_at=?, status=?, stats=?, log=? WHERE id=?", (config.iso(), "ok", db.j(allstats), "\n".join(lines)[-200000:], run_id))
    db.kv_set(con, "last_sync_at", config.iso())
    con.commit()
    if own:
        con.close()
    return allstats
