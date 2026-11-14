"""run.v1 / ab.v1 request accounting, counted at the backend layer."""

import json

import pytest
from conftest import FIXTURES, LIST
from example_site.source import ExampleSite

from scrape_harness import crawl
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureBackend, FixtureHttpClient

FAST = crawl.Politeness(per_minute=6000, override=True)
# The fixture backend has no browser page: no sub-resource, redirect or pre-GET failure to count.
NEW_ZERO = {"site_subresources": 0, "third_party": 0, "redirects": 0, "failed_before_request": 0}
FIX = [
    "--driver",
    "fixture",
    "--fixtures",
    str(FIXTURES),
    "--max-tasks-per-minute",
    "6000",
    "--approve-politeness-override",
]


def runs(data_dir):
    return [json.loads(p.read_text()) for p in sorted((data_dir / "runs").glob("*.json"))]


def test_fixture_runs_report_exact_counts(data_dir):
    FixtureHttpClient.log.clear()
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    assert main(["fetch-ads", *FIX]) == 0
    crawl_run, fetch_run = runs(data_dir)
    assert crawl_run["requests"] == {
        "list_pages": 2,
        "ad_pages": 0,
        "robots": 1,
        "other": 0,
        "egress_trace": 0,
        **NEW_ZERO,
    }
    assert fetch_run["requests"] == {
        "list_pages": 0,
        "ad_pages": 3,
        "robots": 1,
        "other": 0,
        "egress_trace": 0,
        **NEW_ZERO,
    }
    site = sum(
        r["requests"][k]
        for r in (crawl_run, fetch_run)
        for k in ("list_pages", "ad_pages", "robots", "other")
    )
    assert site == len(FixtureHttpClient.log)  # the counter agrees with what the client actually served


def test_refused_at_preflight_reports_zero_site_requests(data_dir, monkeypatch):
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-at.txt").as_uri())
    monkeypatch.setattr(FixtureBackend, "network", True)
    job = crawl.Run.new("crawl-lists", "http", "example.invalid")
    with pytest.raises(crawl.Stop):
        crawl.run(
            job,
            ExampleSite(),
            lambda m, r: None,
            FAST,
            [{"url": f"{LIST}?page=1", "kind": "list", "meta": {}}],
            {"backend": "fixture", "root": str(FIXTURES)},
        )
    assert crawl.Run.load(job.doc["run_id"]).doc["requests"] == {
        "list_pages": 0,
        "ad_pages": 0,
        "robots": 0,
        "other": 0,
        "egress_trace": 1,
        **NEW_ZERO,
    }


def test_ab_reports_requests_per_variant(data_dir, capsys):
    capsys.readouterr()
    assert main(["ab", "--subcategory", "widgets", "--pages", "1", "--variants", "http,mobile", *FIX]) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    for name in ("http", "mobile"):
        assert report["variants"][name]["requests"] == {
            "list_pages": 1,
            "ad_pages": 0,
            "robots": 1,
            "other": 0,
            "egress_trace": 0,
            **NEW_ZERO,
        }


# ── every network attempt, not just navigations (audit of a 600-ad browser run, 2026-11-08) ─────


class FakeRequest:
    def __init__(self, url, *, navigation=False, main_frame=True, redirected_from=None):
        self.url = url
        self._nav = navigation
        self.frame = "main" if main_frame else "child"
        self.redirected_from = redirected_from

    def is_navigation_request(self):
        return self._nav


def test_browser_requests_are_counted_by_kind():
    counts = dict(crawl.ZERO)
    on_request = crawl.request_listener(counts, "example.invalid", main_frame="main")
    first = FakeRequest("https://www.example.invalid/ad/1", navigation=True)
    for r in [
        first,  # the page GET itself: counted by the pre-navigation hook, not here
        FakeRequest("https://www.example.invalid/ad/1b", navigation=True, redirected_from=first),
        FakeRequest("https://www.example.invalid/app.css"),
        FakeRequest("https://img.example.invalid/1.jpg"),  # the site's own subdomain
        FakeRequest("https://cdn.thirdparty.invalid/lib.js"),
        FakeRequest("https://frame.thirdparty.invalid/x", navigation=True, main_frame=False),  # an iframe
        FakeRequest("data:image/png;base64,AAAA"),  # no network
        FakeRequest("https://notexample.invalid/x"),  # a suffix match without the dot is NOT the site
    ]:
        on_request(r)
    assert (counts["redirects"], counts["site_subresources"], counts["third_party"]) == (1, 2, 3)


def test_an_attempt_that_failed_before_its_get_is_counted_as_such():
    counts = dict(crawl.ZERO)
    a = crawl.Attempts(counts)
    a.navigated("k1")
    a.errored("k1")  # failed after its GET: nothing extra
    a.errored("k2")  # failed before any GET (e.g. the browser did not launch)
    a.navigated("k3")
    a.done("k3")
    a.errored("k3")  # a later error for a finished attempt is not a pre-GET failure either
    assert counts["failed_before_request"] == 1


def test_crawlee_statistics_are_kept_per_run(data_dir):
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    from scrape_harness import store

    kv = store.state_dir() / "crawlee" / "key_value_stores" / "default"
    keys = sorted(p.name for p in kv.glob("__CRAWLER_STATISTICS_*") if not p.name.endswith(".json"))
    queues = sorted(r["queue"] for r in runs(data_dir))
    assert keys == sorted(f"__CRAWLER_STATISTICS_{q}" for q in queues)  # one per run, never a shared _0


def test_a_run_correction_event_validates():
    from scrape_harness import contracts

    e = crawl.correction_event(
        run_id="2026-01-01T00:00:00Z-fetch-ads-desktop-abc123",
        field="requests",
        recorded={"ad_pages": 3},
        reconciled={"ad_pages": 3, "site_subresources_min": 10},
        note="synthetic",
        evidence=["runs/x.log.jsonl"],
        site="example.invalid",
    )
    contracts.validate("event.v1", e)
    assert e["kind"] == "run_correction" and e["url"] is None


def test_a_correction_is_appended_never_rewriting_the_run(data_dir):
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    (run,) = runs(data_dir)
    rec = (data_dir / "runs").glob("*.json").__next__().read_bytes()
    from scrape_harness import store

    before = len(list(store.read("events")))
    crawl.append_correction(
        run_id=run["run_id"],
        field="requests",
        recorded=run["requests"],
        reconciled={"x_min": 1},
        note="synthetic",
        evidence=["runs/x.log.jsonl"],
    )
    evs = list(store.read("events"))
    assert len(evs) == before + 1 and evs[-1]["kind"] == "run_correction"
    assert (data_dir / "runs").glob("*.json").__next__().read_bytes() == rec  # the record is untouched
    with pytest.raises(SystemExit):
        crawl.append_correction(
            run_id="2026-01-01T00:00:00Z-nope",
            field="requests",
            recorded={},
            reconciled={},
            note="",
            evidence=[],
        )
