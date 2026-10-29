"""Bugcrowd: public engagements.json listing; scopes from bounty-targets-data (hourly) with a live brief/changelog fallback."""
import re, html as htmlmod
from .. import http
from . import register, need_detail, btd, program_id

LIST = "https://bugcrowd.com/engagements.json?page={page}"

def _money(s):
    if not s: return None
    m = re.search(r"[\d,.]+", s.replace("$", ""))
    return float(m.group(0).replace(",", "")) if m else None

# ---- parsers (pure) -----------------------------------------------------------------------------
def parse_listing(payload):
    """Programs from one engagements.json page (targets=None: scope needs a second fetch)."""
    out = []
    for e in payload.get("engagements") or []:
        if e.get("isDemo"):
            continue
        slug = (e.get("briefUrl") or "").strip("/").split("/")[-1]
        if not slug:
            continue
        handle = f"engagements/{slug}"
        rs = e.get("rewardSummary") or {}
        mn, mx = _money(rs.get("minReward")), _money(rs.get("maxReward"))
        kind_label = ((e.get("productEngagementType") or {}).get("label") or "").lower()
        # Bugcrowd labels every public engagement "Bug Bounty"; VDPs show up as no reward summary, points-only rewards, or a VDP name
        summary = (rs.get("summary") or "").lower(); name_l = (e.get("name") or "").lower()
        points_only = "points" in summary and not mx
        is_vdp = ("vulnerability disclosure" in kind_label or "vulnerability disclosure" in name_l or "vdp" in name_l.split() or not summary or points_only)
        status = "active" if e.get("accessStatus") in ("open", None) else "paused"
        out.append({
            "id": program_id("Bugcrowd", handle), "platform": "Bugcrowd", "handle": handle, "name": (e.get("name") or slug).strip(), "link": f"https://bugcrowd.com/engagements/{slug}",
            "profile_picture": e.get("logoUrl"), "program_kind": "vdp" if is_vdp else "bounty", "program_status": status, "is_active": status == "active",
            "reward_status": "no_reward" if is_vdp else ("paid" if (mx or mn) else "unknown"), "bounty_min": mn, "bounty_max": mx, "reward_currency": "USD",
            "launch_at": None, "launch_at_precision": None, "ends_at": e.get("endsAt"), "report_count": None, "report_count_source": None, "report_count_label": None,
            "fee": None, "detail_marker": f"{rs.get('summary')}|{e.get('accessStatus')}|{e.get('endsAt')}", "targets": None, "payout_facts": [],
            "policy_url": f"https://bugcrowd.com/engagements/{slug}", "safe_harbor": None,
            "raw": {"industry": e.get("industryName"), "engagement_type": kind_label, "managed": e.get("serviceLevel"), "tagline": (e.get("tagline") or "")[:200], "points_rewards": "points" in summary},
            "_slug": slug,
        })
    return out

def changelog_id(brief_html):
    m = re.search(r"/engagements/[^\"&]+/changelog/([0-9a-f-]{36})", htmlmod.unescape(brief_html))
    return m.group(1) if m else None

def parse_scope(payload, slug):
    """Targets from an engagement changelog JSON."""
    out = []
    for grp in (payload.get("data") or {}).get("scope") or []:
        in_scope = bool(grp.get("inScope", True))
        for tg in grp.get("targets") or []:
            out.append({"identifier": (tg.get("uri") or tg.get("name") or tg.get("ipAddress") or "").strip(), "hint_type": tg.get("category") or tg.get("type"),
                        "display_name": tg.get("name"), "description": (tg.get("description") or "")[:500], "in_scope": in_scope, "eligible_for_bounty": in_scope,
                        "severity_label": None, "severity_score": None, "source_url": f"https://bugcrowd.com/engagements/{slug}",
                        "source_added_at": None, "metadata": {"scope_group": grp.get("name"), "category": tg.get("category"), "reward_range_data": grp.get("rewardRangeData")}})
    return [o for o in out if o["identifier"]]

def parse_btd(data, slug):
    """Targets from the bounty-targets-data mirror, or None when the program is not in it."""
    for p in data:
        if p.get("url", "").rstrip("/").endswith("/engagements/" + slug):
            out = []
            for key, ins in (("in_scope", True), ("out_of_scope", False)):
                for tg in (p.get("targets") or {}).get(key) or []:
                    out.append({"identifier": (tg.get("uri") or tg.get("target") or tg.get("ipAddress") or tg.get("name") or "").strip(), "hint_type": tg.get("type"),
                                "display_name": tg.get("name"), "description": "", "in_scope": ins, "eligible_for_bounty": ins,
                                "severity_label": None, "severity_score": None, "source_url": p.get("url"), "source_added_at": None,
                                "metadata": {"source": "bounty-targets-data", "type_label": tg.get("type")}})
            return [o for o in out if o["identifier"]]
    return None

# ---- fetchers ------------------------------------------------------------------------------------
def _brief_scopes(slug, log):
    try:
        t = http.get_text(f"https://bugcrowd.com/engagements/{slug}")
    except http.HttpError as e:
        log(f"bugcrowd brief {slug}: HTTP {e.status}"); return None
    cid = changelog_id(t)
    if not cid:
        return None
    try:
        return parse_scope(http.get_json(f"https://bugcrowd.com/engagements/{slug}/changelog/{cid}.json", headers={"Accept": "application/json"}), slug)
    except Exception as e:
        log(f"bugcrowd changelog {slug}: {e}"); return None

@register("Bugcrowd")
def collect(existing, log):
    page = 1; programs = []
    while page <= 40:
        d = http.get_json(LIST.format(page=page), headers={"Accept": "application/json"})
        got = parse_listing(d)
        programs += got
        meta = d.get("paginationMeta") or {}
        log(f"bugcrowd engagements page {page}: {len(programs)}/{meta.get('totalCount')}")
        if not d.get("engagements") or len(programs) >= int(meta.get("totalCount") or 0):
            break
        page += 1
    try:
        data = btd.load("bugcrowd")
    except Exception as ex:
        log(f"bugcrowd: bounty-targets-data unavailable ({ex}); using brief pages"); data = []
    for p in programs:
        slug = p.pop("_slug")
        if need_detail(existing, p["id"], p["detail_marker"]):
            targets = parse_btd(data, slug)
            p["targets"] = targets if targets is not None else _brief_scopes(slug, log)
    return {"platform": "Bugcrowd", "programs": programs, "complete": True}
