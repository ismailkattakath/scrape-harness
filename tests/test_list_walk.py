"""The census walk (ADR-0017 §A, `crawl-lists --mode walk`, stride 1): pages 1, 2, 3, ... with the NEXT page
enqueued only after the current one is handled. No page count is read (a windowed pager links only a few pages
ahead), nothing is prefetched. The walk stops at a positive END (`list.status: end`, `end_page` = the last page
of the list, exact), at the signed ceiling (`--max-pages` = every list GET, the END page included:
`ceiling_hit`), at a clamp (more than half of a page's ids already seen earlier in this walk: `end_method:
clamped`), or before `--min-pages` (`short_census`, exit 14). Every test runs the offline fixture backend over
SYNTHETIC pages and counts the GETs it actually served.
"""

import dataclasses
import json

import pytest
from conftest import LIST
from example_site.source import ExampleSite
from test_approvals import networked, operator_key, plan_hash  # noqa: F401 - fixtures, used by name
from test_list_snapshots import crawl_lists, list_gets
from test_loud_failures import fix, fixtures, run_docs

from scrape_harness import approvals, cli, contracts, crawl, liveness, policy, refresh, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureHttpClient

MARK = "#no-results"
END_HTML = (
    "<!doctype html><html><head><title>Not found | Example</title></head><body>"
    '<div id="no-results">No widgets match</div></body></html>'
)


def P(n: int) -> str:
    return f"{LIST}?page={n}"


def page(ids) -> str:
    """A SYNTHETIC list page with one card per id, and no pager at all (the walk never reads one)."""
    cards = "".join(
        f'<article class="result" data-id="{i}"><a class="result__link" '
        f'href="/listings/widgets/sample-region/widget-{i}"><h2 class="result__title">Widget {i}</h2></a>'
        '<span class="result__city">Sampletown</span><span class="result__price">10</span></article>'
        for i in ids
    )
    return (
        "<!doctype html><html><head><title>Widgets | Example</title></head><body>"
        f'<main id="results">{cards}</main></body></html>'
    )


def site(tmp_path, pages: list[list[int]], end: int | None = 404):
    """pages[i] = the ids on page i+1; then page len(pages)+1 answers the END template (status `end`), or is
    absent (the fixture client's bare 404) when `end` is None."""
    over, files = {}, []
    for n, ids in enumerate(pages, 1):
        over[P(n)] = f"walk-{n}.html"
        files.append((f"walk-{n}.html", page(ids)))
    if end is not None:
        over[P(len(pages) + 1)] = {"file": "walk-end.html", "status": end}
        files.append(("walk-end.html", END_HTML))
    return fixtures(tmp_path, over, files)


THREE = [[2000001, 2000002], [2000003, 2000004], [2000005, 2000006]]


@pytest.fixture(autouse=True)
def fresh_log():
    FixtureHttpClient.log.clear()


@pytest.fixture
def marker(monkeypatch):
    monkeypatch.setattr(ExampleSite, "end_selectors", (MARK,), raising=False)


def walk(d, *extra) -> int:
    return crawl_lists(d, "--mode", "walk", *extra)


def the_doc(data_dir) -> dict:
    (doc,) = run_docs(data_dir)
    return contracts.validate("run.v1", doc)


def rows(doc) -> list[dict]:
    return [o for o in store.read("observations") if o["crawl_id"] == doc["run_id"]]


# ── the census to a positive END ───────────────────────────────────────────────────────────────────────
def test_census_to_positive_end(data_dir, tmp_path, marker):
    """A 3-page list and its past-end 404: exactly 4 list GETs, in order, each once; END at page 3, exact."""
    d = site(tmp_path, THREE)
    assert walk(d, "--max-pages", "10") == 0
    assert list_gets() == [P(1), P(2), P(3), P(4)]
    doc = the_doc(data_dir)
    lst = doc["list"]
    assert doc["status"] == "completed" and "stop" not in doc and doc["challenges"] == 0
    assert lst["status"] == "end" and lst["mode"] == "walk" and lst["stride"] == 1
    assert lst["end_page"] == 3 and lst["end_read_page"] == 4 and lst["end_method"] == "end_marker"
    assert lst["pages_read"] == 4 and lst["settled"] == 4 and lst["ceiling"] == 10
    assert lst["observations_written"] == 6
    got = rows(doc)
    assert sorted({o["list"]["page"] for o in got}) == [1, 2, 3]
    # census rows: weight 1, and no page count from a pager (the run doc's end_page is the count)
    assert {(o["list"]["page_count"], o["list"]["sample_weight"]) for o in got} == {(None, 1)}


def test_the_walk_enqueues_one_page_at_a_time(data_dir, tmp_path, marker, monkeypatch):
    """No prefetch: when page n is handled, the queue holds nothing beyond page n (page n+1 only after it)."""
    seen = []
    real = cli.list_handler

    def spying(job, source, max_pages):
        h = real(job, source, max_pages)

        def handle(m, res):
            new = h(m, res)
            seen.append((m["page"], [r["meta"]["page"] for r in new or []]))
            return new

        return handle

    monkeypatch.setattr(cli, "list_handler", spying)
    assert walk(site(tmp_path, THREE), "--max-pages", "10") == 0
    assert seen == [(1, [2]), (2, [3]), (3, [4]), (4, [])]


def test_an_empty_subcategory_ends_at_page_zero(data_dir, tmp_path, marker):
    d = site(tmp_path, [])
    assert walk(d) == 0
    assert list_gets() == [P(1)]
    lst = the_doc(data_dir)["list"]
    assert lst["status"] == "end" and lst["end_page"] == 0 and lst["end_read_page"] == 1
    assert lst["observations_written"] == 0


def test_a_bare_404_is_still_a_challenge_stop_in_a_walk(data_dir, tmp_path, marker):
    """No end marker, no END: the fail-closed rule of A.1 holds in a walk too."""
    d = site(tmp_path, THREE, end=None)
    assert walk(d) == crawl.EXIT_CHALLENGE
    assert list_gets() == [P(1), P(2), P(3), P(4)]
    assert the_doc(data_dir)["list"].get("status") != "end"


# ── the signed ceiling: every list GET, the END page included ─────────────────────────────────────────
def test_the_ceiling_is_max_pages_exactly(data_dir, tmp_path, marker):
    d = site(tmp_path, THREE)
    assert walk(d, "--max-pages", "3") == 0
    assert list_gets() == [P(1), P(2), P(3)]  # never page 4: the END page is a GET too
    doc = the_doc(data_dir)
    assert doc["status"] == "completed" and doc["list"]["status"] == "ceiling_hit"
    assert "end_page" not in doc["list"] and doc["list"]["pages_read"] == 3


def test_a_ceiling_one_higher_reaches_the_end(data_dir, tmp_path, marker):
    assert walk(site(tmp_path, THREE), "--max-pages", "4") == 0
    assert list_gets() == [P(1), P(2), P(3), P(4)]
    assert the_doc(data_dir)["list"]["status"] == "end"


# ── the clamp guard ────────────────────────────────────────────────────────────────────────────────────
def test_clamp_to_last(data_dir, tmp_path, marker):
    """A site that serves its last page again for every page after it: page 3 repeats page 2's ids. END by
    clamp at the last non-repeated page; the repeated page's cards are not kept."""
    d = site(tmp_path, [THREE[0], THREE[1], THREE[1]], end=None)
    assert walk(d) == 0
    assert list_gets() == [P(1), P(2), P(3)]
    doc = the_doc(data_dir)
    lst = doc["list"]
    assert lst["status"] == "end" and lst["end_method"] == "clamped"
    assert lst["end_page"] == 2 and lst["end_read_page"] == 3
    assert sorted({o["list"]["page"] for o in rows(doc)}) == [1, 2] and lst["observations_written"] == 4


def test_redirect_to_page1(data_dir, tmp_path, marker):
    """A past-end page that redirects to page 1 serves page 1's cards again: a clamp, not new content."""
    d = site(tmp_path, THREE[:2], end=None)
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(3): {"file": "walk-1.html", "final_url": P(1)}}))
    assert walk(d) == 0
    assert list_gets() == [P(1), P(2), P(3)]
    doc = the_doc(data_dir)
    assert doc["list"]["end_method"] == "clamped" and doc["list"]["end_page"] == 2
    assert sorted({o["list"]["page"] for o in rows(doc)}) == [1, 2]


def test_a_page_read_again_is_not_a_clamp_of_itself(data_dir, tmp_path, marker, monkeypatch):
    """A page whose rows were kept but whose walk state was not saved is read again by the resume: its own ids
    are not "seen earlier in this walk", so it never clamps the walk at the page before it."""
    real, raised = crawl.Run.save, []

    def flaky(self):
        if (self.doc.get("list") or {}).get("pages_read") == 2 and not raised:
            raised.append(1)
            raise OSError("synthetic: the data folder went away")
        return real(self)

    monkeypatch.setattr(crawl.Run, "save", flaky)
    d = site(tmp_path, THREE)
    assert walk(d) == crawl.EXIT_DATA
    rid = the_doc(data_dir)["run_id"]
    FixtureHttpClient.log.clear()
    assert main(["crawl", "resume", "--run-id", rid, *fix(d)]) == 0
    assert list_gets() == [P(2), P(3), P(4)]
    lst = the_doc(data_dir)["list"]
    assert lst["status"] == "end" and lst["end_method"] == "end_marker" and lst["end_page"] == 3


def test_half_repeated_is_not_a_clamp(data_dir, tmp_path, marker):
    """Exactly half of a page's ids seen before (a re-bump across the boundary) is not a clamp: MORE than half."""
    d = site(tmp_path, [[2000001, 2000002], [2000002, 2000003]])
    assert walk(d) == 0
    lst = the_doc(data_dir)["list"]
    assert lst["end_method"] == "end_marker" and lst["end_page"] == 2


# ── the short-census guard ─────────────────────────────────────────────────────────────────────────────
def test_short_census_is_exit_14_and_never_resumed(data_dir, tmp_path, marker):
    d = site(tmp_path, THREE)
    assert walk(d, "--min-pages", "4") == crawl.EXIT_PARSE
    assert list_gets() == [P(1), P(2), P(3), P(4)]
    doc = the_doc(data_dir)
    assert doc["status"] == "stopped" and doc["list"]["status"] == "short_census"
    assert doc["list"]["end_page"] == 3 and doc["list"]["min_pages"] == 4
    assert main(["crawl", "resume", "--run-id", doc["run_id"], *fix(d)]) == crawl.EXIT_PARSE


def test_a_census_of_exactly_min_pages_passes(data_dir, tmp_path, marker):
    assert walk(site(tmp_path, THREE), "--min-pages", "3") == 0
    assert the_doc(data_dir)["list"]["status"] == "end"


def test_a_clamp_before_min_pages_is_short(data_dir, tmp_path, marker):
    d = site(tmp_path, [THREE[0], THREE[0]], end=None)  # page 2 repeats page 1: a pager the site ignores
    assert walk(d, "--min-pages", "2") == crawl.EXIT_PARSE
    assert the_doc(data_dir)["list"]["status"] == "short_census"


# ── refusals: what this slice does not build ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "extra, word",
    [
        (["--stride", "2"], "deferred"),
        (["--pages", "1,2"], "--pages"),
        (["--page-count", "9"], "--page-count"),
        (["--start", "1"], "--start"),
        (["--seed", "3"], "--seed"),
        (["--min-pages", "61"], "--max-pages"),
    ],
)
def test_walk_refuses_what_it_cannot_honour(data_dir, tmp_path, capsys, extra, word):
    with pytest.raises(SystemExit) as e:
        walk(fixtures(tmp_path), *extra)
    assert word in str(e.value)
    assert list_gets() == []


def test_min_pages_needs_the_walk(data_dir, tmp_path):
    with pytest.raises(SystemExit) as e:
        crawl_lists(fixtures(tmp_path), "--min-pages", "2")
    assert "--mode walk" in str(e.value)


# ── signing: mode and min_pages are signed, and only when set ──────────────────────────────────────────
BASE = ["crawl-lists", "--subcategory", "widgets", "--driver", "fixture", "--fixtures", "unused"]


def test_old_plans_hash_as_before(networked, capsys):  # noqa: F811
    """A plan without the new flags is byte-for-byte the plan of before: no mode or min_pages key appears."""
    h = plan_hash(capsys, [*BASE, "--stride", "2", "--start", "1"])
    doc = approvals.load_plan(h)
    assert "mode" not in doc and "min_pages" not in doc
    assert approvals.list_args(doc) == {k: doc[k] for k in approvals.LIST_KEYS}


def test_mode_and_min_pages_move_the_plan_hash(networked, capsys):  # noqa: F811
    plain = plan_hash(capsys, BASE)
    walked = plan_hash(capsys, [*BASE, "--mode", "walk"])
    short = plan_hash(capsys, [*BASE, "--mode", "walk", "--min-pages", "2"])
    assert len({plain, walked, short}) == 3
    doc = approvals.load_plan(short)
    assert doc["mode"] == "walk" and doc["min_pages"] == 2 and doc["stride"] == 1
    assert approvals.list_args(doc)["mode"] == "walk" and approvals.list_args(doc)["min_pages"] == 2


def test_a_walk_plan_signs_the_policy_digest(networked, capsys, monkeypatch):  # noqa: F811
    """A walk is judged by the good-crawl rules (liveness.Rules, in the policy digest): its plan signs the
    digest, so a rule change between the approval and the run is another plan. Only walks: older plans hash
    as before."""
    walked = plan_hash(capsys, [*BASE, "--mode", "walk"])
    assert approvals.load_plan(walked)["policy_digest"] == policy.DEFAULT.digest()
    assert "policy_digest" not in approvals.load_plan(plan_hash(capsys, BASE))
    monkeypatch.setattr(policy, "DEFAULT", dataclasses.replace(policy.DEFAULT, lanes="select_ads.test"))
    assert plan_hash(capsys, [*BASE, "--mode", "walk"]) != walked


def test_a_walk_step_doc_signs_max_pages_list_gets():
    """R30: the walk's ceiling is max_pages exactly (no `1 + max_pages` discovery allowance)."""
    step = cli._step_doc({"command": "crawl-lists", "mode": "walk", "stride": 1, "max_pages": 7})  # noqa: SLF001
    assert step["list_pages"] == 7
    old = cli._step_doc({"command": "crawl-lists", "stride": 2, "max_pages": 7})  # noqa: SLF001
    assert old["list_pages"] == 8  # the discovery path is unchanged
    assert "census walk" in approvals._list_words(  # noqa: SLF001
        {"subcategory": "widgets", "region": "r", "mode": "walk", "stride": 1, "max_pages": 7, "min_pages": 2}
    )


# ── resume continues from the next unread page ─────────────────────────────────────────────────────────
def test_resume_mid_walk(data_dir, tmp_path, marker):
    d = site(tmp_path, THREE)
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(2): {"file": "walk-2.html", "status": 403}}))
    assert walk(d, "--max-pages", "10") == crawl.EXIT_CHALLENGE
    assert list_gets() == [P(1), P(2)]
    doc = the_doc(data_dir)
    assert doc["list"]["pages_read"] == 1 and doc["list"]["settled"] == 1
    (d / "urls.json").write_text(json.dumps(urls))
    FixtureHttpClient.log.clear()
    assert main(["crawl", "resume", "--run-id", doc["run_id"], *fix(d)]) == 0
    assert list_gets() == [P(2), P(3), P(4)]  # page 1 is never read again
    lst = the_doc(data_dir)["list"]
    assert lst["status"] == "end" and lst["end_page"] == 3 and lst["pages_read"] == 4
    assert lst["observations_written"] == 6


def test_resume_mid_walk_keeps_the_clamp_memory(data_dir, tmp_path, marker):
    """The clamp's seen ids come from this crawl's own rows, so a resume still sees page 1's ids."""
    d = site(tmp_path, [THREE[0], THREE[0]], end=None)
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(2): {"file": "walk-2.html", "status": 403}}))
    assert walk(d) == crawl.EXIT_CHALLENGE
    (d / "urls.json").write_text(json.dumps(urls))
    rid = the_doc(data_dir)["run_id"]
    assert main(["crawl", "resume", "--run-id", rid, *fix(d)]) == 0
    lst = the_doc(data_dir)["list"]
    assert lst["end_method"] == "clamped" and lst["end_page"] == 1


def test_resume_keeps_the_signed_ceiling(data_dir, tmp_path, marker, monkeypatch):
    """A resume is bounded by what the walk's signed ceiling leaves (not its own --max-pages), and its plan doc
    binds the walk's state."""
    plans = []
    real = cli.gate
    monkeypatch.setattr(cli, "gate", lambda args, doc, v: plans.append(doc) or real(args, doc, v))
    d = site(tmp_path, THREE)
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(2): {"file": "walk-2.html", "status": 403}}))
    assert walk(d, "--max-pages", "3") == crawl.EXIT_CHALLENGE
    (d / "urls.json").write_text(json.dumps(urls))
    rid = the_doc(data_dir)["run_id"]
    FixtureHttpClient.log.clear()
    assert main(["crawl", "resume", "--run-id", rid, "--max-pages", "60", *fix(d)]) == 0
    assert list_gets() == [P(2), P(3)]  # 3 list GETs in all: never page 4
    assert the_doc(data_dir)["list"]["status"] == "ceiling_hit"
    assert {k: plans[-1][k] for k in ("mode", "ceiling", "settled")} == {
        "mode": "walk",
        "ceiling": 3,
        "settled": 1,
    }


def test_resume_after_failed_page_within_ceiling(data_dir, tmp_path, marker):
    """R10: a failed list GET settles too, so the walk's ceiling counts it, and the resume reads the lost page."""
    d = site(tmp_path, THREE)
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(2): {"file": "walk-2.html", "status": 500}}))
    assert walk(d, "--max-pages", "4") == crawl.EXIT_INCOMPLETE
    doc = the_doc(data_dir)
    assert doc["status"] == "stopped" and doc["list"]["settled"] == 2 and doc["list"]["pages_read"] == 1
    (d / "urls.json").write_text(json.dumps(urls))
    FixtureHttpClient.log.clear()
    assert main(["crawl", "resume", "--run-id", doc["run_id"], *fix(d)]) == 0
    assert list_gets() == [P(2), P(3)]  # the lost page, then one more: 4 settled, the ceiling
    lst = the_doc(data_dir)["list"]
    assert lst["status"] == "ceiling_hit" and lst["settled"] == 4 and lst["pages_read"] == 3


def test_a_walk_whose_ceiling_is_spent_is_not_resumed(data_dir, tmp_path, marker, capsys):
    d = site(tmp_path, THREE)
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(2): {"file": "walk-2.html", "status": 500}}))
    assert walk(d, "--max-pages", "2") == crawl.EXIT_INCOMPLETE
    FixtureHttpClient.log.clear()
    rid = the_doc(data_dir)["run_id"]
    capsys.readouterr()
    assert main(["crawl", "resume", "--run-id", rid, *fix(d)]) == crawl.EXIT_INCOMPLETE
    assert list_gets() == [] and "settled its signed ceiling of 2" in capsys.readouterr().err


# ── which walk outcomes are GOOD crawls (ADR-0017 §B.2) ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "status, good",
    [("ok", True), ("end", True), ("ceiling_hit", True), ("anchored", True),
     ("short_census", False), ("end_unknown", False), ("zero_yield", False), ("clamped", False),
     ("past_end", False), ("redirected", False), ("parse_drift", False), ("failed", False), ("blocked", False)],
)  # fmt: skip
def test_good_crawl_statuses(status, good):
    runs = [{"run_id": "c1", "command": "crawl-lists", "status": "completed", "list": {"status": status}}]
    assert liveness.good_crawl(runs, [], legacy=False)("c1") is good


def _postcondition(monkeypatch, lst: dict, status="completed"):
    doc = {"run_id": "c1", "command": "crawl-lists", "status": status, "approval_ref": {"refresh_run": "R"},
           "list": lst}  # fmt: skip
    monkeypatch.setattr(refresh, "_run_doc", lambda objs: doc)
    return refresh.list_postcondition("R", [{"run_id": "c1"}])


def test_refresh_postcondition_passes_the_walk_outcomes(monkeypatch):
    assert _postcondition(monkeypatch, {"status": "end", "end_page": 3, "observations_written": 6}) is None
    assert _postcondition(monkeypatch, {"status": "ceiling_hit", "observations_written": 6}) is None
    # an empty subcategory: a positive END on page 1 is the one pass with 0 rows
    assert _postcondition(monkeypatch, {"status": "end", "end_page": 0, "observations_written": 0}) is None
    assert "0 observations" in _postcondition(
        monkeypatch, {"status": "end", "end_page": 3, "observations_written": 0}
    )
    assert "short_census" in _postcondition(
        monkeypatch, {"status": "short_census", "observations_written": 6}
    )


def test_a_refresh_walk_step_signs_mode_and_min_pages_and_bounds_max_pages(
    tmp_path,
    monkeypatch,
    data_dir,
    operator_key,  # noqa: F811
):
    """R30 in a refresh: a walk step's signed set carries mode and min_pages, and its bounds count exactly
    max_pages list GETs (no `1 + max_pages` discovery allowance)."""
    from test_refresh import make_pipeline

    p = make_pipeline(tmp_path, monkeypatch, operator_key["fp"], steps=("crawl-lists",))
    p.write_text(
        p.read_text().replace('"--stride", "2", "--start", "1"', '"--mode", "walk", "--min-pages", "2"')
    )
    b, _ = refresh.build_bounds()
    (lst,) = approvals.signed_lists(b)
    assert lst["mode"] == "walk" and lst["min_pages"] == 2 and lst["stride"] == 1
    assert lst["policy_digest"] == policy.DEFAULT.digest()
    assert b["max_list_pages"] == lst["max_pages"]
    assert "a census walk, at least 2 pages" in approvals.bounded_text(b)
    step = cli._step_doc({"command": "crawl-lists", **lst})  # noqa: SLF001
    assert approvals.list_args(step) == lst and step["list_pages"] == lst["max_pages"]
