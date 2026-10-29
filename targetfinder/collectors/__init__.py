"""Platform collectors. Each exposes collect(existing, log) -> {"platform": str, "programs": [program], "complete": bool}.

program contract (plain dicts):
  id, platform, handle, name, link, profile_picture, program_kind (bounty|vdp|contest|challenge), program_status (active|paused),
  is_active, reward_status (paid|no_reward|unknown), bounty_min, bounty_max, reward_currency, launch_at (ISO), launch_at_precision,
  ends_at, report_count, report_count_source, report_count_label, fee {amount, unit, kind, source} | None,
  detail_marker (str; when it changes, detail/scopes are re-fetched), targets (list | None = not fetched this run),
  payout_facts [ {metric, amount, currency, unit, source_label, source_field, period, basis} ], policy_url, safe_harbor, raw (small dict)
target contract: identifier, hint_type, display_name, description, in_scope, eligible_for_bounty, severity_label, severity_score,
  source_url, source_added_at, metadata (dict)
`existing` is {program id: {"detail_marker": str, "detail_fetched_at": iso, "has_targets": bool}}.
"""
from .. import config

# display name per platform slug; the slug is the first part of every program id
PLATFORMS = {"hackerone": "HackerOne", "bugcrowd": "Bugcrowd", "intigriti": "Intigriti", "yeswehack": "YesWeHack", "hackenproof": "HackenProof", "immunefi": "Immunefi", "sherlock": "Sherlock"}

def platform_slug(name):
    return str(name).strip().lower().replace(" ", "-")

def program_id(platform, handle):
    """Program ids are `<platform-slug>:<handle>` in lowercase, e.g. hackerone:gitlab, sherlock:56."""
    return f"{platform_slug(platform)}:{str(handle).strip().lower()}"

def need_detail(existing, pid, marker):
    e = existing.get(pid)
    if not e or not e.get("has_targets"):
        return True
    if marker and e.get("detail_marker") != marker:
        return True
    last = config.parse_ts(e.get("detail_fetched_at"))
    if not last:
        return True
    return (config.utcnow() - last).total_seconds() > config.DETAIL_REFRESH_SECONDS

REGISTRY = {}

def register(name):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco

def load_all():
    from . import hackerone, bugcrowd, intigriti, yeswehack, hackenproof, sherlock  # noqa: F401
    if config.ENABLE_IMMUNEFI:
        from . import immunefi  # noqa: F401   (off by default: Immunefi's terms prohibit automated access)
    REGISTRY.pop("Immunefi", None) if not config.ENABLE_IMMUNEFI else None
    return REGISTRY
