"""fetch-ads --select recruit: a one-off sample of ads seen on list cards but NEVER opened.

Same sampler as --sample (plan.stratified_sample), restricted to never-opened ads, every pick tagged
"recruit". Synthetic observations only."""

import json

import pytest
from test_sampling import FIX, candidates

from scrape_harness import store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureHttpClient


@pytest.fixture
def observed(data_dir):
    store.append("observations", [c[1] for c in candidates()])
    FixtureHttpClient.log.clear()
    return data_dir


def dry(capsys, *args):
    capsys.readouterr()
    assert main(["fetch-ads", *args, "--dry-run"]) == 0
    return [json.loads(x) for x in capsys.readouterr().out.splitlines()]


def test_recruit_reuses_the_sampler_and_tags_every_pick(observed, capsys):
    rec = dry(capsys, "--select", "recruit", "--sample", "12", "--seed", "4", "--subcategories", "widgets")
    ref = dry(capsys, "--sample", "12", "--seed", "4", "--subcategories", "widgets")
    # nothing was ever opened, so the pool is the same: the SAME sampler picks the SAME ads
    assert [x["url"] for x in rec[:-1]] == [x["url"] for x in ref[:-1]]
    assert {x["reason"] for x in rec[:-1]} == {"recruit"} and {x["reason"] for x in ref[:-1]} == {"new"}
    assert {x["subcategory"] for x in rec[:-1]} == {"widgets"} and rec[-1]["sampled"] == 12


def test_recruit_never_picks_an_opened_ad(observed, capsys):
    pool = [o for _, o in candidates() if o["list"]["subcategory"] == "widgets"]
    opened, snapshot_only = pool[0], pool[1]
    store.append(
        "listings",
        [
            {
                "schema": "listing.v1",
                "ad_id": opened["ad_id"],
                "fetched_at": "2026-01-15T12:00:00Z",
                "fetch_reason": "new",
                "source": {"site": opened["site"], "url": opened["ad_url"]},
                "content": {"title": "t", "description": "d"},
                "location": {},
            }
        ],
    )
    store.write_raw(snapshot_only["site"], snapshot_only["ad_id"], "2026-01-15T12:00:00Z", "<html></html>")
    rec = dry(capsys, "--select", "recruit", "--sample", "300", "--seed", "1", "--subcategories", "widgets")
    ids = {x["ad_id"] for x in rec[:-1]}
    assert opened["ad_id"] not in ids and snapshot_only["ad_id"] not in ids
    assert len(ids) == 298  # every other never-opened widgets ad (a census at this n)


def test_recruit_needs_a_sample_size_and_a_seed(observed):
    with pytest.raises(SystemExit, match="--sample"):
        main(["fetch-ads", "--select", "recruit", "--seed", "3", "--dry-run"])
    with pytest.raises(SystemExit, match="--seed"):
        main(["fetch-ads", "--select", "recruit", "--sample", "3", "--dry-run"])


def test_the_recruit_plan_binds_the_ids_and_its_card_names_every_request(observed, capsys):
    args = ["--select", "recruit", "--sample", "12", "--seed", "4", "--subcategories", "widgets"]
    capsys.readouterr()
    assert main(["fetch-ads", *args, "--plan", "--driver", "browser", "--max-tasks-per-minute", "7"]) == 0
    out, err = capsys.readouterr()
    p = json.loads(out)
    assert p["plan"]["select"] == "recruit" and {r for r, _ in p["plan"]["ads"]} == {"recruit"}
    assert len(p["plan"]["ads"]) == 12
    assert "would send 13 requests (ad 12, robots 1)" in err
    assert "would take ~1 min 51 s at 7.0/min" in err  # 13 requests at 7/min
    assert "Egress: checked at run start: 1 request to the egress-check endpoint, never the site" in err
    assert "approve" in err  # the operator approves this plan_hash before anything is sent
    capsys.readouterr()
    assert (
        main(
            [
                "fetch-ads",
                "--sample",
                "12",
                "--seed",
                "4",
                "--subcategories",
                "widgets",
                "--plan",
                "--driver",
                "browser",
                "--max-tasks-per-minute",
                "7",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["plan_hash"] != p["plan_hash"]  # recruit is its own approval


def test_a_recruit_run_tags_listings_and_manifest_and_writes_no_extractor_output(data_dir):
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
    )
    assert main(["fetch-ads", "--select", "recruit", "--sample", "2", "--seed", "9", *FIX]) == 0
    run = next(json.loads(p.read_text()) for p in (data_dir / "runs").glob("*fetch-ads*.json"))
    rows = [json.loads(x) for x in (data_dir / run["sample"]["manifest"]).read_text().splitlines()]
    assert run["sample"]["select"] == "recruit" and {r["reason"] for r in rows} == {"recruit"}
    listings = list(store.read("listings"))
    assert len(listings) == 2 and {r["fetch_reason"] for r in listings} == {"recruit"}
    for r in listings:  # blind labelling: fetching writes no extractor output
        assert "derived" not in r and not (r.get("location") or {}).get("text_hint")


def _card_rows(err):
    text = err[err.index("== RUN CARD ==") :]
    return dict(ln.split(": ", 1) for ln in text.splitlines()[1:] if ": " in ln and not ln.startswith(" "))


def test_a_networked_dry_run_card_says_what_a_real_run_needs(observed, capsys):
    capsys.readouterr()  # default variant: a networked browser backend
    args = ["--select", "recruit", "--sample", "12", "--seed", "4", "--subcategories", "widgets"]
    assert main(["fetch-ads", *args, "--dry-run", "--max-tasks-per-minute", "7"]) == 0
    r = _card_rows(capsys.readouterr().err)
    assert r["Scale"].startswith("would send 13 requests (ad 12, robots 1) at 7.0/min"), r["Scale"]
    assert r["Approval"] == "needed for a real run: --plan, then the operator approves its plan_hash"
    assert r["Egress"] == "checked at run start: 1 request to the egress-check endpoint, never the site"
    assert FixtureHttpClient.log == []  # still a dry run: nothing sent


def test_an_offline_dry_run_card_still_says_offline(observed, capsys):
    capsys.readouterr()
    assert main(["fetch-ads", "--sample", "4", "--seed", "4", "--dry-run", *FIX]) == 0
    r = _card_rows(capsys.readouterr().err)
    assert r["Approval"] == "none needed (offline)" and r["Egress"] == "not checked"
