"""A positive END of a list (ADR-0017 A.1): a past-end list page is END only by the site's own marker.

`Source.end_selectors` (optional, the twin of `gone_selectors`) names the site's "no results" template.
`challenge.end_reason` admits the marker only on a page with no challenge sign: a clean page (reason None), a
page that merely lacks the list expectation (EXPECTED_MISSING) or a 404/410 (GONE). The list handler then
accepts END only with 0 parsed cards (skipped slots allowed) at the URL asked for. A page with 0 cards and no
END is `end_unknown` (page 1: `zero_yield`), exit 14. Every test runs the offline fixture backend over
SYNTHETIC pages and counts the GETs it actually served.
"""

import json

import pytest
from conftest import FIXTURES, LIST
from example_site.source import ExampleSite
from test_list_snapshots import body, crawl_lists, list_gets, sidecar
from test_loud_failures import fixtures, run_docs

from scrape_harness import challenge, contracts, crawl, store
from scrape_harness.drivers.fixture import FixtureHttpClient
from scrape_harness.interfaces import FetchResult, ListPage

P1, P3, P400 = (f"{LIST}?page={n}" for n in (1, 3, 400))
MARK = "#no-results"
END_HTML = (
    "<!doctype html><html><head><title>Not found | Example</title></head><body>"
    '<div id="no-results">No widgets match</div></body></html>'
)
# a past-end page that still renders the site's promoted block: slots the parser skips, no card it reads
PROMOTED_END = (
    "<!doctype html><html><head><title>Not found | Example</title></head><body>"
    '<main id="results"><article class="result promo"></article><article class="result promo"></article></main>'
    '<div id="no-results">No widgets match</div></body></html>'
)
P3_WITH_MARK = (
    (FIXTURES / "list-p3.html").read_text().replace("</body>", '<div id="no-results">x</div></body>')
)
EMPTY_WITH_MARK = (
    "<!doctype html><html><head><title>Widgets | Example</title></head><body>"
    '<main id="results"></main><div id="no-results">No widgets match</div></body></html>'
)


@pytest.fixture(autouse=True)
def fresh_log():
    FixtureHttpClient.log.clear()


@pytest.fixture
def marker(monkeypatch):
    monkeypatch.setattr(ExampleSite, "end_selectors", (MARK,), raising=False)


def res(status=200, html=END_HTML, final=None, headers=None, url=P400):
    return FetchResult(url, status, final or url, html, headers or {})


def events(kind):
    return [e for e in store.read("events") if e["kind"] == kind]


# ── the pure rule ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("reason", [None, "unexpected_page", *challenge.GONE])
def test_end_reason_admits_the_marker_on_a_page_with_no_challenge_sign(reason):
    assert challenge.end_reason(res(), reason, (MARK,)) == "end_marker"


@pytest.mark.parametrize("reason", ["signin_wall", "consent_gate"])
def test_end_reason_never_admits_a_wall(reason):
    """A sign-in wall or a consent gate is a stop (exit 5), whatever generic "no results" component its
    template also carries: a wall must never end a census early as a good crawl."""
    assert challenge.end_reason(res(), reason, (MARK,)) is None


def test_a_signin_wall_carrying_the_marker_stops_the_walk(data_dir, tmp_path, marker):
    wall = ("<!doctype html><html><head><title>Sign in | Example</title></head><body><h1>Sign in</h1>"
            '<form><input type="password"><input type="submit" value="Sign in"></form>'
            '<div id="no-results">No widgets match</div></body></html>')  # fmt: skip
    d = fixtures(tmp_path, {P3: "wall.html"}, [("wall.html", wall)])
    assert crawl_lists(d, "--pages", "1,3") == crawl.EXIT_CHALLENGE
    (doc,) = run_docs(data_dir)
    assert doc["list"].get("status") != "end" and events("challenge")


@pytest.mark.parametrize("status, nominated", [(404, True), (410, True), (200, False)])
def test_declared_end_statuses_are_the_only_statuses_that_nominate(status, nominated):
    """A source that declares end_statuses (its measured past-end answer, e.g. 404/410) ends a list only on
    them: a 200 "no results" page (a filter's empty result, a soft block) is never its END."""
    reason = None if status == 200 else f"http_{status}"
    got = challenge.end_reason(res(status), reason, (MARK,), end_statuses=(404, 410))
    assert got == ("end_marker" if nominated else None)


def test_without_end_statuses_any_admitted_status_nominates():
    assert challenge.end_reason(res(200), None, (MARK,)) == "end_marker"  # the default: as before


def test_a_200_no_results_page_mid_list_is_not_end_when_the_source_declares_its_end_statuses(
    data_dir, tmp_path, marker, monkeypatch
):
    from test_list_walk import P, site

    monkeypatch.setattr(ExampleSite, "end_statuses", (404, 410), raising=False)
    d = site(tmp_path, [[2000001], [2000002], [2000003]])
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(2): "soft.html"}))
    (d / "soft.html").write_text(EMPTY_WITH_MARK)
    assert crawl_lists(d, "--mode", "walk", "--max-pages", "10") == crawl.EXIT_PARSE
    (doc,) = run_docs(data_dir)
    assert doc["list"]["status"] == "end_unknown" and "end_read_page" not in doc["list"]


@pytest.mark.parametrize(
    "reason",
    ["http_403", "http_429", "http_503", "http_500", "cf_mitigated", "challenge_title", "captcha_iframe",
     "challenge_marker", "login_redirect"],
)  # fmt: skip
def test_end_reason_never_admits_a_challenge_sign_or_another_error(reason):
    assert challenge.end_reason(res(), reason, (MARK,)) is None


def test_end_reason_needs_a_declared_selector_that_matches():
    assert challenge.end_reason(res(), "http_404", ()) is None  # no end_selectors: the default
    assert challenge.end_reason(res(), "http_404", ("#elsewhere",)) is None
    assert challenge.end_reason(res(), "http_404", ("#elsewhere", MARK)) == "end_marker"


def test_end_reason_ignores_the_sources_chrome():
    assert challenge.end_reason(res(), None, (MARK,), ignore=(MARK,)) is None


@pytest.mark.parametrize(
    "page",
    [
        res(403),  # a block status
        res(200, headers={"cf-mitigated": "challenge"}),  # Cloudflare says it served a mitigation
        res(404, END_HTML.replace("Not found | Example", "Just a moment...")),  # a challenge title
        res(
            404,
            END_HTML.replace("</body>", '<script src="/cdn-cgi/challenge-platform/h/b/x"></script></body>'),
        ),
        res(404, END_HTML.replace("</body>", '<iframe src="https://hcaptcha.com/x"></iframe></body>')),
    ],
)
def test_detect_then_end_reason_never_turns_a_challenge_into_an_end(page):
    reason = challenge.detect(page, ExampleSite.expect["list"])
    assert reason is not None and challenge.end_reason(page, reason, (MARK,)) is None


# ── END on the default path (no --snapshot) ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [404, 410])
def test_past_end_404_with_marker_is_end(data_dir, tmp_path, marker, status):
    d = fixtures(tmp_path, {P400: {"file": "end.html", "status": status}}, [("end.html", END_HTML)])
    assert crawl_lists(d, "--pages", "1,3,400") == 0
    assert list_gets() == [P1, P3, P400]  # no retry of the END page
    (doc,) = run_docs(data_dir)
    contracts.validate("run.v1", doc)
    assert doc["status"] == "completed" and doc["challenges"] == 0 and "stop" not in doc
    assert doc["list"]["status"] == "end"
    assert (
        doc["list"]["end_read_page"] == 400
        and "end_page" not in doc["list"]
        and doc["list"]["end_method"] == "end_marker"
    )
    assert not events("challenge") and not events("cooldown")
    assert {o["list"]["page"] for o in store.read("observations")} == {1, 3}


def test_past_end_promoted_slots_is_end(data_dir, tmp_path, marker, monkeypatch):
    """R9: a past-end page may still render the promoted block. Slots the parser skips never make it drift,
    and never stop the END: END is decided first."""
    parse = ExampleSite.parse_list

    def counting(self, html, **kw) -> ListPage:
        lp = parse(self, html, **kw)
        lp.skipped = html.count('class="result promo"')
        return lp

    monkeypatch.setattr(ExampleSite, "parse_list", counting)
    d = fixtures(tmp_path, {P400: {"file": "end.html", "status": 404}}, [("end.html", PROMOTED_END)])
    assert crawl_lists(d, "--pages", "1,400") == 0
    (doc,) = run_docs(data_dir)
    assert (
        doc["list"]["status"] == "end"
        and doc["list"]["end_read_page"] == 400
        and "end_page" not in doc["list"]
    )


def test_a_200_page_with_the_marker_and_no_cards_is_end(data_dir, tmp_path, marker):
    d = fixtures(tmp_path, {P400: "end.html"}, [("end.html", EMPTY_WITH_MARK)])
    assert crawl_lists(d, "--pages", "1,400") == 0
    (doc,) = run_docs(data_dir)
    assert (
        doc["list"]["status"] == "end"
        and doc["list"]["end_read_page"] == 400
        and "end_page" not in doc["list"]
    )


def test_empty_subcategory_ok(data_dir, tmp_path, marker):
    """Page 1 with 0 cards is legitimate only with a positive END. Its template has no pager, so it must be
    END before the page-count check would call it parser drift."""
    d = fixtures(tmp_path, {P1: {"file": "end.html", "status": 404}}, [("end.html", END_HTML)])
    assert crawl_lists(d, "--stride", "2") == 0
    assert list_gets() == [P1]
    (doc,) = run_docs(data_dir)
    assert doc["status"] == "completed" and doc["list"]["status"] == "end"
    # page 1 ENDED: the list has 0 pages (end_page, exact), read on page 1 (end_read_page)
    assert doc["list"]["end_page"] == 0 and doc["list"]["end_read_page"] == 1
    assert doc["list"]["observations_written"] == 0


# ── what is NOT an END ─────────────────────────────────────────────────────────────────────────────────
def test_a_404_without_the_marker_is_still_a_challenge_stop(data_dir, tmp_path, marker):
    bare = END_HTML.replace('id="no-results"', 'id="other"')
    d = fixtures(tmp_path, {P400: {"file": "bare.html", "status": 404}}, [("bare.html", bare)])
    assert crawl_lists(d, "--pages", "1,400,3") == crawl.EXIT_CHALLENGE
    assert list_gets() == [P1, P400]
    (doc,) = run_docs(data_dir)
    assert doc["challenges"] == 1 and "end_read_page" not in doc["list"]


def test_without_end_selectors_a_marked_404_is_still_a_challenge_stop(data_dir, tmp_path):
    d = fixtures(tmp_path, {P400: {"file": "end.html", "status": 404}}, [("end.html", END_HTML)])
    assert crawl_lists(d, "--pages", "1,400,3") == crawl.EXIT_CHALLENGE
    assert list_gets() == [P1, P400]


def test_a_200_page_with_the_marker_and_cards_is_a_normal_page(data_dir, tmp_path, marker):
    d = fixtures(tmp_path, {P3: "p3.html"}, [("p3.html", P3_WITH_MARK)])
    assert crawl_lists(d, "--pages", "1,3") == 0
    (doc,) = run_docs(data_dir)
    assert doc["list"]["status"] == "ok" and "end_read_page" not in doc["list"]
    assert {o["list"]["page"] for o in store.read("observations")} == {1, 3}


def test_a_404_with_the_marker_and_cards_stops_and_keeps_none_of_its_cards(data_dir, tmp_path, marker):
    """Fail closed: a 404 used to stop the run. A marker that does not make it an END never lets its cards in."""
    d = fixtures(tmp_path, {P3: {"file": "p3.html", "status": 404}}, [("p3.html", P3_WITH_MARK)])
    assert crawl_lists(d, "--pages", "1,3,400") == crawl.EXIT_PARSE
    assert list_gets() == [P1, P3]
    (doc,) = run_docs(data_dir)
    assert doc["status"] == "stopped" and doc["list"]["status"] == "end_unknown"
    assert {o["list"]["page"] for o in store.read("observations")} == {1}  # page 1's rows are kept


@pytest.mark.parametrize("status", [200, 404])
def test_redirect_to_page1_with_the_marker_is_not_end(data_dir, tmp_path, marker, status):
    d = fixtures(tmp_path, {P400: {"file": "end.html", "status": status, "final_url": P1}},
                 [("end.html", EMPTY_WITH_MARK)])  # fmt: skip
    assert crawl_lists(d, "--pages", "1,400,3") == crawl.EXIT_PARSE
    assert list_gets() == [P1, P400]
    (doc,) = run_docs(data_dir)
    assert doc["list"]["status"] == "end_unknown" and "end_read_page" not in doc["list"]
    assert doc["stop"]["code"] == crawl.EXIT_PARSE


@pytest.mark.parametrize(
    "entry",
    [
        {"file": "end.html", "status": 403},
        {"file": "end.html", "status": 200, "headers": {"cf-mitigated": "challenge"}},
        {"file": "chl.html", "status": 404},
    ],
)
def test_end_marker_never_hides_challenge(data_dir, tmp_path, marker, entry):
    chl = END_HTML.replace("</body>", '<script src="/cdn-cgi/challenge-platform/h/b/x"></script></body>')
    d = fixtures(tmp_path, {P400: entry}, [("end.html", END_HTML), ("chl.html", chl)])
    assert crawl_lists(d, "--pages", "1,400,3") == crawl.EXIT_CHALLENGE
    assert list_gets() == [P1, P400]
    (doc,) = run_docs(data_dir)
    assert doc["challenges"] == 1 and "end_read_page" not in doc["list"]


def test_broken_selector_not_end(data_dir, tmp_path):
    """0 cards past page 1 with no marker (e.g. a card selector that no longer matches) is never an END."""
    empty = EMPTY_WITH_MARK.replace('<div id="no-results">No widgets match</div>', "")
    d = fixtures(tmp_path, {P3: "empty.html"}, [("empty.html", empty)])
    assert crawl_lists(d, "--pages", "1,3") == crawl.EXIT_PARSE
    (doc,) = run_docs(data_dir)
    assert doc["status"] == "stopped" and doc["list"]["status"] == "end_unknown"
    assert doc["list"]["observations_written"] == 3  # rows are kept: page 1 was read
    from scrape_harness import incidents

    assert [i["kind"] for i in incidents.open_ones()] == ["parse"]


def test_a_marker_on_page_one_never_rides_on_to_the_pages_it_plans(data_dir, tmp_path, marker):
    """Page 1 with cards AND the marker is a normal page. The sample it plans must not inherit the nomination:
    a planned page with 0 cards and no marker of its own is end_unknown, never an END."""
    p1 = (FIXTURES / "list-p1.html").read_text().replace("</body>", '<div id="no-results">x</div></body>')
    empty = EMPTY_WITH_MARK.replace('<div id="no-results">No widgets match</div>', "")
    d = fixtures(tmp_path, {P1: "p1.html", P3: "empty.html"}, [("p1.html", p1), ("empty.html", empty)])
    assert crawl_lists(d, "--stride", "2", "--start", "1") == crawl.EXIT_PARSE
    assert list_gets() == [P1, P3]
    (doc,) = run_docs(data_dir)
    assert doc["list"]["status"] == "end_unknown" and "end_read_page" not in doc["list"]


def test_a_200_page_without_the_list_expectation_but_with_the_marker_is_end(data_dir, tmp_path, marker):
    d = fixtures(tmp_path, {P400: "end.html"}, [("end.html", END_HTML)])  # no #results: unexpected_page
    assert crawl_lists(d, "--pages", "1,400") == 0
    (doc,) = run_docs(data_dir)
    assert (
        doc["challenges"] == 0
        and doc["list"]["status"] == "end"
        and doc["list"]["end_read_page"] == 400
        and "end_page" not in doc["list"]
    )


# ── --snapshot agrees with the default path ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [404, 410])
def test_a_snapshot_run_reads_a_marked_404_as_end(data_dir, tmp_path, marker, status):
    d = fixtures(tmp_path, {P400: {"file": "end.html", "status": status}}, [("end.html", END_HTML)])
    assert crawl_lists(d, "--pages", "1,3,400", "--snapshot") == 0
    assert list_gets() == [P1, P3, P400]
    (doc,) = run_docs(data_dir)
    rid = doc["run_id"]
    contracts.validate("run.v1", doc)
    assert (
        doc["list"]["status"] == "end"
        and doc["list"]["end_read_page"] == 400
        and "end_page" not in doc["list"]
    )
    assert doc["list"]["snapshot"]["past_end"] == [400]  # the page's own outcome stays past_end
    s = sidecar(data_dir, rid, 400)
    assert s["outcome"] == "past_end" and s["parsed"] == 0 and s["reason"] == f"http_{status}"
    assert body(data_dir, rid, 400) == END_HTML


def test_a_snapshot_run_records_a_200_end_without_the_list_expectation_as_listed(data_dir, tmp_path, marker):
    """Its sidecar must not say `stopped` (an exit-5 challenge stop): the page went to the parser and ended."""
    d = fixtures(tmp_path, {P400: "end.html"}, [("end.html", END_HTML)])
    assert crawl_lists(d, "--pages", "1,400", "--snapshot") == 0
    (doc,) = run_docs(data_dir)
    assert (
        doc["list"]["status"] == "end"
        and doc["list"]["end_read_page"] == 400
        and "end_page" not in doc["list"]
    )
    s = sidecar(data_dir, doc["run_id"], 400)
    assert s["outcome"] == "listed" and s["reason"] == "unexpected_page" and s["parsed"] == 0
    assert body(data_dir, doc["run_id"], 400) == END_HTML


def test_a_snapshot_run_keeps_a_bare_404_as_evidence(data_dir, tmp_path, marker):
    bare = END_HTML.replace('id="no-results"', 'id="other"')
    d = fixtures(tmp_path, {P400: {"file": "bare.html", "status": 404}}, [("bare.html", bare)])
    assert crawl_lists(d, "--pages", "1,400", "--snapshot") == 0
    (doc,) = run_docs(data_dir)
    assert doc["list"]["status"] == "past_end" and "end_read_page" not in doc["list"]


def test_a_snapshot_run_with_an_end_and_a_bare_404_is_evidence_not_an_end(data_dir, tmp_path, marker):
    bare = END_HTML.replace('id="no-results"', 'id="other"')
    p401 = f"{LIST}?page=401"
    d = fixtures(tmp_path, {P400: {"file": "end.html", "status": 404}, p401: {"file": "bare.html", "status": 404}},
                 [("end.html", END_HTML), ("bare.html", bare)])  # fmt: skip
    assert crawl_lists(d, "--pages", "1,400,401", "--snapshot") == 0
    (doc,) = run_docs(data_dir)
    assert (
        doc["list"]["status"] == "past_end"
        and doc["list"]["end_read_page"] == 400
        and "end_page" not in doc["list"]
    )


def test_a_snapshot_run_with_a_redirected_marked_page_keeps_it_as_redirected(data_dir, tmp_path, marker):
    d = fixtures(tmp_path, {P400: {"file": "end.html", "final_url": P1}}, [("end.html", EMPTY_WITH_MARK)])
    assert crawl_lists(d, "--pages", "1,400", "--snapshot") == 0
    (doc,) = run_docs(data_dir)
    assert doc["list"]["status"] == "redirected" and "end_read_page" not in doc["list"]


# ── contracts ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "status", ["end", "end_unknown", "short_census", "ceiling_hit", "anchored", "clamped"]
)
def test_the_walk_list_statuses_validate(status):
    run = json.loads(
        (FIXTURES.parent.parent / "schemas" / "examples" / "run.list-stop.example.json").read_text()
    )
    contracts.validate("run.v1", run | {"list": {"status": status, "observations_written": 0}})


def test_end_page_and_method_validate_and_are_closed():
    run = json.loads(
        (FIXTURES.parent.parent / "schemas" / "examples" / "run.list-stop.example.json").read_text()
    )
    lst = {
        "status": "end",
        "observations_written": 3,
        "end_page": 3,
        "end_read_page": 4,
        "end_method": "end_marker",
    }
    contracts.validate("run.v1", run | {"list": lst})
    contracts.validate("run.v1", run | {"list": lst | {"end_page": 0, "end_read_page": 1}})  # empty: 0 pages
    for bad in ({"end_page": -1}, {"end_read_page": 0}, {"end_method": "guess"}):
        with pytest.raises(contracts.ContractError):
            contracts.validate("run.v1", run | {"list": lst | bad})
