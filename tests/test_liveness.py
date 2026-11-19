"""The liveness ledger: one pure function from the append-only records (observations, listings, events and
run docs) to a state per ad. No state file, no I/O, no clock. Every record here is SYNTHETIC."""

import random
from datetime import datetime

import pytest

from scrape_harness import corrections, liveness

SITE = "example.invalid"
URL = "https://example.invalid/ads/item-{}"


def t(day: int, hour: int = 0) -> str:
    return f"2030-01-{day:02d}T{hour:02d}:00:00Z"


def obs(ad, at, crawl="LIST1"):
    return {"site": SITE, "ad_id": ad, "ad_url": URL.format(ad), "seen_at": at, "crawl_id": crawl,
            "card_hash": "h", "list": {"page": 1}}  # fmt: skip


def listing(ad, at, reason="new"):
    return {"ad_id": ad, "fetched_at": at, "fetch_reason": reason,
            "source": {"site": SITE, "url": URL.format(ad)}}  # fmt: skip


def gone(ad, at, run="ADS1", with_id=True):
    e = {"schema": "event.v1", "at": at, "run_id": run, "kind": "gone", "url": URL.format(ad),
         "reason": "expired_notice", "until": None, "site": SITE}  # fmt: skip
    if with_id:
        e["ad_id"] = ad
    return e


def parse_failed(ad, at, run="ADS1"):
    return {"schema": "event.v1", "at": at, "run_id": run, "kind": "parse_failed", "url": URL.format(ad),
            "reason": "ValueError", "until": None, "site": SITE, "ad_id": ad}  # fmt: skip


def correction(run, field, reconciled, at="2030-01-28T00:00:00Z"):
    return {"schema": "event.v1", "at": at, "run_id": run, "kind": "run_correction", "url": None,
            "reason": None, "until": None, "site": SITE,
            "correction": {"field": field, "recorded": {}, "reconciled": reconciled, "note": "n",
                           "evidence": []}}  # fmt: skip


def run_doc(rid, status="completed", list_status=None):
    d = {"run_id": rid, "status": status}
    if list_status:
        d["list"] = {"status": list_status}
    return d


RUNS = [run_doc("LIST1"), run_doc("LIST2"), run_doc("ADS1"), run_doc("ADS2")]


def state(observations=(), listings=(), events=(), runs=RUNS, ad="1"):
    return liveness.build(observations, listings, events, runs).status(SITE, ad)


# ── tombstones ─────────────────────────────────────────────────────────────────────────────


def test_never_gone_is_alive():
    assert state([obs("1", t(1))]) == "alive"


def test_gone_after_latest_sighting_tombstones():
    assert state([obs("1", t(1))], [], [gone("1", t(2))]) == "tombstoned"


def test_gone_at_the_same_instant_as_the_sighting_tombstones():
    """at/after: a gone event stamped with the sighting's own instant still kills it."""
    assert state([obs("1", t(1))], [], [gone("1", t(1))]) == "tombstoned"


def test_ok_fetch_after_gone_clears_it():
    assert state([obs("1", t(1))], [listing("1", t(3))], [gone("1", t(2))]) == "alive"


def test_reparse_listing_is_not_a_live_sighting():
    """A reparse copies the snapshot's fetched_at and is offline: it proves nothing about the site."""
    assert state([obs("1", t(1))], [listing("1", t(3), reason="reparse")], [gone("1", t(2))]) == "tombstoned"


def test_three_gone_events_make_one_tombstone_at_the_earliest():
    led = liveness.build([obs("1", t(1))], [], [gone("1", t(4)), gone("1", t(2)), gone("1", t(3))], RUNS)
    a = led.ad(SITE, "1")
    assert a.status == "tombstoned"
    assert a.gone_at == t(2)
    assert led.counts()["tombstoned"] == 1


def test_other_ads_are_unaffected():
    led = liveness.build([obs("1", t(1)), obs("2", t(1))], [], [gone("1", t(2))], RUNS)
    assert (led.status(SITE, "1"), led.status(SITE, "2")) == ("tombstoned", "alive")


def test_unknown_ad_has_no_status():
    assert liveness.build([], [], [], RUNS).status(SITE, "nope") is None


# ── re-admission: once, after 24 h ─────────────────────────────────────────────────────────


def test_resight_within_24h_keeps_the_tombstone():
    """The literal 'gone after the latest sighting' would be a loop: a list crawl one hour later clears it."""
    assert state([obs("1", t(1)), obs("1", t(2, 23), "LIST2")], [], [gone("1", t(2))]) == "tombstoned"


def test_resight_after_24h_readmits_once():
    led = liveness.build([obs("1", t(1)), obs("1", t(3), "LIST2")], [], [gone("1", t(2))], RUNS)
    a = led.ad(SITE, "1")
    assert (a.status, a.readmitted) == ("alive", 1)
    assert led.counts()["readmitted"] == 1


def test_readmit_once_per_ad_a_second_gone_is_final():
    """gone, re-sighted (re-admitted), gone again, re-sighted: still tombstoned. Only an OK fetch or an
    operator void clears the second one, so a tombstoned ad costs at most one extra GET."""
    o = [obs("1", t(1)), obs("1", t(3), "LIST2"), obs("1", t(6), "LIST2"), obs("1", t(9), "LIST2")]
    g = [gone("1", t(2)), gone("1", t(4), "ADS2")]
    a = liveness.build(o, [], g, RUNS).ad(SITE, "1")
    assert (a.status, a.readmitted, a.gone_at) == ("tombstoned", 1, t(4))
    assert state(o, [listing("1", t(10))], g) == "alive"  # an OK fetch always clears


def test_resight_from_a_bad_crawl_never_readmits():
    """A good sighting only: not from an aborted (corrected) crawl, not from a zero-yield one."""
    runs = [*RUNS, run_doc("LISTX", "completed"), run_doc("LISTZ", "stopped", "zero_yield")]
    o = [obs("1", t(1)), obs("1", t(5), "LISTX"), obs("1", t(6), "LISTZ")]
    ev = [gone("1", t(2)), correction("LISTX", "status", {"status": "aborted"})]
    assert state(o, [], ev, runs) == "tombstoned"


@pytest.mark.parametrize(
    ("status", "list_status", "good"),
    [
        ("completed", "ok", True),  # the one passing crawl (ADR-0017 §B.2: completed, a passing list.status)
        ("completed", None, True),  # a completed crawl from before list.status: cannot be judged, counts
        ("stopped", "parse_drift", False),  # exit 14: page-1 rows kept before the stop, never a reference
        ("stopped", "failed", False),  # exit 14 (a handler contract error) or 13 (a lost page): stopped
        ("stopped", "zero_yield", False),
        ("stopped", "blocked", False),  # exit 13: robots.txt left a hole in the walk
        ("stopped", None, False),  # a challenge, an interrupt, a ceiling: rows before a Stop (§B.1)
        ("running", None, False),  # in progress, or a process that died: not a finished read
        ("aborted", None, False),
        ("completed", "zero_yield", False),
    ],
)
def test_a_good_crawl_is_completed_with_a_passing_list_status(status, list_status, good):
    runs = [run_doc("L", status, list_status)]
    assert liveness.good_crawl(runs, [])("L") is good
    assert liveness.good_crawl(runs, [])("no-doc") is True  # no run doc: cannot be judged, counts


def test_a_correction_decides_whether_a_crawl_is_good():
    runs = [run_doc("L", "stopped", "ok")]  # e.g. interrupted after its last page, list.status recorded
    assert not liveness.good_crawl(runs, [])("L")
    assert liveness.good_crawl(runs, [correction("L", "status", {"status": "completed"})])("L")


def test_good_sighting_is_the_latest_good_one():
    """A bad crawl's later sighting neither re-admits nor moves the tombstone's anchor."""
    runs = [*RUNS, run_doc("LISTA", "aborted")]
    o = [obs("1", t(1)), obs("1", t(3), "LISTA")]
    a = liveness.build(o, [], [gone("1", t(2))], runs).ad(SITE, "1")
    assert (a.status, a.last_seen) == ("tombstoned", t(1))


def test_unknown_or_missing_crawl_counts_as_good():
    """Rows from before run docs (or a free-form crawl_id) cannot be judged bad, so they count."""
    o = [obs("1", t(1), "free-form/legacy"), {**obs("1", t(4)), "crawl_id": None}]
    assert state(o[:1], [], [gone("1", t(2))]) == "tombstoned"
    o[1].pop("crawl_id")
    assert state(o, [], [gone("1", t(2))]) == "alive"


# ── legacy gone events: the url join ───────────────────────────────────────────────────────


def test_legacy_gone_without_ad_id_joins_by_observation_url():
    assert state([obs("1", t(1))], [], [gone("1", t(2), with_id=False)]) == "tombstoned"


def test_legacy_gone_joins_by_listing_url():
    li = listing("1", t(1))
    assert state([], [li], [gone("1", t(2), with_id=False)]) == "tombstoned"


def test_url_join_skips_unknown_and_ambiguous_urls():
    two = {**obs("2", t(1)), "ad_url": URL.format("1")}  # two ads claim one url: never guess
    led = liveness.build([obs("1", t(1)), two], [], [gone("1", t(2), with_id=False),
                         gone("9", t(2), with_id=False)], RUNS)  # fmt: skip
    assert (led.status(SITE, "1"), led.status(SITE, "2")) == ("alive", "alive")


# ── quarantine and void ────────────────────────────────────────────────────────────────────


def test_quarantined_gone_neither_kills_nor_clears():
    q = correction("ADS2", "gone", {"quarantine": True})
    # on a live ad: not a tombstone, but not eligible either
    assert state([obs("1", t(1))], [], [gone("1", t(2), "ADS2"), q]) == "quarantined"
    # on a tombstoned ad: the tombstone stays
    assert state([obs("1", t(1))], [], [gone("1", t(2)), gone("1", t(3), "ADS2"), q]) == "tombstoned"
    # a quarantined gone never becomes the tombstone, so a later real gone is the one that counts
    a = liveness.build([obs("1", t(1))], [], [gone("1", t(2), "ADS2"), gone("1", t(3)), q], RUNS).ad(
        SITE, "1"
    )
    assert (a.status, a.gone_at) == ("tombstoned", t(3))


def test_quarantine_is_not_cleared_by_a_list_sighting_only_by_an_ok_fetch():
    q = correction("ADS2", "gone", {"quarantine": True})
    o = [obs("1", t(1)), obs("1", t(5), "LIST2")]
    assert state(o, [], [gone("1", t(2), "ADS2"), q]) == "quarantined"
    assert state(o, [listing("1", t(6))], [gone("1", t(2), "ADS2"), q]) == "alive"


def test_readmission_after_a_later_quarantined_gone_stays_quarantined():
    """real gone, then a doubtful one, then a re-admitting sighting: the latest gone is the doubtful one."""
    q = correction("ADS2", "gone", {"quarantine": True})
    o = [obs("1", t(1)), obs("1", t(6), "LIST2")]
    assert state(o, [], [gone("1", t(2)), gone("1", t(3), "ADS2"), q]) == "quarantined"


def test_a_later_real_gone_replaces_an_earlier_quarantine():
    """doubtful gone, then a real one, then a re-admitting sighting: alive, not a stale quarantine."""
    q = correction("ADS2", "gone", {"quarantine": True})
    o = [obs("1", t(1)), obs("1", t(6), "LIST2")]
    a = liveness.build(o, [], [gone("1", t(2), "ADS2"), gone("1", t(3)), q], RUNS).ad(SITE, "1")
    assert (a.status, a.readmitted) == ("alive", 1)


def test_void_drops_the_runs_gone_events():
    v = correction("ADS1", "gone", {"void": True})
    assert state([obs("1", t(1))], [], [gone("1", t(2)), v]) == "alive"


def test_the_latest_correction_of_a_run_wins():
    """`breaker clear --resolve keep` later lifts a quarantine: the gones count again."""
    ev = [gone("1", t(2), "ADS2"), correction("ADS2", "gone", {"quarantine": True}),
          correction("ADS2", "gone", {"quarantine": False})]  # fmt: skip
    assert state([obs("1", t(1))], [], ev) == "tombstoned"


# ── held: a parse failure ──────────────────────────────────────────────────────────────────


def test_parse_failed_holds_the_ad():
    assert state([obs("1", t(1))], [], [parse_failed("1", t(2))]) == "held"


def test_held_survives_later_list_sightings():
    """Else the next list crawl un-holds it, and a broken ad parser costs one view per ad per day."""
    assert state([obs("1", t(1)), obs("1", t(5), "LIST2")], [], [parse_failed("1", t(2))]) == "held"


def test_held_cleared_by_a_reparse_of_the_failed_snapshot():
    """The reparse listing carries the snapshot's fetched_at, which is BEFORE the parse_failed event."""
    li = listing("1", t(2), reason="reparse")
    assert state([obs("1", t(1))], [li], [parse_failed("1", t(2, 1))]) == "alive"


def test_older_listing_does_not_clear_held():
    o, li = [obs("1", t(3))], [listing("1", t(1))]
    assert state(o, li, [parse_failed("1", t(4))]) == "held"
    assert state(o, [*li, listing("1", t(5))], [parse_failed("1", t(4))]) == "alive"


def test_tombstone_outranks_held():
    assert state([obs("1", t(1))], [], [parse_failed("1", t(2)), gone("1", t(3))]) == "tombstoned"


# ── spans, purity, determinism ─────────────────────────────────────────────────────────────


def test_spans_one_row_per_ad_sorted():
    o = [obs("2", t(1)), obs("1", t(1)), obs("1", t(3), "LIST2")]
    rows = liveness.build(o, [listing("1", t(2))], [gone("2", t(2))], RUNS).spans()
    assert [(r["site"], r["ad_id"]) for r in rows] == [(SITE, "1"), (SITE, "2")]
    one, two = rows
    assert (one["first_seen"], one["last_seen"], one["n_sightings"], one["status"]) == (
        t(1),
        t(3),
        3,
        "alive",
    )
    assert (two["gone_at"], two["gone_reason"], two["status"]) == (t(2), "expired_notice", "tombstoned")


def _mixed():
    o = [obs(str(i), t(1 + i % 3), "LIST1" if i % 2 else "LIST2") for i in range(12)]
    o += [obs(str(i), t(8)) for i in range(0, 12, 3)]
    li = [listing(str(i), t(5)) for i in range(0, 12, 4)]
    ev = [gone(str(i), t(4 + i % 2), "ADS1" if i % 2 else "ADS2", with_id=bool(i % 3)) for i in range(12)]
    ev += [parse_failed("7", t(6))]
    corr = [
        correction("ADS2", "gone", {"quarantine": True}),
        correction("LIST2", "status", {"status": "aborted"}),
    ]
    return o, li, ev, corr


def test_deterministic_under_input_order():
    o, li, ev, corr = _mixed()
    want = liveness.build(o, li, [*ev, *corr], RUNS).spans()
    rng = random.Random(7)
    for _ in range(5):
        o2, li2, ev2 = o[:], li[:], ev[:]
        for xs in (o2, li2, ev2):
            rng.shuffle(xs)
        assert liveness.build(o2, li2, [*ev2, *corr], list(reversed(RUNS))).spans() == want


def test_pure_reads_no_clock_and_no_store(monkeypatch):
    class NoClock(datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("liveness read the clock")

        @classmethod
        def utcnow(cls):
            raise AssertionError("liveness read the clock")

    monkeypatch.setattr(liveness, "datetime", NoClock)
    o, li, ev, corr = _mixed()
    liveness.build(o, li, [*ev, *corr], RUNS).spans()  # no raise
    for mod in (liveness, corrections):  # no I/O module within reach
        assert not {"store", "crawl", "os", "pathlib", "open"} & set(vars(mod)), mod.__name__


# ── corrections.apply ──────────────────────────────────────────────────────────────────────


def test_corrections_apply_status_and_gone():
    ev = [correction("R1", "status", {"status": "aborted"}), correction("R2", "gone", {"void": True}),
          correction("R3", "gone", {"quarantine": True}), correction("R1", "stop", {"kind": "x"}),
          correction("R1", "status", {"status": "completed"})]  # fmt: skip
    runs = [run_doc("R1", "stopped"), run_doc("R2"), run_doc("R3"), run_doc("R4", "aborted")]
    fixed = corrections.apply(runs, ev)
    assert fixed["R1"] == {"status": "completed", "list_status": None, "gone": None, "gone_scope": None}
    assert fixed["R2"]["gone"] == "void" and fixed["R3"]["gone"] == "quarantine"
    assert fixed["R4"]["status"] == "aborted"
    assert corrections.latest(ev, "status")["R1"] == {"status": "completed"}


def test_corrections_for_unknown_runs_still_apply():
    """A run doc may be missing (moved, legacy): its gone correction still holds."""
    fixed = corrections.apply([], [correction("GONE", "gone", {"void": True})])
    assert fixed["GONE"] == {"status": None, "list_status": None, "gone": "void", "gone_scope": None}


@pytest.mark.parametrize("bad", [{"void": True, "quarantine": True}])
def test_void_wins_over_quarantine_in_one_correction(bad):
    assert corrections.apply([], [correction("R", "gone", bad)])["R"]["gone"] == "void"


@pytest.mark.parametrize(("reason", "effect"), [("served_list", "quarantine"), ("http_404", "quarantine"),
                                                ("expired_notice", None), ("http_410", None)])  # fmt: skip
def test_a_weak_scoped_correction_leaves_positive_markers_standing(reason, effect):
    """A breaker trip quarantines only the run's WEAK gones: the site's own expiry notice stays a tombstone."""
    fixed = corrections.apply([], [correction("R", "gone", {"quarantine": True, "scope": "weak"})])
    assert corrections.gone_effect(fixed["R"], reason) == effect
    assert corrections.gone_effect(corrections.apply([], [correction("R", "gone", {"void": True})])["R"],
                                   reason) == "void"  # unscoped: every gone of the run  # fmt: skip
