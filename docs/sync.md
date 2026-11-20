# Incremental sync (slice 1)

How the harness decides **which ad pages to GET**, and how it fails when a
list read cannot be trusted. Every ad GET adds a view to a real person's ad,
so the default is **not** to fetch. List GETs add no ad views, but they still
run one at a time, under the politeness ceiling and robots.txt, and they stop
on a challenge.

This page describes the code as it stands. Slice 1 covers four things: loud
failures, the liveness ledger with tombstones, the fresh-sighting gate with
`select_ads`, and the guards that make tombstones safe. List walk modes come
in a later slice.

## Why

A consumer saw three silent faults. Each one cost ad views or hid a broken
read:

| Fault | Effect |
|---|---|
| A list page whose handler raised was retried, then marked failed, and the crawl still ended `completed`, exit 0. | A broken parser looked like a good crawl. Later stages planned from it. |
| An ad sighted once, long ago, stayed `new` forever. | A backlog of old sightings competed with today's ads for the budget. |
| A gone ad was written only as an event, and the planner never read events. | The planner sent the same dead ad page again on later runs. |

All three are fixed by reading facts already in DATA. No new state file was
added.

## Exit 14: a parse or yield stop

`crawl.EXIT_PARSE = 14` means **what the site sent cannot be trusted**. It is
terminal: a refresh spends its approval, and `crawl resume` refuses it before
the gate, with 0 GETs. It opens a `parse` incident. A breaker trip opens a
`gone_storm` incident.

| Cause | Exit | Recorded as |
|---|---|---|
| A list handler raised (a contract error or a parser crash) | 14 | `list.status: failed` |
| Page 1 named no page count | 14 | `list.status: parse_drift` |
| A walk's page 1 gave 0 cards with no positive END | 14 | `list.status: zero_yield` |
| A walk's later page gave 0 cards with no positive END, or any page carried the end marker on a 404/410 but is not an END | 14 | `list.status: end_unknown` |
| A census walk ENDED before its signed `--min-pages` | 14 | `list.status: short_census` |
| A census walk drained with neither an END nor its ceiling | 14 | `list.status: end_unknown` |
| List pages answered, but 0 observations were written | 14 | `list.status: zero_yield` |
| A list page failed after its retries | 13 (resumable) | `list.status: failed` |
| A list page was disallowed by robots.txt | 13 | `list.status: blocked` |
| An ad page could not be parsed | none: the run goes on | one `parse_failed` event (with `ad_id`), no retry |
| The data folder refused a write after a page answered | 4 (resumable) | a list page is handed back; an ad page stays done in the queue, so no resume GETs it again |
| A fetch-ads run parsed no ad, and the site's parse failures since its last parsed ad reach `breaker_floor` | 14 | `ads.parse_streak`, `stop{}` |
| The breaker tripped, or the site is latched | 14 | `breaker{}` on the run doc |

Each stop is recorded on the run doc as `stop{code, reason}`. The ad page that
could not be parsed is saved as a raw snapshot, so a fixed parser can reparse
it with no new view.

## The liveness ledger

`liveness.build(observations, listings, events, runs)` is pure. It folds each
ad's history, in time order, into one state:

| State | Set by | Cleared by |
|---|---|---|
| `alive` | a live sighting: an OK fetch, or a list card from a good crawl | (the default) |
| `tombstoned` | the first gone event after the latest live sighting | an OK fetch; or a list sighting at least `readmit_h` after the gone, **once per ad** |
| `quarantined` | the ad's latest gone came from a run whose gones a correction quarantined | a later real gone, an OK fetch, or the operator |
| `held` | a `parse_failed` after the latest live sighting, with no listing since | a listing written after it (an OK fetch or a reparse) |

A crawl is **good** only when its corrected status is `completed` and its
`list.status` is `ok`, `end`, `ceiling_hit` or `anchored` (`liveness.good_crawl`,
an allowlist; `anchored` is the frontier walk's confirmed stop). So the rows a
crawl kept before a stop never count as sightings: not after `parse_drift`,
`failed`, `zero_yield`, `end_unknown`, `short_census` or `blocked`, not from an
evidence run (`past_end`, `redirected`), and not from a run that is `stopped`,
`running` or `aborted`. Two legacy cases cannot be judged and count as good:
a crawl_id with no run doc, and a completed run from before `list.status`.
A refresh's own list steps get no such benefit of the doubt. Run corrections
are read through one module, `corrections.py`, where the latest correction in
file order wins.

## The fresh-sighting gate

`plan.eligible` takes every ad that `plan.fetch_reason` names (`new`,
`card_changed` or `stale_7d`) at the plan's `as_of`. It keeps an ad only when
the ad is:

1. `alive` in the ledger;
2. **freshly** sighted, by a good, non-sponsored list card.

Otherwise the ad is counted once in `excluded{tombstoned, quarantined, held,
dormant}`. **Dormant** means the ad was sighted, but not freshly: it gets
0 GETs and nothing is deleted. A later list sighting brings it back.

What counts as fresh:

- **Standalone fetch-ads:** sighted within `window_h` of `as_of`. `as_of` is
  the plan time, to the second, and it is signed. The run rebuilds the plan at
  the signed `as_of`, so the plan that was approved is the run that happens.
- **Inside a refresh:** sighted by a crawl-lists step of the same refresh that
  passed. `as_of` is the latest such sighting, so it comes from DATA, not from
  the clock.

## `select_ads`: two lanes under a budget B

`--max-fetches` is B. B is a **ceiling**: an empty pool sends 0 GETs.

| Lane | Takes | Order |
|---|---|---|
| **revisit** (`card_changed`, `stale_7d`) | q = min(n_revisit, ceil(rho * B)), a **hard cap** | `card_changed` first, then the oldest fetch |
| **new** | B - q, split equally across subcategories; a short subcategory gives its share to the others | latest crawl first, then page, position, (site, ad_id) |

- **Unused revisit quota flows to new. Unused new quota never flows to
  revisits.** This bounds the daily views spent on ads the store already has.
- `ceil(rho * B)` is computed in exact arithmetic. In floats, ceil(0.07 * 100)
  gives 8, one view too many.
- `--sample` and `--select recruit` draw from the same gated pool, under the
  same hard cap with B = N (`plan.sample_lanes`). The revisit lane is
  sampled first, with n = ceil(rho * N); the new lane takes what is left.
  The lane is one more stratum, so each design weight N_h / n_h stays exact.
- The postcondition (`plan.assert_admissible`) checks the cap on its own: at
  most ceil(rho * B) planned ads may already have a listing. A superseding
  queue is not re-checked: it can only shrink the plan that queued it, and
  the tail of a select_ads queue is mostly revisits. A queue planned before
  this cap existed is therefore not capped either.

## One signed policy

`policy.SyncPolicy` holds every tunable that decides which ads get a view.
Its sha256 `digest()` is signed. A standalone plan carries the digest, and a
refresh's bounds carry the fetch-ads step's digest. If the digests differ, the
run refuses with exit 10 and appends nothing.

| Field | Default | Meaning |
|---|---|---|
| `window_h` | 72 | a sighting is fresh within this many hours of `as_of` |
| `rho` | 0.2 | the revisit lane's hard share of B |
| `stale_after_d` | 7 | a stored copy older than this, of an ad still listed, is due a revisit |
| `readmit_h` | 24 | how long after a gone a list sighting may re-admit the ad |
| `breaker_p0`, `breaker_alpha`, `breaker_floor` | 0.10, 1e-4, 5 | the breaker's trip rule |
| `parse_streak` | true | ad parse failures count across a site's runs, up to `breaker_floor` |
| `lanes` | `select_ads.v2` | the version of the selection rules in code |

These are judgement values, not measured ones. Changing one is an operator
decision, and the change moves the digest.

The digest also covers the rules themselves, because a refresh's bounds sign
only the digest:

- **Rule constants** are digested directly: the good-crawl status sets, the
  gone reasons that count as the site's own statement, and the revisit
  lane's reasons (`SyncPolicy.rule_constants`).
- **Rule code** (the gate, the lanes and their order, the ledger's fold,
  the breaker, admission) is versioned by `lanes`. A test pins each `lanes`
  value to a fingerprint of that code: the AST, with docstrings stripped.
  A code change fails the suite until `lanes` is bumped. A comment or a
  reformat does not.

## Guards that make tombstones safe

- **The breaker** (`breaker.py`, pure). After each ad page that answered, n is
  the number of pages that answered and g is a counter. The run trips when
  g >= floor and P(X >= g | Binomial(n, p0)) < alpha, in exact integer
  arithmetic.
  - With the defaults, g must reach 5 at n = 5, 9 at n = 20, 24 at n = 100
    and 52 at n = 300.
  - The gone counter counts only **weak** reasons: absence, a redirect, or a
    bare 404. The site's own expiry notice and a 410 never count, so a real
    wave of expiries cannot trip it.
  - The failure counter counts `parse_failed`.
  - **Below the floor a run cannot trip**, so a small run could lose every
    ad page to a broken parser and still exit 0. So parse failures also
    **count across runs** (`breaker.parse_streak`, operator decision
    2026-11-19). The streak is the site's `parse_failed` events since its
    last fetch-ads run that parsed an ad (`ads.ok > 0`), read from the run
    docs and the event log, newest run first in log order. There is no
    counter file.
  - A run rings when it parsed no ad, failed at least one page, and brings
    the streak to `breaker_floor`. It ends with exit 14 and opens a `parse`
    incident. A refresh treats that as terminal. It writes no `breaker{}`,
    so nothing is latched or quarantined.
  - One failed ad never ends a run on its own. Neither does a run with no
    parse failure, such as a gone-only run. A run that parses one ad resets
    the streak to 0.
  - A trip writes `breaker{}` on the run doc, quarantines that run's weak
    gones, stops the run with exit 14, and **latches** the site.
  - Every networked command and `--plan` then refuses until
    `<cli> breaker clear --site S --resolve keep|void --note N --approve-operator`.
- **Admission** (`plan.admit`). `fetch-ads --supersede` and `crawl resume`
  never re-send a queue unfiltered. They keep a queued ad only if the gate
  would plan it now.
- **An independent postcondition** (`plan.assert_admissible`). It reads the
  raw records, not the ledger or the selector. Before the gate, it refuses a
  plan with exit 14 if any of these hold:
  - the same ad is planned twice;
  - an ad has no sighting inside the window;
  - an ad was already fetched at or after its latest sighting;
  - an ad has a gone with no re-admitting sighting.

## Refresh postconditions

A refresh runs its steps as a **strict doit chain**: a failed step stops every
step after it.

- A **crawl-lists step passes only** when all of these hold:
  - it exited 0;
  - the run doc it names is a completed crawl-lists run of this refresh;
  - its `list.mode` is the mode the step declares (`--mode` in its args);
  - its `list.status` passes for that mode (`refresh.PASS_STATUS`, below);
  - `observations_written` is above 0, or, for a walk mode, page 1 itself
    positively ENDED (`end_page: 0`, an empty subcategory). A step with no
    mode needs rows, as before the walks: a renamed slug or a soft block on
    page 1 carries the same marker.

  | Step's `--mode` | Passes on `list.status` | Exit 14 on |
  |---|---|---|
  | `walk` (the census) | `end` only. A clamp is an END (`end_method: clamped`), so it passes | `ceiling_hit`, `short_census`, `end_unknown`, every other value |
  | `frontier` | `anchored`, `end` or `ceiling_hit` (its GAP is recorded and shown) | `end_unknown`, `ok`, every other value |
  | none | the good set: `ok`, `end`, `ceiling_hit`, `anchored` | every other value |

  Each set is a subset of `liveness.Rules.good_list_status` (tested), so a
  refresh never passes a list read that the fetch-ads gate would not count.
  A census must read the whole list, so its ceiling fails the refresh: size
  `--max-pages` above the list's length. A frontier may stop at its ceiling,
  because the bumps it leaves unread are recorded as its GAP.
- Anything else is recorded as **exit 14**. That is terminal: the approval is
  spent, and fetch-ads never plans from that list read.
- **The fetch-ads gate** of a refresh takes its sightings only from the list
  steps of this refresh that passed (`liveness.good_crawl` over the run docs
  that name this refresh). The strict chain means fetch-ads runs only after
  every list step passed, so `end`, `anchored` and a frontier's `ceiling_hit`
  all feed it.
- **A walk step needs the site's end marker** (ADR-0017 Migration step 5). A
  census or frontier reaches a positive END only through the source's
  `end_selectors`. So `refresh` refuses a `--mode walk|frontier` step whose
  source declares none, with **exit 2**, before any plan is saved or signed
  (`refresh._needs_end_marker`). It checks the same on every resume. The
  harness checks only that the selectors are declared. The plug-in's own
  test shows that they match a saved past-end page, with its status and
  final_url.
- **A census step needs `--min-pages`** (ADR-0017 A.1: about half the last
  census). `refresh` refuses a `--mode walk` step without it, with **exit 2**,
  before any plan is saved (`refresh._needs_min_pages`). Fail closed: without
  it, an empty or 1-page census of a renamed or soft-blocked list, or of a
  site that ignores the page parameter, would pass as a complete END.
- The refresh's **run card** has a line per walk step: its mode, its signed
  ceiling, `--min-pages`, and what it passes on. For a frontier, it also
  names the reference crawl as DATA holds it now, and that reference's open
  GAP. The step chooses its reference again when it starts.
- The refresh's end summary shows:
  - each crawl-lists step's `list{}`. A walk step's line also has its mode,
    its `end_page` and `end_method`, its pages read of its ceiling and its
    anchor, and a frontier's `GAP after page N (since reference R)` line;
  - each fetch-ads step's selection: eligible, its new and revisit lanes,
    excluded, and its GET, ok and gone counts.

  It reads these from the run docs. A step that stopped prints no run id, so
  the summary finds its run doc through `approval_ref`. A fetch-ads step that
  planned nothing has no run doc, so it prints its `ads{}` on its own stdout
  line instead.
- An offline backend spends no approval, so its run doc names no refresh. Its
  list step therefore fails the postcondition, the same way fetch-ads would
  find no fresh sighting from it.

## Evidence runs: list snapshots and named pages

The first piece of slice 2. It lets the operator take ONE approved run of a
redesigned list: page 1, page 2 and a past-end page, with 0 ad views. Walk
modes and anchors are not built yet; positive END is (below).

```
<cli> crawl-lists --subcategory X --pages 1,2,400 --snapshot --plan
```

| Flag | Does |
|---|---|
| `--pages 1,2,400` | Reads exactly these pages, in this order. No page-count discovery, no sample weight. |
| `--snapshot` | Saves each answered list page as `raw/lists/<site>/<crawl_id>/<page>.html.gz` (ADR-0005), with a `list_snapshot.v1` sidecar `<page>.json`. |

**Signed.** Both join the plan (`run_plan`) and a refresh's signed list set
(`approvals.list_args`) only when set. So a plan without them hashes as before.
A different page list, or a different order, is a different plan_hash.

**Bounded.** `--pages` counts toward `--max-pages` and is refused when it names
more, never truncated. It is refused with `--stride` other than 1, `--start`,
`--seed` or `--page-count`. Its GETs run under the same politeness ceiling.

**Saved before any decision.** `challenge.detect` is pure, so it runs first.
Then, before any parse or stop, the body and its sidecar are written. The
sidecar holds what HTML alone cannot prove: `status`, `final_url`,
`requested_url`, `fetched_at`, `reason`, `outcome`, `parsed`, `skipped`, and
the body's raw_ref. `parsed`/`skipped` are filled in after the parse, before
the handler's own stop.

| The page | Outcome | The run |
|---|---|---|
| A normal list page | `listed` | Rows kept, as without `--snapshot` |
| Page 1 with no page count (discovery mode) | `listed` | Still exit 14 `parse_drift`; the page and its counts are saved |
| 404 or 410 | `past_end` | Saved and recorded, no rows, **not** a challenge, no cooldown; the run goes on |
| 200 at another URL (a redirect) | `redirected` | Saved, cards counted but **not** kept: they are not the page asked for |
| A challenge sign (cf_mitigated, a challenge title, a marker, captcha, 403/429/503, a login redirect) or any other error status | `stopped` | Exit 5, as today. Sidecar only: the body stays in STATE `stops/`, never synced |
| No list expectation and no challenge sign (`unexpected_page`, `signin_wall`, `consent_gate`) | `stopped` | Exit 5, as today. The body is saved: it is the site's own page |
| The same, carrying the source's end marker | `listed` | Handed to the list parser, which decides the END (see "Positive END") |

**Fail-closed.** Without `--snapshot`, a 404 list page is still a challenge stop
(exit 5). A run that recorded a `past_end` or `redirected` page completes with
`list.status` `past_end` or `redirected`, never `ok`. So it is never a good
crawl: its sightings feed no fetch-ads gate and it is never a reference crawl.
`list.snapshot{dir, saved, past_end, redirected}` on the run doc names them.
A listed page that left no row is still a zero yield (exit 14), even beside a
past-end page. A redirect means a different path or query: the host's case, a
default port and a fragment are ignored, because a browser's `page.url` can
differ from the request in exactly these.

**A refused write** (the data folder, a full disk) takes slice 1's path: exit 4,
the list page handed back for the resume, no retry GET.

## Positive END of a list

A list ends only when the site **says** it ended. Absence is never an end: a
broken card selector also gives 0 cards.

- **The plug-in declares it.** `Source.end_selectors` (optional, the twin of
  `gone_selectors`) names the site's own "no results" template. Prove it on a
  saved past-end page, with its sidecar `{status, final_url}`: HTML alone
  cannot prove the status or the missing redirect.
- **The nominee.** `challenge.end_reason(res, reason, end_selectors, ignore)`
  is pure. It returns `end_marker` only when a selector matches AND
  `challenge.detect` found no challenge sign and no wall: a clean page, an
  `unexpected_page` (merely not the list), or a 404/410. Every challenge sign
  (403/429/503, cf-mitigated, a challenge title, a corroborated marker or
  captcha, a login redirect) returns first, so it still stops the run with
  exit 5; so do a sign-in wall and a consent gate, even when their template
  carries a generic "no results" component.
- **The END statuses.** Optional `Source.end_statuses` names the statuses the
  site's END answers with (its measured past-end page, e.g. `(404, 410)`).
  When set, a "no results" page with any other status (a 200 empty filter
  result, a soft block) is never nominated: in a walk it is a 0-card page
  with no END, `end_unknown`, exit 14. Unset, every admitted status
  nominates, as before.
- **The seam.** In crawl-lists runs, a nominated list page skips the challenge
  stop and goes to the list handler, with and without `--snapshot`.
- **The handler decides, END first.** END needs the marker, **0 parsed cards**
  (skipped promoted slots are allowed) and the page **served at the URL asked
  for** (`crawl.redirected`, the same check the snapshots use). It is decided
  before the page-count check and any drift guard: a past-end template has no
  pager, and it may still render a promoted block.

| The page | Result |
|---|---|
| Marker, 0 cards, no redirect (200, 404 or 410) | **END**: `list.end_read_page`, `list.end_method: end_marker`; the run ends `list.status: end` |
| Page 1 is that END | an empty subcategory: `end`, `end_page: 0`, 0 observations, exit 0 |
| Marker and cards on a 200 page | a normal page: rows kept |
| Marker on a 404/410 or `unexpected_page` that is not an END (cards, or a redirect) | `end_unknown`, exit 14; none of its cards are kept |
| Marker, 0 cards, at another URL (200) | in a walk: `end_unknown` (page 1: `zero_yield`), exit 14; a sample or named pages read past it |
| 0 cards, no marker | in a walk: page 1 `zero_yield`, later pages `end_unknown`, exit 14, earlier rows kept. A sample or named pages read past it (no row), as before the walks; the run's zero-yield backstop still applies |
| 404/410 without the marker | as before: exit 5 without `--snapshot`, `past_end` evidence with it |

**With `--snapshot`** the END page keeps its own outcome (`past_end` for a
404/410) and is also the run's `end_read_page`. A run with an END and no bare
past-end or redirected page records `end`; one bare past-end page still makes
it an evidence run (`past_end`).

**Two page numbers.** `end_read_page` is the page whose read ENDED the list.
`end_page` is the **last page of the list** (its page count), the page before
it. It is recorded only where it is exact: in a census walk (below), which
reads every page from 1, or when page 1 itself ENDED (`end_page: 0`). A run
of named pages (`--pages 1,3,400`) records only `end_read_page`: page 399 may
or may not hold cards.

An `end` run is a **good crawl** (see the liveness ledger).

## The census walk

```
<cli> crawl-lists --subcategory X --mode walk --max-pages 40 --min-pages 18 --plan
```

`--mode walk` (ADR-0017 §A, stride 1) reads pages 1, 2, 3, .... The list
handler enqueues the **next page only after it handles the current one**: no
page count is read (a windowed pager links only a few pages ahead), and no page
is prefetched.

| It stops at | Recorded | Exit |
|---|---|---|
| A positive END (the end marker, as above) | `list.status: end`, `end_method: end_marker`, `end_page` = the last page, exact | 0 |
| A clamp: more than half of a page's ids were already seen earlier in this walk | `end`, `end_method: clamped`, `end_page` = the last non-repeated page; the repeated page's cards are not kept | 0 |
| A page served at another URL (a redirect, e.g. to page 1) | `end_unknown`, none of its cards kept: a redirect is never an END or a clamp (the measured past-end page is a 404 with no redirect) | 14 |
| The signed ceiling, `--max-pages` list GETs (the END page included) | `list.status: ceiling_hit`, plus `list.gap` (`since_ref` null) | 0 |
| An END with `end_page` below the signed `--min-pages` | `short_census`; terminal, never resumed | 14 |
| A 0-card page with no END, a bare 404, a failed or robots-blocked page | as for every list read (above) | 14, 5 or 13 |

- **Signed.** `mode` and `min_pages` join the plan (`run_plan`) and a
  refresh's signed list set (`approvals.LIST_KEYS_IF_SET`) only when set, so a
  plan without them hashes as before. A walk also signs `policy_digest`
  (`policy.DEFAULT`): its outcome is judged by the good-crawl rules, which the
  digest covers, so a rule change after the approval is a different plan. The
  run doc records that digest (`list.policy_digest`); `crawl resume` refuses a
  walk whose recorded digest differs from today's (exit 10, before the gate,
  0 GETs), and a walk's resume plan binds it. Inside a refresh, a walk step
  that differs from its signed set only in its digest (or a frontier's order)
  is no approval at all: `NotApproved`, exit 10, nothing appended, never a
  bound breach. A refresh's bounds count exactly
  `max_pages` list GETs for a walk step (no `1 +` discovery allowance), and the
  operator's text says "a census walk".
- **Refused with it:** `--stride` other than 1 (the stride-k depth walk, H4, is
  deferred), `--pages`, `--page-count`, `--start`, `--seed`, and a
  `--min-pages` above `--max-pages`. `--min-pages` without `--mode walk` is
  refused.
- **Rows.** Each kept card has `sample_weight` 1 and `page_count` null: the
  run doc's `end_page` is the count.
- **State on the run doc**, saved after every page: `list{mode, stride,
  ceiling, min_pages, pages_read, settled}`. `settled` counts every list GET
  the walk SENDS, counted and saved just before the GET: a retry, a GET a
  challenge stops, a page a crash or a stop hands back. So the signed ceiling
  is the walk's GETs across every attempt, never more. The walk enqueues the
  next page only while `settled < ceiling`, and the GET that reaches the
  ceiling is never retried (its retry would be one past it).
- **Resume** continues from the next unread page: the page that stopped the
  run (a challenge hands it back; a failed page is queued again). It is bounded
  by what the signed ceiling leaves (`ceiling - settled`), never by the resume's
  own `--max-pages`, and its plan binds `mode`, `ceiling` and `settled`. A walk
  that already settled its ceiling is refused (exit 13). The clamp guard's
  memory is this crawl's own rows in DATA, by the lowest page each id was kept
  on: a resume still sees earlier ids, and a page read again (its rows were
  kept, its state was not saved) is never a repeat of itself.
- **Good crawls.** `end` and `ceiling_hit` are good: every page read is a true
  sighting (a `ceiling_hit` leaves the rest of the list unread, the GAP of
  ADR-0017 A.3, recorded by the frontier step). `short_census` and
  `end_unknown` are terminal stops. A clamp is an END by the walk's own
  evidence (A.1 names it an `end_method`), so it records `end`; the reserved
  `clamped` status is never written and stays out of the good set.
- **The lanes version is now `select_ads.v3`**, because the good-crawl set
  changed: the policy digest moves with it.

## The frontier walk

```
<cli> crawl-lists --subcategory X --mode frontier --max-pages 30 --plan
```

`--mode frontier` (ADR-0017 §A.2, §A.3) reads pages 1, 2, 3, ... exactly like
the census walk (one page ahead, the same END, clamp, ceiling, resume and
failure rules), and may also **stop early at a confirmed anchor**: the point
where today's list runs into what the reference crawl already read.

| It stops at | Recorded | Exit |
|---|---|---|
| A confirmed anchor (below), only when the source declares `order = "bump_desc"` | `list.status: anchored`, `list.anchor{page, confirm_page, ref_crawl_id}` | 0 |
| A positive END or a clamp | `end`, as for the census | 0 |
| The signed ceiling | `ceiling_hit`, plus `list.gap` | 0 |

- **Early stop is opt-in by the source.** Optional `Source.order`, default
  `unordered`. Only the exact value `bump_desc` allows an anchor; anything
  else walks to a positive END or the ceiling. That costs list GETs, never ad
  views. A plug-in declares `bump_desc` only behind an offline evidence test.
  The run doc records `list.order`.
- **The reference** (`plan.frontier_reference`, pure) is the latest
  crawl-lists run of the same site, subcategory and region that was read
  contiguously from page 1 (`mode` walk or frontier, stride 1), whose
  corrected status is `completed` and whose `list.status` is `anchored`, `end`
  or `ceiling_hit` (`plan.REFERENCE_STATUS`). `ok` (a sample or named pages) and
  every terminal stop are never a reference. Walks record `list.subcategory`
  and `list.region` so a later frontier can find them. The reference is chosen
  ONCE, when the run starts, and recorded as `list.ref_crawl_id` (null: none
  existed, a bootstrap). A resume keeps it, and its plan binds it.
- **Candidate** (`plan.frontier_stop`, pure): on the page just before the
  current one, the topmost window of K consecutive parsed cards (K =
  `SyncPolicy.frontier_k`, 5) whose ids hold **consecutive places** in the
  reference's parsed order (page, then position), none seen on an earlier page
  of this walk, whose last card sat on a reference page before the
  reference's last page read. That place is judged in the reference, not by
  the walk's page number: new bumps push the boundary deeper every day, often
  past the reference's own last page.
- **Confirmation:** the ONE next page. At least `confirm_share` (90%) of its
  parsed cards are reference ids below the candidate's last card, and they keep
  the reference order with at most `confirm_inversions` (5%) adjacent
  inversions. Otherwise the candidate is dropped and the walk goes on; the
  confirmation page is itself searched for the next candidate. The rule reads
  only this crawl's kept rows and the reference's, so a stop between the
  candidate page and its confirmation page resumes with no saved candidate.
  The confirmation page is a list GET under the ceiling, and its rows are kept.
- **Why each case cannot stop early** (tested): random re-bumps of known ads
  are almost never adjacent in the reference; a pinned block on page 1 is
  followed by today's new bumps, so the confirmation fails; a rotated block is
  followed by new ids, or by old content out of order (the inversion check);
  an ad pushed across a page boundary was seen earlier in this walk.
- **The GAP.** A walk that hits its ceiling records `list.gap{after_page,
  since_ref, opened_by}`: the bumps past its ceiling, back to its reference
  (`since_ref` null in a bootstrap, and for a census, which has no reference),
  were never read. A frontier whose reference still has an open GAP keeps that
  GAP's `since_ref` and `opened_by` (the oldest unread origin) and the smaller
  `after_page`, so a chain of ceiling-bound frontiers never shortens it. A
  later frontier that anchors on it carries the same `gap` on; only a census
  that read to its END closes it (as the reference, it has no gap). A walk
  that reads to its END records none. The crawl
  prints a `GAP:` line, the run card names an open GAP of the reference, and
  the refresh summary prints `GAP after page N (since reference R)`.
- **Rows** carry `sample_weight` null and `page_count` null: a frontier is
  neither a census nor a sample of the list.
- **Signed.** `mode: frontier`, `k`, `order` (the source's `Source.order`
  at plan time) and `policy_digest` join the plan and a refresh's list set
  (`approvals.LIST_KEYS_IF_SET`, only when set). A plug-in that switches its
  order after the approval is a different plan (exit 10); a resume reads the
  order recorded at the walk's start, and its plan binds it. The
  confirmation thresholds ride in the digest. `--min-pages` is a census guard
  and is refused with a frontier, as are `--stride` other than 1, `--pages`,
  `--page-count`, `--start` and `--seed`.
- **Good crawls.** `anchored` is good (liveness.Rules, unchanged), and a
  refresh's list postcondition passes it, as it passes a frontier's `end` and
  `ceiling_hit` (see "Refresh postconditions").
- **The lanes version is now `select_ads.v4`** (the anchor and reference rules
  are pinned rule code in `plan.py`); the policy digest moves with it and with
  the three new `SyncPolicy` fields.

## Known gaps in slice 1

- **A held ad has no clear path yet.** `reparse` walks only listings that have
  a snapshot. A snapshot that never became a listing is not reached, and there
  is no operator clear. So a held ad gets no further views.
- **A walk with a robots-blocked list page can never complete.** Every resume
  ends with exit 13 again. Plan a new crawl instead.
- **A policy mismatch inside a refresh cannot be resumed.** It exits 10, which
  the refresh treats as resumable, but no resume can pass. Plan a new refresh.
- **A refresh-level exit 14 opens no incident.** When a list step fails its
  postcondition, the refresh records exit 14 and spends the approval, but only
  a step's own exit 14 opens a `parse` incident.
- **The refresh's plan card cannot show the ad selection.** Inside a refresh,
  freshness depends on the list steps that have not run yet. The selection
  appears in the end summary and on the fetch-ads step's own run card.
- **The stride-k depth walk is not built yet.** The census and frontier walks
  are (above). The page-count path stays, but it now fails loudly.
- **The end-marker precondition checks a declaration, not a test.** `refresh`
  refuses a walk step whose source declares no `end_selectors`. It cannot see
  whether the plug-in tests them on a saved past-end page.
- **The frontier's reference is chosen at run time, not signed.** It is
  recorded on the run doc and bound by a resume's plan. It only decides where
  the walk may stop early, inside the signed ceiling, so it can save list GETs
  but never add one. `Source.order` is signed (above), but the rest of the
  plug-in (its selectors, its parser) is not: no `source_digest` yet.
- **Run docs created in the same second tie on `created_at`.** The reference
  is then the higher `run_id`: deterministic, not necessarily the later run.
- **A census walk has no parser-drift ratio guard yet** (ADR-0017 A.4,
  skipped/cards > 0.5). A broken card selector still stops it at once: 0 cards
  with no END is exit 14.
