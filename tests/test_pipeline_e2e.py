"""The generic pipeline, offline: fixture driver, the example source, real CLI, real storage."""

import json
import subprocess

import pytest
from conftest import EXAMPLE, EXAMPLES, FIXTURES, LIST

from scrape_harness import contracts, store
from scrape_harness.cli import EXIT_CONFIG, EXIT_CONTRACT, main

FAST = ["--max-tasks-per-minute", "6000", "--approve-politeness-override"]  # tests only, explicit
FIX = ["--driver", "fixture", "--fixtures", str(FIXTURES), *FAST]


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


@pytest.fixture
def no_sleep():
    return None


def test_end_to_end(data_dir, capsys, no_sleep):
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    obs = lines(data_dir / "observations.jsonl")
    assert len(obs) == 4  # page 1 (3 cards) + page 3 (1 card); page count read from page 1's pager
    assert {o["list"]["sample_weight"] for o in obs} == {1.5}  # 3 pages / 2 sampled

    assert main(["fetch-ads", "--dry-run"]) == 0
    plan = [json.loads(x) for x in capsys.readouterr().out.splitlines() if '"reason"' in x]
    assert {p["ad_id"] for p in plan} == {"1000001", "1000002", "1000003"}  # sponsored card skipped
    assert all(p["reason"] == "new" for p in plan)

    assert main(["fetch-ads", *FIX]) == 0
    assert len(lines(data_dir / "listings.jsonl")) == 3
    assert main(["fetch-ads", *FIX]) == 0  # second run: nothing new, nothing changed -> no GET
    assert len(lines(data_dir / "listings.jsonl")) == 3

    for kind in ("observations", "listings"):
        assert list(store.read(kind))  # every file re-validates against its contract

    assert main(["findings", "append", str(EXAMPLES / "finding.example.json")]) == 0
    assert (
        main(["findings", "append", str(EXAMPLES / "invalid" / "finding.bad-status.json")]) == EXIT_CONTRACT
    )
    assert len(lines(data_dir / "findings.jsonl")) == 1  # the bad batch wrote nothing


def test_ab_compares_variants_on_list_pages_only(data_dir, capsys, no_sleep):
    assert main(["ab", "--subcategory", "widgets", "--pages", "1", *FIX]) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert set(report["variants"]) == {"http", "desktop", "desktop-nofp", "mobile"}
    assert all(v["stopped"] is None for v in report["variants"].values())
    assert report["variants"]["desktop"]["cards"] == 2  # sponsored excluded
    assert report["variants"]["desktop"]["coverage"]["city_label"] == 1.0
    assert not (data_dir / "observations.jsonl").exists() and not (data_dir / "listings.jsonl").exists()


def test_ab_all_403_stops_after_two_page_requests_and_cools_the_site(data_dir, tmp_path, capsys):
    from scrape_harness.drivers.fixture import FixtureHttpClient

    fx = tmp_path / "fx"
    fx.mkdir()
    for f in FIXTURES.iterdir():
        (fx / f.name).write_bytes(f.read_bytes())
    urls = json.loads((FIXTURES / "urls.json").read_text())
    urls[f"{LIST}?page=1"] = {"file": "challenge-cf.html", "status": 403, "headers": {"server": "cloudflare"}}
    (fx / "urls.json").write_text(json.dumps(urls))
    FixtureHttpClient.log.clear()
    code = main(
        [
            "ab",
            "--subcategory",
            "widgets",
            "--pages",
            "2",
            "--driver",
            "fixture",
            "--fixtures",
            str(fx),
            *FAST,
        ]
    )
    assert code == 6
    pages = [u for u in FixtureHttpClient.log if "page=" in u]
    assert pages == [f"{LIST}?page=1", f"{LIST}?page=1"]  # variant 1, variant 2; variants 3-4 never start
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert list(report["variants"]) == ["http", "desktop"]
    assert all(v["stopped"] == "http_403" for v in report["variants"].values())
    ev = lines(data_dir / "events.jsonl")
    assert [e["kind"] for e in ev].count("challenge") == 2 and [e["kind"] for e in ev].count("cooldown") == 1
    assert next(e for e in ev if e["kind"] == "challenge")["evidence"]["server"] == "cloudflare"
    assert main(["ab", "--subcategory", "widgets", "--driver", "fixture", "--fixtures", str(fx), *FAST]) == 6
    assert [u for u in FixtureHttpClient.log if "page=" in u] == pages  # cooling down: not one more request


def test_cli_challenge_exit_and_resume(data_dir, tmp_path, capsys, no_sleep):
    fx = tmp_path / "fx"
    fx.mkdir()
    for f in FIXTURES.iterdir():
        (fx / f.name).write_bytes(f.read_bytes())
    urls = json.loads((FIXTURES / "urls.json").read_text())
    good = urls[f"{LIST}?page=3"]
    urls[f"{LIST}?page=3"] = {"file": "challenge-cf.html", "status": 200}
    (fx / "urls.json").write_text(json.dumps(urls))
    args = [
        "crawl-lists",
        "--subcategory",
        "widgets",
        "--page-count",
        "3",
        "--stride",
        "2",
        "--start",
        "1",
        "--driver",
        "fixture",
        "--fixtures",
        str(fx),
        *FAST,
    ]
    assert main(args) == 5
    assert "STOPPED: challenge_title" in capsys.readouterr().err
    urls[f"{LIST}?page=3"] = good  # the operator cleared it
    (fx / "urls.json").write_text(json.dumps(urls))
    assert main(["crawl", "resume", "--driver", "fixture", "--fixtures", str(fx), *FAST]) == 0
    obs = lines(data_dir / "observations.jsonl")
    assert sorted(o["list"]["page"] for o in obs) == [1, 1, 1, 3]  # no duplicate sightings of page 1


def test_network_runs_refuse_without_approval(data_dir, monkeypatch):
    # with the network flag but no signed approval: refused at the approval (exit 10)
    assert main(["crawl-lists", "--subcategory", "x", "--driver", "browser", "--approve-network"]) == 10
    # without the flag: refused on the flag FIRST, before any approval could be spent (exit 3)
    with pytest.raises(SystemExit) as e:
        main(["crawl-lists", "--subcategory", "x", "--driver", "browser"])
    assert e.value.code == 3


def test_data_dir_inside_a_repo_is_refused(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setenv("EXAMPLE_DATA", str(tmp_path / "data"))
    assert main(["findings", "current"]) == EXIT_CONFIG


def test_validate_subcommand(capsys):
    ref = EXAMPLE / "schemas" / "examples"
    assert main(["validate", "observation.v1", str(ref / "observation.example.json")]) == 0
    assert main(["validate", "observation.v1", str(ref / "invalid" / "observation.bad-page.json")]) == 2
    assert contracts.names()
