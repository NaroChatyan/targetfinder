"""YesWeHack public API: programs list + per-program detail (scopes, submission credits, reports_count)."""
from .. import http, config
from . import register, need_detail

LIST = "https://api.yeswehack.com/programs?page={page}"

@register("YesWeHack")
def collect(existing, log):
    page = 1; items = []
    while page <= 20:
        d = http.get_json(LIST.format(page=page))
        items += d.get("items") or []
        pg = d.get("pagination") or {}
        log(f"yeswehack programs page {page}/{pg.get('nb_pages')}: {len(items)}")
        if page >= int(pg.get("nb_pages") or 1):
            break
        page += 1
    programs = []
    for e in items:
        slug = e.get("slug")
        if not slug or e.get("demo"):
            continue
        pid = f"YesWeHack:{slug}"
        status = "paused" if e.get("disabled") or e.get("archived") else "active"
        is_vdp = bool(e.get("vdp")) or not e.get("bounty")
        marker = f"{e.get('last_update_at')}|{e.get('scopes_count')}|{e.get('report_submission_cost')}"
        targets = None; raw_detail = {}
        if need_detail(existing, pid, marker):
            try:
                d = http.get_json(f"https://api.yeswehack.com/programs/{slug}")
                targets = []
                for s in d.get("scopes") or []:
                    targets.append({"identifier": s.get("scope") or "", "hint_type": s.get("scope_type"), "display_name": s.get("scope_type_name"),
                                    "description": "", "in_scope": True, "eligible_for_bounty": not is_vdp, "severity_label": s.get("asset_value"), "severity_score": None,
                                    "source_url": f"https://yeswehack.com/programs/{slug}", "source_added_at": None,
                                    "metadata": {"scope_type": s.get("scope_type"), "asset_value": s.get("asset_value"), "report_count": s.get("report_count")}})
                for s in d.get("out_of_scope") or []:
                    ident = s.get("scope") if isinstance(s, dict) else str(s)
                    if ident:
                        targets.append({"identifier": ident, "hint_type": (s.get("scope_type") if isinstance(s, dict) else None), "display_name": ident, "description": "",
                                        "in_scope": False, "eligible_for_bounty": False, "severity_label": None, "severity_score": None,
                                        "source_url": f"https://yeswehack.com/programs/{slug}", "source_added_at": None, "metadata": {}})
                targets = [t for t in targets if t["identifier"]]
                raw_detail = {"qualifying": (d.get("qualifying_vulnerability") or "")[:300], "sla_enabled": d.get("sla_enabled"), "vpn_active": d.get("vpn_active")}
            except Exception as ex:
                log(f"yeswehack detail {slug}: {ex}")
        programs.append({
            "id": pid, "platform": "YesWeHack", "handle": slug, "name": e.get("title") or slug, "link": f"https://yeswehack.com/programs/{slug}",
            "profile_picture": (e.get("thumbnail") or {}).get("url"), "program_kind": "vdp" if is_vdp else "bounty", "program_status": status, "is_active": status == "active",
            "reward_status": "no_reward" if is_vdp else "paid", "bounty_min": e.get("bounty_reward_min"), "bounty_max": e.get("bounty_reward_max"),
            "reward_currency": ((e.get("business_unit") or {}).get("currency") or "EUR"), "launch_at": None, "launch_at_precision": None, "ends_at": None,
            "targets_updated_at": config.iso(config.parse_ts(e.get("last_update_at"))) if config.parse_ts(e.get("last_update_at")) else None,
            "report_count": e.get("reports_count"), "report_count_source": "reports_count", "report_count_label": "reports",
            "fee": {"amount": e.get("report_submission_cost") or 0, "unit": "credits", "kind": "fee", "source": "yeswehack_programs_api"},
            "detail_marker": marker, "targets": targets,
            "payout_facts": ([{"metric": "first_response_avg_native_90d", "amount": float(e["average_first_response_time"]) * 24.0, "currency": None, "unit": "hours", "source_label": "Average first response time", "source_field": "average_first_response_time", "period": "native", "basis": "reported"}] if e.get("average_first_response_time") is not None else []),
            "policy_url": f"https://yeswehack.com/programs/{slug}", "safe_harbor": None,
            "raw": {"country": e.get("country"), "activity_area": e.get("activity_area"), "hall_of_fame": e.get("hall_of_fame"), "last_update_at": e.get("last_update_at"), "scopes_count": e.get("scopes_count"), **raw_detail},
        })
    return {"platform": "YesWeHack", "programs": programs, "complete": True}
