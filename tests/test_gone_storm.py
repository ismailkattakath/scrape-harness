"""The gone-storm and ad-failure breaker (ADR-0017 §C.5): a one-sided binomial trip, QUARANTINE not void,
and a per-site latch that every networked run obeys until the operator clears it. Exact GET counts from the
fixture client; every record and page here is SYNTHETIC."""

import json
from datetime import UTC, datetime

import pytest
from conftest import FIXTURES
from test_fresh_gate import card_obs

from scrape_harness import breaker, cli, crawl, incidents, liveness, policy, store
from scrape_harness.cli import EXIT_APPROVAL, main
from scrape_harness.drivers.fixture import FixtureHttpClient

POL = policy.DEFAULT
SITE = "example.invalid"


# ── the trip rule: exact arithmetic ────────────────────────────────────────────────────────────────


def test_the_trip_thresholds_are_the_adrs():
    assert [breaker.threshold(n, POL) for n in (5, 20, 100, 300)] == [5, 9, 24, 52]
    assert breaker.trips(5, 5, POL)  # a 100% storm trips at GET 5
    assert not breaker.trips(4, 4, POL)  # the floor: 4 of 4 is never a storm
    assert not breaker.trips(8, 20, POL) and breaker.trips(9, 20, POL)


def test_the_run_level_false_trip_rate_is_small_exact_dp():
    """P(the breaker ever trips in a 300-GET run) when every gone is independent at rate p (exact DP)."""
    assert breaker.false_trip(0.04, 300, POL) < 1e-4
    assert breaker.false_trip(0.06, 300, POL) <= 1e-4
    assert breaker.false_trip(0.10, 300, POL) <= 2e-3
    assert breaker.false_trip(1.0, 5, POL) == pytest.approx(1.0)  # and a full storm always trips


@pytest.mark.parametrize("reason", ["served_list", "redirected_to_list", "http_404", "some_new_reason"])
def test_absence_and_redirect_gones_are_weak(reason):
    assert breaker.weak(reason)


@pytest.mark.parametrize("reason", ["expired_notice", "http_410"])
def test_positive_marker_gones_never_count(reason):
    assert not breaker.weak(reason)


def first_trip(pattern: list[bool]) -> int | None:
    """The GET the shipped rule trips at, for a sequence of weak (True) / ok (False) outcomes."""
    g = 0
    for n, w in enumerate(pattern, 1):
        g += w
        if w and breaker.trips(g, n, POL):
            return n
    return None


# ── end to end through fetch-ads ───────────────────────────────────────────────────────────────────

N = 20
STORM = [i % 5 in (0, 2, 4) for i in range(N)]  # 60% of the ads answer with the category list


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sighted(n=N, offset=0):
    rows = [card_obs(i + offset, now()) for i in range(n)]
    store.append("observations", rows)
    return rows


def pages(tmp_path, rows, weak: list[bool], gone_file="list-p1.html", gone_status=200):
    """A fixture folder: each ad URL answers the list page (a weak gone) or a real ad page."""
    d = tmp_path / f"fx{len(list(tmp_path.glob('fx*')))}"
    d.mkdir()
    for f in FIXTURES.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    urls = json.loads((FIXTURES / "urls.json").read_text())
    for o, w in zip(rows, weak, strict=True):
        urls[o["ad_url"]] = {"file": gone_file, "status": gone_status} if w else "ad-1000001.html"
    (d / "urls.json").write_text(json.dumps(urls))
    return d


def fix(d):
    return ["--driver", "fixture", "--fixtures", str(d), "--max-tasks-per-minute", "6000",
            "--approve-politeness-override"]  # fmt: skip


def ad_gets():
    return [u for u in FixtureHttpClient.log if "synthetic_ad-" in u]


def fetch_docs(data_dir):
    return sorted(
        (json.loads(p.read_text()) for p in (data_dir / "runs").glob("*fetch-ads*.json")),
        key=lambda d: d["created_at"],
    )


def ledger():
    return liveness.build(
        store.read("observations"), store.read("listings"), store.read("events"), cli._run_docs()
    )


@pytest.fixture
def stormed(data_dir, tmp_path):
    rows = sighted()
    FixtureHttpClient.log.clear()
    code = main(["fetch-ads", *fix(pages(tmp_path, rows, STORM))])
    return code, rows


def test_a_storm_trips_quarantines_and_stops_exit_14(data_dir, stormed):
    code, rows = stormed
    at = first_trip(STORM)
    assert code == crawl.EXIT_PARSE and at is not None and 5 <= at <= 11
    assert len(ad_gets()) == at  # stopped AT the trip: not one view more
    (doc,) = fetch_docs(data_dir)
    g = sum(STORM[:at])
    assert doc["breaker"] == {"tripped": True, "at": doc["breaker"]["at"], "kind": "gone", "g": g, "n": at,
                              "p0": POL.breaker_p0, "alpha": POL.breaker_alpha}  # fmt: skip
    assert doc["status"] == "stopped" and doc["stop"]["code"] == crawl.EXIT_PARSE
    (corr,) = [e for e in store.read("events") if e["kind"] == "run_correction"]
    assert corr["run_id"] == doc["run_id"] and corr["correction"]["field"] == "gone"
    assert corr["correction"]["reconciled"] == {"quarantine": True, "scope": "weak"}
    gones = [e for e in store.read("events") if e["kind"] == "gone"]
    assert len(gones) == g and all(e.get("ad_id") for e in gones)  # gone events now name their ad
    led = ledger()
    assert led.counts()["tombstoned"] == 0 and led.counts()["quarantined"] == g
    assert [i["kind"] for i in incidents.open_ones()] == ["gone_storm"]  # one incident, the right kind


def test_the_latch_refuses_every_later_networked_run_before_the_gate(data_dir, tmp_path, stormed, capsys):
    rows = sighted(5, offset=100)  # new, fresh, alive ads: still refused, the site is latched
    d = pages(tmp_path, rows, [False] * 5)
    FixtureHttpClient.log.clear()
    assert main(["fetch-ads", *fix(d)]) == crawl.EXIT_PARSE
    assert main(["fetch-ads", "--plan", *fix(d)]) == crawl.EXIT_PARSE  # no plan to approve either
    lists = ["crawl-lists", "--subcategory", "widgets", "--page-count", "3", "--stride", "2", "--start", "1"]
    assert main([*lists, *fix(d)]) == crawl.EXIT_PARSE
    assert FixtureHttpClient.log == [] and len(fetch_docs(data_dir)) == 1
    capsys.readouterr()
    assert main(["fetch-ads", "--dry-run"]) == 0  # a dry run sends nothing: it may still look
    assert "breaker" in capsys.readouterr().err


def test_resume_and_supersede_refuse_a_tripped_run(data_dir, tmp_path, stormed):
    (doc,) = fetch_docs(data_dir)
    d = pages(tmp_path, [], [])
    FixtureHttpClient.log.clear()
    assert main(["crawl", "resume", "--run-id", doc["run_id"], *fix(d)]) == crawl.EXIT_PARSE
    assert main(["fetch-ads", "--supersede", doc["run_id"], *fix(d)]) == crawl.EXIT_PARSE
    assert FixtureHttpClient.log == []


def test_clear_needs_the_operator_and_keep_makes_the_deaths_real(data_dir, tmp_path, stormed, capsys):
    _, rows = stormed
    (doc,) = fetch_docs(data_dir)
    args = ["breaker", "clear", "--site", SITE, "--resolve", "keep", "--note", "synthetic: they were removed"]
    assert main(args) == EXIT_APPROVAL  # an agent cannot clear it
    assert main([*args, "--approve-operator"]) == 0
    fields = [e["correction"]["field"] for e in store.read("events") if e["kind"] == "run_correction"]
    assert fields == ["gone", "gone", "breaker"]  # appended, nothing rewritten
    led = ledger()
    assert led.counts()["quarantined"] == 0 and led.counts()["tombstoned"] == sum(STORM[: first_trip(STORM)])
    more = sighted(3, offset=200)
    FixtureHttpClient.log.clear()
    assert main(["fetch-ads", *fix(pages(tmp_path, rows + more, [False] * (N + 3)))]) == 0  # unlatched
    # the 3 new ads, and the storm run's ads it never reached (still fresh, alive and new)
    assert len(ad_gets()) == 3 + N - first_trip(STORM)
    capsys.readouterr()
    assert main([*args, "--approve-operator"]) == 0  # nothing latched: nothing appended
    assert json.loads(capsys.readouterr().out)["cleared"] == []


def test_clear_void_drops_only_the_weak_gones(data_dir, tmp_path, stormed):
    (doc,) = fetch_docs(data_dir)
    crawl.event(doc["run_id"], "gone", rows_url := "https://www.example.invalid/x-1", "expired_notice",
                site=SITE, ad_id="1")  # a positive-marker gone of the same run  # fmt: skip
    store.append("observations", [card_obs(0, "2026-01-01T00:00:00Z") | {"ad_id": "1", "ad_url": rows_url}])
    assert main(["breaker", "clear", "--site", SITE, "--resolve", "void", "--note", "synthetic: the parser",
                 "--approve-operator"]) == 0  # fmt: skip
    led = ledger()
    assert led.counts()["quarantined"] == 0 and led.status(SITE, "1") == "tombstoned"  # it stands
    assert led.counts()["tombstoned"] == 1  # every weak gone of the run is void


def test_a_positive_marker_expiry_wave_never_trips(data_dir, tmp_path):
    rows = sighted()
    FixtureHttpClient.log.clear()
    assert main(["fetch-ads", *fix(pages(tmp_path, rows, [True] * N, gone_file="", gone_status=410))]) == 0
    assert len(ad_gets()) == N
    (doc,) = fetch_docs(data_dir)
    assert "breaker" not in doc and doc["status"] == "completed" and doc["ads"]["gone"] == N
    assert ledger().counts()["tombstoned"] == N and incidents.open_ones() == []


def test_two_eligible_both_gone_is_a_normal_small_run(data_dir, tmp_path):
    rows = sighted(2)
    FixtureHttpClient.log.clear()
    assert main(["fetch-ads", *fix(pages(tmp_path, rows, [True, True]))]) == 0
    assert len(ad_gets()) == 2 and "breaker" not in fetch_docs(data_dir)[0]
    assert ledger().counts()["tombstoned"] == 2


def test_a_parse_failure_storm_trips_the_ad_failure_breaker(data_dir, tmp_path, monkeypatch):
    from example_site.source import ExampleSite

    def broken(self, html, **kw):
        raise AttributeError("synthetic parser break")

    rows = sighted()
    monkeypatch.setattr(ExampleSite, "parse_ad", broken)
    FixtureHttpClient.log.clear()
    assert main(["fetch-ads", *fix(pages(tmp_path, rows, [False] * N))]) == crawl.EXIT_PARSE
    assert len(ad_gets()) == 5  # every GET failed to parse: the floor
    (doc,) = fetch_docs(data_dir)
    assert doc["breaker"]["kind"] == "failure" and doc["breaker"]["g"] == 5
    assert [i["kind"] for i in incidents.open_ones()] == ["parse"]
    assert ledger().counts()["held"] == 5


def test_the_trip_is_saved_before_anything_else_can_fail(data_dir, tmp_path, monkeypatch):
    def refuse(**kw):
        raise store.DataDirError("synthetic: the events file refused the write")

    rows = sighted()
    monkeypatch.setattr(crawl, "append_correction", refuse)
    FixtureHttpClient.log.clear()
    assert main(["fetch-ads", *fix(pages(tmp_path, rows, STORM))]) == crawl.EXIT_PARSE
    assert len(ad_gets()) == first_trip(STORM)
    (doc,) = fetch_docs(data_dir)
    assert doc["breaker"]["tripped"] is True  # so the latch holds even without the correction
    assert main(["fetch-ads", *fix(pages(tmp_path, sighted(2, offset=300), [False] * 2))]) == crawl.EXIT_PARSE


def test_a_refused_gone_write_still_counts_and_a_trip_on_it_wins(data_dir, tmp_path, monkeypatch):
    """settle() runs before the data-folder stop: the fifth weak gone trips the breaker even though its own
    event write was refused, so the louder stop (exit 14, latched) is the one recorded."""
    from test_loud_failures import _refusing_gone

    rows = sighted()
    _refusing_gone(monkeypatch, nth=5)
    FixtureHttpClient.log.clear()
    assert main(["fetch-ads", *fix(pages(tmp_path, rows, [True] * N))]) == crawl.EXIT_PARSE
    assert len(ad_gets()) == 5  # no retry of the fifth
    (doc,) = fetch_docs(data_dir)
    assert doc["breaker"]["g"] == 5 and doc["stop"]["code"] == crawl.EXIT_PARSE
