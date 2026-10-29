# Platform access review (2026-10-29)

Every collector reads only unauthenticated, public endpoints at low rates (≥ 1 request/second per host, one
request in flight per host, descriptive User-Agent). This file records what each platform's public terms say
about automated access and reuse, where that was checked, and the decision for the public release.
Request rates are the per-host minimum delays in `targetfinder/config.py` (`HOST_DELAY`); the User-Agent is
`targetfinder/<version> (+repository url)`. Repository HEAD polling uses `git ls-remote` with at most 4 workers.
Terms change; re-check before a release. None of this is legal advice.

| platform | endpoints used | rate | terms checked | clause found | decision |
|---|---|---|---|---|---|
| HackerOne | `hackerone.com/graphql` DirectoryQuery + `structured_scopes` (the same unauthenticated query the public directory page runs) | 1.5 s | General T&C (hackerone.com/terms/general) and Customer T&C (hackerone.com/terms), 2026-10-29; robots.txt allows all | none on automation/scraping/copying | keep |
| Bugcrowd | `bugcrowd.com/engagements.json`, `engagements/<slug>/changelog/<id>.json`; scopes also via bounty-targets-data | 1.0 s | Website T&C (bugcrowd.com/website-terms-and-conditions), 2026-10-29; robots.txt allows all but `/*?preview`, `/external_redirect` | IP clause: "The copying, redistribution, use or publication by you of any portion of our Website is strictly prohibited." No automation clause. | keep collector; snapshot redistribution of scope data follows the long-standing bounty-targets-data practice (MIT, public since 2018) but is the publisher's judgment, see note |
| Intigriti | `app.intigriti.com/api/core/public/programs` (public, unauthenticated); scopes via bounty-targets-data | 1.0 s | Researcher T&C (kb.intigriti.com article 5466165), 2026-10-29 | 3.4.3 "harvest or otherwise collect information about Companies or Researchers without their consent" (aimed at personal/company data, not the public program list); no automation clause | keep |
| YesWeHack | `api.yeswehack.com/programs`, `/programs/<slug>` (public API behind the public site) | 1.0 s | Terms page not locatable by URL guess or site links (SPA); robots.txt allows `/` and `/programs` | not reviewed, terms not found | keep; re-check if a terms URL is found |
| HackenProof | `hackenproof.com/programs?page=N`, `/programs/<slug>` (Nuxt payload) | 1.0 s | Terms and Conditions PDF (hproof-static S3 bucket), 2026-10-29; robots.txt allows all | IP clause: content may not be reproduced/distributed "for commercial or illegal purposes without our prior written permission"; personal-use license granted; no automation clause | keep collector (non-commercial); snapshot redistribution: see note |
| Immunefi | `immunefi.com/bug-bounty/` and `/bug-bounty/<slug>/scope/` (Next.js RSC payload) | 1.0 s | Terms of Use (immunefi.com/terms-of-use), 2026-10-29 | **explicit:** §4 "Use any robot, spider, or other similar automatic device, process, or means to access the Website for any purpose"; §7 no reproduction/distribution/derivative works | **disabled by default in the public release** (`TARGETFINDER_ENABLE_IMMUNEFI=1` opts in for personal use at your own risk); not included in published snapshots |
| Sherlock | `audits.sherlock.xyz/bug-bounties` (RSC payload), `/bug-bounties/<id>` | 1.0 s | Platform rules (docs.sherlock.xyz), 2026-10-29; no terms-of-service page found on audits.sherlock.xyz or docs; robots.txt allows all | "Automated testing of services that generates significant amounts of traffic is not permitted" (about hunting project services, not the listing) | keep |
| bounty-targets-data | `raw.githubusercontent.com/arkadiyt/bounty-targets-data` | 1.0 s | repository license | MIT | keep |

**Note on redistribution.** Collecting for personal use and publishing hourly snapshots are different acts.
The published snapshots contain only structured scope facts (identifiers, types, eligibility, dates) that the
platforms already expose publicly to drive hunting, not program descriptions, policies or logos. That is the same
line bounty-targets-data draws. Whether to include Bugcrowd and HackenProof rows in public snapshots is the
publisher's call; the `export-snapshot --exclude-platforms` option exists so it can be made per platform.
