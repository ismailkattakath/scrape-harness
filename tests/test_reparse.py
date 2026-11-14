"""Raw ad snapshots and offline reparse: new selectors applied with ZERO new page views."""

import gzip
import hashlib
import json

import pytest
from conftest import FIXTURES

from scrape_harness import store
from scrape_harness.cli import EXIT_CONTRACT, latest_listings, main

FIX = [
    "--driver",
    "fixture",
    "--fixtures",
    str(FIXTURES),
    "--max-tasks-per-minute",
    "6000",
    "--approve-politeness-override",
]


@pytest.fixture
def fetched(data_dir):
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    assert main(["fetch-ads", *FIX]) == 0
    return data_dir


@pytest.fixture
def offline(monkeypatch):
    """Any attempt to build a fetch backend fails the test (sockets are already blocked in conftest)."""

    def refuse(*_a, **_k):
        raise AssertionError("reparse built a fetch backend")

    for mod, cls in (
        ("fixture", "FixtureBackend"),
        ("browser", "BrowserBackend"),
        ("http", "ImpitHttpBackend"),
    ):
        monkeypatch.setattr(f"scrape_harness.drivers.{mod}.{cls}.__init__", refuse)


def lines(p):
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def test_every_fetched_version_has_a_matching_snapshot(fetched):
    recs = lines(fetched / "listings.jsonl")
    assert len(recs) == 3
    for r in recs:
        ref = r["raw_ref"]
        body = gzip.decompress((fetched / ref["path"]).read_bytes())
        assert hashlib.sha256(body).hexdigest() == ref["sha256"] and len(body) == ref["bytes"]
        assert (
            ref["path"].startswith(f"raw/example.invalid/{r['ad_id']}/")
            and 'id="description"' in body.decode()
        )


def test_reparse_of_unchanged_snapshots_is_identical_and_appends_nothing(fetched, offline, capsys):
    before = (fetched / "listings.jsonl").read_text()
    capsys.readouterr()
    assert main(["reparse"]) == 0
    assert json.loads(capsys.readouterr().out) == {"snapshots": 3, "reparsed": 0, "unchanged": 3}
    assert (fetched / "listings.jsonl").read_text() == before


def test_a_new_selector_is_applied_offline_as_a_reparse_version(fetched, offline, monkeypatch, capsys):
    from example_site.source import ExampleSite

    original = ExampleSite.parse_ad

    def with_website(self, html, **kw):  # stands in for a future contact selector
        rec = original(self, html, **kw)
        rec["content"]["contact"]["website"] = "https://example.invalid/synthetic"
        return rec

    monkeypatch.setattr(ExampleSite, "parse_ad", with_website)
    monkeypatch.setattr(ExampleSite, "parser", "example-ad@2")
    capsys.readouterr()
    assert main(["reparse", "--site", "example.invalid"]) == 0
    assert json.loads(capsys.readouterr().out)["reparsed"] == 3
    new = [r for r in lines(fetched / "listings.jsonl") if r["fetch_reason"] == "reparse"]
    assert len(new) == 3 and all(r["derived"]["method"] == "example-ad@2" for r in new)
    old = {r["ad_id"]: r for r in lines(fetched / "listings.jsonl") if r["fetch_reason"] != "reparse"}
    for r in new:
        assert r["fetched_at"] == old[r["ad_id"]]["fetched_at"] and r["raw_ref"] == old[r["ad_id"]]["raw_ref"]
    assert all(r["content"]["contact"]["website"] for r in latest_listings())  # the reparse is now current
    for r in store.read("listings"):
        assert r  # every line, reparse versions included, re-validates as listing.v1
    assert main(["fetch-ads", "--dry-run"]) == 0  # the refetch rule still plans nothing new
    assert "reason" not in capsys.readouterr().out


def test_a_tampered_snapshot_is_refused(fetched, offline):
    ref = lines(fetched / "listings.jsonl")[0]["raw_ref"]
    p = fetched / ref["path"]
    p.write_bytes(gzip.compress(b"<html>edited</html>", mtime=0))
    assert main(["reparse"]) == EXIT_CONTRACT


def test_since_filters_by_fetch_time(fetched, offline, capsys):
    capsys.readouterr()
    assert main(["reparse", "--since", "2999-01-01T00:00:00Z"]) == 0
    assert json.loads(capsys.readouterr().out) == {"snapshots": 3, "reparsed": 0, "unchanged": 0}
