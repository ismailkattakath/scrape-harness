"""Loud failures in the crawl handler: an exception inside the handler never reaches crawlee's retry (a
second GET is a second view of a real person's ad), and a list walk that lost a page never ends `completed`.

Every test runs the offline fixture backend over SYNTHETIC pages and counts the GETs it actually served.
"""

import errno
import json

import pytest
from conftest import FIXTURES, LIST, ORIGIN
from example_site.source import ExampleSite

from scrape_harness import contracts, crawl, incidents, refresh, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureHttpClient

FAST = crawl.Politeness(per_minute=6000, override=True)
P1, P3 = f"{LIST}?page=1", f"{LIST}?page=3"
AD1 = f"{LIST}/blue-widget-1000001"
AD2 = f"{LIST}/red-widget-1000002"
AD3 = f"{LIST}/green-widget-1000003"
EMPTY_LIST = (
    "<!doctype html><html><head><title>Widgets | Example</title></head><body>"
    '<main id="results"></main>'
    '<nav class="pager"><a href="/listings/widgets/sample-region?page=3">3</a></nav></body></html>'
)
NO_PAGER = (FIXTURES / "list-p1.html").read_text().split('<nav class="pager">')[0] + "</body></html>"


def fixtures(tmp_path, overrides=None, extra_files=()):
    d = tmp_path / "fx"
    d.mkdir(parents=True, exist_ok=True)
    for f in FIXTURES.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    for name, text in extra_files:
        (d / name).write_text(text)
    (d / "urls.json").write_text(
        json.dumps(json.loads((FIXTURES / "urls.json").read_text()) | (overrides or {}))
    )
    return d


def opts(d):
    return {"backend": "fixture", "root": str(d)}


def fix(d):
    return [
        "--driver",
        "fixture",
        "--fixtures",
        str(d),
        "--max-tasks-per-minute",
        "6000",
        "--approve-politeness-override",
    ]


def lists(*urls):
    return [{"url": u, "kind": "list", "meta": {"page": i}} for i, u in enumerate(urls, 1)]


def run_docs(data_dir):
    return [json.loads(p.read_text()) for p in sorted((data_dir / "runs").glob("*.json"))]


def served(prefix: str) -> list[str]:
    return [u for u in FixtureHttpClient.log if u.startswith(prefix)]


@pytest.fixture(autouse=True)
def fresh_log():
    FixtureHttpClient.log.clear()


# ── a handler exception never reaches crawlee's retry ───────────────────────────────────────────────
@pytest.mark.parametrize("code", [crawl.EXIT_PARSE, 4])
def test_a_handler_stop_is_recorded_terminal_and_sends_nothing_more(data_dir, code):
    job = crawl.Run.new("crawl-lists", "desktop", "example.invalid")

    def handle(m, r):
        raise crawl.Stop(code, "the page cannot be trusted")

    with pytest.raises(crawl.Stop) as e:
        crawl.run(job, ExampleSite(), handle, FAST, lists(P1, P3), opts(FIXTURES))
    assert e.value.code == crawl.EXIT_PARSE  # a request the handler stopped on is DONE: never resumable
    doc = crawl.Run.load(job.doc["run_id"]).doc
    assert doc["requests"]["list_pages"] == 1 and served(LIST) == [P1]  # no retry, page 3 never sent
    assert doc["status"] == "stopped"
    assert doc["stop"]["code"] == crawl.EXIT_PARSE and "cannot be trusted" in doc["stop"]["reason"]


# A write the DATA root refuses mid-run: the store's own refusal, or the OS's (a full disk, a permission
# error, a cloud-sync mount that went away). Both are resumable; neither is a parse failure.
REFUSED = [
    store.DataDirError("the data folder went away"),
    OSError(errno.ENOSPC, "No space left on device"),
    PermissionError(errno.EACCES, "Permission denied"),
]


@pytest.mark.parametrize("err", REFUSED, ids=type)
def test_a_data_dir_error_mid_run_is_resumable_exit_4_and_keeps_the_request_pending(data_dir, err):
    job = crawl.Run.new("crawl-lists", "desktop", "example.invalid")

    def handle(m, r):
        raise err

    with pytest.raises(crawl.Stop) as e:
        crawl.run(job, ExampleSite(), handle, FAST, lists(P1, P3), opts(FIXTURES))
    assert e.value.code == 4
    doc = crawl.Run.load(job.doc["run_id"]).doc
    assert doc["requests"]["list_pages"] == 1 and served(LIST) == [P1]
    assert doc["status"] == "stopped" and doc["stop"]["code"] == 4
    assert crawl.pending(job) == 2  # page 1 was handed back, page 3 never left the queue


def test_a_contract_error_on_a_list_page_is_terminal_exit_14(data_dir):
    job = crawl.Run.new("crawl-lists", "desktop", "example.invalid")

    def handle(m, r):
        raise contracts.ContractError("observation.v1: record 1: 'ad_id' is a required property")

    with pytest.raises(crawl.Stop) as e:
        crawl.run(job, ExampleSite(), handle, FAST, lists(P1, P3), opts(FIXTURES))
    assert e.value.code == crawl.EXIT_PARSE
    doc = crawl.Run.load(job.doc["run_id"]).doc
    assert served(LIST) == [P1] and doc["status"] == "stopped"
    assert doc["list"]["status"] == "failed" and doc["stop"]["code"] == crawl.EXIT_PARSE


# ── list rows are persisted before any stop; a missing page count is terminal ───────────────────────
def test_page_one_rows_are_written_before_its_page_count_stop(data_dir, tmp_path):
    d = fixtures(tmp_path, {P1: "nopager.html"}, [("nopager.html", NO_PAGER)])
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", *fix(d)]) == crawl.EXIT_PARSE
    (doc,) = run_docs(data_dir)
    rows = [o for o in store.read("observations") if o["crawl_id"] == doc["run_id"]]
    assert len(rows) == 3  # page 1's cards are kept: they were read, and a view is never wasted
    assert doc["list"] == {"status": "parse_drift", "observations_written": 3}
    assert doc["status"] == "stopped" and served(LIST) == [P1]
    # the run cannot be resumed into a reference crawl: refused before the gate, nothing sent
    assert main(["crawl", "resume", "--run-id", doc["run_id"], *fix(d)]) == crawl.EXIT_PARSE
    assert served(LIST) == [P1] and crawl.Run.load(doc["run_id"]).doc["status"] == "stopped"


def test_list_pages_with_no_observations_are_zero_yield_exit_14(data_dir, tmp_path):
    d = fixtures(tmp_path, {P1: "empty.html"}, [("empty.html", EMPTY_LIST)])
    code = main(["crawl-lists", "--subcategory", "widgets", "--page-count", "1", *fix(d)])
    assert code == crawl.EXIT_PARSE
    (doc,) = run_docs(data_dir)
    assert doc["list"] == {"status": "zero_yield", "observations_written": 0}
    assert doc["status"] == "stopped" and doc["stop"]["code"] == crawl.EXIT_PARSE
    assert doc["requests"]["list_pages"] == 1
    assert [i["kind"] for i in incidents.open_ones()] == ["parse"]  # loud: it rings
    assert not [e for e in store.read("events") if e["run_id"] == doc["run_id"] and e["kind"] == "completed"]
    assert main(["crawl", "resume", "--run-id", doc["run_id"], *fix(d)]) == crawl.EXIT_PARSE
    assert served(LIST) == [P1]


def test_a_good_crawl_records_the_observations_it_wrote(data_dir):
    assert (
        main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *fix(FIXTURES)])
        == 0
    )
    (doc,) = run_docs(data_dir)
    n = sum(1 for o in store.read("observations") if o["crawl_id"] == doc["run_id"])
    assert n > 0 and doc["list"] == {"status": "ok", "observations_written": n}
    assert doc["status"] == "completed" and "stop" not in doc


# ── a list page lost to a failure or to robots.txt never lets the walk end `completed` ──────────────
def test_a_failed_list_page_stops_the_walk_resumable_and_a_resume_rereads_it(data_dir, tmp_path):
    d = fixtures(tmp_path, {P3: {"file": "list-p3.html", "status": 502}})
    args = ["crawl-lists", "--subcategory", "widgets", "--page-count", "3", "--stride", "2", "--start", "1"]
    assert main([*args, *fix(d)]) == crawl.EXIT_INCOMPLETE
    (doc,) = run_docs(data_dir)
    assert doc["status"] == "stopped" and doc["list"]["status"] == "failed"
    assert doc["stop"]["code"] == crawl.EXIT_INCOMPLETE
    assert served(LIST) == [P1, P3, P3]  # page 3: crawlee's one retry of a 5xx, then it failed
    # the site recovers: the resume re-reads exactly the lost page, and only then the walk completes
    (d / "urls.json").write_text((FIXTURES / "urls.json").read_text())
    assert main(["crawl", "resume", "--run-id", doc["run_id"], *fix(d)]) == 0
    after = crawl.Run.load(doc["run_id"]).doc
    assert served(LIST) == [P1, P3, P3, P3]
    assert after["status"] == "completed" and after["list"]["status"] == "ok" and "stop" not in after


def test_a_list_page_robots_txt_skips_is_blocked_and_never_completed(data_dir):
    job = crawl.Run.new("crawl-lists", "desktop", "example.invalid")
    hidden = f"{ORIGIN}/private/widgets?page=2"
    with pytest.raises(crawl.Stop) as e:
        crawl.run(job, ExampleSite(), lambda m, r: None, FAST, lists(P1, hidden), opts(FIXTURES))
    assert e.value.code == crawl.EXIT_INCOMPLETE
    doc = crawl.Run.load(job.doc["run_id"]).doc
    assert doc["status"] == "stopped" and doc["list"]["status"] == "blocked"
    assert hidden not in FixtureHttpClient.log


# ── an ad page that cannot be parsed costs ONE view, once ───────────────────────────────────────────
def _crawl_and_plan(d):
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *fix(d)]) == 0
    FixtureHttpClient.log.clear()


@pytest.mark.parametrize("broken", ["raises", "invalid"])
def test_an_ad_parse_failure_is_one_get_one_event_and_the_run_goes_on(data_dir, monkeypatch, broken):
    _crawl_and_plan(FIXTURES)
    real = ExampleSite.parse_ad

    def parse_ad(self, html, *, url, **kw):
        rec = real(self, html, url=url, **kw)
        if url != AD2:
            return rec
        if broken == "raises":
            raise AttributeError("'NoneType' object has no attribute 'text'")
        return rec | {"content": None}  # fails listing.v1

    monkeypatch.setattr(ExampleSite, "parse_ad", parse_ad)
    assert main(["fetch-ads", *fix(FIXTURES)]) == 0
    fetch = run_docs(data_dir)[-1]
    assert fetch["requests"]["ad_pages"] == 3 and sorted(served(LIST)) == sorted([AD1, AD2, AD3])
    failed = [e for e in store.read("events") if e["kind"] == "parse_failed"]
    assert len(failed) == 1
    assert {k: failed[0][k] for k in ("run_id", "url", "ad_id", "site")} == {
        "run_id": fetch["run_id"],
        "url": AD2,
        "ad_id": "1000002",  # carried in the request's meta, so liveness can hold this ad
        "site": "example.invalid",
    }
    assert sorted(r["ad_id"] for r in store.read("listings")) == ["1000001", "1000003"]
    raw = data_dir / "raw" / store.safe_name("example.invalid") / "1000002"
    assert raw.is_dir() and len(list(raw.iterdir())) == 1  # the page is kept: a fixed parser reparses it
    assert not [e for e in store.read("events") if e["kind"] == "failed_request"]


@pytest.mark.parametrize("err", REFUSED, ids=type)
def test_a_data_dir_error_in_the_ad_handler_is_exit_4_after_one_get(data_dir, monkeypatch, err):
    _crawl_and_plan(FIXTURES)
    real = store.append

    def append(kind, records):
        if kind == "listings":
            raise err
        return real(kind, records)

    monkeypatch.setattr(store, "append", append)
    assert main(["fetch-ads", *fix(FIXTURES)]) == 4
    fetch = run_docs(data_dir)[-1]
    assert fetch["requests"]["ad_pages"] == 1 and len(served(LIST)) == 1
    assert fetch["status"] == "stopped" and fetch["stop"]["code"] == 4
    assert crawl.pending(crawl.Run(fetch)) == 3  # nothing lost: a resume fetches all three
    assert not [e for e in store.read("events") if e["kind"] == "parse_failed"]


# ── in a refresh, exit 14 is terminal ───────────────────────────────────────────────────────────────
def test_exit_14_ends_a_refresh_for_good():
    assert refresh._terminal({"approval_id": "x"}, {"name": "crawl-lists", "exit": 14, "reason": ""})


def test_a_list_page_handed_back_twice_is_still_read_before_the_walk_completes(data_dir):
    """A reclaimed request comes back with its one retry spent; a second DataDirError on it reaches
    crawlee's failed path. The page must still be queued again, or the walk completes without it."""
    job = crawl.Run.new("crawl-lists", "desktop", "example.invalid")

    def broken(m, r):
        raise store.DataDirError("the data folder went away")

    for requests in (lists(P1), None):  # one page, so both attempts hit the SAME request
        with pytest.raises(crawl.Stop) as e:
            crawl.run(job, ExampleSite(), broken, FAST, requests, opts(FIXTURES))
        assert e.value.code == 4  # the data folder's stop, both times: the first stop wins
        assert crawl.Run.load(job.doc["run_id"]).doc["status"] == "stopped"
    assert served(LIST) == [P1, P1]
    read = []
    crawl.run(job, ExampleSite(), lambda m, r: read.append(r.url), FAST, None, opts(FIXTURES))
    assert read == [P1] and served(LIST) == [P1, P1, P1]  # page 1 was read in the end: no hole
    assert crawl.Run.load(job.doc["run_id"]).doc["status"] == "completed"


# ── fetch-ads: every answered ad page failed to parse is a zero yield, loud ─────────────────────────
def test_fetch_ads_where_every_ad_fails_to_parse_is_zero_yield_exit_14(data_dir, monkeypatch):
    """Below the breaker's floor (3 failures < 5) nothing trips, so the postcheck must ring: no listing was
    written and at least one answered page could not be parsed. Terminal, a parse incident opens."""
    _crawl_and_plan(FIXTURES)

    def parse_ad(self, html, *, url, **kw):
        raise AttributeError("'NoneType' object has no attribute 'text'")

    monkeypatch.setattr(ExampleSite, "parse_ad", parse_ad)
    assert main(["fetch-ads", *fix(FIXTURES)]) == crawl.EXIT_PARSE
    fetch = run_docs(data_dir)[-1]
    assert fetch["requests"]["ad_pages"] == 3 and sorted(served(LIST)) == sorted([AD1, AD2, AD3])  # no retry
    assert fetch["ads"]["ok"] == 0 and fetch["ads"]["ad_gets"] == 3
    assert fetch["status"] == "stopped" and fetch["stop"]["code"] == crawl.EXIT_PARSE
    assert "zero yield" in fetch["stop"]["reason"]
    assert "breaker" not in fetch or not fetch["breaker"].get("tripped")  # the breaker did not do it
    assert [i["kind"] for i in incidents.open_ones()] == ["parse"]
    assert not [e for e in store.read("events") if e["run_id"] == fetch["run_id"] and e["kind"] == "completed"]


def test_fetch_ads_where_one_ad_parses_is_not_zero_yield(data_dir, monkeypatch):
    _crawl_and_plan(FIXTURES)
    real = ExampleSite.parse_ad

    def parse_ad(self, html, *, url, **kw):
        if url == AD1:
            return real(self, html, url=url, **kw)
        raise AttributeError("synthetic parser break")

    monkeypatch.setattr(ExampleSite, "parse_ad", parse_ad)
    assert main(["fetch-ads", *fix(FIXTURES)]) == 0
    fetch = run_docs(data_dir)[-1]
    assert fetch["ads"]["ok"] == 1 and fetch["status"] == "completed"
