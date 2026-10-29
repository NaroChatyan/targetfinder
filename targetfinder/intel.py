"""Target Intelligence, competition risk, opportunity tiers and submission-cost policy.
Heuristic point models; see docs/SCORING.md."""
import datetime as dt
from . import config

def _days_between(a, b):
    return max(0.0, (b - a).total_seconds() / 86400.0)

# ---- target opportunity score ------------------------------------------------------------
def freshness_factor(changed_at, now=None):
    now = now or config.utcnow()
    d = config.parse_ts(changed_at)
    if d is None:
        return {"score": 0, "label": "Older than 30 days", "days": None}
    days = round(_days_between(d, now), 2)
    if days < 1: return {"score": 35, "label": "Last 24 hours", "days": days}
    if days < 7: return {"score": 25, "label": "Last 7 days", "days": days}
    if days < 30: return {"score": 12, "label": "Last 30 days", "days": days}
    return {"score": 0, "label": "Older than 30 days", "days": days}

def target_type_factor(ttype, wildcard, scope_tags):
    tags = set(scope_tags or [])
    if "api" in tags or ttype == "api": return {"score": 25, "label": "API", "key": "api"}
    if "graphql" in tags: return {"score": 25, "label": "GraphQL", "key": "graphql"}
    if wildcard: return {"score": 22, "label": "Wildcard", "key": "wildcard"}
    if "staging" in tags: return {"score": 22, "label": "Staging/dev-style asset", "key": "staging"}
    if ttype == "smart contract": return {"score": 20, "label": "Smart contract", "key": "smart_contract"}
    if ttype == "source code": return {"score": 20, "label": "Source code", "key": "source_code"}
    if ttype == "mobile": return {"score": 18, "label": "Mobile app", "key": "mobile"}
    if ttype == "domain": return {"score": 14, "label": "Web/domain", "key": "domain"}
    return {"score": 5, "label": "Generic/unclear", "key": "generic"}

def reward_factor(bounty_max, reward_status):
    mb = float(bounty_max or 0)
    no = (reward_status == "no_reward") or mb <= 0
    if no: return {"score": 0, "label": "Reward amount undisclosed" if reward_status != "no_reward" else "No bounty offered", "maxBounty": mb or 0, "isNoReward": reward_status == "no_reward"}
    if mb >= 20000: return {"score": 25, "label": "Very high bounty", "maxBounty": mb, "isNoReward": False}
    if mb >= 10000: return {"score": 22, "label": "High bounty", "maxBounty": mb, "isNoReward": False}
    if mb >= 5000: return {"score": 18, "label": "Strong bounty", "maxBounty": mb, "isNoReward": False}
    if mb >= 2000: return {"score": 14, "label": "Good bounty", "maxBounty": mb, "isNoReward": False}
    if mb >= 1000: return {"score": 10, "label": "Moderate bounty", "maxBounty": mb, "isNoReward": False}
    return {"score": 6, "label": "Low bounty", "maxBounty": mb, "isNoReward": False}

def report_bucket(count):
    if count is None: return None
    if count <= 2: return "very low"
    if count <= 10: return "low"
    if count <= 50: return "moderate"
    if count <= 100: return "elevated"
    return "high"

def report_factor(report_count):
    if report_count is None:
        return {"score": 7, "label": "Report count unavailable", "reportCount": None, "fallback": "neutral"}
    b = report_bucket(report_count)
    score = {"very low": 15, "low": 12, "moderate": 7, "elevated": 3, "high": 0}[b]
    return {"score": score, "label": f"{b.capitalize()} public report count", "reportCount": report_count, "fallback": None}

def score_label(score):
    if score >= 85: return "Elite Target"
    if score >= 70: return "Strong Target"
    if score >= 50: return "Potential Target"
    return "Normal"

def age_phrase(launch_at, now=None):
    now = now or config.utcnow()
    d = config.parse_ts(launch_at)
    if d is None: return None
    days = _days_between(d, now)
    if days > 1095: return "older program"
    if days > 364: return "mature program"
    return None

def asset_phrase(ttype, wildcard, scope_tags):
    tags = set(scope_tags or [])
    if ttype in ("source code", "other", None):
        return None, 0
    if ttype == "domain" and not wildcard:
        if "api" in tags or "graphql" in tags or "staging" in tags:
            return "web target", 1
        return "generic web scope", 2
    return "less obvious asset type", -1

def competition_risk(report_count, launch_at, ttype, wildcard, scope_tags, recent_change_days=None, platform=None, has_data=True, asset=None):
    """Returns (risk, reason). Heuristic point model (docs/SCORING.md)."""
    if platform == "Self-Hosted":
        return "Unknown", "Self-Hosted programs do not have platform report-count data."
    parts = []; pts = 0
    b = report_bucket(report_count)
    if b:
        parts.append(f"{b} public report count")
        pts += {"very low": -2, "low": -1, "moderate": 1, "elevated": 2, "high": 3}[b]
    ap = age_phrase(launch_at)
    if ap:
        parts.append(ap); pts += 1 if ap == "mature program" else 2
    if recent_change_days is not None and recent_change_days < 7:
        parts.append("recent target change"); pts -= 3
    phrase, v = asset if asset is not None else asset_phrase(ttype, wildcard, scope_tags)
    if phrase:
        parts.append(phrase); pts += v
    if not parts:
        return "Unknown", "Not enough public report or target data to estimate competition risk."
    risk = "Low" if pts <= 0 else ("Medium" if pts <= 2 else "High")
    return risk, "Estimated from " + ", ".join(parts) + "."

def program_asset_phrase(scope_tags):
    tags = set(scope_tags or [])
    if not tags:
        return None, 0
    generic = {"domain", "web"} & tags and not ({"wildcard", "api", "graphql"} & tags)
    non_generic = tags - {"domain", "web", "other"}
    if generic and not non_generic:
        return "generic web scope", 2
    if non_generic and not ({"domain", "web"} & tags) or (tags & {"wildcard"} and not (tags - {"domain", "web", "wildcard", "other"})):
        return "less obvious asset type", -1
    if {"api", "graphql"} & tags and not (tags - {"domain", "web", "api", "graphql", "other"}):
        return "web target", 1
    return None, 0

def program_competition_risk(p, scope_tags):
    return competition_risk(p.get("report_count"), p.get("launch_at"), None, False, scope_tags, None, p.get("platform"), True,
                            asset=program_asset_phrase(scope_tags))

def why_this_target(fresh, tf, rf, rp, risk, in_scope, eligible):
    adj = {"Last 24 hours": "Fresh", "Last 7 days": "Fresh", "Last 30 days": "Recent", "Older than 30 days": "Older"}[fresh["label"]]
    noun = {"api": "API target", "graphql": "GraphQL target", "wildcard": "wildcard scope expansion", "staging": "staging/dev asset", "smart_contract": "smart contract target",
            "source_code": "source code target", "mobile": "mobile app target", "domain": "web/domain target", "generic": "generic/unclear target"}[tf["key"]]
    when = {"Last 24 hours": " added today", "Last 7 days": " added this week", "Last 30 days": " added recently", "Older than 30 days": ""}[fresh["label"]]
    if tf["key"] == "wildcard": when = ""
    elig = "eligible and in scope" if (in_scope and eligible) else ("in scope but not bounty-eligible" if in_scope else "out of scope")
    rew = {25: "high bounty potential", 22: "high bounty potential", 18: "strong reward potential", 14: "good reward potential", 10: "moderate reward potential", 6: "low reward potential", 0: "no disclosed bounty"}[rf["score"]]
    return f"{adj} {noun}{when}, with {elig}, {rew}, {risk.lower()} competition risk."

def score_target(t, program, now=None):
    """t: target row dict; program: program dict. Returns the score block."""
    now = now or config.utcnow()
    changed = t.get("last_changed_at") or t.get("added_at")
    fresh = freshness_factor(changed, now)
    tf = target_type_factor(t.get("target_type"), bool(t.get("wildcard")), t.get("scope_tags") or [])
    rf = reward_factor(program.get("bounty_max"), program.get("reward_status"))
    rp = report_factor(program.get("report_count"))
    score = fresh["score"] + tf["score"] + rf["score"] + rp["score"]
    avail = 3 + (0 if rp["fallback"] else 1)
    risk, reason = competition_risk(program.get("report_count"), program.get("launch_at"), t.get("target_type"), bool(t.get("wildcard")), t.get("scope_tags") or [],
                                    fresh.get("days"), program.get("platform"), True)
    return {
        "scoreVersion": config.SCORING_VERSION,
        "evidenceCoverage": {"available": avail, "total": 4},
        "targetOpportunityScore": score,
        "targetOpportunityLabel": score_label(score),
        "targetScoreBreakdown": {"scoreVersion": config.SCORING_VERSION, "evidenceCoverage": {"available": avail, "total": 4},
                                 "freshness": fresh, "targetType": tf, "reward": rf, "reportActivity": rp},
        "competitionRisk": risk, "competitionRiskReason": reason,
        "whyThisTarget": why_this_target(fresh, tf, rf, rp, risk, bool(t.get("in_scope")), bool(t.get("eligible_for_bounty"))),
    }

# ---- program opportunity tag (elite/hot/strong/potential) -----------------------------------
def opportunity_tag(p, scope_added_30d=0, now=None):
    now = now or config.utcnow()
    added = config.parse_ts(p.get("added_at") or p.get("launch_at"))
    days = _days_between(added, now) if added else 9999
    if days <= 7: fresh = 100
    elif days <= 14: fresh = 90
    elif days <= 30: fresh = 75
    elif days <= 60: fresh = 55
    elif days <= 90: fresh = 40
    elif days <= 180: fresh = 25
    else: fresh = 10
    rc = p.get("report_count")
    if rc is None: comp = 50
    elif rc <= 2: comp = 100
    elif rc <= 10: comp = 85
    elif rc <= 25: comp = 50
    elif rc <= 50: comp = 30
    else: comp = 15
    mb = float(p.get("bounty_max") or 0)
    if p.get("reward_status") == "no_reward" or mb <= 0: rew = 0
    elif mb >= 20000: rew = 100
    elif mb >= 10000: rew = 90
    elif mb >= 5000: rew = 80
    elif mb >= 2000: rew = 65
    elif mb >= 1000: rew = 50
    else: rew = 30
    bonus = min(10, 2 * int(scope_added_30d or 0))
    score = round(0.4 * fresh + 0.3 * comp + 0.3 * rew + bonus, 1)
    if score >= 78 and days <= 30: tier = "elite"
    elif score >= 70: tier = "hot"
    elif score >= 40: tier = "strong"
    elif score >= 20: tier = "potential"
    else: tier = None
    desc = {"elite": "Very fresh, lightly reported, and strong payout potential.", "hot": "Fresh or lightly reported with good payout potential.",
            "strong": "Solid reward potential with manageable competition.", "potential": "Worth a look; weaker on freshness or competition."}
    reason = [f"Added {int(days)} days ago" if days < 9999 else "Launch date unknown", (f"{rc} public reports" if rc is not None else "Report count unavailable"),
              (f"Up to ${mb:,.0f} bounty" if mb > 0 else "No disclosed bounty")]
    if scope_added_30d: reason.append(f"{scope_added_30d} confirmed new in-scope asset{'s' if scope_added_30d != 1 else ''} in 30 days")
    return {"tier": tier, "label": tier.capitalize() if tier else None, "description": desc.get(tier), "reason": " • ".join(reason),
            "opportunity_score": score, "freshness": fresh, "competition": comp, "reward": rew, "scope_bonus": bonus, "signal_count": 3,
            "score_version": config.SCORING_VERSION, "evidence_coverage": {"available": 3 if rc is not None else 2, "total": 3},
            "is_vdp": p.get("program_kind") == "vdp" or p.get("reward_status") == "no_reward"}

# ---- submission cost --------------------------------------------------------------------------
FREE_PLATFORMS = {"HackerOne", "BugCrowd", "Intigriti", "BugRap", "Bugbase", "Standoff365", "BI.ZONE", "Bug Bounty Switzerland", "Inspectiv", "Remedy",
                  "IssueHunt", "GObugfree", "Compass Security", "CertiK", "HackZar", "AuditOne", "PatchDay", "Huntr"}

def submission_cost(platform, observed=None, updated_at=None):
    """observed: dict(amount, unit, kind, source) from the collector; None for unknown."""
    if platform == "Self-Hosted":
        return None
    if observed is not None:
        d = {"amount": observed.get("amount", 0) or 0, "unit": observed.get("unit", "USD"), "kind": observed.get("kind", "fee"), "source": observed.get("source", "platform_catalog")}
        if observed.get("maximum_amount") is not None: d["maximum_amount"] = observed["maximum_amount"]
        d["updated_at"] = updated_at or config.iso()
        return d
    if platform in FREE_PLATFORMS:
        return {"amount": 0, "unit": "USD", "kind": "fee", "source": "platform_policy", "policy_version": config.SUBMISSION_COST_POLICY_VERSION, "updated_at": updated_at}
    return {"amount": 0, "unit": "USD", "kind": "fee", "source": "missing_defaults_free", "policy_version": config.SUBMISSION_COST_POLICY_VERSION, "updated_at": updated_at}
