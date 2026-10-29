# TargetFinder — design

TargetFinder collects the public scope of bug-bounty and vulnerability-disclosure programs from the
platforms that publish it, diffs each run against the last one, and serves the result from a local
SQLite database through a CLI, an HTTP API and an MCP server. Everything runs on one machine with
the Python standard library; there are no API keys and no paid accounts.

## Goals
- Every program and every in-scope target the platforms publish, refreshed on a schedule you choose.
- Honest change tracking: a target is "added" only when a program's scope was already known and
  the target was not in it. First observations are baselines, never events.
- Source-repository watching: in-scope GitHub/GitLab repositories are polled with `git ls-remote`
  and every push is recorded with a compare link and a rough classification (code / docs / deps / ci).
- Heuristic lead scoring that is explained field by field (docs/SCORING.md).
- A one-command refresh (`targetfinder sweep --sync`) that writes a readable report.

## Non-goals
Web UI, vote/rating features, analytics dashboards, platforms that forbid automated access
(Immunefi is off by default, see docs/PLATFORMS.md), anything that needs credentials.

## Data flow
```
collectors/<platform>.py   fetch listing + per-program scope  ->  plain dicts (parse_* functions are pure)
sync.py                    apply_program / apply_targets      ->  programs, targets, target_changes, program_events
repowatch.py               git ls-remote per in-scope repo    ->  repos, repo_changes (+ push classification)
queries.py / model.py      read model                          ->  api.py (HTTP), mcp_server.py (MCP), cli.py, sweep.py
snapshot.py                export/import JSON                  ->  public/*.json (GitHub Action data branch)
```

## Storage
`data/targetfinder.db` (SQLite, WAL). Tables: programs, targets, target_changes, program_events,
repos, repo_changes, payout_observations, collector_runs, sync_runs, kv, watchlist. The schema is
created on first connect; additive migrations live in `db.MIGRATIONS`.

## Identifiers
Program id = `<platform-slug>:<handle>` (lowercase slug, e.g. `hackerone:gitlab`). Target rows are
unique per (program, normalized key); the key strips scheme, wildcard prefix and trailing slash so a
program cannot list the same asset twice under two spellings.

## Baseline semantics
- First listing of a platform: all programs are recorded, no `first_seen` events.
- First scope fetch of a program: all targets are recorded with `added_at` = the platform's own
  date when it publishes one, otherwise the program launch date; no `added` events.
- A scope fetch that returns nothing while the program has three or more active targets is
  ignored (empty-scope guard) so a parser hiccup never produces mass removals.
- Status flips (paused/resumed) must be seen on two consecutive runs before they become events.
