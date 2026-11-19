"""The liveness ledger: one state per ad, derived from the append-only records. There is no state file: the
same observations, listings, events and run docs always give the same answer (ADR-0009: DATA holds facts,
nothing holds a second copy of them).

Pure: the caller passes the records in; nothing here reads a file or the clock, and every output is sorted
by (site, ad_id).

States:
  alive        none of the below.
  tombstoned   a gone event at or after the ad's latest live sighting. The EARLIEST such gone is the tombstone,
               so repeated gone events make one. An OK fetch (a non-reparse listing) after it clears it. A list
               sighting clears it only as a RE-ADMISSION: at least `readmit_after` after the gone, and only
               the ad's first one. A second tombstone is final until an OK fetch or an operator void, so a
               site that keeps showing a dead card costs at most one extra GET per ad.
  quarantined  the ad's LATEST gone event belongs to a run whose gone events a correction quarantined (a breaker
               trip, which quarantines only the run's weak gones: `corrections.gone_effect`): doubtful, so it makes no tombstone and clears none, and the ad is not eligible either.
               An OK fetch clears it; a list sighting does not (the operator resolves the run).
  held         a parse_failed event after the ad's latest live sighting before it, with no listing written
               since that sighting (a later fetch, or a `reparse` of the saved snapshot, which carries the
               snapshot's own fetched_at). A later LIST sighting does not clear it: else the next list crawl
               re-plans the ad and a broken ad parser costs one view per ad per day.
  Precedence when several hold: tombstoned, quarantined, held.

Live sightings: a list card (an observation's seen_at) from a GOOD crawl, and an OK fetch (a listing's
fetched_at; a `reparse` is offline and proves nothing about the site). A crawl is good when its corrected
status and its `list.status` are in the good sets (`Rules`, `good_crawl`). A crawl with no run doc (a legacy or
free-form crawl_id, or none), or a completed one from before list.status, cannot be judged and counts as good.

Gone and parse_failed events are keyed by (site, ad_id). Events without an ad_id (written before the field)
join by url: an observation's `ad_url` or a listing's `source.url`. A url that matches no ad, or more than
one, is skipped: the ledger never guesses. The join is `adkey.event_key`, shared with plan.assert_admissible."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from scrape_harness import adkey, corrections

STATES = ("alive", "tombstoned", "quarantined", "held")


@dataclass(frozen=True)
class Rules:
    """The ledger's tunables. Judgement values; a later signed policy supplies them."""

    readmit_after: timedelta = timedelta(hours=24)
    # A GOOD crawl (ADR-0017 §B.2): corrected status in good_status AND list.status in good_list_status. An
    # allowlist: every stop value (zero_yield, parse_drift, failed, blocked, end_unknown, short_census) and
    # every unfinished run (stopped, running, aborted) is out, so rows persisted before a Stop never count as
    # sightings (§B.1); so are the evidence runs (past_end, redirected). The passing values: ok (a sample or
    # named pages read), end (a positive END: the end marker, or a walk's clamp), ceiling_hit (a walk read to
    # its signed ceiling: every page it read is a true sighting, the GAP lies past it) and anchored (the
    # frontier walk, reserved). `clamped` is reserved and stays out (a clamp records end, end_method clamped).
    good_status: frozenset = frozenset({"completed"})
    good_list_status: frozenset = frozenset({"ok", "end", "ceiling_hit", "anchored"})


DEFAULT = Rules()


@dataclass(frozen=True)
class Ad:
    site: str
    ad_id: str
    status: str
    first_seen: str | None  # first and last LIVE sighting (good list card or OK fetch)
    last_seen: str | None
    n_sightings: int
    gone_at: str | None  # the tombstone (or, when quarantined, the doubtful gone)
    gone_reason: str | None
    gone_run: str | None
    readmitted: int  # list re-admissions used (0 or 1)
    held_since: str | None  # the parse_failed event that holds it


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class _Acc:
    seen: list = field(default_factory=list)  # (dt, at) good list sightings
    fetched: list = field(default_factory=list)  # (dt, at) OK fetches
    reparsed: list = field(default_factory=list)  # (dt, at) reparse listings
    gone: list = field(default_factory=list)  # (dt, event, quarantined)
    failed: list = field(default_factory=list)  # (dt, event)


def _fold(site: str, ad_id: str, a: _Acc, rules: Rules) -> Ad:
    # One chronological pass. On a tie a sighting comes before a gone ("at or after" the sighting kills it).
    items = [(dt, 0, "fetch", at) for dt, at in a.fetched]
    items += [(dt, 0, "seen", at) for dt, at in a.seen]
    items += [(dt, 1, "gone", e) for dt, e, q in a.gone if not q]
    items += [(dt, 1, "quarantine", e) for dt, e, q in a.gone if q]
    items.sort(
        key=lambda x: (x[0], x[1], x[2], x[3] if isinstance(x[3], str) else (x[3]["run_id"], x[3]["at"]))
    )
    tomb = quar = None
    readmitted = 0
    for dt, _, kind, x in items:
        if kind == "fetch":  # an OK fetch: the ad is there, whatever the gone events said
            tomb = quar = None
        elif kind == "seen":
            if tomb is not None and readmitted == 0 and dt - tomb[0] >= rules.readmit_after:
                tomb, readmitted = None, 1
        elif kind == "gone":
            quar = None  # the latest gone is a real one again
            if tomb is None:
                tomb = (dt, x)
        else:  # a quarantined gone: no tombstone, and it clears none; it outlives a later re-admission
            quar = (dt, x)
    live = sorted(a.seen + a.fetched)
    held = None
    if a.failed:
        p_dt, p = max(a.failed, key=lambda f: (f[0], f[1]["run_id"]))
        before = [dt for dt, _ in live if dt < p_dt]
        anchor = max(before) if before else None
        written = [dt for dt, _ in a.fetched + a.reparsed]
        if not any(anchor is None or dt > anchor for dt in written):
            held = p
    marker = tomb or quar
    status = "tombstoned" if tomb else "quarantined" if quar else "held" if held else "alive"
    return Ad(
        site=site,
        ad_id=ad_id,
        status=status,
        first_seen=live[0][1] if live else None,
        last_seen=live[-1][1] if live else None,
        n_sightings=len(live),
        gone_at=marker[1]["at"] if marker else None,
        gone_reason=marker[1].get("reason") if marker else None,
        gone_run=marker[1].get("run_id") if marker else None,
        readmitted=readmitted,
        held_since=held["at"] if held else None,
    )


class Ledger:
    def __init__(self, ads: dict[tuple, Ad]):
        self.ads = dict(sorted(ads.items()))

    def ad(self, site: str, ad_id: str) -> Ad | None:
        return self.ads.get((site, ad_id))

    def status(self, site: str, ad_id: str) -> str | None:
        """alive | tombstoned | quarantined | held, or None for an ad the records never mention."""
        a = self.ad(site, ad_id)
        return a.status if a else None

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(STATES, 0)
        for a in self.ads.values():
            out[a.status] += 1
        out["readmitted"] = sum(a.readmitted for a in self.ads.values())
        return out

    def spans(self) -> list[dict]:
        """ONE row per (site, ad_id), sorted: first/last live sighting, the gone record, the state."""
        return [
            {
                "site": a.site,
                "ad_id": a.ad_id,
                "first_seen": a.first_seen,
                "last_seen": a.last_seen,
                "n_sightings": a.n_sightings,
                "gone_at": a.gone_at,
                "gone_reason": a.gone_reason,
                "gone_run": a.gone_run,
                "readmitted": a.readmitted,
                "held_since": a.held_since,
                "status": a.status,
            }
            for a in self.ads.values()
        ]


def good_crawl(runs: Iterable[Mapping], events: Iterable[dict], rules: Rules = DEFAULT, legacy: bool = True):
    """crawl_id -> whether its list sightings count (the ONE rule; the planner's gate and the refresh's list
    postcondition read it too). Good: its corrected status is in `rules.good_status` and its list.status in
    `rules.good_list_status`. With `legacy` (the ledger and the standalone gate), what cannot be judged counts
    as good: a crawl_id with no run doc, or none at all, and a completed crawl from before list.status. Without
    it (a refresh's own steps, which always record list.status), only a judged pass counts."""
    fixed = corrections.apply(runs, events)

    def good(crawl_id) -> bool:
        r = fixed.get(crawl_id) if crawl_id else None
        if r is None:
            return legacy
        if r["status"] not in rules.good_status:
            return False
        return r["list_status"] in rules.good_list_status or (legacy and r["list_status"] is None)

    return good


def build(
    observations: Iterable[dict],
    listings: Iterable[dict],
    events: Iterable[dict],
    runs: Iterable[Mapping],
    rules: Rules = DEFAULT,
) -> Ledger:
    """The ledger of every ad the records mention. `events` in FILE order (the corrections' order counts);
    the other inputs in any order."""
    observations, listings, events = list(observations), list(listings), list(events)
    runs = list(runs)
    fixed = corrections.apply(runs, events)
    good = good_crawl(runs, events, rules)
    accs: dict[tuple, _Acc] = {}

    def acc(k: tuple) -> _Acc:
        return accs.setdefault(k, _Acc())

    for o in observations:
        if good(o.get("crawl_id")):
            acc((o["site"], o["ad_id"])).seen.append((_t(o["seen_at"]), o["seen_at"]))
    for r in listings:
        bucket = (
            acc(adkey.listing_key(r)).reparsed
            if r.get("fetch_reason") == "reparse"
            else acc(adkey.listing_key(r)).fetched
        )
        bucket.append((_t(r["fetched_at"]), r["fetched_at"]))
    idx = adkey.url_index(observations, listings)
    for e in events:
        if e.get("kind") not in ("gone", "parse_failed"):
            continue
        k = adkey.event_key(e, idx)
        if k is None:
            continue
        if e["kind"] == "parse_failed":
            acc(k).failed.append((_t(e["at"]), e))
            continue
        mode = corrections.gone_effect(fixed.get(e.get("run_id")), e.get("reason"))
        if mode != "void":
            acc(k).gone.append((_t(e["at"]), e, mode == "quarantine"))
    return Ledger({k: _fold(k[0], k[1], a, rules) for k, a in accs.items()})
