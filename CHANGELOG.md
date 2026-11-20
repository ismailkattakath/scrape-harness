# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is the one in `pyproject.toml`.

## [Unreleased]

Incremental sync, slice 1. The design is in [docs/sync.md](docs/sync.md).

### Fixed

- A list walk (`--mode walk|frontier`) writes one observation per ad. A card a
  bump pushed down from an earlier page of the same walk, or one listed twice
  on a page, is counted in `list.repeats_skipped` (run.v1, additive) and not
  written again. Before this, a consumer keyed on `(site, ad_id, crawl_id)`
  failed on the repeat.

### Added

- `crawl-lists --snapshot`: each answered list page is saved as gzip under
  `raw/lists/<site>/<crawl_id>/<page>.html.gz`, with a `list_snapshot.v1`
  sidecar (status, final_url, requested_url, fetched_at, parsed, skipped),
  before any parse or stop. A 404/410 list page is saved and recorded
  (`list.status: past_end`), not a challenge stop; a redirected page is saved,
  its cards not kept (`redirected`). A challenge still stops with exit 5. See
  "Evidence runs" in [docs/sync.md](docs/sync.md).
- `crawl-lists --pages 1,2,400`: exactly these pages, in order, no page-count
  discovery. Signed in the plan and in a refresh's list set (only when set, so
  older plans hash as before); bounded by `--max-pages`; refused with
  `--stride`, `--start`, `--seed` or `--page-count`.
- Positive END of a list (ADR-0017 A.1): optional `Source.end_selectors`,
  pure `challenge.end_reason`, and a seam in the crawl that hands a list page
  carrying the site's "no results" marker (on a clean page or a 404/410, never
  on a challenge sign) to the list handler. It is END only with 0 parsed cards
  at the URL asked for: `list.status: end`, `list.end_read_page`,
  `list.end_method`. A sign-in wall or consent gate is never nominated (it
  stays a stop), and optional `Source.end_statuses` (e.g. `(404, 410)`)
  limits END to the site's measured past-end statuses. In a walk, a 0-card
  page with no END stops with exit 14 at once: `zero_yield` on page 1,
  `end_unknown` after it. A sample or named pages read past such a page, as
  before. run.v1 `list.status` gains
  `end`, `end_unknown` and the reserved walk values `short_census`,
  `ceiling_hit`, `anchored`, `clamped`. See "Positive END" in
  [docs/sync.md](docs/sync.md).
- `crawl-lists --mode walk` (ADR-0017 §A, the census walk): pages 1, 2, 3, ...,
  each next page enqueued only after the current one is handled, with no
  page-count discovery. It stops at a positive END (`end_page` exact), a clamp
  (`end_method: clamped`, a repeat served at the URL asked for; a redirected
  page is `end_unknown`, exit 14), or its signed ceiling of `--max-pages` list GETs
  (`ceiling_hit`). `--min-pages N` (signed): an END before N pages is
  `short_census`, exit 14. `mode`, `min_pages` and (for a walk) the policy
  digest are signed only when set,
  and a walk step's bounds count `max_pages` list GETs (no `1 +`). A resume
  continues from the next unread page within what the ceiling leaves.
  run.v1 `list` gains `end_read_page`, `mode`, `stride`, `ceiling`,
  `min_pages`, `pages_read` and `settled`. Stride k is refused (deferred).
- `crawl-lists --mode frontier` (ADR-0017 §A.2, §A.3): the census walk's
  reading, plus an early stop at an anchor confirmed against the latest
  reference crawl (pure `plan.frontier_reference` and `plan.frontier_stop`),
  only when the source declares the optional `Source.order = "bump_desc"`
  (default `unordered`: to a positive END or the ceiling). A candidate is K
  consecutive cards holding consecutive places in the reference's parsed order;
  one more page confirms it (90% reference ids below it, at most 5% inversions).
  `list.status: anchored`, `list.anchor`, `list.ref_crawl_id`, `list.order`,
  `list.k`; a walk at its ceiling (a census too) records `list.gap`, whose
  oldest origin a later ceiling-bound frontier keeps; later anchored frontiers
  carry it on until a census that read to its END is the reference; it is
  shown as GAP. `SyncPolicy` gains
  `frontier_k`, `confirm_share` and `confirm_inversions`; the lanes version is
  `select_ads.v4`. `mode`, `k`, a frontier's `order` and the policy digest
  are signed only when set. A walk records its policy digest
  (`list.policy_digest`); `crawl resume` refuses it under another digest
  (exit 10, 0 GETs), and a refresh walk step whose digest or order differs
  from its signed set is `NotApproved` (exit 10, nothing appended).
  Walks record `list.subcategory` and `list.region`. See "The frontier walk" in
  [docs/sync.md](docs/sync.md).
- `ListPage.skipped` (optional): cards the parser could not read.
- Exit 14 (`crawl.EXIT_PARSE`): a parse or yield stop, terminal, with a
  `parse` incident (and `gone_storm` for a breaker trip).
- Additive, optional contract fields. run.v1 gains `stop{}`, `list{}`,
  `ads{}` and `breaker{}`. event.v1 gains `ad_id` and the kind `parse_failed`.
  approval.v2 bounds gain `policy_digest`, and their list sets gain `mode`,
  `min_pages`, `k`, `order` and `policy_digest`. Old records still validate.
- `liveness.py`: a pure per-ad ledger with four states (alive, tombstoned,
  quarantined, held). `corrections.py`: one reader of run corrections.
- `policy.SyncPolicy`: window, revisit share, stale age, re-admission delay,
  breaker values and the lanes version. Its digest is signed.
- The fresh-sighting gate (`plan.eligible`) and `plan.select_ads`. Revisits
  are hard-capped at ceil(rho * B), and unused new quota never flows to
  revisits. The backlog stays dormant: 0 GETs, nothing deleted.
- `plan.admit` and an independent postcondition, `plan.assert_admissible`.
- The gone-storm and ad-failure breaker (`breaker.py`), with a per-site latch.
  `<cli> breaker status` and `<cli> breaker clear --approve-operator` were
  added.
- Ad parse failures also count across a site's fetch-ads runs
  (`breaker.parse_streak`, signed as `SyncPolicy.parse_streak`). A run that
  parsed no ad and brings the streak since the last parsed ad to
  `breaker_floor` ends with exit 14. run.v1 `ads{}` gains `parse_streak`.
- Refresh postconditions. A crawl-lists step passes only on exit 0, a run doc
  with `list.status` ok, and at least one observation written. Anything else is
  recorded as exit 14 (terminal), and the strict chain never reaches fetch-ads.
- The refresh end summary shows each list step's `list{}` and each fetch-ads
  step's selection. A fetch-ads run that plans nothing prints its `ads{}`.
- Walk steps in a refresh (ADR-0017 §C.4). A `[crawl.refresh]` crawl-lists
  step may carry `--mode walk|frontier`, `--min-pages` and `--max-pages`; the
  bounds sign them. Its postcondition is per mode (`refresh.PASS_STATUS`): a
  census passes only on `end` (a clamp included); a frontier on `anchored`,
  `end` or `ceiling_hit` (with its GAP); a step with no mode as before. The
  run doc's `list.mode` must be the step's declared mode. Anything else is
  exit 14, and fetch-ads never runs. The refresh run card has a line per walk
  step (mode, ceiling, what it passes on, a frontier's reference and open
  GAP), and the end summary shows a walk's mode, pages read, `end_page`,
  anchor and GAP. See "Refresh postconditions" in [docs/sync.md](docs/sync.md).
- A walk step enters a refresh only when its source declares `end_selectors`
  (ADR-0017 Migration step 5), and a census step only with `--min-pages`:
  `refresh` refuses it at plan time, exit 2, before any plan is saved.

### Changed (breaking for consumers)

- A list handler error, a missing page count (before: exit 4) and a list read
  that yields no observation now end with exit 14.
- A good crawl (`liveness.good_crawl`, and a refresh's list postcondition) is
  now a completed run with `list.status` `ok`, `end`, `ceiling_hit` or
  `anchored`; a refresh walk step that positively ENDED on page 1 (an empty
  subcategory) passes with 0 observations (a step with no mode still needs
  rows). `SyncPolicy.lanes` is
  `select_ads.v3`, so the policy digest changes.
- A list page that fails after its retries, or that robots.txt blocks, now ends
  the crawl with exit 13, not `completed`.
- An ad page that fails to parse records one `parse_failed` event, and the ad
  is held. It is never retried.
- fetch-ads plans only from the gate. Its plan signs `as_of` (the plan time)
  and `policy_digest`, so plan hashes now depend on the plan time.
- `crawl resume` of a fetch-ads run now makes a new, filtered, superseding run.
  It is refused only when a successor took over the pending scope; a
  `--supersede --failed` successor no longer blocks it.
- A write the data folder refuses after an ad page answered (exit 4) leaves
  that ad done in the queue, so no resume GETs it again. A refused gone event
  is guarded the same way and still counts toward the breaker.
- A crawl's sightings count only when the crawl is `completed` with
  `list.status` ok. Rows kept before a stop (`parse_drift`, `failed`,
  `blocked`, any stopped run) no longer buy ad GETs.
- The postcondition joins a gone to its ad by the ledger's own rule
  (`adkey`). A legacy url-only gone whose url names two ads no longer
  refuses the whole plan.
- `cli.fetch_plan` was removed. Use `cli.fetch_pool` and the selection.
- Gone events carry `ad_id`. A consumer's golden digests need re-recording
  when it bumps its harness pin.
- `SyncPolicy`'s digest now also covers the selection rules' constants, and
  `lanes` is `select_ads.v2`, pinned by a test to a fingerprint of the rule
  code. Plans and refresh approvals signed under the old digest no longer
  match (exit 10): plan them again.
- A fetch-ads run that plans nothing prints `{"planned": 0, "ads": {...}}`,
  not `{"planned": 0}`. A consumer that matches the old line exactly must
  change.

## [0.1.0] - 2026-11-13

### Added

- Initial public release: the site-agnostic harness for careful,
  approval-gated scraping pipelines. The `pipeline.v1` contract, signed
  single-use approvals with a hash-chained ledger, the egress preflight,
  politeness ceiling and stop-on-challenge crawling, run cards, the
  append-only store, `leak-gate`, `public-gate` and `ship`, the Claude Code
  plugin pin, the synthetic example consumer (`examples/example_site`) and
  the consumer template (`templates/default`).
- Community files (licence, code of conduct, contributing guide, security
  policy, issue and pull request templates) and CI: `gitleaks` over the whole
  history and `nix flake check`, behind one required `gate` check.

[Unreleased]: https://github.com/ismailkattakath/scrape-harness/commits/main
[0.1.0]: https://github.com/ismailkattakath/scrape-harness/commit/873de43
