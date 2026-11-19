"""fetch-ads plans only ads with a FRESH good sighting that are alive and due, through plan.select_ads, and
signs its policy: the fixture site end to end, with exact GET counts. Every record here is SYNTHETIC."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIXTURES, LIST
from test_approvals import networked, operator_key  # noqa: F401 - fixtures
from test_sampling import FIX

from scrape_harness import approvals, cli, crawl, policy, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureBackend, FixtureHttpClient

SITE = "example.invalid"
OLD = "2026-01-15T11:58:00Z"


def card_obs(i, seen_at, sub="widgets", page=1, crawl_id=None):
    ad = f"{9000000 + i}"
    o = {
        "schema": "observation.v1",
        "ad_id": ad,
        "site": SITE,
        "seen_at": seen_at,
        "ad_url": f"{LIST.replace('widgets', sub)}/synthetic_ad-{ad}",
        "list": {
            "url": f"{LIST}?page={page}",
            "subcategory": sub,
            "page": page,
            "page_count": 9,
            "position": i + 1,
            "sponsored": False,
            "sample_weight": None,
        },  # fmt: skip
        "card": {"title": "t", "city_label": None},
        "bumped_at": None,
        "card_hash": f"sha256:{i:016x}",
    }
    if crawl_id:
        o["crawl_id"] = crawl_id
    return o


def crawled(*extra):
    """A real fixture crawl: ads 1000001-1000003, sighted NOW (a good crawl)."""
    args = ["crawl-lists", "--subcategory", "widgets", "--page-count", "3", "--stride", "2", "--start", "1"]
    assert main([*args, *FIX, *extra]) == 0
    FixtureHttpClient.log.clear()


def ad_gets():
    return [u for u in FixtureHttpClient.log if "-widget-" in u]  # the fixture site's ad pages


def fetch_run(data_dir):
    (doc,) = [json.loads(p.read_text()) for p in (data_dir / "runs").glob("*fetch-ads*.json")]
    return doc


def dry(capsys, *args):
    capsys.readouterr()
    assert main(["fetch-ads", *args, "--dry-run"]) == 0
    return [json.loads(x) for x in capsys.readouterr().out.splitlines()]


def gone_event(ad_id, url):
    crawl.event("ADS0", "gone", url, "expired_notice", site=SITE, ad_id=ad_id)


# ── the fresh-sighting gate ────────────────────────────────────────────────────────────────────────


def test_only_old_sightings_send_zero_gets_at_budget_300(data_dir, capsys):
    store.append("observations", [card_obs(i, OLD) for i in range(400)])
    FixtureHttpClient.log.clear()
    capsys.readouterr()
    assert main(["fetch-ads", "--max-fetches", "300", *FIX]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"planned": 0}
    assert FixtureHttpClient.log == [] and not list((data_dir / "runs").glob("*fetch-ads*"))
    assert dry(capsys, "--max-fetches", "300") == []


def test_a_fresh_crawl_is_fetched_and_the_backlog_stays_dormant(data_dir, capsys):
    store.append("observations", [card_obs(i, OLD) for i in range(50)])
    crawled()
    assert main(["fetch-ads", "--max-fetches", "300", *FIX]) == 0
    assert len(ad_gets()) == 3 and all("widget-100000" in u for u in ad_gets())
    ads = fetch_run(data_dir)["ads"]
    assert ads["lanes"] == {"new": 3, "revisit": 0} and ads["eligible"] == 3
    assert ads["excluded"] == {"tombstoned": 0, "dormant": 50, "quarantined": 0, "held": 0}
    assert ads["ad_gets"] == 3 and ads["ok"] == 3 and ads["gone"] == 0
    assert ads["policy_digest"] == policy.DEFAULT.digest() and ads["as_of"]


def test_a_tombstoned_ad_is_excluded(data_dir, capsys):
    crawled()
    (o,) = [o for o in store.read("observations") if o["ad_id"] == "1000002"]
    gone_event("1000002", o["ad_url"])
    assert {x["ad_id"] for x in dry(capsys)} == {"1000001", "1000003"}
    assert main(["fetch-ads", *FIX]) == 0
    assert len(ad_gets()) == 2 and not any("1000002" in u for u in ad_gets())
    assert fetch_run(data_dir)["ads"]["excluded"]["tombstoned"] == 1


def test_a_sighting_from_an_aborted_crawl_is_not_fresh(data_dir, capsys):
    crawled()
    (run,) = [json.loads(p.read_text()) for p in (data_dir / "runs").glob("*crawl-lists*.json")]
    crawl.append_correction(run_id=run["run_id"], field="status", recorded={"status": "completed"},
                            reconciled={"status": "aborted"}, note="synthetic", evidence=[])  # fmt: skip
    assert dry(capsys) == []


def test_sample_and_recruit_draw_only_from_the_gated_pool(data_dir, capsys):
    store.append("observations", [card_obs(i, OLD) for i in range(40)])
    crawled()
    picks = dry(capsys, "--sample", "20", "--seed", "3")
    assert picks[-1]["sampled"] == 3 and {x["ad_id"] for x in picks[:-1]} == {"1000001", "1000002", "1000003"}
    rec = dry(capsys, "--select", "recruit", "--sample", "20", "--seed", "3")
    assert rec[-1]["sampled"] == 3 and {x["reason"] for x in rec[:-1]} == {"recruit"}


def test_the_dry_run_card_shows_the_lanes_and_exclusions(data_dir, capsys):
    store.append("observations", [card_obs(i, OLD) for i in range(4)])
    crawled()
    capsys.readouterr()
    assert main(["fetch-ads", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "Selection: 3 eligible; new 3, revisit 0" in err and "dormant 4" in err


# ── the policy is signed ───────────────────────────────────────────────────────────────────────────


def planned(capsys, *extra):
    capsys.readouterr()
    assert main(["fetch-ads", *extra, "--plan", "--driver", "browser", "--max-tasks-per-minute", "7"]) == 0
    return json.loads(capsys.readouterr().out)


def test_the_plan_signs_as_of_and_the_policy_and_a_policy_change_changes_the_hash(data_dir, capsys):
    crawled()
    p = planned(capsys)
    assert p["plan"]["policy_digest"] == policy.DEFAULT.digest() and p["plan"]["as_of"]
    assert len(p["plan"]["ads"]) == 3
    for flag in (["--rho", "0.3"], ["--window-h", "48"], ["--stale-after-d", "6"]):
        q = planned(capsys, *flag)
        assert q["plan"]["ads"] == p["plan"]["ads"] and q["plan_hash"] != p["plan_hash"]


def test_window_h_is_the_freshness_bound(data_dir, capsys, monkeypatch):
    crawled()
    later = datetime.now(UTC) + timedelta(hours=73)
    monkeypatch.setattr(cli, "_now", lambda: later)
    assert dry(capsys) == []
    assert len(dry(capsys, "--window-h", "100")) == 3


NET = ["--driver", "fixture", "--fixtures", str(FIXTURES), "--max-tasks-per-minute", "6000",
       "--approve-politeness-override"]  # fmt: skip


def offline_crawl(monkeypatch):
    monkeypatch.setattr(FixtureBackend, "network", False)  # the list crawl is not what is under test
    crawled()
    monkeypatch.setattr(FixtureBackend, "network", True)


def test_the_run_reproduces_the_signed_as_of_29_minutes_later(networked, capsys, monkeypatch):  # noqa: F811
    offline_crawl(monkeypatch)
    t0 = datetime.now(UTC)
    monkeypatch.setattr(cli, "_now", lambda: t0)
    capsys.readouterr()
    assert main(["fetch-ads", *NET, "--plan"]) == 0
    p = json.loads(capsys.readouterr().out)
    assert main(["approve", p["plan_hash"]]) == 0
    monkeypatch.setattr(cli, "_now", lambda: t0 + timedelta(minutes=29))
    assert main(["fetch-ads", *NET, "--approve-network"]) == 0
    assert len(ad_gets()) == 3
    doc = fetch_run(networked)
    assert doc["approval_ref"]["plan_hash"] == p["plan_hash"] and doc["ads"]["as_of"] == p["plan"]["as_of"]


def test_a_policy_change_after_approval_is_refused_exit_10(networked, capsys, monkeypatch):  # noqa: F811
    offline_crawl(monkeypatch)
    capsys.readouterr()
    assert main(["fetch-ads", *NET, "--plan"]) == 0
    assert main(["approve", json.loads(capsys.readouterr().out)["plan_hash"]]) == 0
    assert main(["fetch-ads", *NET, "--rho", "0.5", "--approve-network"]) == approvals.EXIT_UNAPPROVED
    assert ad_gets() == []


@pytest.mark.parametrize("rho", ["1.5", "-1"])
def test_a_bad_rho_is_refused(data_dir, rho):
    with pytest.raises(SystemExit):
        main(["fetch-ads", "--rho", rho, "--dry-run"])


# ── the ledger reads what fetch-ads itself writes ────────────────────────────────────────────────────


def gone_fixtures(tmp_path, url):
    d = tmp_path / "fx"
    d.mkdir()
    for f in FIXTURES.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    urls = json.loads((FIXTURES / "urls.json").read_text()) | {url: {"file": "", "status": 410}}
    (d / "urls.json").write_text(json.dumps(urls))
    return d


def test_an_ad_a_run_found_gone_gets_no_get_on_the_next_run(data_dir, tmp_path, capsys):
    crawled()
    (o,) = [o for o in store.read("observations") if o["ad_id"] == "1000001"]
    d = gone_fixtures(tmp_path, o["ad_url"])
    fix = ["--driver", "fixture", "--fixtures", str(d), *FIX[4:]]
    assert main(["fetch-ads", *fix]) == 0
    assert [e["reason"] for e in store.read("events") if e["kind"] == "gone"] == ["http_410"]
    FixtureHttpClient.log.clear()
    crawled()  # the ad's card is still listed, a sighting AFTER the gone, inside the re-admit delay
    assert main(["fetch-ads", *FIX]) == 0
    assert ad_gets() == []  # its gone event (joined by url) tombstones it; the other two are not due
    assert dry(capsys) == []


def test_an_ad_whose_page_failed_to_parse_is_held_not_re_planned(data_dir, monkeypatch, capsys):
    from example_site.source import ExampleSite

    crawled()
    (o,) = [o for o in store.read("observations") if o["ad_id"] == "1000002"]
    real = ExampleSite.parse_ad

    def parse_ad(self, html, *, url, **kw):
        if url == o["ad_url"]:
            raise AttributeError("synthetic parser break")
        return real(self, html, url=url, **kw)

    monkeypatch.setattr(ExampleSite, "parse_ad", parse_ad)
    assert main(["fetch-ads", *FIX]) == 0 and len(ad_gets()) == 3
    monkeypatch.setattr(ExampleSite, "parse_ad", real)
    FixtureHttpClient.log.clear()
    crawled()  # listed again: a list sighting never clears a hold
    capsys.readouterr()
    assert main(["fetch-ads", "--dry-run"]) == 0
    out, err = capsys.readouterr()
    assert out == "" and "held 1" in err
    assert main(["fetch-ads", *FIX]) == 0 and ad_gets() == []
