"""Pure planning functions: which list pages to read, which ad pages to (re)fetch. No I/O here,
so every rule is unit-tested offline."""

from __future__ import annotations

import random
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


def fetch_reason(latest_listing: dict | None, sightings: list[dict], now: datetime) -> str | None:
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
    if after and now - fetched > STALE_AFTER:
        return "stale_7d"
    return None


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
