"""Local HTTP API server."""
import json, re, urllib.parse, threading, traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from . import db, config, queries, activity, model

class Handler(BaseHTTPRequestHandler):
    server_version = f"targetfinder/{config.VERSION}"

    def log_message(self, fmt, *args):
        if config.API_LOG:
            super().log_message(fmt, *args)

    def _send(self, code, obj, content_type="application/json"):
        body = (json.dumps(obj, ensure_ascii=False) if content_type == "application/json" else obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-RateLimit-Limit", "100000"); self.send_header("X-RateLimit-Remaining", "99999"); self.send_header("X-RateLimit-Reset", "60")
        self.end_headers()
        self.wfile.write(body)

    def _bad(self, msg, errors=None):
        self._send(400, {"detail": msg, "errors": errors or [msg]})

    def _route(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        for prefix in ("/api/v1/pro/", "/api/v1/", "/pro/", "/"):
            if path.startswith(prefix):
                path = path[len(prefix):]; break
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
        return path.strip("/"), q

    def do_GET(self):
        path, q = self._route()
        try:
            con = db.connect()
            try:
                self._get(con, path, q)
            finally:
                con.close()
        except Exception as ex:
            self._send(500, {"detail": str(ex), "trace": traceback.format_exc(limit=4)})

    def do_POST(self):
        path, q = self._route()
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return self._send(413, {"detail": "request body exceeds 65536 bytes"})
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            return self._bad("invalid JSON body")
        try:
            con = db.connect()
            try:
                if path == "targets/export":
                    pids = body.get("program_ids") or None
                    if pids and len(pids) > 50: return self._bad("program_ids max 50")
                    res = queries.export_targets(con, pids, body.get("platforms"), body.get("tags"), body.get("opportunity_levels"), bool(body.get("include_out_of_scope")), bool(body.get("include_ineligible")),
                                                 body.get("format") or "json", int(body.get("limit") or 500))
                    if res["format"] == "csv":
                        return self._send(200, res.pop("csv"), "text/csv")
                    return self._send(200, res)
                self._send(404, {"detail": "Not found"})
            finally:
                con.close()
        except Exception as ex:
            self._send(500, {"detail": str(ex), "trace": traceback.format_exc(limit=4)})

    def _get(self, con, path, q):
        Q = queries
        if path in ("", "health"):
            return self._send(200, {"ok": True, "service": "targetfinder", "version": config.VERSION})
        if path == "status":
            return self._send(200, Q.status(con))
        if path == "capabilities":
            return self._send(200, Q.capabilities(con))
        if path == "programs":
            ps = Q.intv(q.get("page_size"), 50, 1, 100); page = Q.intv(q.get("page"), 1, 1)
            kind = q.get("program_kind"); rs = q.get("reward_status")
            if q.get("program_kind") and kind not in ("bounty", "vdp", "contest", "challenge"): return self._bad("invalid program_kind")
            return self._send(200, Q.list_programs(con, Q.csv(q.get("platforms")), Q.csv(q.get("tags")), q.get("search"), q.get("updated_since"), page, ps, kind, rs, q.get("program_status"),
                                                   Q.boolean(q.get("include_unlisted")), Q.boolean(q.get("free_submission_only")), Q.csv(q.get("competitionRisk")), full=Q.boolean(q.get("full"), True), order=q.get("order") or "name"))
        m = re.match(r"^programs/(.+?)(/targets)?$", path)
        if m:
            pid = m.group(1)
            if m.group(2):
                ps = Q.intv(q.get("page_size"), 250, 1, 500); page = Q.intv(q.get("page"), 1, 1)
                elig = q.get("eligibility") or "all"
                if elig not in ("all", "eligible", "ineligible"): return self._bad("eligibility must be all|eligible|ineligible")
                ms = q.get("minTargetScore"); ms = int(ms) if ms not in (None, "") else None
                res = Q.program_targets(con, pid, elig, q.get("target_type"), q.get("search"), ms, q.get("targetOpportunityLabel"), q.get("competitionRisk"), page, ps, Q.boolean(q.get("include_inactive")))
                return self._send(404, {"detail": "Program not found"}) if res is None else self._send(200, res)
            res = Q.get_program(con, pid)
            return self._send(404, {"detail": "Program not found"}) if res is None else self._send(200, res)
        m = re.match(r"^opportunities/(elite|hot|strong|potential)$", path)
        if m:
            return self._send(200, Q.opportunities(con, m.group(1), Q.csv(q.get("platforms")), Q.csv(q.get("tags")), q.get("search"), Q.intv(q.get("page"), 1, 1), Q.intv(q.get("page_size"), 50, 1, 100), q.get("updated_since")))
        if path == "targets/search":
            qq = q.get("q")
            if qq is not None and qq != "" and not (2 <= len(qq) <= 512): return self._bad("q must be 2-512 characters")
            mm = q.get("match_mode") or "contains"
            if mm not in ("contains", "exact", "suffix", "wildcard"): return self._bad("invalid match_mode")
            return self._send(200, Q.search_targets(con, qq or None, mm, Q.csv(q.get("platforms")), Q.csv(q.get("tags")), Q.boolean(q.get("include_out_of_scope")), Q.boolean(q.get("include_ineligible")),
                                                    Q.intv(q.get("page"), 1, 1), Q.intv(q.get("page_size"), 50, 1, 500)))
        if path == "targets/changes":
            ct = q.get("change_type")
            if ct and ct not in ("added", "readded", "updated", "removed"): return self._bad("invalid change_type")
            after = q.get("after_id")
            if after not in (None, "") and Q.intv(q.get("page"), 1, 1) != 1: return self._bad("cursor mode requires page=1")
            ms = q.get("minTargetScore"); ms = int(ms) if ms not in (None, "") else None
            return self._send(200, Q.target_changes(con, ct, Q.boolean(q.get("include_removed")), Q.boolean(q.get("include_ineligible")), Q.boolean(q.get("include_out_of_scope")), q.get("search"),
                                                    Q.csv(q.get("platforms")), Q.csv(q.get("tags")), ms, q.get("targetOpportunityLabel"), q.get("competitionRisk"), Q.intv(q.get("page"), 1, 1),
                                                    Q.intv(q.get("page_size"), 50, 1, 100), int(after) if after not in (None, "") else None, Q.boolean(q.get("free_submission_only")), q.get("since"), q.get("program_id")))
        if path == "repositories/changes":
            after = q.get("after_id")
            return self._send(200, Q.repo_changes(con, q.get("search"), Q.csv(q.get("platforms")), Q.csv(q.get("tags")), Q.boolean(q.get("free_submission_only")), Q.boolean(q.get("include_targets"), True),
                                                  Q.intv(q.get("page"), 1, 1), Q.intv(q.get("page_size"), 50, 1, 100), int(after) if after not in (None, "") else None, q.get("since")))
        if path == "activity/capabilities":
            return self._send(200, activity.capabilities(con))
        if path == "activity/snapshot":
            return self._send(200, activity.snapshot())
        if path == "activity/rankings":
            tab = q.get("tab")
            if not tab: return self._bad("tab is required")
            res, err = activity.rankings(con, tab, q.get("period"), q.get("sort"), q.get("direction") or "desc", Q.intv(q.get("limit"), 50, 1, 100), Q.csv(q.get("platforms")), Q.csv(q.get("tags")),
                                         Q.csv(q.get("program_types")), Q.csv(q.get("labels")), Q.intv(q.get("next_cursor"), 0, 0), q.get("basis") or "tracked")
            return self._send(422, {"detail": err}) if err else self._send(200, res)
        m = re.match(r"^activity/events/(.+)$", path)
        if m:
            eid = m.group(1).replace("ev-", "")
            e = con.execute("SELECT * FROM program_events WHERE id=?", (eid,)).fetchone()
            if not e: return self._send(404, {"detail": "event not found"})
            return self._send(200, {"event_id": f"ev-{e['id']}", "program_id": e["program_id"], "observed_at": e["observed_at"], "change_type": e["change_type"], "category": e["category"],
                                    "items": [{"before": db.uj(e["before"]), "after": db.uj(e["after"]), "counts": db.uj(e["counts"])}], "meta": {"has_more": False, "next_cursor": None}})
        m = re.match(r"^payouts/programs/(.+)$", path)
        if m:
            prow = Q.program_row(con, m.group(1))
            return self._send(404, {"detail": "Program not found"}) if prow is None else self._send(200, activity.program_payouts(con, prow))
        m = re.match(r"^history/programs/(.+)$", path)
        if m:
            prow = Q.program_row(con, m.group(1))
            if prow is None: return self._send(404, {"detail": "Program not found"})
            return self._send(200, activity.history_changes(con, prow["id"], Q.intv(q.get("limit"), 50, 1, 200), Q.intv(q.get("cursor"), 0, 0), q.get("since"), q.get("category")))
        m = re.match(r"^activity/programs/(.+)$", path)
        if m:
            prow = Q.program_row(con, m.group(1))
            if prow is None: return self._send(404, {"detail": "Program not found"})
            return self._send(200, {"program": model.program_activity_block(con, prow), "activity_summary": model.activity_summary(con, prow["id"]), "as_of": config.iso()})
        if path == "history/changes":
            return self._send(200, activity.history_changes(con, q.get("program_id"), Q.intv(q.get("limit"), 50, 1, 200), Q.intv(q.get("cursor"), 0, 0), q.get("since"), q.get("category")))
        m = re.match(r"^history/changes/(.+)$", path)
        if m:
            eid = m.group(1).replace("ev-", "")
            e = con.execute("SELECT * FROM program_events WHERE id=?", (eid,)).fetchone()
            if not e: return self._send(404, {"detail": "change not found"})
            return self._send(200, {"id": f"ev-{e['id']}", "program_id": e["program_id"], "observed_at": e["observed_at"], "change_type": e["change_type"], "category": e["category"],
                                    "items": [{"before": db.uj(e["before"]), "after": db.uj(e["after"]), "counts": db.uj(e["counts"])}], "meta": {"has_more": False, "next_cursor": None}})
        if path.startswith("analytics/"):
            return self._send(503, {"detail": "analytics views are not available"})
        self._send(404, {"detail": "Not found"})

def serve(host=None, port=None):
    host = host or config.API_HOST; port = port or config.API_PORT
    config.ensure_dirs()
    db.connect().close()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"TargetFinder API listening on http://{host}:{port}/api/v1/pro/  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
