"""HackerOne: unauthenticated GraphQL directory + structured scopes + public stats."""
import json
from .. import http, config
from . import register, program_id

GQL = "https://hackerone.com/graphql"
TEAM_FIELDS = """
  id handle name submission_state state offers_bounties launched_at resolved_report_count triage_active currency
  formatted_total_bounties_paid_amount formatted_bounties_paid_last_90_days reports_received_last_90_days
  last_report_resolved_at response_efficiency_percentage base_bounty website hide_bounty_amounts allows_bounty_splitting
  most_recent_sla_snapshot { average_time_to_first_program_response }
  external_program { id offers_rewards }
  profile_picture(size: medium)
  structured_scopes(first: 250, archived: false) { pageInfo { endCursor hasNextPage } edges { node { asset_type asset_identifier eligible_for_bounty eligible_for_submission max_severity instruction created_at updated_at } } }
"""
DIRECTORY = """query DirectoryQuery($first: Int, $after: String, $where: FiltersTeamFilterInput, $secureOrderBy: FiltersTeamFilterOrder) {
  teams(first: $first, after: $after, where: $where, secure_order_by: $secureOrderBy) {
    pageInfo { endCursor hasNextPage } edges { node { %s } } } }""" % TEAM_FIELDS
SCOPES_PAGE = """query Scopes($handle: String!, $after: String) { team(handle: $handle) { structured_scopes(first: 250, after: $after, archived: false) {
  pageInfo { endCursor hasNextPage } edges { node { asset_type asset_identifier eligible_for_bounty eligible_for_submission max_severity instruction created_at updated_at } } } } }"""

def _where():
    return {"_and": [{"_or": [{"submission_state": {"_eq": "open"}}, {"submission_state": {"_eq": "paused"}}]}, {"_not": {"external_program": {}}}]}

def _sev(s):
    return {"critical": ("Critical", 9.5), "high": ("High", 8.0), "medium": ("Medium", 5.5), "low": ("Low", 2.5), "none": ("None", 0.0)}.get((s or "").lower(), (None, None))

# ---- parsers (pure) -----------------------------------------------------------------------------
def _target(node, handle):
    lab, sc = _sev(node.get("max_severity"))
    return {"identifier": node["asset_identifier"], "hint_type": node.get("asset_type"), "display_name": node.get("asset_type"),
            "description": node.get("instruction") or "", "in_scope": bool(node.get("eligible_for_submission")),
            "eligible_for_bounty": bool(node.get("eligible_for_bounty")), "severity_label": lab, "severity_score": sc,
            "source_url": f"https://hackerone.com/{handle}/policy_scopes?type=team", "source_added_at": node.get("created_at"),
            "metadata": {"source_asset_type": node.get("asset_type"), "cvss_score": node.get("max_severity"),
                         "history_source": {"created_at": node.get("created_at"), "updated_at": node.get("updated_at"), "instruction": (node.get("instruction") or "")[:500]}}}

def parse_scope(payload, handle):
    """Targets from a Scopes query response (one page of structured_scopes)."""
    ss = ((payload.get("data") or {}).get("team") or {}).get("structured_scopes") or {}
    return [_target(e["node"], handle) for e in ss.get("edges") or []]

def _facts(t):
    facts = []
    if t.get("formatted_total_bounties_paid_amount") is not None and not t.get("hide_bounty_amounts"):
        facts.append({"metric": "payout_total", "amount": float(t["formatted_total_bounties_paid_amount"]), "currency": "USD", "unit": "currency", "source_label": "Total bounties paid", "source_field": "formatted_total_bounties_paid_amount", "period": "all_time", "basis": "reported"})
    if t.get("formatted_bounties_paid_last_90_days") is not None and not t.get("hide_bounty_amounts"):
        facts.append({"metric": "payout_native_90d", "amount": float(t["formatted_bounties_paid_last_90_days"]), "currency": "USD", "unit": "currency", "source_label": "Bounties paid in the last 90 days", "source_field": "formatted_bounties_paid_last_90_days", "period": "native_90d", "basis": "reported"})
    if t.get("reports_received_last_90_days") is not None:
        facts.append({"metric": "reports_received_native_90d", "amount": float(t["reports_received_last_90_days"]), "currency": None, "unit": "reports", "source_label": "Reports received in the last 90 days", "source_field": "reports_received_last_90_days", "period": "native_90d", "basis": "reported"})
    if t.get("resolved_report_count") is not None:
        facts.append({"metric": "reports_resolved_total", "amount": float(t["resolved_report_count"]), "currency": None, "unit": "reports", "source_label": "Reports resolved", "source_field": "resolved_report_count", "period": "all_time", "basis": "reported"})
    sla = (t.get("most_recent_sla_snapshot") or {}).get("average_time_to_first_program_response")
    if sla is not None:
        facts.append({"metric": "first_response_avg_native_90d", "amount": float(sla), "currency": None, "unit": "hours", "source_label": "Average time to first response", "source_field": "most_recent_sla_snapshot.average_time_to_first_program_response", "period": "native_90d", "basis": "reported"})
    if t.get("last_report_resolved_at"):
        d = config.parse_ts(t["last_report_resolved_at"])
        if d: facts.append({"metric": "last_report_resolved_at", "amount": d.timestamp(), "currency": None, "unit": "epoch_seconds", "source_label": "Last report resolved", "source_field": "last_report_resolved_at", "period": "current", "basis": "reported"})
    return facts

def parse_listing(payload, now=None):
    """Programs from one DirectoryQuery response page. Targets come from the embedded first scope page; when the team has more,
    the program carries `_scope_cursor` and collect() fetches the rest with parse_scope."""
    now = now or config.iso()
    out = []
    for e in ((payload.get("data") or {}).get("teams") or {}).get("edges") or []:
        t = e["node"]
        if t.get("external_program"):
            continue  # external (non-HackerOne-hosted) listings are not HackerOne programs
        handle = t["handle"]
        offers = bool(t.get("offers_bounties"))
        sub = t.get("submission_state")
        status = "paused" if sub == "paused" else "active"
        ss = t.get("structured_scopes") or {}
        pi = ss.get("pageInfo") or {}
        out.append({
            "id": program_id("HackerOne", handle), "platform": "HackerOne", "handle": handle, "name": t.get("name") or handle,
            "link": f"https://hackerone.com/{handle}", "profile_picture": t.get("profile_picture"),
            "program_kind": "bounty" if offers else "vdp", "program_status": status, "is_active": status == "active",
            "reward_status": "paid" if offers else "no_reward", "bounty_min": float(t["base_bounty"]) if offers and t.get("base_bounty") else None,
            "bounty_max": None, "reward_currency": (t.get("currency") or "usd").upper(), "launch_at": t.get("launched_at"), "launch_at_precision": "time",
            "ends_at": None, "report_count": t.get("resolved_report_count"), "report_count_source": "resolved_report_count", "report_count_label": "resolved reports total",
            "fee": None, "detail_marker": now[:13], "targets": [_target(x["node"], handle) for x in ss.get("edges") or []], "payout_facts": _facts(t),
            "policy_url": f"https://hackerone.com/{handle}?type=team", "safe_harbor": None,
            "raw": {"submission_state": sub, "state": t.get("state"), "triage_active": t.get("triage_active"), "website": t.get("website"), "response_efficiency_percentage": t.get("response_efficiency_percentage")},
            "_scope_cursor": pi.get("endCursor") if pi.get("hasNextPage") else None,
        })
    return out

# ---- fetchers ------------------------------------------------------------------------------------
def _page(after):
    q = {"operationName": "DirectoryQuery", "query": DIRECTORY, "variables": {"first": 50, "after": after, "where": _where(), "secureOrderBy": {"launched_at": {"_direction": "DESC"}}}}
    d = http.post_json(GQL, q, headers={"Accept": "application/json"})
    if d.get("errors"):
        raise RuntimeError("hackerone graphql: " + json.dumps(d["errors"])[:300])
    return d

def _more_scopes(handle, after, log):
    """Page the remaining structured scopes of a program with more than 250 assets."""
    out = []
    for _ in range(80):
        d = http.post_json(GQL, {"operationName": "Scopes", "query": SCOPES_PAGE, "variables": {"handle": handle, "after": after}}, headers={"Accept": "application/json"})
        out += parse_scope(d, handle)
        pi = (((d.get("data") or {}).get("team") or {}).get("structured_scopes") or {}).get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")
    log(f"hackerone {handle}: +{len(out)} scopes beyond the first page")
    return out

@register("HackerOne")
def collect(existing, log):
    programs = []; after = None; page = 0
    while True:
        page += 1
        d = _page(after)
        programs += parse_listing(d)
        log(f"hackerone directory page {page}: {len(programs)} teams")
        pi = d["data"]["teams"]["pageInfo"]
        if not pi.get("hasNextPage") or page > 200:
            break
        after = pi.get("endCursor")
    finish_scopes(programs, log)
    return {"platform": "HackerOne", "programs": programs, "complete": True}

def finish_scopes(programs, log, more=None):
    """Fetch the remaining scope pages of programs with more than 250 assets. A failed page sets targets=None
    so the stored scope is kept, instead of shrinking it to page one and reporting every later asset as removed."""
    more = more or _more_scopes
    for p in programs:
        cur = p.pop("_scope_cursor", None)
        if cur:
            try:
                p["targets"] = p["targets"] + more(p["handle"], cur, log)
            except Exception as ex:
                log(f"hackerone {p['handle']}: scope paging failed, keeping the stored scope: {ex}")
                p["targets"] = None
