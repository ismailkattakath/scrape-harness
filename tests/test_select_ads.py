"""The fresh-sighting gate (plan.eligible) and the ad selector (plan.select_ads): pure functions, so every
rule is checked offline, with Hypothesis for the budget invariants. Every record here is SYNTHETIC."""

import dataclasses
import random
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scrape_harness import plan, policy
from scrape_harness.policy import SyncPolicy

SITE = "example.invalid"
AS_OF = datetime(2030, 1, 10, 12, tzinfo=UTC)
P = policy.DEFAULT


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def obs(ad, seen=AS_OF, sub="widgets", page=1, position=1, crawl="LIST1", card="h"):
    return {"site": SITE, "ad_id": str(ad), "ad_url": f"https://example.invalid/ads/item-{ad}",
            "seen_at": ts(seen), "crawl_id": crawl, "card_hash": card,
            "list": {"subcategory": sub, "page": page, "position": position}}  # fmt: skip


def cand(ad, reason="new", fetched=None, **kw):
    return {"reason": reason, "obs": obs(ad, **kw), "fetched_at": ts(fetched) if fetched else None}


def ids(picks):
    return [o["ad_id"] for _, o in picks]


# ── SyncPolicy: one frozen set of tunables, one stable digest ──────────────────────────────────────


def test_policy_defaults_are_the_operator_decisions():
    assert (P.window_h, P.rho, P.stale_after_d, P.readmit_h) == (72, 0.2, 7, 24)
    assert P.rules().readmit_after == timedelta(hours=24)
    with pytest.raises(dataclasses.FrozenInstanceError):
        P.rho = 0.5  # type: ignore[misc]


def test_policy_digest_is_stable_and_canonical():
    assert P.digest() == SyncPolicy().digest() and P.digest().startswith("sha256:")
    assert len(P.digest()) == len("sha256:") + 64


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(SyncPolicy)])
def test_every_policy_field_changes_the_digest(field):
    v = getattr(P, field)
    other = v + "x" if isinstance(v, str) else v + 1 if isinstance(v, int) else v / 2
    assert dataclasses.replace(P, **{field: other}).digest() != P.digest()


@pytest.mark.parametrize(
    "rho, budget, cap",
    [(0.2, 300, 60), (0.07, 100, 7), (0.1, 30, 3), (0.2, 1, 1), (0.2, 0, 0), (0.3, 10, 3), (0, 300, 0)],
)
def test_revisit_cap_is_exact_ceil(rho, budget, cap):
    # float math says ceil(0.07 * 100) = 8 (0.07 * 100 is 7.000000000000001); the cap must not drift up by one
    assert dataclasses.replace(P, rho=rho).revisit_cap(budget) == cap


def test_policy_refuses_nonsense():
    for bad in ({"rho": 1.5}, {"rho": -0.1}, {"window_h": 0}, {"stale_after_d": -1}):
        with pytest.raises(ValueError):
            SyncPolicy(**bad)


# ── eligibility: alive, freshly sighted, due ─────────────────────────────────────────────────────


def gate(sightings, status=None, latest=None, window=None):
    window = window or timedelta(hours=P.window_h)
    by_ad = {}
    for o in sightings:
        by_ad.setdefault((o["site"], o["ad_id"]), []).append(o)
    return plan.eligible(
        by_ad,
        latest or {},
        lambda k: (status or {}).get(k[1], "alive"),
        lambda o: plan.ts(o["seen_at"]) >= AS_OF - window,
        AS_OF,
        P,
    )


def test_only_old_sightings_are_dormant_and_never_eligible():
    old = [obs(i, seen=AS_OF - timedelta(days=12)) for i in range(50)]
    cands, excluded = gate(old)
    assert cands == [] and excluded["dormant"] == 50
    picks, lanes = plan.select_ads(cands, 300, P)
    assert picks == [] and lanes == {"new": 0, "revisit": 0}  # B is a ceiling: an empty pool sends 0


def test_tombstoned_quarantined_held_are_excluded_and_counted():
    sightings = [obs(i) for i in range(5)]
    status = {"1": "tombstoned", "2": "quarantined", "3": "held"}
    cands, excluded = gate(sightings, status)
    assert sorted(c["obs"]["ad_id"] for c in cands) == ["0", "4"]
    assert excluded == {"tombstoned": 1, "quarantined": 1, "held": 1, "dormant": 0}


def test_a_state_outranks_dormant_and_a_fresh_sighting_is_the_one_kept():
    sightings = [obs(1, seen=AS_OF - timedelta(days=9)), obs(2, seen=AS_OF - timedelta(days=9)),
                 obs(2, seen=AS_OF - timedelta(hours=1), page=3)]  # fmt: skip
    cands, excluded = gate(sightings, {"1": "tombstoned"})
    assert excluded["tombstoned"] == 1 and excluded["dormant"] == 0
    (c,) = cands
    assert c["obs"]["list"]["page"] == 3 and c["reason"] == "new"


def test_an_ad_that_is_not_due_is_neither_eligible_nor_excluded():
    fetched = AS_OF - timedelta(days=1)
    latest = {(SITE, "1"): {"fetched_at": ts(fetched)}}
    cands, excluded = gate([obs(1)], latest=latest)
    assert cands == [] and sum(excluded.values()) == 0


def test_stale_reads_as_of_and_the_policy_not_the_wall_clock():
    fetched = AS_OF - timedelta(days=8)
    latest = {(SITE, "1"): {"fetched_at": ts(fetched)}}
    (c,), _ = gate([obs(1)], latest=latest)
    assert c["reason"] == "stale_7d" and c["fetched_at"] == ts(fetched)
    by_ad = {(SITE, "1"): [obs(1)]}
    longer = dataclasses.replace(P, stale_after_d=9)
    cands, _ = plan.eligible(by_ad, latest, lambda k: "alive", lambda o: True, AS_OF, longer)
    assert cands == []


# ── select_ads: lanes, the hard cap, order ──────────────────────────────────────────────────────────


def stale(i, days=8, **kw):
    return cand(i, "stale_7d", fetched=AS_OF - timedelta(days=days, minutes=i), **kw)


def test_hard_cap_holds_with_1000_revisits():
    pool = [stale(i) for i in range(1000)]
    picks, lanes = plan.select_ads(pool, 300, P)
    assert lanes == {"new": 0, "revisit": 60} and len(picks) == 60  # ceil(0.2 * 300), never the B left over


def test_new_lane_leftover_never_flows_into_revisits():
    pool = [cand(f"n{i}") for i in range(10)] + [stale(i) for i in range(1000)]
    picks, lanes = plan.select_ads(pool, 300, P)
    assert lanes == {"new": 10, "revisit": 60} and len(picks) == 70


def test_unused_revisit_quota_flows_to_new():
    pool = [cand(f"n{i}", position=i) for i in range(1000)]
    pool += [cand(f"c{i}", "card_changed", fetched=AS_OF - timedelta(days=2)) for i in range(3)]
    pool += [stale(i) for i in range(4)]
    picks, lanes = plan.select_ads(pool, 300, P)
    assert lanes == {"new": 293, "revisit": 7} and len(picks) == 300


def test_revisit_order_card_changed_first_then_oldest_fetch():
    pool = [stale(1, days=8), stale(2, days=20), cand("c", "card_changed", fetched=AS_OF - timedelta(days=1))]
    picks, _ = plan.select_ads(pool, 300, P)
    assert ids(picks) == ["c", "2", "1"]
    picks, _ = plan.select_ads(pool, 5, P)  # cap ceil(0.2 * 5) = 1: the card_changed one
    assert ids(picks) == ["c"]


def test_new_lane_newest_crawl_first_then_page_then_position():
    early, late = AS_OF - timedelta(hours=30), AS_OF - timedelta(hours=2)
    pool = [
        cand("a", seen=early, crawl="OLD", page=1, position=1),
        cand("b", seen=late, crawl="NEW", page=2, position=1),
        cand("c", seen=late, crawl="NEW", page=1, position=2),
        cand("d", seen=late - timedelta(minutes=5), crawl="NEW", page=1, position=1),
    ]
    picks, _ = plan.select_ads(pool, 300, P)
    assert ids(picks) == ["d", "c", "b", "a"]
    picks, _ = plan.select_ads(pool, 2, P)
    assert ids(picks) == ["d", "c"]


def test_new_lane_equal_split_with_leftover_flowing_between_subcategories():
    pool = [cand(f"w{i}", sub="widgets", position=i) for i in range(100)]
    pool += [cand(f"g{i}", sub="gadgets", position=i) for i in range(5)]
    picks, lanes = plan.select_ads(pool, 50, P)
    subs = [o["list"]["subcategory"] for _, o in picks]
    assert lanes["new"] == 50 and subs.count("gadgets") == 5 and subs.count("widgets") == 45
    picks, _ = plan.select_ads(pool, 6, P)
    subs = [o["list"]["subcategory"] for _, o in picks]
    assert subs.count("gadgets") == 3 and subs.count("widgets") == 3


def test_new_lane_first_then_revisits_in_the_request_order():
    picks, _ = plan.select_ads([stale(1), cand("n")], 300, P)
    assert [r for r, _ in picks] == ["new", "stale_7d"]


# ── properties ───────────────────────────────────────────────────────────────────────────────────────

REASONS = ("new", "card_changed", "stale_7d")


@st.composite
def pools(draw):
    n = draw(st.integers(0, 120))
    out = []
    for i in range(n):
        reason = draw(st.sampled_from(REASONS))
        sub = draw(st.sampled_from(("widgets", "gadgets", "gizmos")))
        page, pos = draw(st.integers(1, 9)), draw(st.integers(1, 30))
        hours = draw(st.integers(0, 70))
        fetched = AS_OF - timedelta(days=draw(st.integers(1, 40))) if reason != "new" else None
        out.append(cand(i, reason, fetched=fetched, sub=sub, page=page, position=pos,
                        seen=AS_OF - timedelta(hours=hours), crawl=f"L{hours % 3}"))  # fmt: skip
    return out


@settings(max_examples=300, deadline=None)
@given(
    pool=pools(),
    budget=st.integers(0, 150),
    rho=st.sampled_from((0, 0.1, 0.2, 0.25, 0.5, 1.0)),
    seed=st.integers(),
)
def test_budget_invariants_and_determinism(pool, budget, rho, seed):
    pol = dataclasses.replace(P, rho=rho)
    picks, lanes = plan.select_ads(pool, budget, pol)
    n_rev = sum(1 for c in pool if c["reason"] != "new")
    n_new = len(pool) - n_rev
    cap = pol.revisit_cap(budget)
    assert len(picks) == lanes["new"] + lanes["revisit"] <= budget
    assert lanes["revisit"] == min(n_rev, cap)  # the HARD cap, whatever the new lane leaves
    assert lanes["new"] == min(n_new, budget - lanes["revisit"])  # unused revisit quota flows to new
    assert len({o["ad_id"] for _, o in picks}) == len(picks)
    shuffled = list(pool)
    random.Random(seed).shuffle(shuffled)
    assert plan.select_ads(shuffled, budget, pol) == (picks, lanes)  # independent of the input order


# ── the sample path: the same lanes and the same HARD cap ───────────────────────────────────────────


def tup(c):
    return (c["reason"], c["obs"] | {"list": c["obs"]["list"] | {"page_count": 9}})


@settings(max_examples=300, deadline=None)
@given(
    pool=pools(),
    n=st.integers(1, 150),
    rho=st.sampled_from((0, 0.1, 0.2, 0.25, 0.5, 1.0)),
    seed=st.integers(0, 2**32),
    allocation=st.sampled_from(("equal", "proportional")),
)
def test_sample_lanes_hard_cap_and_determinism(pool, n, rho, seed, allocation):
    pol = dataclasses.replace(P, rho=rho)
    cands = [tup(c) for c in pool]
    picks, strata, lanes = plan.sample_lanes(cands, n, seed, allocation, None, pol)
    n_rev = sum(1 for r, _ in cands if r in plan.REVISIT)
    n_new = len(cands) - n_rev
    assert lanes["revisit"] == sum(1 for p in picks if p["reason"] in plan.REVISIT)
    assert lanes["revisit"] == min(n_rev, pol.revisit_cap(n))  # the HARD cap
    assert lanes["new"] == min(n_new, n - lanes["revisit"])  # unused revisit quota flows to new, never back
    assert len(picks) == lanes["new"] + lanes["revisit"] <= n
    assert len({p["obs"]["ad_id"] for p in picks}) == len(picks)
    assert sum(s["sampled"] for s in strata) == len(picks)
    shuffled = list(cands)
    random.Random(seed).shuffle(shuffled)
    assert plan.sample_lanes(shuffled, n, seed, allocation, None, pol) == (picks, strata, lanes)


@settings(max_examples=100, deadline=None)
@given(pool=pools(), n=st.integers(1, 150), seed=st.integers(0, 2**32))
def test_an_all_new_sample_is_the_stratified_sample_unchanged(pool, n, seed):
    cands = [tup(c) for c in pool if c["reason"] == "new"]
    picks, strata, lanes = plan.sample_lanes(cands, n, seed, "equal", None, P)
    assert (picks, strata) == plan.stratified_sample(cands, n, seed, "equal", None)
    assert lanes == {"new": len(picks), "revisit": 0}
