"""The frontier walk's pure rules (ADR-0017 §A.2, §A.3): `plan.frontier_stop` (a candidate of K consecutive
cards that hold consecutive places in the reference's parsed order, confirmed by ONE more page) and
`plan.frontier_reference` (which earlier crawl is the reference). Every id here is SYNTHETIC; no crawl runs.
"""

import builtins
import dataclasses
import random

import pytest

from scrape_harness import plan, policy, store

P = policy.DEFAULT
SITE = "example"


def ref_of(order: list, last_page: int = 99, crawl_id: str = "ref-1", per: int = 10) -> dict:
    """A reference with `per` parsed cards per page."""
    return {"crawl_id": crawl_id, "rank": {a: i for i, a in enumerate(order)},
            "page": {a: i // per + 1 for i, a in enumerate(order)}, "last_page": last_page}  # fmt: skip


def walk_of(*pages, order: str = "bump_desc") -> dict:
    """pages[i] = the parsed ids of walk page i+1; the walk has just handled its last page."""
    return {"order": order, "page": len(pages), "pages": {n: list(ids) for n, ids in enumerate(pages, 1)}}


OLD = [f"o{i:03d}" for i in range(200)]  # the reference crawl's parsed order, top to bottom


def new(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:03d}" for i in range(n)]


# ── the policy carries K and the confirmation thresholds, signed by the digest ─────────────────────────
def test_policy_carries_the_frontier_tunables():
    assert (P.frontier_k, P.confirm_share, P.confirm_inversions) == (5, 0.9, 0.05)
    assert dataclasses.replace(P, frontier_k=6).digest() != P.digest()
    assert "reference_status" in P.rule_constants()
    with pytest.raises(ValueError):
        dataclasses.replace(P, frontier_k=1)
    with pytest.raises(ValueError):
        dataclasses.replace(P, confirm_share=1.5)


# ── the anchor: candidate, then ONE confirmation page ─────────────────────────────────────────────────
def test_a_true_boundary_is_confirmed_on_the_next_page():
    ref = ref_of(OLD)
    w = walk_of(new("n", 10), OLD[0:10])
    assert plan.frontier_stop(w, ref, P) is None  # a candidate on page 2: not a stop until confirmed
    w = walk_of(new("n", 10), OLD[0:10], OLD[10:20])
    got = plan.frontier_stop(w, ref, P)
    assert got == {"page": 2, "confirm_page": 3, "ref_crawl_id": "ref-1"}


def test_the_candidate_starts_mid_page():
    """New bumps fill the top of page 2, the old content starts below them: the window is found mid-page."""
    ref = ref_of(OLD)
    w = walk_of(new("n", 10), new("m", 4) + OLD[0:6], OLD[6:16])
    assert plan.frontier_stop(w, ref, P) == {"page": 2, "confirm_page": 3, "ref_crawl_id": "ref-1"}


def test_unordered_never_stops():
    ref = ref_of(OLD)
    w = walk_of(new("n", 10), OLD[0:10], OLD[10:20], order="unordered")
    assert plan.frontier_stop(w, ref, P) is None
    w["order"] = "bump-desc"  # anything but the exact declaration is unordered: fail safe
    assert plan.frontier_stop(w, ref, P) is None


def test_no_reference_never_stops():
    assert plan.frontier_stop(walk_of(OLD[0:10], OLD[10:20]), None, P) is None


def test_fewer_than_k_adjacent_is_no_candidate():
    ref = ref_of(OLD)
    four = [OLD[0], OLD[1], OLD[2], OLD[3], "n1", OLD[5], "n2", OLD[7]]
    assert plan.frontier_stop(walk_of(four, OLD[10:20]), ref, P) is None


def test_ids_in_order_but_not_adjacent_are_no_candidate():
    """Order alone is not enough: the K ids must hold
    CONSECUTIVE places in the reference."""
    ref = ref_of(OLD)
    every_other = OLD[0:20:2]
    assert plan.frontier_stop(walk_of(every_other, OLD[20:30]), ref, P) is None


def test_the_candidate_must_sit_before_the_references_last_page():
    """Judged by the candidate's place IN THE REFERENCE: its last card on a reference page before the last one
    the reference read (else no reference content lies below it to confirm)."""
    w = walk_of(new("n", 10), OLD[0:10], OLD[10:20])  # the candidate OLD[0:5] is on reference page 1
    assert plan.frontier_stop(w, ref_of(OLD, last_page=1), P) is None
    assert plan.frontier_stop(w, ref_of(OLD, last_page=2), P)["page"] == 2
    # deep in today's walk, early in the reference: the walk's own page number does not matter
    deep = walk_of(*[new(f"n{i}", 10) for i in range(5)], OLD[0:10], OLD[10:20])
    assert plan.frontier_stop(deep, ref_of(OLD, last_page=2), P)["page"] == 6
    # the window's LAST card decides: a window that ends on the reference's last page is no candidate
    edge = walk_of(new("n", 10), OLD[5:15], OLD[15:25])  # OLD[5:10] ends on reference page 1
    assert plan.frontier_stop(edge, ref_of(OLD, last_page=2), P)["page"] == 2
    edge = walk_of(new("n", 10), OLD[8:18], OLD[18:28])  # OLD[8:13] ends on reference page 2
    assert plan.frontier_stop(edge, ref_of(OLD, last_page=2), P) is None


def test_confirmation_needs_90_percent_reference_ids_below_the_candidate():
    ref = ref_of(OLD)
    nine_of_ten = OLD[10:19] + ["n1"]
    assert plan.frontier_stop(walk_of(OLD[0:10], nine_of_ten), ref, P)["page"] == 1
    eight_of_ten = OLD[10:18] + ["n1", "n2"]
    assert plan.frontier_stop(walk_of(OLD[0:10], eight_of_ten), ref, P) is None
    # reference ids ABOVE the candidate (re-bumps of older-than-candidate content) do not count
    above = OLD[0:2] + OLD[12:20]  # 8 of 10 below the candidate (OLD[2:7], last place 6)
    assert plan.frontier_stop(walk_of(OLD[2:12], above), ref, P) is None


def test_confirmation_allows_at_most_5_percent_inversions():
    ref = ref_of(OLD)
    tail = OLD[10:31]  # 21 ids, 20 adjacent pairs: one swap = 1 inversion (5%), two swaps = 10%
    one = tail[:]
    one[3], one[4] = one[4], one[3]
    assert plan.frontier_stop(walk_of(OLD[0:10], one), ref, P)["page"] == 1
    two = one[:]
    two[13], two[14] = two[14], two[13]
    assert plan.frontier_stop(walk_of(OLD[0:10], two), ref, P) is None


def test_a_failed_confirmation_page_is_searched_for_the_next_candidate():
    """Page 1 holds a false candidate; page 2 fails to confirm it but itself holds a true one; page 3
    confirms that: the anchor is page 2, never page 1."""
    ref = ref_of(OLD)
    w = walk_of(OLD[100:105] + new("n", 5), new("m", 4) + OLD[0:6])
    assert plan.frontier_stop(w, ref, P) is None
    w = walk_of(OLD[100:105] + new("n", 5), new("m", 4) + OLD[0:6], OLD[6:16])
    assert plan.frontier_stop(w, ref, P) == {"page": 2, "confirm_page": 3, "ref_crawl_id": "ref-1"}


# ── the ADR's table: why each case cannot stop the walk early ──────────────────────────────────────────
def test_pinned_block_no_early_stop():
    """A pinned block on every page 1 (also on the reference's page 1, so its ids are adjacent there): the
    page after it holds today's new bumps, not reference ids, so the confirmation fails."""
    pinned = OLD[0:5]
    ref = ref_of(OLD)
    w = walk_of(pinned + new("n", 5), new("m", 10))
    assert plan.frontier_stop(w, ref, P) is None
    w = walk_of(pinned + new("n", 5), new("m", 10), new("q", 10))
    assert plan.frontier_stop(w, ref, P) is None


def test_rotation_block():
    """A fixed block that rotates: ads posted between rotations sit below it and are new ids, so the
    confirmation fails; a block of old content served out of order fails the 5% inversion check."""
    ref = ref_of(OLD)
    rotated = OLD[40:45]  # another fixed block, adjacent in the reference
    assert plan.frontier_stop(walk_of(rotated + new("n", 5), new("m", 10)), ref, P) is None
    shuffled = OLD[50:70]
    random.Random(7).shuffle(shuffled)
    assert plan.frontier_stop(walk_of(rotated + new("n", 5), shuffled), ref, P) is None


def test_boundary_push_excluded():
    """An ad pushed across a page boundary mid-walk is read twice; it was seen earlier in this walk, so it is
    never part of a candidate (the window starts after it)."""
    ref = ref_of(OLD)
    pushed = OLD[0]
    p1 = new("n", 9) + [pushed]
    p2 = [pushed] + OLD[1:5] + new("m", 5)  # with the pushed id the window would be 5; without it, 4
    p3 = OLD[5:15]
    assert plan.frontier_stop(walk_of(p1, p2, p3), ref, P) is None
    p2 = [pushed] + OLD[1:6] + new("m", 4)  # 5 unseen adjacent ids after it: a candidate
    assert plan.frontier_stop(walk_of(p1, p2, OLD[6:16]), ref, P)["page"] == 2


def test_random_rebump_no_false_stop():
    """1000 seeds: random re-bumps of known ads, mixed into today's new bumps on the first pages, never
    anchor the walk before the old content starts. (Holes the re-bumps leave can delay the anchor into the
    old content, never pull it forward: the walk then reads to its ceiling, which costs list GETs, not views.)"""
    ref, per, anchored = ref_of(OLD), 20, 0
    for seed in range(1000):
        rnd = random.Random(seed)
        fresh_pages = rnd.randint(1, 4)
        rebumped = rnd.sample(OLD, rnd.randint(0, per))
        items = new("n", fresh_pages * per - len(rebumped)) + rebumped
        rnd.shuffle(items)
        gone = set(rebumped)
        rest = [a for a in OLD if a not in gone]
        pages = [items[i * per : (i + 1) * per] for i in range(fresh_pages)]
        pages += [rest[i * per : (i + 1) * per] for i in range(4)]
        for upto in range(1, len(pages) + 1):
            got = plan.frontier_stop(walk_of(*pages[:upto]), ref, P)
            if got is not None:
                # the walk stops only after it has read every page of today's fresh content
                assert got["confirm_page"] > fresh_pages, (seed, upto, got)
                anchored += 1
                break
    assert anchored >= 990  # and the walk does find the boundary


# ── which crawl is the reference (§A.3) ──────────────────────────────────────────────────────────────
def run(rid, created, status="completed", lst_status="end", mode="walk", sub="widgets", region="r", **lst):
    return {
        "run_id": rid,
        "command": "crawl-lists",
        "source": SITE,
        "created_at": created,
        "status": status,
        "list": {"mode": mode, "stride": 1, "status": lst_status, "subcategory": sub, "region": region,
                 "pages_read": 3, **lst},
    }  # fmt: skip


def obs(rid, page, pos, ad):
    return {"site": SITE, "ad_id": ad, "crawl_id": rid, "list": {"page": page, "position": pos}}


def choose(runs, rows=(), events=()):
    return plan.frontier_reference(runs, list(events), list(rows), site=SITE, subcategory="widgets",
                                   region="r")  # fmt: skip


def test_reference_is_the_latest_valid_crawl():
    runs = [run("a", "2000-01-01T00:00:01Z"), run("b", "2000-01-01T00:00:03Z", lst_status="anchored",
            mode="frontier"), run("c", "2000-01-01T00:00:02Z", lst_status="ceiling_hit")]  # fmt: skip
    rows = [obs("b", 2, 1, "x2"), obs("b", 1, 2, "x1b"), obs("b", 1, 1, "x1a"), obs("a", 1, 1, "z")]
    ref = choose(runs, rows)
    assert ref["crawl_id"] == "b" and ref["last_page"] == 3
    assert ref["rank"] == {"x1a": 0, "x1b": 1, "x2": 2}  # parsed order: page, then position
    assert ref["page"] == {"x1a": 1, "x1b": 1, "x2": 2}


@pytest.mark.parametrize(
    "lst_status", ["ok", "end_unknown", "zero_yield", "short_census", "parse_drift", "failed", "blocked",
                   "past_end", "redirected", None],
)  # fmt: skip
def test_terminal_and_unordered_outcomes_are_never_a_reference(lst_status):
    assert choose([run("a", "2000-01-01T00:00:01Z", lst_status=lst_status)]) is None


def test_reference_needs_a_completed_contiguous_crawl_of_the_same_list():
    t = "2000-01-01T00:00:01Z"
    assert choose([run("a", t, status="stopped")]) is None
    assert choose([run("a", t, mode=None)]) is None  # a sample or named pages: not read from page 1
    assert choose([run("a", t, sub="gadgets")]) is None
    assert choose([run("a", t, region="elsewhere")]) is None
    other = run("a", t)
    other["source"] = "another"
    assert choose([other]) is None
    assert choose([run("a", t, lst_status="ceiling_hit")])["crawl_id"] == "a"  # a ceiling crawl may be one


def test_a_corrected_run_is_judged_by_its_correction():
    t = "2000-01-01T00:00:01Z"
    fix = {"kind": "run_correction", "run_id": "a", "at": t,
           "correction": {"field": "status", "reconciled": {"status": "aborted"}}}  # fmt: skip
    assert choose([run("a", t)], events=[fix]) is None


def test_the_references_gap_rides_along():
    gap = {"after_page": 3, "since_ref": None, "opened_by": "a"}
    ref = choose([run("a", "2000-01-01T00:00:01Z", lst_status="ceiling_hit", mode="frontier", gap=gap)])
    assert ref["gap"] == gap


def test_the_frontier_rules_are_pure(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("I/O in a pure rule")

    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(store, "read", boom)
    ref = ref_of(OLD)
    w = walk_of(new("n", 10), OLD[0:10], OLD[10:20])
    assert plan.frontier_stop(w, ref, P) == plan.frontier_stop(w, ref, P)
    assert choose([run("a", "2000-01-01T00:00:01Z")], [obs("a", 1, 1, "x")])["crawl_id"] == "a"
