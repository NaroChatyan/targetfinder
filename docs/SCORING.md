# Scoring model (heuristic, scoring version 1)

Every band and weight here is TargetFinder's own heuristic: competitionRisk and opportunity scores are
comparative estimates derived from public counts, not measured probabilities. The inputs are public counts and dates that the
platforms publish; the weights were chosen to order leads sensibly, not fitted to any measured
outcome. Treat scores as a comparative ranking, never as a probability. Always read the program
page before you spend time on a target.

## Target score (0–100)
Sum of four factors, computed for in-scope, bounty-eligible targets:

| factor | values |
|---|---|
| freshness (last change) | <1 day 35 · <7 days 25 · <30 days 12 · older 0 |
| target type | API/GraphQL 25 · wildcard 22 · staging/dev 22 · smart contract 20 · source code 20 · mobile 18 · web/domain 14 · other 5 |
| reward (max bounty) | ≥20k 25 · ≥10k 22 · ≥5k 18 · ≥2k 14 · ≥1k 10 · >0 6 · none/undisclosed 0 |
| reports (public report count) | ≤2 15 · ≤10 12 · ≤50 7 · ≤100 3 · >100 0 · unknown 7 |

Labels: `top` ≥85, `strong` ≥70, `promising` ≥50, otherwise `normal`.

## Competition (low / medium / high / unknown)
Point model: report count bucket (very low −2, low −1, moderate +1, elevated +2, high +3),
program age (mature >1 y +1, older >3 y +2), a target change in the last 7 days −3, asset shape
(generic web scope +2, API/GraphQL/staging web target +1, less obvious asset type −1).
low ≤0 · medium 1–2 · high ≥3. "unknown" when no report count and no target data exist.
The `reason` field lists the inputs that were used.

## Lead tiers (program level)
`score = 0.4·freshness + 0.3·competition + 0.3·reward + scope bonus` where freshness decays with
days since the program appeared (≤7 d 100 … >180 d 10), competition comes from the public
report count (≤2 100 … >50 15, unknown 50), reward from the max bounty (≥20k 100 … none 0) and the
bonus is 2 points per confirmed new in-scope asset in 30 days (max 10).
Tiers: `top` ≥78 and ≤30 days old · `hot` ≥70 · `solid` ≥40 · `possible` ≥20.

"Appeared" means the platform's launch date, or the day TargetFinder saw the program join a listing it
was already tracking. Programs picked up by the very first run of a platform are not treated as new:
they predate tracking, so only their launch date counts. On a fresh install `top` is therefore often
empty for a while; `hot` and `solid` still rank by launch date, reports and reward.

## Submission fee
Platforms that charge a fee or deposit per report (HackenProof, Immunefi, Sherlock, some
YesWeHack programs) are read from the program page. Platforms with no such mechanism are
recorded as fee 0 by policy. The sweep uses "paid fee" as its only deprioritiser.

## Known limits
Report counts mean different things on different platforms (resolved reports on HackerOne,
submissions on HackenProof, nothing on Intigriti). The score does not correct for that.
