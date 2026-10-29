"""HackerOne: unauthenticated GraphQL directory + structured scopes + public stats."""
import json
from .. import http, config
from . import register

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

def _where():
    return {"_and": [{"_or": [{"submission_state": {"_eq": "open"}}, {"submission_state": {"_eq": "paused"}}]}, {"_not": {"external_program": {}}}]}

def _teams(log):
    after = None; out = []; page = 0
    while True:
        page += 1
        q = {"operationName": "DirectoryQuery", "query": DIRECTORY,
             "variables": {"first": 50, "after": after, "where": _where(), "secureOrderBy": {"launched_at": {"_direction": "DESC"}}}}
        d = http.post_json(GQL, q, headers={"Accept": "application/json"})
        if d.get("errors"):
            raise RuntimeError("hackerone graphql: " + json.dumps(d["errors"])[:300])
        teams = d["data"]["teams"]
        out += [e["node"] for e in teams["edges"]]
        log(f"hackerone directory page {page}: {len(out)} teams")
        if not teams["pageInfo"]["hasNextPage"]:
            break
        after = teams["pageInfo"]["endCursor"]
        if page > 200:
            break
    return out

SCOPES_PAGE = """query Scopes($handle: String!, $after: String) { team(handle: $handle) { structured_scopes(first: 250, after: $after, archived: false) {
  pageInfo { endCursor hasNextPage } edges { node { asset_type asset_identifier eligible_for_bounty eligible_for_submission max_severity instruction created_at updated_at } } } } }"""

def _more_scopes(handle, after, log):
    """Page the remaining structured scopes of a program with more than 250 assets."""
    out = []
    for _ in range(80):
        d = http.post_json(GQL, {"operationName": "Scopes", "query": SCOPES_PAGE, "variables": {"handle": handle, "after": after}}, headers={"Accept": "application/json"})
        ss = ((d.get("data") or {}).get("team") or {}).get("structured_scopes") or {}
        out += ss.get("edges") or []
        if not ss.get("pageInfo", {}).get("hasNextPage"):
            break
        after = ss["pageInfo"]["endCursor"]
    log(f"hackerone {handle}: +{len(out)} scopes beyond the first page")
    return out

def _sev(s):
    return {"critical": ("Critical", 9.5), "high": ("High", 8.0), "medium": ("Medium", 5.5), "low": ("Low", 2.5), "none": ("None", 0.0)}.get((s or "").lower(), (None, None))

@register("HackerOne")
def collect(existing, log):
    teams = _teams(log)
    programs = []
    now = config.iso()
    for t in teams:
        handle = t["handle"]
        ext = t.get("external_program")
        if ext:
            continue  # external (non-HackerOne-hosted) listings are not HackerOne programs
        offers = bool(t.get("offers_bounties")) or bool(ext and ext.get("offers_rewards"))
        sub = t.get("submission_state")
        status = "paused" if sub == "paused" else "active"
        targets = []
        edges = (t.get("structured_scopes") or {}).get("edges") or []
        pi = (t.get("structured_scopes") or {}).get("pageInfo") or {}
        if pi.get("hasNextPage"):
            try:
                edges = edges + _more_scopes(handle, pi.get("endCursor"), log)
            except Exception as ex:
                log(f"hackerone {handle}: scope paging failed: {ex}")
        for e in edges:
            n = e["node"]
            lab, sc = _sev(n.get("max_severity"))
            targets.append({"identifier": n["asset_identifier"], "hint_type": n.get("asset_type"), "display_name": n.get("asset_type"),
                            "description": n.get("instruction") or "", "in_scope": bool(n.get("eligible_for_submission")),
                            "eligible_for_bounty": bool(n.get("eligible_for_bounty")), "severity_label": lab, "severity_score": sc,
                            "source_url": f"https://hackerone.com/{handle}/policy_scopes?type=team", "source_added_at": n.get("created_at"),
                            "metadata": {"source_asset_type": n.get("asset_type"), "cvss_score": n.get("max_severity"), "history_source": {"created_at": n.get("created_at"), "updated_at": n.get("updated_at"), "instruction": (n.get("instruction") or "")[:500]}}})
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
        programs.append({
            "id": f"HackerOne:{handle}", "platform": "HackerOne", "handle": handle, "name": t.get("name") or handle,
            "link": f"https://hackerone.com/{handle}", "profile_picture": t.get("profile_picture"),
            "program_kind": "bounty" if offers else "vdp", "program_status": status, "is_active": status == "active",
            "reward_status": "paid" if offers else "no_reward", "bounty_min": float(t["base_bounty"]) if offers and t.get("base_bounty") else None,
            "bounty_max": None, "reward_currency": (t.get("currency") or "usd").upper(), "launch_at": t.get("launched_at"), "launch_at_precision": "time",
            "ends_at": None, "report_count": t.get("resolved_report_count"), "report_count_source": "resolved_report_count", "report_count_label": "resolved reports total",
            "fee": None, "detail_marker": now[:13], "targets": targets, "payout_facts": facts, "policy_url": f"https://hackerone.com/{handle}?type=team",
            "safe_harbor": None, "raw": {"submission_state": sub, "state": t.get("state"), "triage_active": t.get("triage_active"), "website": t.get("website"), "external_program": bool(ext), "response_efficiency_percentage": t.get("response_efficiency_percentage")},
        })
    return {"platform": "HackerOne", "programs": programs, "complete": True}
