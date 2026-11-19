"""Depth-stratified ad sampling for H4: seeded, stratified, approved as an exact ad list."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIXTURES, LIST

from scrape_harness import cli, plan, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureHttpClient

FIX = [
    "--driver",
    "fixture",
    "--fixtures",
    str(FIXTURES),
    "--max-tasks-per-minute",
    "6000",
    "--approve-politeness-override",
]
SUBS = {"widgets": 530, "gadgets": 36}
# a FRESH sighting: fetch-ads plans only ads sighted within the policy window (72 h) of its plan time
FRESH = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def obs(sub, page, i, count=None):
    ad = f"{9000000 + i}"
    return {
        "schema": "observation.v1",
        "ad_id": ad,
        "site": "example.invalid",
        "seen_at": FRESH,
        "ad_url": f"{LIST.replace('widgets', sub)}/synthetic_ad-{ad}",
        "list": {
            "url": f"{LIST}?page={page}",
            "subcategory": sub,
            "page": page,
            "page_count": count or SUBS[sub],
            "position": 1,
            "sponsored": False,
            "sample_weight": None,
        },
        "card": {"title": "t", "city_label": None},
        "bumped_at": None,
        "card_hash": f"sha256:{i:016x}",
    }


def candidates():
    """300 synthetic widgets ads spread over all 530 pages, 40 trans ads over 36 pages."""
    fe = [("new", obs("widgets", 1 + (i * 7) % 530, i)) for i in range(300)]
    ts = [("new", obs("gadgets", 1 + i % 36, 1000 + i)) for i in range(40)]
    return fe + ts


def test_deciles():
    assert plan.depth_decile(obs("widgets", 1, 0)) == 0
    assert plan.depth_decile(obs("widgets", 530, 0)) == 9
    assert plan.depth_decile(obs("widgets", 266, 0)) == 5
    no_count = obs("widgets", 3, 0)
    no_count["list"]["page_count"] = None
    assert plan.depth_decile(no_count) is None


def test_deterministic_for_a_seed_and_different_for_another():
    a, _ = plan.stratified_sample(candidates(), 40, seed=7)
    b, _ = plan.stratified_sample(list(reversed(candidates())), 40, seed=7)  # input order does not matter
    c, _ = plan.stratified_sample(candidates(), 40, seed=8)
    urls = lambda ps: [p["obs"]["ad_url"] for p in ps]  # noqa: E731
    assert urls(a) == urls(b) and urls(a) != urls(c)


def test_equal_allocation_and_full_decile_coverage():
    picks, strata = plan.stratified_sample(candidates(), 40, seed=7)
    per_sub = {s: sum(1 for p in picks if p["subcategory"] == s) for s in SUBS}
    assert per_sub == {"widgets": 20, "gadgets": 20}  # equal: the comparison design
    fe_deciles = {p["decile"] for p in picks if p["subcategory"] == "widgets"}
    assert fe_deciles == set(range(10))  # proportional within: every decile of the deep list is covered
    for s in strata:
        if s["sampled"]:
            assert s["weight"] == pytest.approx(s["population"] / s["sampled"])
    assert sum(s["sampled"] for s in strata) == len(picks) == 40


def test_a_short_subcategory_gives_its_share_back():
    few = [c for c in candidates() if c[1]["list"]["subcategory"] == "widgets"]
    few += [("new", obs("gadgets", 1, 5000 + i)) for i in range(5)]
    picks, _ = plan.stratified_sample(few, 40, seed=1)
    per_sub = {s: sum(1 for p in picks if p["subcategory"] == s) for s in SUBS}
    assert per_sub == {"widgets": 35, "gadgets": 5} and len(picks) == 40


def test_proportional_allocation_and_subcategory_filter():
    picks, _ = plan.stratified_sample(candidates(), 34, seed=3, allocation="proportional")
    per_sub = {s: sum(1 for p in picks if p["subcategory"] == s) for s in SUBS}
    assert per_sub == {"widgets": 30, "gadgets": 4}  # 300:40 of 340
    only, _ = plan.stratified_sample(candidates(), 10, seed=3, subcategories=["gadgets"])
    assert {p["subcategory"] for p in only} == {"gadgets"}


@pytest.fixture
def observed(data_dir):
    store.append("observations", [c[1] for c in candidates()])
    FixtureHttpClient.log.clear()
    return data_dir


def test_dry_run_sends_nothing_and_shows_strata(observed, capsys):
    capsys.readouterr()
    assert main(["fetch-ads", "--sample", "12", "--seed", "4", "--dry-run"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines[-1]["summary"] is True and lines[-1]["sampled"] == 12
    assert all({"subcategory", "decile", "weight", "url"} <= set(x) for x in lines[:-1])
    assert FixtureHttpClient.log == [] and not (observed / "runs").exists()


def test_the_plan_lists_the_exact_ads_and_the_design(observed, capsys, monkeypatch):
    t0 = datetime.now(UTC)
    monkeypatch.setattr(
        cli, "_now", lambda: t0
    )  # the plan signs its time (as_of): re-planning at t0 is equal

    def planned(*extra):
        capsys.readouterr()
        assert (
            main(["fetch-ads", "--sample", "12", "--seed", "4", *extra, "--plan", "--driver", "browser"]) == 0
        )
        return json.loads(capsys.readouterr().out)

    p = planned()
    capsys.readouterr()
    main(["fetch-ads", "--sample", "12", "--seed", "4", "--dry-run"])
    dry_urls = [json.loads(x)["url"] for x in capsys.readouterr().out.splitlines()[:-1]]
    assert [u for _, u in p["plan"]["ads"]] == dry_urls  # the approval covers exactly these ads
    assert p["plan"]["sample"]["seed"] == 4 and p["plan"]["sample"]["strata"]
    assert planned()["plan_hash"] == p["plan_hash"]
    capsys.readouterr()
    assert main(["fetch-ads", "--sample", "12", "--seed", "5", "--plan", "--driver", "browser"]) == 0
    assert (
        json.loads(capsys.readouterr().out)["plan_hash"] != p["plan_hash"]
    )  # another seed, another approval


def test_sample_needs_a_seed(observed):
    with pytest.raises(SystemExit, match="--seed"):
        main(["fetch-ads", "--sample", "3", "--dry-run"])


def test_a_sampled_run_records_design_and_manifest(data_dir):
    assert (
        main(
            [
                "crawl-lists",
                "--subcategory",
                "widgets",
                "--page-count",
                "3",
                "--stride",
                "2",
                "--start",
                "1",
                *FIX,
            ]
        )
        == 0
    )  # ads 1000001-3 on pages 1 and 3 of 3
    assert main(["fetch-ads", "--sample", "2", "--seed", "9", *FIX]) == 0
    run = next(json.loads(p.read_text()) for p in (data_dir / "runs").glob("*fetch-ads*.json"))
    assert run["sample"]["n"] == 2 and run["sample"]["seed"] == 9 and run["requests"]["ad_pages"] == 2
    rows = [json.loads(x) for x in (data_dir / run["sample"]["manifest"]).read_text().splitlines()]
    assert len(rows) == 2 and all(r["schema"] == "sample.v1" and r["weight"] > 0 for r in rows)
    assert {r["url"] for r in rows} <= {r["source"]["url"] for r in store.read("listings")}
