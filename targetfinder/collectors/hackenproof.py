"""HackenProof: Nuxt __NUXT_DATA__ payload (devalue encoding) on the listing and program pages."""
import re, json
from .. import http, config
from . import register, need_detail, program_id

LIST = "https://hackenproof.com/programs?page={page}"
DETAIL = "https://hackenproof.com/programs/{slug}"

def devalue(data):
    memo = {}
    def rec(i):
        if not isinstance(i, int) or i < 0 or i >= len(data):
            return i
        if i in memo:
            return memo[i]
        v = data[i]
        if isinstance(v, list) and v and isinstance(v[0], str) and v[0] in ("ShallowReactive", "Reactive", "Ref", "ShallowRef", "Set", "Map", "Date", "NaN", "BigInt", "undefined", "-0", "Infinity", "-Infinity"):
            if v[0] == "Date": r = v[1] if len(v) > 1 else None
            elif v[0] in ("Set", "Map"): r = [rec(x) for x in v[1:]]
            elif v[0] in ("NaN", "undefined", "-0", "Infinity", "-Infinity"): r = None
            else: r = rec(v[1]) if len(v) > 1 else None
            memo[i] = r; return r
        if isinstance(v, list):
            r = []; memo[i] = r
            for x in v: r.append(rec(x) if isinstance(x, int) else x)
            return r
        if isinstance(v, dict):
            r = {}; memo[i] = r
            for k, x in v.items(): r[k] = rec(x) if isinstance(x, int) else x
            return r
        return v
    return rec(0)

def nuxt_data(html):
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    return devalue(json.loads(m.group(1)))

def _money(s):
    if s is None: return None
    if isinstance(s, (int, float)): return float(s)
    m = re.search(r"[\d,.]+", str(s))
    return float(m.group(0).replace(",", "")) if m else None

# ---- parsers (pure) -----------------------------------------------------------------------------
def listing_page(html):
    """(programs_raw, next_page) from one listing page."""
    root = nuxt_data(html)
    d = (root or {}).get("data", {}).get("programs-api-bounty") or {}
    return d.get("programs") or [], d.get("next_page")

def parse_listing(html):
    """Programs from one listing page (targets=None, fee=None: both come from the detail page)."""
    out = []
    for e in listing_page(html)[0]:
        slug = e.get("slug")
        if not slug:
            continue
        st = (e.get("activityStatus") or {}).get("name") or e.get("status")
        status = "active" if (e.get("state") == "published" and (st or "").lower() not in ("paused", "finished", "ended", "closed")) else "paused"
        tags = e.get("tags") or {}
        mx = _money(e.get("reward"))
        out.append({
            "id": program_id("HackenProof", slug), "platform": "HackenProof", "handle": slug, "name": e.get("name") or slug, "link": DETAIL.format(slug=slug),
            "profile_picture": ("https://dashboard.hackenproof.com" + e["image"]) if (e.get("image") or "").startswith("/") else e.get("image"),
            "program_kind": "contest" if e.get("isAudit") else "bounty", "program_status": status, "is_active": status == "active",
            "reward_status": "paid" if mx else "unknown", "bounty_min": None, "bounty_max": mx, "reward_currency": "USD",
            "launch_at": config.iso(config.parse_ts(e.get("startDate"))) if config.parse_ts(e.get("startDate")) else None, "launch_at_precision": "date",
            "ends_at": None if e.get("isUnending") else (config.iso(config.parse_ts(e.get("endDate"))) if config.parse_ts(e.get("endDate")) else None),
            "targets_updated_at": config.iso(config.parse_ts(e.get("lastUpdated"))) if config.parse_ts(e.get("lastUpdated")) else None,
            "report_count": e.get("submittedReports"), "report_count_source": "submitted_reports", "report_count_label": "submitted reports",
            "fee": None, "detail_marker": f"{e.get('lastUpdated')}|{e.get('reward')}|{e.get('status')}|{e.get('state')}", "targets": None,
            "payout_facts": ([{"metric": "payout_total", "amount": _money(e["rewardPaid"]), "currency": "USD", "unit": "currency", "source_label": "Rewards paid", "source_field": "rewardPaid", "period": "all_time", "basis": "reported"}] if _money(e.get("rewardPaid")) else []),
            "policy_url": DETAIL.format(slug=slug), "safe_harbor": None,
            "raw": {"types": tags.get("types"), "languages": tags.get("languages"), "platforms": tags.get("platforms"), "project_types": tags.get("project_types"),
                    "triaged": e.get("triagedByHackenproof"), "status": st, "last_updated": e.get("lastUpdated"), "repo_url": e.get("repoUrl") or None},
        })
    return out

def _detail_record(html, slug):
    root = nuxt_data(html)
    if not root:
        return None
    d = (root.get("data") or {}).get(f"programs-{slug}") or {}
    if not d:
        for k, v in (root.get("data") or {}).items():
            if k.startswith("programs-") and isinstance(v, dict) and "title" in v:
                d = v; break
    return d or None

def parse_scope(html, slug):
    """Targets from a program page; `out_of_scope: true` entries are kept but marked out of scope."""
    d = _detail_record(html, slug) or {}
    out = []
    groups = [(k, d[k], True) for k in ("scopes", "inScope", "in_scope", "scope", "targets") if isinstance(d.get(k), list)]
    groups += [(k, d[k], False) for k in ("outOfScope", "out_of_scope", "outScope") if isinstance(d.get(k), list)]
    for key, lst, ins in groups:
        for s in lst:
            if not isinstance(s, dict):
                continue
            ident = s.get("target") or s.get("url") or s.get("name") or ""
            if not ident:
                continue
            in_scope = ins and not s.get("out_of_scope")
            type_label = s.get("title") or s.get("type") or s.get("targetType") or s.get("kind")
            out.append({"identifier": ident, "hint_type": type_label, "display_name": ident,
                        "description": (s.get("target_description") or s.get("description") or "")[:500], "in_scope": in_scope, "eligible_for_bounty": in_scope and (s.get("eligibleForBounty", True) is not False),
                        "severity_label": s.get("criticality") or s.get("severity"), "severity_score": None, "source_url": DETAIL.format(slug=slug), "source_added_at": None,
                        "metadata": {"type_label": type_label, "criticality": s.get("criticality"), "scope_id": s.get("id"), "languages": s.get("languages"), "platform": s.get("platform")}})
    return out

def parse_detail(html, slug):
    """Fee, reward table and extra flags from a program page (fee defaults to 0: the page omits submissionFee when free)."""
    d = _detail_record(html, slug)
    if d is None:
        return None
    rewards = d.get("rewards") or {}
    return {"fee": {"amount": float(d.get("submissionFee") or 0), "unit": "USD", "kind": "fee", "source": "hackenproof_catalog"}, "rewards": rewards,
            "bounty_min": min([v for k, v in rewards.items() if k.endswith("_min") and v], default=None), "bounty_max": rewards.get("critical_max"),
            "raw": {"kyc_required": d.get("kycRequired"), "min_reputation": d.get("minReputation"), "poc_required": d.get("pocRequired")}}

# ---- fetcher ----------------------------------------------------------------------------------------
@register("HackenProof")
def collect(existing, log):
    page = 1; programs = []
    while page <= 60:
        html = http.get_text(LIST.format(page=page))
        got = parse_listing(html); programs += got
        nxt = listing_page(html)[1]
        log(f"hackenproof page {page}: {len(programs)}")
        if not got or not nxt:
            break
        page = int(nxt)
    for p in programs:
        slug = p["handle"]
        if need_detail(existing, p["id"], p["detail_marker"]):
            try:
                html = http.get_text(DETAIL.format(slug=slug))
            except Exception as ex:
                log(f"hackenproof detail {slug}: {ex}"); continue
            det = parse_detail(html, slug)
            if det:
                p["targets"] = parse_scope(html, slug); p["fee"] = det["fee"]; p["raw"].update(det["raw"])
                if det["bounty_min"] is not None: p["bounty_min"] = det["bounty_min"]
                if not p["bounty_max"] and det["bounty_max"]: p["bounty_max"] = det["bounty_max"]
        elif existing.get(p["id"]) and existing[p["id"]].get("fee") is not None:
            p["fee"] = existing[p["id"]]["fee"]
    return {"platform": "HackenProof", "programs": programs, "complete": True}
