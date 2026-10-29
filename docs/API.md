# TargetFinder HTTP API and MCP tools

The API is served by `targetfinder serve` on `http://127.0.0.1:8787/api/` (no authentication; it
binds to localhost). Every list response is `{"items": [...], "page": {...}}`; every error is
`{"error": {"code": "bad_request|not_found|payload_too_large|server_error", "message": "..."}}`.
Timestamps are ISO-8601 UTC strings. Program ids are `<platform-slug>:<handle>` in lowercase
(`hackerone:gitlab`, `bugcrowd:engagements/example`, `intigriti:upcloud`, `sherlock:56`).

## Objects

**Program** (`/api/programs/{id}`; the list view omits `payouts` and `activity`)
```
id, platform, handle, name, url, logo, kind (bounty|vdp|contest), status (active|paused|unlisted),
rewards {offers_bounty, min, max, currency, status}, submission_fee {amount, unit, kind, source, updated_at} | null,
report_count {value, source, label} | null, launched_at, ends_at, first_seen_at, updated_at, scope_updated_at, last_seen_at,
tags [], scope_counts {total, in_scope, out_of_scope, bounty_eligible, wildcards},
scope_activity {last_change_at, last_added_at, added_24h, added_7d, removed_7d},
competition {level (low|medium|high|unknown), reason}, policy_url, source,
payouts {last_7d, last_30d, total}, activity {last_changed_at, active_days_30d, changes_30d, scope_added_30d, ..., labels [], signals {}}
```

**Target**
```
id, identifier, display_name, normalized, type, in_scope, bounty_eligible, wildcard, tags [], languages [],
severity {label, score} | null, source_url, added_at, last_seen_at, last_change {type, at} | null, active, metadata {},
repo {url, path_kind, path_prefix} | null,
score {value, label (top|strong|promising|normal), version, factors {freshness, target_type, reward, reports}, summary} | null,
competition {level, reason} | null          -- score and competition only for in-scope, bounty-eligible targets
```

**Target change row** (`/api/changes/targets`)
```
id, type (added|readded|updated|removed), at, program {id, name, platform, handle, url, kind, status, rewards, submission_fee, report_count, competition, tags, scope_updated_at},
target {...}, diff {before, after} | null, source
```

**Repository push row** (`/api/changes/repos`)
```
id, at, repo {provider, host, name, url}, ref, before, after, compare_url, kind (code|docs|deps|ci|tests|mixed|unknown|null),
files [] | null, source, programs [{id, name, platform, url}], targets [{...}]
```

**Lead** (`/api/leads/{tier}`): a list-view program plus
`lead {tier (top|hot|solid|possible), score, factors {freshness, competition, reward, scope_bonus}, reason, description, version}`.

**Ranking row** (`/api/rankings/{view}`): `{program {id, name, platform, handle, url, kind, status, tags, in_scope_now}, metrics {...}, labels []}`.

## Routes

| route | notes |
|---|---|
| `GET /api/health` | `{ok, service, version}` |
| `GET /api/status` | counts, platforms, collectors, last sync |
| `GET /api/capabilities` | routes, page-size limits, vocabularies, rankings views |
| `GET /api/programs` | `search, platforms, tags, kind, status, reward_status, free_submission_only, competition, updated_since, include_unlisted, full, order (name|updated|added|launch|scope), page, page_size≤100` |
| `GET /api/programs/{id}` | full program; `{id}` may also be a handle or an unambiguous name |
| `GET /api/programs/{id}/targets` | `eligibility (all|eligible|ineligible), type, search, min_score, label, competition, include_inactive, page, page_size≤500`; response adds `program` |
| `GET /api/programs/{id}/payouts` | `{program, facts, windows {24h,7d,30d,90d,all}, signals, labels, series, reports, response}` |
| `GET /api/programs/{id}/history` | event feed for one program (`category, since, limit, cursor`) |
| `GET /api/targets/search` | `q (2–512 chars), match_mode (contains|exact|suffix|wildcard), platforms, tags, include_out_of_scope, include_ineligible, page, page_size≤500`; items are `{program, target}` |
| `POST /api/targets/export` | body `{program_ids≤50 | platforms, tags, tiers, include_out_of_scope, include_ineligible, format (json|csv), limit≤500}` |
| `GET /api/changes/targets` | `type, since, program_id, platforms, tags, search, min_score, label, competition, free_submission_only, include_removed, include_ineligible, include_out_of_scope, page, page_size≤100`; `after_id` switches to cursor mode (`page.next_after_id`); response adds `facets` |
| `GET /api/changes/repos` | `since, program_id, platforms, tags, search, kinds, free_submission_only, include_targets, page, page_size≤100`; `after_id` cursor mode |
| `GET /api/leads/{tier}` | `top|hot|solid|possible`; `platforms, tags, search, free_submission_only, page, page_size≤100` |
| `GET /api/rankings` | views, periods, sorts, labels |
| `GET /api/rankings/{view}` | `scope|rewards|updates|status|payouts|response|all`; `period (24h|7d|30d|90d|all as allowed per view), sort, direction, limit≤100, cursor, platforms, tags, kinds, labels` |
| `GET /api/history` | all-program event feed (`program_id, category, since, limit, cursor`) |
| `GET /api/history/ev-{id}` | one event with before/after |

Examples:
```
curl 'http://127.0.0.1:8787/api/programs?free_submission_only=1&order=scope&page_size=5'
curl 'http://127.0.0.1:8787/api/programs/hackerone:gitlab/targets?eligibility=eligible&min_score=70'
curl 'http://127.0.0.1:8787/api/changes/targets?type=added&since=2026-10-01T00:00:00Z&free_submission_only=1'
curl 'http://127.0.0.1:8787/api/changes/targets?after_id=0&page_size=100'      # then poll with page.next_after_id
curl 'http://127.0.0.1:8787/api/changes/repos?kinds=code,mixed&since=2026-10-28T00:00:00Z'
curl 'http://127.0.0.1:8787/api/leads/top?free_submission_only=1'
curl 'http://127.0.0.1:8787/api/rankings/scope?period=7d&sort=net'
curl -X POST 'http://127.0.0.1:8787/api/targets/export' -d '{"program_ids":["hackerone:gitlab"],"format":"csv"}'
```

## MCP tools (`targetfinder mcp`, stdio)

`guide, status, find_programs, get_program, program_brief, list_targets, search_targets,
find_program_by_target, recent_target_changes, recent_repo_pushes, leads, rankings, program_payouts,
program_history, watchlist_new_targets, export_targets, list_filters, run_sweep`.
Arguments mirror the route parameters above; `recent_target_changes` and `recent_repo_pushes`
accept `after_id` and return `page.next_after_id` for polling. List tools return compact objects by
default (`compact: false` for full ones).

Client registration (Claude Code example):
```json
{"mcpServers": {"targetfinder": {"command": "/path/to/targetfinder/bin/targetfinder", "args": ["mcp"]}}}
```
