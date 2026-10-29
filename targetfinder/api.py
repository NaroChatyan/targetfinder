"""Local HTTP API server (routes in docs/API.md). List responses are {"items", "page"}; errors are {"error": {"code", "message"}}."""
import json, re, urllib.parse, traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from . import db, config, queries, activity

MAX_BODY = 65536

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
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, kind, msg):
        self._send(code, {"error": {"code": kind, "message": msg}})

    def _bad(self, msg):
        self._error(400, "bad_request", msg)

    def _route(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        if path.startswith("/api/"):
            path = path[5:]
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
            self._send(500, {"error": {"code": "server_error", "message": str(ex)}, "trace": traceback.format_exc(limit=4)})

    def do_POST(self):
        path, q = self._route()
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._error(413, "payload_too_large", f"request body exceeds {MAX_BODY} bytes")
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
                    if pids and len(pids) > 50: return self._bad("program_ids: max 50")
                    res = queries.export_targets(con, pids, body.get("platforms"), body.get("tags"), body.get("tiers"), bool(body.get("include_out_of_scope")), bool(body.get("include_ineligible")),
                                                 body.get("format") or "json", int(body.get("limit") or 500))
                    if res["format"] == "csv":
                        return self._send(200, res.pop("csv"), "text/csv")
                    return self._send(200, res)
                self._error(404, "not_found", "no such route")
            finally:
                con.close()
        except Exception as ex:
            self._send(500, {"error": {"code": "server_error", "message": str(ex)}, "trace": traceback.format_exc(limit=4)})

    def _get(self, con, path, q):
        Q = queries
        if path in ("", "health"):
            return self._send(200, {"ok": True, "service": "targetfinder", "version": config.VERSION})
        if path == "status":
            return self._send(200, Q.status(con))
        if path == "capabilities":
            return self._send(200, {**Q.capabilities(con), "rankings": activity.capabilities(con)})
        if path == "programs":
            ps = Q.intv(q.get("page_size"), 50, 1, 100); page = Q.intv(q.get("page"), 1, 1)
            kind = q.get("kind")
            if kind and kind not in ("bounty", "vdp", "contest"): return self._bad("kind must be bounty|vdp|contest")
            return self._send(200, Q.list_programs(con, Q.csv(q.get("platforms")), Q.csv(q.get("tags")), q.get("search"), q.get("updated_since"), page, ps, kind, q.get("reward_status"), q.get("status"),
                                                   Q.boolean(q.get("include_unlisted")), Q.boolean(q.get("free_submission_only")), Q.csv(q.get("competition")), full=Q.boolean(q.get("full")), order=q.get("order") or "name"))
        m = re.match(r"^programs/(.+?)(/targets|/payouts|/history)?$", path)
        if m:
            pid, sub = m.group(1), m.group(2)
            prow = Q.program_row(con, pid)
            if prow is None: return self._error(404, "not_found", f"program {pid!r} not found")
            if sub == "/targets":
                ps = Q.intv(q.get("page_size"), 250, 1, 500); page = Q.intv(q.get("page"), 1, 1)
                elig = q.get("eligibility") or "all"
                if elig not in ("all", "eligible", "ineligible"): return self._bad("eligibility must be all|eligible|ineligible")
                ms = q.get("min_score"); ms = int(ms) if ms not in (None, "") else None
                return self._send(200, Q.program_targets(con, prow["id"], elig, q.get("type"), q.get("search"), ms, q.get("label"), q.get("competition"), page, ps, Q.boolean(q.get("include_inactive"))))
            if sub == "/payouts":
                return self._send(200, activity.program_payouts(con, prow))
            if sub == "/history":
                return self._send(200, activity.history_changes(con, prow["id"], Q.intv(q.get("limit"), 50, 1, 200), Q.intv(q.get("cursor"), 0, 0), q.get("since"), q.get("category")))
            return self._send(200, Q.get_program(con, prow["id"]))
        m = re.match(r"^leads/([a-z]+)$", path)
        if m:
            if m.group(1) not in Q.TIERS: return self._bad(f"tier must be one of {', '.join(Q.TIERS)}")
            return self._send(200, Q.leads(con, m.group(1), Q.csv(q.get("platforms")), Q.csv(q.get("tags")), q.get("search"), Q.intv(q.get("page"), 1, 1), Q.intv(q.get("page_size"), 50, 1, 100), q.get("updated_since"),
                                           free_submission_only=Q.boolean(q.get("free_submission_only"))))
        if path == "targets/search":
            qq = q.get("q")
            if qq is not None and qq != "" and not (2 <= len(qq) <= 512): return self._bad("q must be 2-512 characters")
            mm = q.get("match_mode") or "contains"
            if mm not in ("contains", "exact", "suffix", "wildcard"): return self._bad("match_mode must be contains|exact|suffix|wildcard")
            return self._send(200, Q.search_targets(con, qq or None, mm, Q.csv(q.get("platforms")), Q.csv(q.get("tags")), Q.boolean(q.get("include_out_of_scope")), Q.boolean(q.get("include_ineligible")),
                                                    Q.intv(q.get("page"), 1, 1), Q.intv(q.get("page_size"), 50, 1, 500)))
        if path == "changes/targets":
            ct = q.get("type")
            if ct and ct not in Q.CHANGE_TYPES: return self._bad("type must be added|readded|updated|removed")
            after = q.get("after_id")
            ms = q.get("min_score"); ms = int(ms) if ms not in (None, "") else None
            return self._send(200, Q.target_changes(con, ct, Q.boolean(q.get("include_removed")), Q.boolean(q.get("include_ineligible")), Q.boolean(q.get("include_out_of_scope")), q.get("search"),
                                                    Q.csv(q.get("platforms")), Q.csv(q.get("tags")), ms, q.get("label"), q.get("competition"), Q.intv(q.get("page"), 1, 1),
                                                    Q.intv(q.get("page_size"), 50, 1, 100), int(after) if after not in (None, "") else None, Q.boolean(q.get("free_submission_only")), q.get("since"), q.get("program_id")))
        if path == "changes/repos":
            after = q.get("after_id")
            return self._send(200, Q.repo_changes(con, q.get("search"), Q.csv(q.get("platforms")), Q.csv(q.get("tags")), Q.boolean(q.get("free_submission_only")), Q.boolean(q.get("include_targets"), True),
                                                  Q.intv(q.get("page"), 1, 1), Q.intv(q.get("page_size"), 50, 1, 100), int(after) if after not in (None, "") else None, q.get("since"), Q.csv(q.get("kinds")), q.get("program_id")))
        m = re.match(r"^rankings/([a-z_]+)$", path)
        if m:
            res, err = activity.rankings(con, m.group(1), q.get("period"), q.get("sort"), q.get("direction") or "desc", Q.intv(q.get("limit"), 50, 1, 100), Q.csv(q.get("platforms")), Q.csv(q.get("tags")),
                                         Q.csv(q.get("kinds")), Q.csv(q.get("labels")), Q.intv(q.get("cursor"), 0, 0))
            return self._bad(err) if err else self._send(200, res)
        if path == "rankings":
            return self._send(200, activity.capabilities(con))
        if path == "history":
            return self._send(200, activity.history_changes(con, q.get("program_id"), Q.intv(q.get("limit"), 50, 1, 200), Q.intv(q.get("cursor"), 0, 0), q.get("since"), q.get("category")))
        m = re.match(r"^history/ev-(\d+)$", path)
        if m:
            e = con.execute("SELECT * FROM program_events WHERE id=?", (m.group(1),)).fetchone()
            if not e: return self._error(404, "not_found", "event not found")
            return self._send(200, {"id": f"ev-{e['id']}", "program_id": e["program_id"], "at": e["observed_at"], "type": e["change_type"], "category": e["category"],
                                    "before": db.uj(e["before"]), "after": db.uj(e["after"]), "counts": db.uj(e["counts"]), "source": e["source"]})
        self._error(404, "not_found", "no such route")

def serve(host=None, port=None):
    host = host or config.API_HOST; port = port or config.API_PORT
    config.ensure_dirs()
    db.connect().close()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"TargetFinder API listening on http://{host}:{port}/api/  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
