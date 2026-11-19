"""Pure planning functions: which list pages to read, which ad pages to (re)fetch. No I/O here,
so every rule is unit-tested offline."""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, timedelta

STALE_AFTER = timedelta(days=7)


def systematic_pages(
    page_count: int, stride: int, start: int | None = None, seed: int | None = None
) -> tuple[int, list[int]]:
    """Every `stride`-th page from a random start in [1, stride]: a systematic sample that spreads
    evenly over list DEPTH (Cochran 1977, ch. 8). stride=1 is a census. Returns (start, pages)."""
    if stride < 1 or page_count < 1:
        raise ValueError("stride and page_count must be >= 1")
    if start is None:
        start = random.Random(seed).randint(1, min(stride, page_count))
    if not 1 <= start <= min(stride, page_count):
        raise ValueError(f"start must be in [1, {min(stride, page_count)}]")
    return start, list(range(start, page_count + 1, stride))


def sample_weight(page_count: int, pages: list[int]) -> float:
    """Inverse sampling fraction of the stratum, e.g. 530 pages / 36 sampled."""
    return round(page_count / len(pages), 6)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


ts = _t  # public: parse a record's ISO-8601 UTC time


def fetch_reason(
    latest_listing: dict | None, sightings: list[dict], now: datetime, stale_after: timedelta = STALE_AFTER
) -> str | None:
    """Why this ad page should be GET now, or None. Each GET adds a view to a real person's ad
    counter, so the default is NOT to fetch:
      new          never fetched;
      card_changed the card hash seen after the last fetch differs from the one seen before it;
      stale_7d     the stored version is >7 days old and the ad is still being listed."""
    if not sightings:
        return None
    if latest_listing is None:
        return "new"
    fetched = _t(latest_listing["fetched_at"])
    seen = sorted(sightings, key=lambda o: o["seen_at"])
    before = [o for o in seen if _t(o["seen_at"]) <= fetched]
    after = [o for o in seen if _t(o["seen_at"]) > fetched]
    if before and after and after[-1]["card_hash"] != before[-1]["card_hash"]:
        return "card_changed"
    if after and now - fetched > stale_after:
        return "stale_7d"
    return None


# ── the fresh-sighting gate and the ad selector (ADR-0017 §B) ─────────────────────────────────────

EXCLUDED = ("tombstoned", "dormant", "quarantined", "held")
REVISIT = ("card_changed", "stale_7d")


def eligible(
    sightings: dict[tuple, list[dict]],
    latest: dict[tuple, dict],
    status: Callable[[tuple], str | None],
    fresh: Callable[[dict], bool],
    as_of: datetime,
    policy,
) -> tuple[list[dict], dict[str, int]]:
    """(candidates, excluded): the ads a fetch may plan, and why the others the refetch rule names may not.

    sightings: (site, ad_id) -> its GOOD, non-sponsored list sightings; latest: -> its latest listing;
    status: -> its liveness state; fresh: whether a sighting is fresh (the caller's clock rule).
    An ad is a candidate when the refetch rule names it at `as_of` (never the wall clock), its state is
    alive, and it has a fresh sighting; the candidate carries its LATEST fresh sighting. An ad the rule
    names that fails is counted once, by the first that applies: its liveness state, else `dormant` (only
    old sightings: 0 GETs, nothing deleted). An ad the rule does not name is neither."""
    out, excluded = [], dict.fromkeys(EXCLUDED, 0)
    for key in sorted(sightings):
        seen = sightings[key]
        reason = fetch_reason(latest.get(key), seen, as_of, policy.stale_after)
        if reason is None:
            continue
        state = status(key) or "alive"
        if state != "alive":
            excluded[state] += 1
            continue
        now_seen = [o for o in seen if fresh(o)]
        if not now_seen:
            excluded["dormant"] += 1
            continue
        lst = latest.get(key)
        out.append(
            {
                "reason": reason,
                "obs": max(now_seen, key=lambda o: (o["seen_at"], o.get("crawl_id") or "")),
                "fetched_at": lst["fetched_at"] if lst else None,
            }
        )
    return out, excluded


def _ad_key(o: dict) -> tuple:
    return (o["site"], o["ad_id"])


def select_ads(cands: list[dict], budget: int, policy) -> tuple[list[tuple[str, dict]], dict[str, int]]:
    """([(reason, observation)], lanes): at most `budget` ads, in request order. Pure and deterministic.

    revisit lane (card_changed, stale_7d): q = min(n_revisit, ceil(rho * B)), a HARD cap; card_changed first,
      then the oldest fetch first, then (site, ad_id).
    new lane: B - q. Split equally across subcategories (`_equal`: a subcategory short of its share gives the
      rest to the others); inside one, the latest crawl first (by its latest sighting), then page, position,
      (site, ad_id). Unused revisit quota flows to new by construction; unused new quota never goes back.
    B is a ceiling: an empty pool plans nothing. New picks come first in the request order (today's bumps),
    then the revisits."""
    budget = max(budget, 0)
    rev = [c for c in cands if c["reason"] in REVISIT]
    new = [c for c in cands if c["reason"] not in REVISIT]
    rev.sort(key=lambda c: (c["reason"] != "card_changed", c["fetched_at"] or "", _ad_key(c["obs"])))
    rev = rev[: min(len(rev), policy.revisit_cap(budget))]
    crawl_at: dict = {}
    for c in new:
        o = c["obs"]
        k = o.get("crawl_id") or ("", o["seen_at"])
        crawl_at[k] = max(crawl_at.get(k, ""), o["seen_at"])

    def new_key(c: dict) -> tuple:
        o = c["obs"]
        latest = crawl_at[o.get("crawl_id") or ("", o["seen_at"])]
        lst = o.get("list") or {}
        return (-_t(latest).timestamp(), lst.get("page") or 0, lst.get("position") or 0, _ad_key(o))

    by_sub: dict[str, list[dict]] = {}
    for c in sorted(new, key=new_key):
        by_sub.setdefault((c["obs"].get("list") or {}).get("subcategory") or "unknown", []).append(c)
    alloc = _equal(budget - len(rev), {s: len(v) for s, v in by_sub.items()})
    new = sorted((c for s, v in by_sub.items() for c in v[: alloc[s]]), key=new_key)
    picks = [(c["reason"], c["obs"]) for c in new + rev]
    return picks, {"new": len(new), "revisit": len(rev)}


# ── depth-stratified ad sampling (H4) ───────────────────────────────────────────────────────


def depth_decile(obs: dict) -> int | None:
    """0 = the first tenth of the list, 9 = the deepest tenth; None when the page count is unknown."""
    page, count = obs["list"].get("page"), obs["list"].get("page_count")
    if not page or not count:
        return None
    return min(9, int(10 * (page - 1) / count))


def _largest_remainder(total: int, sizes: dict) -> dict:
    """Split `total` across keys proportionally to `sizes` (the largest-remainder method), never above a size."""
    pop = sum(sizes.values())
    if pop == 0 or total <= 0:
        return {k: 0 for k in sizes}
    total = min(total, pop)
    quota = {k: total * v / pop for k, v in sizes.items()}
    alloc = {k: min(int(q), sizes[k]) for k, q in quota.items()}
    order = sorted(sizes, key=lambda k: (-(quota[k] - int(quota[k])), str(k)))
    while sum(alloc.values()) < total:
        for k in order:
            if sum(alloc.values()) >= total:
                break
            if alloc[k] < sizes[k]:
                alloc[k] += 1
    return alloc


def _equal(total: int, sizes: dict) -> dict:
    """Equal shares across keys; a key short of its share gives the rest back to the others."""
    alloc = {k: 0 for k in sizes}
    left, open_ = min(total, sum(sizes.values())), sorted(k for k in sizes if sizes[k] > 0)
    while left > 0 and open_:
        share, extra = divmod(left, len(open_))
        for i, k in enumerate(open_):
            take = min(share + (1 if i < extra else 0), sizes[k] - alloc[k])
            alloc[k] += take
            left -= take
        open_ = [k for k in open_ if alloc[k] < sizes[k]]
    return alloc


def stratified_sample(
    cands: list[tuple[str, dict]],
    n: int,
    seed: int,
    allocation: str = "equal",
    subcategories: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Pick `n` of the refetch-rule candidates, stratified by (subcategory, depth decile).

    Across subcategories: `equal` (the comparison design: Cochran 1977 ch. 5) or `proportional`.
    Within a subcategory: proportional across deciles (largest remainder). Inside a stratum: a seeded
    simple random sample. Returns (picks, strata); each pick carries its stratum and design weight
    N_h / n_h (candidates in the stratum / sampled from it). Deterministic for a given seed and input."""
    if allocation not in ("equal", "proportional"):
        raise ValueError("allocation must be equal or proportional")
    rng = random.Random(seed)
    strata: dict[tuple, list] = {}
    for reason, o in sorted(cands, key=lambda t: (t[1]["ad_url"], t[0])):
        sub = o["list"].get("subcategory") or "unknown"
        if subcategories and sub not in subcategories:
            continue
        strata.setdefault((sub, depth_decile(o)), []).append((reason, o))
    by_sub: dict[str, dict] = {}
    for (sub, dec), members in strata.items():
        by_sub.setdefault(sub, {})[dec] = len(members)
    sub_sizes = {s: sum(d.values()) for s, d in by_sub.items()}
    sub_alloc = (_equal if allocation == "equal" else _largest_remainder)(n, sub_sizes)
    picks, table = [], []
    for sub in sorted(by_sub):
        dec_alloc = _largest_remainder(sub_alloc[sub], by_sub[sub])
        for dec in sorted(by_sub[sub], key=lambda d: (d is None, d)):
            members, k = strata[(sub, dec)], dec_alloc[dec]
            chosen = rng.sample(members, k) if k else []
            weight = round(len(members) / k, 6) if k else None
            table.append(
                {
                    "subcategory": sub,
                    "decile": dec,
                    "population": len(members),
                    "sampled": k,
                    "weight": weight,
                }
            )
            for reason, o in sorted(chosen, key=lambda t: t[1]["ad_url"]):
                picks.append(
                    {"reason": reason, "obs": o, "subcategory": sub, "decile": dec, "weight": weight}
                )
    return picks, table
