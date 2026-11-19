"""SyncPolicy: every tunable that decides WHICH real ads get a view, in one frozen value with one digest.

The digest is signed: a standalone fetch-ads plan carries it (so its plan_hash moves with the policy), and a
refresh's bounds carry the fetch-ads step's digest, which `approvals.consume_bounded` compares before anything
else (a mismatch is NotApproved, exit 10, with nothing appended). A policy change after an approval therefore
never runs under the old signature.

Fields (judgement values; changing one is an operator decision):
  window_h       a sighting is FRESH within this many hours of the plan's as_of; older ones are dormant (0 GETs,
                 nothing deleted). 72.
  rho            the revisit lane's share of the budget B, a HARD cap: at most ceil(rho * B) revisits, and
                 unused new-lane quota never flows into revisits (unused revisit quota does flow to new). 0.2.
  stale_after_d  a stored copy older than this, of an ad still listed, is due a revisit (stale_7d). 7.
  readmit_h      a list sighting re-admits a tombstoned ad only this long after the gone (liveness). 24.
  breaker_*      the gone-storm and ad-failure breaker (p0, alpha, floor). Signed now; no breaker reads them yet.
  lanes          the version of select_ads' lane and order rules, so a rule change is a policy change."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import timedelta
from fractions import Fraction


@dataclass(frozen=True)
class SyncPolicy:
    window_h: int = 72
    rho: float = 0.2
    stale_after_d: int = 7
    readmit_h: int = 24
    breaker_p0: float = 0.10
    breaker_alpha: float = 1e-4
    breaker_floor: int = 5
    lanes: str = "select_ads.v1"

    def __post_init__(self) -> None:
        if not 0 <= self.rho <= 1:
            raise ValueError(f"rho must be in [0, 1], not {self.rho}")
        if self.window_h <= 0 or self.stale_after_d < 0 or self.readmit_h < 0:
            raise ValueError("window_h must be > 0; stale_after_d and readmit_h >= 0")

    @property
    def window(self) -> timedelta:
        return timedelta(hours=self.window_h)

    @property
    def stale_after(self) -> timedelta:
        return timedelta(days=self.stale_after_d)

    def revisit_cap(self, budget: int) -> int:
        """ceil(rho * B) in exact arithmetic: in floats ceil(0.2 * 300) is 61, one view too many."""
        return math.ceil(Fraction(str(self.rho)) * max(budget, 0))

    def rules(self):
        """The liveness ledger's rules under this policy (one copy of readmit_h)."""
        from . import liveness

        return dataclasses.replace(liveness.DEFAULT, readmit_after=timedelta(hours=self.readmit_h))

    def doc(self) -> dict:
        return dataclasses.asdict(self)

    def digest(self) -> str:
        text = json.dumps(self.doc(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


DEFAULT = SyncPolicy()


def from_args(ns) -> SyncPolicy:
    """The policy a fetch-ads invocation runs under: its --window-h / --rho / --stale-after-d over DEFAULT.
    The ONE constructor for the CLI and for `refresh.build_bounds`, so both sign the same digest."""
    try:
        return dataclasses.replace(
            DEFAULT,
            window_h=DEFAULT.window_h if getattr(ns, "window_h", None) is None else ns.window_h,
            rho=DEFAULT.rho if getattr(ns, "rho", None) is None else ns.rho,
            stale_after_d=(
                DEFAULT.stale_after_d if getattr(ns, "stale_after_d", None) is None else ns.stale_after_d
            ),
        )
    except ValueError as e:
        raise SystemExit(f"fetch-ads: {e}") from None
