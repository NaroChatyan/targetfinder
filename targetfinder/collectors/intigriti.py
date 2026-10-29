"""Intigriti: public programs API for metadata; scopes from bounty-targets-data (the public detail API refuses non-browser clients)."""
import collections
from .. import http, config
from . import register, need_detail, btd, program_id

LIST = "https://app.intigriti.com/api/core/public/programs"
STATUS = {1: "draft", 2: "upcoming", 3: "active", 4: "paused", 5: "closed", 6: "archived"}
CONF = {1: "application", 2: "registered", 3: "public", 4: "public"}

# ---- parsers (pure) -----------------------------------------------------------------------------
def parse_listing(payload):
    """Programs from the public programs list (targets=None). Ambiguous handles become company/handle."""
    hcount = collections.Counter((e.get("handle") or "").lower() for e in payload)
    out = []
    for e in payload:
        company, handle = e.get("companyHandle"), e.get("handle")
        if not company or not handle:
            continue
        hid = handle if hcount[handle.lower()] == 1 else f"{company}/{handle}"
        st = STATUS.get(e.get("status"), "active")
        status = "active" if st in ("active", "upcoming") else "paused"
        mn = (e.get("minBounty") or {}).get("value"); mx = (e.get("maxBounty") or {}).get("value"); cur = (e.get("maxBounty") or {}).get("currency") or "EUR"
        is_vdp = not mx
        out.append({
            "id": program_id("Intigriti", hid), "platform": "Intigriti", "handle": hid, "name": e.get("name") or handle,
            "link": f"https://app.intigriti.com/programs/{company}/{handle}/detail",
            "profile_picture": f"https://app.intigriti.com/api/file/api/file/{e['logoId']}" if e.get("logoId") else None,
            "program_kind": "vdp" if is_vdp else "bounty", "program_status": status, "is_active": status == "active",
            "reward_status": "no_reward" if is_vdp else "paid", "bounty_min": mn, "bounty_max": mx, "reward_currency": cur,
            "launch_at": config.iso(config.parse_ts(e.get("createdAt"))) if e.get("createdAt") else None, "launch_at_precision": "time", "ends_at": None,
            "targets_updated_at": config.iso(config.parse_ts(e.get("lastUpdatedAt"))) if e.get("lastUpdatedAt") else None,
            "report_count": None, "report_count_source": None, "report_count_label": None, "fee": None, "detail_marker": f"{e.get('lastUpdatedAt')}|{mx}|{e.get('status')}", "targets": None,
            "payout_facts": ([{"metric": "last_submission_at", "amount": float(e["lastSubmissionAt"]), "currency": None, "unit": "epoch_seconds", "source_label": "Last submission", "source_field": "lastSubmissionAt", "period": "current", "basis": "reported"}] if e.get("lastSubmissionAt") else []),
            "policy_url": f"https://app.intigriti.com/programs/{company}/{handle}/detail", "safe_harbor": None,
            "raw": {"company": e.get("companyName"), "confidentiality": CONF.get(e.get("confidentialityLevel")), "industry": e.get("industry"), "status": st,
                    "last_updated_at": e.get("lastUpdatedAt"), "tac_required": e.get("tacRequired"), "two_factor_required": e.get("twoFactorRequired")},
            "_key": (company, handle),
        })
    return out

def parse_scope(data, key):
    """Targets for (company_handle, handle) from the bounty-targets-data mirror, or None when absent."""
    company, handle = key
    for p in data:
        if p.get("company_handle") == company and p.get("handle") == handle:
            out = []
            for k, ins in (("in_scope", True), ("out_of_scope", False)):
                for tg in (p.get("targets") or {}).get(k) or []:
                    ident = tg.get("endpoint") or ""
                    out.append({"identifier": ident, "hint_type": tg.get("type"), "display_name": ident, "description": (tg.get("description") or "")[:500],
                                "in_scope": ins, "eligible_for_bounty": ins and (tg.get("impact") not in ("none", "No bounty", "No Bounty", None) if "impact" in tg else True),
                                "severity_label": tg.get("impact"), "severity_score": None, "source_url": p.get("url"), "source_added_at": None,
                                "metadata": {"source": "bounty-targets-data", "type_label": tg.get("type"), "impact": tg.get("impact")}})
            return [o for o in out if o["identifier"]]
    return None

# ---- fetcher ----------------------------------------------------------------------------------------
@register("Intigriti")
def collect(existing, log):
    items = http.get_json(LIST, headers={"Accept": "application/json"})
    log(f"intigriti public programs: {len(items)}")
    programs = parse_listing(items)
    try:
        data = btd.load("intigriti")
    except Exception as ex:
        log(f"intigriti: bounty-targets-data unavailable ({ex})"); data = []
    for p in programs:
        key = p.pop("_key")
        if need_detail(existing, p["id"], p["detail_marker"]):
            p["targets"] = parse_scope(data, key)
    return {"platform": "Intigriti", "programs": programs, "complete": True}
