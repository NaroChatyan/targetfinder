"""Immunefi: bounty list and per-program scope assets from the Next.js RSC payloads.
Disabled by default: Immunefi's terms prohibit automated access (docs/PLATFORMS.md). Enable with TARGETFINDER_ENABLE_IMMUNEFI=1."""
from .. import http
from . import register, need_detail, program_id
from .rsc import blob, extract_array

LIST = "https://immunefi.com/bug-bounty/"
INFO = "https://immunefi.com/bug-bounty/{slug}/information/"
SCOPE = "https://immunefi.com/bug-bounty/{slug}/scope/"

# ---- parsers (pure) -----------------------------------------------------------------------------
def parse_scope(html, slug):
    assets = extract_array(blob(html), "assets")
    if assets is None:
        return None
    out = []
    for a in assets:
        if not isinstance(a, dict) or not a.get("url"):
            continue
        out.append({"identifier": a["url"], "hint_type": a.get("type"), "display_name": (a.get("description") or "")[:120] or a["url"], "description": (a.get("description") or "")[:500],
                    "in_scope": True, "eligible_for_bounty": True, "severity_label": None, "severity_score": None,
                    "source_url": INFO.format(slug=slug), "source_added_at": a.get("addedAt"),
                    "metadata": {"asset_id": a.get("id"), "asset_type": a.get("type"), "added_at": a.get("addedAt"), "is_safe_harbor": a.get("isSafeHarbor"),
                                 "is_primacy_of_impact": a.get("isPrimacyOfImpact"), "history_source": {"description": a.get("description")}}})
    return out

def parse_listing(html):
    items = extract_array(blob(html), "bounties")
    if not items:
        raise RuntimeError("immunefi: no bounties array in RSC payload")
    out = []
    for e in items:
        slug = e.get("slug")
        if not slug:
            continue
        status = "paused" if e.get("pausedAt") else "active"
        tags = e.get("tags") or {}
        pm = e.get("performanceMetrics") or {}
        facts = []
        if pm.get("totalPaidMetricEnabled") and pm.get("totalPaidAmount") is not None:
            facts.append({"metric": "payout_total", "amount": float(pm["totalPaidAmount"]), "currency": "USD", "unit": "currency", "source_label": "Total paid", "source_field": "performanceMetrics.totalPaidAmount", "period": "all_time", "basis": "reported"})
        if pm.get("responseTimeMetricEnabled") and pm.get("averageResponseTime") is not None:
            facts.append({"metric": "first_response_avg_native_90d", "amount": float(pm["averageResponseTime"]), "currency": None, "unit": "hours", "source_label": "Average response time", "source_field": "performanceMetrics.averageResponseTime", "period": "native", "basis": "reported"})
        fee_amt = float(e.get("payToSubmitFeeUsdc") or 0) if e.get("isPayToSubmit") else 0.0
        out.append({
            "id": program_id("Immunefi", slug), "platform": "Immunefi", "handle": slug, "name": e.get("project") or slug, "link": INFO.format(slug=slug),
            "profile_picture": e.get("logo"), "program_kind": "bounty", "program_status": status, "is_active": status == "active",
            "reward_status": "paid" if e.get("maxBounty") else "unknown", "bounty_min": None, "bounty_max": float(e["maxBounty"]) if e.get("maxBounty") else None, "reward_currency": "USD",
            "launch_at": e.get("launchDate"), "launch_at_precision": "time", "ends_at": None, "report_count": None, "report_count_source": None, "report_count_label": None,
            "targets_updated_at": e.get("updatedDate"),
            "fee": {"amount": fee_amt, "unit": "USDC", "kind": "fee", "source": "immunefi_catalog"}, "detail_marker": f"{e.get('updatedDate')}|{e.get('maxBounty')}|{e.get('payToSubmitFeeUsdc')}|{e.get('pausedAt')}",
            "targets": None, "payout_facts": facts, "policy_url": INFO.format(slug=slug), "safe_harbor": "partial" if e.get("isSafeHarborActive") else "not_stated",
            "raw": {"languages": tags.get("language"), "ecosystem": tags.get("ecosystem"), "program_type": tags.get("programType"), "project_type": tags.get("projectType"),
                    "kyc": e.get("kyc"), "poc": e.get("proofOfConceptType"), "invite_only": e.get("inviteOnly"), "updated_date": e.get("updatedDate"), "vault_balance": e.get("vaultBalance")},
        })
    return out

# ---- fetcher ----------------------------------------------------------------------------------------
@register("Immunefi")
def collect(existing, log):
    programs = parse_listing(http.get_text(LIST))
    log(f"immunefi bounties: {len(programs)}")
    for p in programs:
        if need_detail(existing, p["id"], p["detail_marker"]):
            try:
                p["targets"] = parse_scope(http.get_text(SCOPE.format(slug=p["handle"])), p["handle"])
            except Exception as ex:
                log(f"immunefi scope {p['handle']}: {ex}")
    return {"platform": "Immunefi", "programs": programs, "complete": True}
