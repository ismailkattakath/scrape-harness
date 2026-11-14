"""A revisit of a known ad that answers 404/410 with no challenge sign is "gone" (an event), never a
challenge; a first visit's 404 and a challenge served as a 404 still stop the run."""

import json

import pytest
from conftest import FIXTURES, LIST
from example_site.source import ExampleSite

from scrape_harness import crawl, store

FAST = crawl.Politeness(per_minute=6000, override=True)
AD = f"{LIST}/blue-widget-1000001"


def fixtures(tmp_path, entry):
    d = tmp_path / "fx"
    d.mkdir()
    for f in FIXTURES.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    urls = json.loads((FIXTURES / "urls.json").read_text()) | {AD: entry}
    (d / "urls.json").write_text(json.dumps(urls))
    return d


def run(d, revisit):
    job = crawl.Run.new("fetch-ads", "http", "example.invalid")
    meta = {"reason": "stale_7d", **({"revisit": True} if revisit else {})}
    crawl.run(
        job,
        ExampleSite(),
        lambda m, r: None,
        FAST,
        [{"url": AD, "kind": "ad", "meta": meta}],
        {"backend": "fixture", "root": str(d)},
    )
    return job


def events():
    return list(store.read("events"))


@pytest.mark.parametrize("status", [404, 410])
def test_a_revisit_404_or_410_is_gone_not_a_challenge(data_dir, tmp_path, status):
    job = run(fixtures(tmp_path, {"file": "", "status": status}), revisit=True)
    assert job.doc["status"] == "completed" and job.doc["challenges"] == 0
    (gone,) = [e for e in events() if e["kind"] == "gone"]
    assert gone["url"] == AD and gone["reason"] == f"http_{status}"
    assert not [e for e in events() if e["kind"] == "challenge"]


def test_a_first_visit_404_is_gone_too(data_dir, tmp_path):
    """The standard rule (operator decision 2026-11-08) replaced "a first visit 404 stops": a removed ad's
    404 is gone whether or not the ad was seen before, unless a challenge sign is present (next test)."""
    job = run(fixtures(tmp_path, {"file": "", "status": 404}), revisit=False)  # no Stop raised
    assert job.doc["status"] == "completed" and job.doc["challenges"] == 0
    assert [x["reason"] for x in events() if x["kind"] == "gone"] == ["http_404"]


def test_a_challenge_served_as_a_404_still_stops_a_revisit(data_dir, tmp_path):
    d = fixtures(tmp_path, {"file": "challenge-cf.html", "status": 404})
    with pytest.raises(crawl.Stop) as e:
        run(d, revisit=True)
    assert e.value.code == crawl.EXIT_CHALLENGE
    (ch,) = [x for x in events() if x["kind"] == "challenge"]
    assert ch["reason"] in ("challenge_title", "challenge_marker")
