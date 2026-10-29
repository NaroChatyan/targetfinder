"""Sherlock bug bounties: RSC payload of audits.sherlock.xyz/bug-bounties (list + scopes on detail pages when available)."""
import re, json
from .. import http, config
from . import register, need_detail
from .rsc import blob, extract_array, extract_object

LIST = "https://audits.sherlock.xyz/bug-bounties"

def _find_bounties(b):
    # the page keeps the list in a react-query cache: look for the array whose items carry 'bug-bounties/' links or id+title+max reward
    for key in ("bounties", "items", "data", "bugBounties", "bug_bounties", "results"):
        arr = extract_array(b, key)
        if arr and isinstance(arr[0], dict) and any(k in arr[0] for k in ("protocol", "title", "name", "slug")):
            return arr
    # fallback: scan all top-level arrays of dicts with an 'id' and something reward-like
    best = []
    for m in re.finditer(r'"queries":\[', b):
        arr = extract_array(b[m.start():], "queries")
        for q in arr or []:
            data = ((q.get("state") or {}).get("data")) if isinstance(q, dict) else None
            if isinstance(data, list) and data and isinstance(data[0], dict) and len(data) > len(best):
                best = data
            elif isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict) and len(v) > len(best):
                        best = v
    return best

def _money(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    m = re.search(r"[\d,.]+", str(v)); return float(m.group(0).replace(",", "")) if m else None

def _detail(handle, log):
    """Bounty detail page: returns {"scope": [...]} with repo/contract identifiers found in the RSC payload."""
    try:
        b = blob(http.get_text(f"https://audits.sherlock.xyz/bug-bounties/{handle}"))
    except Exception as ex:
        log(f"sherlock detail {handle}: {ex}"); return None
    scope = []
    for key in ("scope", "assets", "repos", "repositories", "contracts", "inScope", "scopeItems"):
        arr = extract_array(b, key)
        if arr and isinstance(arr[0], (dict, str)):
            scope = arr; break
    if not scope:
        # fall back to any github/etherscan links in the payload
        seen = set()
        for m in re.finditer(r'https://(?:github\.com|etherscan\.io|arbiscan\.io|basescan\.org|polygonscan\.com|bscscan\.com)/[A-Za-z0-9_./#-]+', b):
            u = m.group(0).rstrip('.,')
            if u not in seen and "sherlock" not in u:
                seen.add(u); scope.append(u)
    return {"scope": scope}

@register("Sherlock")
def collect(existing, log):
    html = http.get_text(LIST)
    b = blob(html)
    items = _find_bounties(b)
    log(f"sherlock bounties: {len(items)}")
    if not items:
        raise RuntimeError("sherlock: bounty list not found in RSC payload")
    programs = []
    for e in items:
        bid = e.get("id") or e.get("slug")
        if bid is None:
            continue
        handle = f"bounty-{bid}" if str(bid).isdigit() else str(bid)
        pid = f"Sherlock:{handle}"
        name = e.get("title") or e.get("protocol_name") or e.get("protocol") or e.get("name") or handle
        if isinstance(name, dict): name = name.get("name") or handle
        mx = _money(e.get("payout") or e.get("max_reward") or e.get("maxReward") or e.get("max_payout") or e.get("reward"))
        deps = [s.get("depositAmount") for s in (e.get("severities") or []) if isinstance(s, dict) and s.get("depositAmount") is not None]
        dep = max(deps) if deps else (e.get("deposit") or e.get("deposit_amount"))
        status = "active" if e.get("isReady", True) and (e.get("status") or "active") not in ("paused", "ended", "finished", "closed") else "paused"
        marker = f"{e.get('lastUpdatedTimestamp') or e.get('updated_at')}|{mx}|{status}"
        targets = []
        det = _detail(str(bid), log) if need_detail(existing, pid, marker) else None
        for s in ((det or {}).get("scope") or (det or {}).get("assets") or (det or {}).get("repos") or e.get("scope") or []):
            ident = s if isinstance(s, str) else (s.get("url") or s.get("repo") or s.get("address") or s.get("name"))
            if ident:
                targets.append({"identifier": ident, "hint_type": (s.get("type") if isinstance(s, dict) else "smart contract"), "display_name": ident, "description": "",
                                "in_scope": True, "eligible_for_bounty": True, "severity_label": None, "severity_score": None,
                                "source_url": f"https://audits.sherlock.xyz/bug-bounties/{bid}", "source_added_at": None, "metadata": {}})
        programs.append({
            "id": pid, "platform": "Sherlock", "handle": handle, "name": name, "link": f"https://audits.sherlock.xyz/bug-bounties/{bid}",
            "profile_picture": e.get("logo") or e.get("logo_url") or e.get("image"), "program_kind": "bounty", "program_status": status, "is_active": status == "active",
            "reward_status": "paid" if mx else "unknown", "bounty_min": None, "bounty_max": mx, "reward_currency": "USDC",
            "launch_at": None, "launch_at_precision": None, "ends_at": None,
            "targets_updated_at": config.iso(config.parse_ts(e.get("lastUpdatedTimestamp"))) if e.get("lastUpdatedTimestamp") else None,
            "report_count": None, "report_count_source": None, "report_count_label": None,
            "fee": {"amount": _money(dep) if dep else 250, "unit": "USDC", "kind": "deposit", "source": "sherlock_bug_bounties_api"},
            "detail_marker": marker, "targets": targets or None, "payout_facts": [], "policy_url": f"https://audits.sherlock.xyz/bug-bounties/{bid}", "safe_harbor": None,
            "raw": {"last_updated_ts": e.get("lastUpdatedTimestamp"), "categories": [c.get("name") for c in (e.get("categories") or []) if isinstance(c, dict)], "currency": e.get("displayCurrency")},
        })
    return {"platform": "Sherlock", "programs": programs, "complete": True}
