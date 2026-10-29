"""HackenProof: Nuxt __NUXT_DATA__ payload (devalue encoding) on the listing and program pages."""
import re, json
from .. import http, config
from . import register, need_detail, program_id

LIST = "https://hackenproof.com/programs?page={page}"

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

def _detail(slug, log):
    try:
        root = nuxt_data(http.get_text(f"https://hackenproof.com/programs/{slug}"))
    except Exception as ex:
        log(f"hackenproof detail {slug}: {ex}"); return None
    if not root:
        return None
    d = (root.get("data") or {}).get(f"programs-{slug}") or {}
    if not d:
        for k, v in (root.get("data") or {}).items():
            if k.startswith("programs-") and isinstance(v, dict) and "title" in v:
                d = v; break
    return d

def _scopes(d, slug):
    out = []
    groups = []
    for key in ("scopes", "inScope", "in_scope", "scope", "targets"):
        if isinstance(d.get(key), list):
            groups.append((key, d[key], True))
    for key in ("outOfScope", "out_of_scope", "outScope"):
        if isinstance(d.get(key), list):
            groups.append((key, d[key], False))
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
                        "severity_label": s.get("criticality") or s.get("severity"), "severity_score": None, "source_url": f"https://hackenproof.com/programs/{slug}", "source_added_at": None,
                        "metadata": {"type_label": type_label, "criticality": s.get("criticality"), "scope_id": s.get("id"), "languages": s.get("languages"), "platform": s.get("platform")}})
    return out

@register("HackenProof")
def collect(existing, log):
    page = 1; items = []
    while page <= 60:
        root = nuxt_data(http.get_text(LIST.format(page=page)))
        d = (root or {}).get("data", {}).get("programs-api-bounty") or {}
        progs = d.get("programs") or []
        items += progs
        log(f"hackenproof page {page}: {len(items)}/{d.get('total_items')}")
        if not progs or not d.get("next_page"):
            break
        page = int(d["next_page"])
    programs = []
    for e in items:
        slug = e.get("slug")
        if not slug:
            continue
        pid = program_id("HackenProof", slug)
        marker = f"{e.get('lastUpdated')}|{e.get('reward')}|{e.get('status')}|{e.get('state')}"
        targets = None; det = None
        if need_detail(existing, pid, marker):
            det = _detail(slug, log)
            if det:
                targets = _scopes(det, slug)
        st = (e.get("activityStatus") or {}).get("name") or e.get("status")
        status = "active" if (e.get("state") == "published" and (st or "").lower() not in ("paused", "finished", "ended", "closed")) else "paused"
        fee = None
        if det is not None:   # the detail page omits submissionFee when the program is free to submit to
            fee = {"amount": float(det.get("submissionFee") or 0), "unit": "USD", "kind": "fee", "source": "hackenproof_catalog"}
        elif existing.get(pid) and existing[pid].get("fee") is not None:
            fee = existing[pid]["fee"]
        tags = e.get("tags") or {}
        rewards = (det or {}).get("rewards") or {}
        mn = min([v for k, v in rewards.items() if k.endswith("_min") and v], default=None)
        mx = _money(e.get("reward")) or rewards.get("critical_max")
        programs.append({
            "id": pid, "platform": "HackenProof", "handle": slug, "name": e.get("name") or slug, "link": f"https://hackenproof.com/programs/{slug}",
            "profile_picture": ("https://dashboard.hackenproof.com" + e["image"]) if e.get("image", "").startswith("/") else e.get("image"),
            "program_kind": "contest" if e.get("isAudit") else "bounty", "program_status": status, "is_active": status == "active",
            "reward_status": "paid" if mx else "unknown", "bounty_min": mn, "bounty_max": mx, "reward_currency": "USD",
            "launch_at": config.iso(config.parse_ts(e.get("startDate"))) if config.parse_ts(e.get("startDate")) else None, "launch_at_precision": "date",
            "ends_at": None if e.get("isUnending") else (config.iso(config.parse_ts(e.get("endDate"))) if config.parse_ts(e.get("endDate")) else None),
            "targets_updated_at": config.iso(config.parse_ts(e.get("lastUpdated"))) if config.parse_ts(e.get("lastUpdated")) else None,
            "report_count": e.get("submittedReports"), "report_count_source": "submitted_reports", "report_count_label": "submitted reports",
            "fee": fee, "detail_marker": marker, "targets": targets, "payout_facts": ([{"metric": "payout_total", "amount": _money(e["rewardPaid"]), "currency": "USD", "unit": "currency", "source_label": "Rewards paid", "source_field": "rewardPaid", "period": "all_time", "basis": "reported"}] if _money(e.get("rewardPaid")) else []),
            "policy_url": f"https://hackenproof.com/programs/{slug}", "safe_harbor": None,
            "raw": {"types": tags.get("types"), "languages": tags.get("languages"), "platforms": tags.get("platforms"), "project_types": tags.get("project_types"),
                    "triaged": e.get("triagedByHackenproof"), "status": st, "last_updated": e.get("lastUpdated"), "repo_url": e.get("repoUrl") or None,
                    "kyc_required": (det or {}).get("kycRequired"), "min_reputation": (det or {}).get("minReputation"), "poc_required": (det or {}).get("pocRequired")},
        })
    return {"platform": "HackenProof", "programs": programs, "complete": True}
