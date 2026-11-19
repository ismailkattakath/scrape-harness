"""The gone-storm and ad-failure breaker (ADR-0017 §C.5), and the per-site latch it sets. Pure: the caller
passes the records in; nothing here reads a file or the clock.

Trip rule (signed in SyncPolicy: breaker_p0, breaker_alpha, breaker_floor). After each ad page that answered,
n is the run's answered ad pages and g a counter of one kind; the breaker trips when
    g >= floor  and  P(X >= g | X ~ Binomial(n, p0)) < alpha,
computed in exact integer arithmetic (a float tail rounds near alpha). Two counters, the same rule:
  gone     gone events whose reason rests on ABSENCE or a redirect (served_list, redirected_to_list, a bare
           http_404, or any reason not known to be positive). A positive marker (`POSITIVE`: the site's own
           expired notice, a 410) never counts: it is the site's statement, so a real expiry wave cannot trip.
  failure  parse_failed: an ad page that answered but could not be parsed or validated (a parser break).
With the defaults (p0 0.10, alpha 1e-4, floor 5) the threshold is g = 5 at n = 5 (a full storm trips at GET 5),
9 at n = 20, 24 at n = 100 and 52 at n = 300; the run-level false-trip rate over 300 GETs at a true gone rate
of 0.04 is about 1e-6 (`false_trip`, an exact DP).

Latch: a run doc whose `breaker.tripped` is true latches its site until a `breaker` run_correction with
`clear: true` names that run (`<cli> breaker clear --approve-operator`). Derived from DATA (run docs and the
event log), so there is no second state file; the trip is saved on the run doc FIRST, so the latch holds even
when nothing else about the trip could be written."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from fractions import Fraction
from functools import lru_cache

POSITIVE = ("expired_notice", "http_410")  # gone reasons that rest on the site's own statement


def weak(reason: str | None) -> bool:
    """A gone reason that rests on absence or a redirect: it counts toward the gone breaker."""
    return reason not in POSITIVE


@lru_cache(maxsize=4096)
def _threshold(n: int, p0: str, alpha: str, floor: int) -> int | None:
    p, a = Fraction(p0), Fraction(alpha)
    num, den = p.numerator, p.denominator
    # tail(g) * den**n = sum_{k >= g} C(n, k) num**k (den - num)**(n - k): integers only
    bound = a * den**n
    tail = 0
    best = None
    for k in range(n, max(floor, 0) - 1, -1):  # the tail only grows as g falls: stop at the first miss
        tail += math.comb(n, k) * num**k * (den - num) ** (n - k)
        if tail >= bound:
            break
        best = k
    return best


def threshold(n: int, pol) -> int | None:
    """The least g that trips after n answered ad pages (None: nothing can trip yet). Non-decreasing in n."""
    return _threshold(n, str(pol.breaker_p0), str(pol.breaker_alpha), pol.breaker_floor)


def trips(g: int, n: int, pol) -> bool:
    t = threshold(n, pol)
    return t is not None and g >= t


def false_trip(p: float, n_max: int, pol) -> float:
    """P(the breaker ever trips within n_max answered ad pages) when each is independently a counted event at
    rate p: an exact dynamic programme over the shipped thresholds (the ADR's table)."""
    alive = {0: 1.0}  # g -> probability of being at g, not yet tripped
    tripped = 0.0
    for n in range(1, n_max + 1):
        t = threshold(n, pol)
        nxt: dict[int, float] = {}
        for g, pr in alive.items():
            nxt[g] = nxt.get(g, 0.0) + pr * (1 - p)
            if t is not None and g + 1 >= t:
                tripped += pr * p
            else:
                nxt[g + 1] = nxt.get(g + 1, 0.0) + pr * p
        alive = nxt
    return tripped


class Breaker:
    """One run attempt's counters. `settle` after each ad page that answered; it returns the run.v1
    `breaker` object (without `at`, the caller's clock) the first time a counter trips, else None."""

    def __init__(self, pol):
        self.pol = pol
        self.n = 0
        self.g = {"gone": 0, "failure": 0}
        self.tripped: dict | None = None

    def settle(self, outcome: str, reason: str | None = None) -> dict | None:
        """outcome: ok | gone | failed (parse_failed)."""
        if self.tripped:
            return None
        self.n += 1
        kind = "gone" if outcome == "gone" and weak(reason) else "failure" if outcome == "failed" else None
        if kind is None:
            return None
        self.g[kind] += 1
        if not trips(self.g[kind], self.n, self.pol):
            return None
        self.tripped = {
            "tripped": True,
            "kind": kind,
            "g": self.g[kind],
            "n": self.n,
            "p0": self.pol.breaker_p0,
            "alpha": self.pol.breaker_alpha,
        }
        return self.tripped


def latched(site: str, runs: Iterable[Mapping], events: Iterable[dict]) -> list[str]:
    """The run ids that latch `site`: a tripped breaker with no `breaker` clear correction since. Sorted."""
    from . import corrections

    cleared = corrections.latest(events, "breaker")
    return sorted(
        d["run_id"]
        for d in runs
        if d.get("source") == site
        and (d.get("breaker") or {}).get("tripped")
        and not (cleared.get(d["run_id"]) or {}).get("clear")
    )
