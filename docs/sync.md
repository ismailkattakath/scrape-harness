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
`list.status` is `ok` (`liveness.good_crawl`, an allowlist). So the rows a
crawl kept before a stop never count as sightings: not after `parse_drift`,
`failed`, `zero_yield` or `blocked`, and not from a run that is `stopped`,
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
  - its `list.status` is `ok`;
  - `observations_written` is above 0.
- Anything else is recorded as **exit 14**. That is terminal: the approval is
  spent, and fetch-ads never plans from that list read.
- The refresh's end summary shows:
  - each crawl-lists step's `list{}`;
  - each fetch-ads step's selection: eligible, its new and revisit lanes,
    excluded, and its GET, ok and gone counts.

  It reads these from the run docs. A step that stopped prints no run id, so
  the summary finds its run doc through `approval_ref`. A fetch-ads step that
  planned nothing has no run doc, so it prints its `ads{}` on its own stdout
  line instead.
- An offline backend spends no approval, so its run doc names no refresh. Its
  list step therefore fails the postcondition, the same way fetch-ads would
  find no fresh sighting from it.

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
- **List walk modes are not built yet.** These are census to a positive end,
  frontier walk to a confirmed anchor, and gallop. The page-count path stays,
  but it now fails loudly.
