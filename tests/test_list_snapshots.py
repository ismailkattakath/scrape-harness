"""List-page snapshots (`crawl-lists --snapshot`) and explicit pages (`--pages`), ADR-0017 slice 2's first piece.

An evidence run reads a few named list pages (page 1, page 2, a far past-end page) and keeps each answered page
as gzip under raw/lists/<site>/<crawl_id>/<page>.html.gz, with a JSON sidecar. The body and its sidecar are
saved BEFORE any parse or stop decision, so the page that does not parse as a normal list page is kept too.
List GETs add no ad views; every test counts the GETs the offline fixture client actually served.
"""

import gzip
import hashlib
import json

import pytest
from conftest import FIXTURES, LIST
from test_approvals import networked, operator_key, plan_hash  # noqa: F401 - fixtures, used by name
from test_loud_failures import NO_PAGER, fix, fixtures, run_docs

from scrape_harness import contracts, crawl, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureHttpClient

P1, P3, P400 = (f"{LIST}?page={n}" for n in (1, 3, 400))
SITE = "example.invalid"


@pytest.fixture(autouse=True)
def fresh_log():
    FixtureHttpClient.log.clear()


def list_gets() -> list[str]:
    return [u for u in FixtureHttpClient.log if "?page=" in u]


def crawl_lists(d, *extra) -> int:
    return main(["crawl-lists", "--subcategory", "widgets", *extra, *fix(d)])


def snap_dir(data_dir, run_id):
    return data_dir / "raw" / "lists" / SITE / store.safe_name(run_id)


def sidecar(data_dir, run_id, page) -> dict:
    doc = json.loads((snap_dir(data_dir, run_id) / f"{page}.json").read_text())
    return contracts.validate("list_snapshot.v1", doc)


def body(data_dir, run_id, page) -> str:
    return gzip.decompress((snap_dir(data_dir, run_id) / f"{page}.html.gz").read_bytes()).decode()


# ── a normal page ──────────────────────────────────────────────────────────────────────────────────────
def test_a_normal_page_is_saved_with_its_sidecar(data_dir, tmp_path):
    d = fixtures(tmp_path)
    assert crawl_lists(d, "--pages", "1,3", "--snapshot") == 0
    assert list_gets() == [P1, P3]  # exactly the named pages, in order, once each
    (doc,) = run_docs(data_dir)
    rid = doc["run_id"]
    assert doc["status"] == "completed" and doc["list"]["status"] == "ok"
    html = (FIXTURES / "list-p1.html").read_text()
    assert body(data_dir, rid, 1) == html
    s = sidecar(data_dir, rid, 1)
    assert s["status"] == 200 and s["final_url"] == s["requested_url"] == P1
    assert s["parsed"] == 3 and s["skipped"] is None and s["outcome"] == "listed" and s["reason"] is None
    assert s["body"] == {
        "path": f"raw/lists/{SITE}/{store.safe_name(rid)}/1.html.gz",
        "sha256": hashlib.sha256(html.encode()).hexdigest(),
        "bytes": len(html.encode()),
    }
    assert sidecar(data_dir, rid, 3)["parsed"] == 1
    assert doc["list"]["snapshot"] == {
        "dir": f"raw/lists/{SITE}/{store.safe_name(rid)}",
        "saved": 2,
        "past_end": [],
        "redirected": [],
    }
    assert len([o for o in store.read("observations") if o["crawl_id"] == rid]) == 4


def test_without_snapshot_nothing_is_saved_and_pages_is_a_normal_read(data_dir, tmp_path):
    d = fixtures(tmp_path)
    assert crawl_lists(d, "--pages", "3,1") == 0
    assert list_gets() == [P3, P1]  # in the order named
    (doc,) = run_docs(data_dir)
    assert not (data_dir / "raw").exists() and "snapshot" not in doc["list"]
    rows = [o for o in store.read("observations") if o["crawl_id"] == doc["run_id"]]
    assert sorted(o["list"]["page"] for o in rows) == [1, 1, 1, 3]
    assert {o["list"]["sample_weight"] for o in rows} == {None}  # named pages are not a probability sample


# ── page 1 without its page count: still exit 14, but the page is kept ────────────────────────────────
def test_page_one_without_a_page_count_still_stops_14_and_its_snapshot_is_kept(data_dir, tmp_path):
    d = fixtures(tmp_path, {P1: "nopager.html"}, [("nopager.html", NO_PAGER)])
    assert crawl_lists(d, "--stride", "2", "--snapshot") == crawl.EXIT_PARSE
    assert list_gets() == [P1]
    (doc,) = run_docs(data_dir)
    assert doc["list"]["status"] == "parse_drift"
    assert body(data_dir, doc["run_id"], 1) == NO_PAGER
    s = sidecar(data_dir, doc["run_id"], 1)
    assert s["parsed"] == 3 and s["outcome"] == "listed"  # counted BEFORE the handler's stop


def test_pages_never_discover_so_a_missing_page_count_is_no_stop(data_dir, tmp_path):
    d = fixtures(tmp_path, {P1: "nopager.html"}, [("nopager.html", NO_PAGER)])
    assert crawl_lists(d, "--pages", "1", "--snapshot") == 0
    assert list_gets() == [P1]


# ── a past-end page that answers 404 or 410 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [404, 410])
def test_a_past_end_404_is_saved_recorded_and_not_a_challenge(data_dir, tmp_path, status):
    end = "<!doctype html><html><head><title>No results</title></head><body><p class=none>0</p></body></html>"
    d = fixtures(tmp_path, {P400: {"file": "end.html", "status": status}}, [("end.html", end)])
    assert crawl_lists(d, "--pages", "1,3,400", "--snapshot") == 0
    assert list_gets() == [P1, P3, P400]  # no retry of the 404
    (doc,) = run_docs(data_dir)
    rid = doc["run_id"]
    assert doc["challenges"] == 0 and doc["status"] == "completed"
    assert doc["list"]["status"] == "past_end"  # never `ok`: not a good crawl, never a reference
    assert doc["list"]["snapshot"]["past_end"] == [400] and doc["list"]["snapshot"]["saved"] == 3
    assert body(data_dir, rid, 400) == end
    s = sidecar(data_dir, rid, 400)
    assert s["status"] == status and s["outcome"] == "past_end" and s["reason"] == f"http_{status}"
    assert s["parsed"] == 0
    assert not [e for e in store.read("events") if e["kind"] in ("challenge", "cooldown")]
    assert {o["list"]["page"] for o in store.read("observations")} == {1, 3}
    assert not good(rid)


def good(run_id) -> bool:
    from scrape_harness import liveness

    return liveness.good_crawl(crawl.run_docs(), list(store.read("events")), liveness.DEFAULT, legacy=False)(
        run_id
    )


def test_a_plain_pages_run_is_a_good_crawl(data_dir, tmp_path):
    assert crawl_lists(fixtures(tmp_path), "--pages", "1,3", "--snapshot") == 0
    (doc,) = run_docs(data_dir)
    assert good(doc["run_id"])


def test_listed_pages_with_no_rows_are_still_a_zero_yield_beside_a_past_end_page(data_dir, tmp_path):
    empty = "<!doctype html><html><head><title>W</title></head><body><main id=results></main></body></html>"
    d = fixtures(tmp_path, {P1: "empty.html", P400: {"file": "empty.html", "status": 404}},
                 [("empty.html", empty)])  # fmt: skip
    assert crawl_lists(d, "--pages", "1,400", "--snapshot") == crawl.EXIT_PARSE
    (doc,) = run_docs(data_dir)
    assert doc["list"]["status"] == "zero_yield" and doc["list"]["snapshot"]["past_end"] == [400]


def test_only_past_end_pages_complete_without_a_zero_yield(data_dir, tmp_path):
    d = fixtures(tmp_path, {P400: {"file": "list-p3.html", "status": 410}})
    assert crawl_lists(d, "--pages", "400", "--snapshot") == 0
    (doc,) = run_docs(data_dir)
    assert doc["list"]["status"] == "past_end"


def test_the_flag_rides_on_the_sample_page_one_plans(data_dir, tmp_path):
    d = fixtures(tmp_path)
    assert crawl_lists(d, "--stride", "2", "--start", "1", "--snapshot") == 0
    (doc,) = run_docs(data_dir)
    assert {p.name for p in snap_dir(data_dir, doc["run_id"]).glob("*.html.gz")} == {"1.html.gz", "3.html.gz"}


@pytest.mark.parametrize(
    ("asked", "served", "moved"),
    [
        (P1, P1 + "#top", False),
        (P1, P1.replace("https://www.", "HTTPS://WWW."), False),
        (P1, P1.replace(".invalid/", ".invalid:443/"), False),
        (P400, P1, True),
        (P1, P1 + "&sort=new", True),
    ],
)
def test_a_redirect_is_a_different_path_or_query(asked, served, moved):
    from scrape_harness.interfaces import FetchResult

    res = FetchResult(asked, 200, served, "")
    assert (crawl._served_at(res) != crawl._served_at(res, final=False)) is moved  # noqa: SLF001


def test_without_snapshot_a_404_list_page_is_still_a_challenge_stop(data_dir, tmp_path):
    d = fixtures(tmp_path, {P400: {"file": "list-p3.html", "status": 404}})
    assert crawl_lists(d, "--pages", "1,400,3") == crawl.EXIT_CHALLENGE
    assert list_gets() == [P1, P400]
    assert not (data_dir / "raw").exists()


# ── a redirected page ─────────────────────────────────────────────────────────────────────────────────
def test_a_redirected_page_is_saved_with_its_final_url_and_keeps_no_rows(data_dir, tmp_path):
    d = fixtures(tmp_path, {P400: {"file": "list-p3.html", "final_url": P3}})
    assert crawl_lists(d, "--pages", "1,400", "--snapshot") == 0
    assert list_gets() == [P1, P400]
    (doc,) = run_docs(data_dir)
    rid = doc["run_id"]
    s = sidecar(data_dir, rid, 400)
    assert s["requested_url"] == P400 and s["final_url"] == P3 and s["outcome"] == "redirected"
    assert s["status"] == 200 and s["parsed"] == 1  # counted, not kept: its cards are not page 400's
    assert doc["list"]["status"] == "redirected" and doc["list"]["snapshot"]["redirected"] == [400]
    assert not good(rid)
    assert {o["list"]["page"] for o in store.read("observations")} == {1}


# ── a challenge still stops the run ────────────────────────────────────────────────────────────────────
def test_a_challenge_still_stops_exit_5_and_its_body_is_never_synced(data_dir, tmp_path):
    d = fixtures(tmp_path, {P3: {"file": "challenge-cf.html", "status": 403}})
    assert crawl_lists(d, "--pages", "1,3,400", "--snapshot") == crawl.EXIT_CHALLENGE
    assert list_gets() == [P1, P3]  # page 400 never sent
    (doc,) = run_docs(data_dir)
    rid = doc["run_id"]
    assert doc["challenges"] == 1 and doc["status"] == "stopped"
    s = sidecar(data_dir, rid, 3)
    assert s["outcome"] == "stopped" and s["reason"] == "http_403" and s["status"] == 403
    assert s["body"] is None and s["parsed"] is None  # a challenge body stays in STATE (stops/), never DATA
    assert not (snap_dir(data_dir, rid) / "3.html.gz").exists()
    (ev,) = [e for e in store.read("events") if e["kind"] == "challenge"]
    assert ev["evidence"]["snapshot"].startswith("STATE:stops/")


def test_an_unexpected_page_stops_exit_5_and_its_body_is_kept(data_dir, tmp_path):
    """A 200 page without the list expectation and with no challenge sign (`unexpected_page`): detect() found
    nothing that marks a challenge on it, so the body is the site's own page, kept for the evidence."""
    odd = "<!doctype html><html><head><title>Widgets</title></head><body><p>nothing here</p></body></html>"
    d = fixtures(tmp_path, {P400: "odd.html"}, [("odd.html", odd)])
    assert crawl_lists(d, "--pages", "400", "--snapshot") == crawl.EXIT_CHALLENGE
    (doc,) = run_docs(data_dir)
    s = sidecar(data_dir, doc["run_id"], 400)
    assert s["outcome"] == "stopped" and s["reason"] == "unexpected_page"
    assert body(data_dir, doc["run_id"], 400) == odd


# ── a refused write: exit 4, the page handed back, no retry GET ───────────────────────────────────────
@pytest.mark.parametrize("err", [store.DataDirError("the data folder went away"), OSError(28, "No space")])
def test_a_refused_snapshot_write_is_exit_4_with_no_retry(data_dir, tmp_path, monkeypatch, err):
    def refuse(*_a, **_k):
        raise err

    monkeypatch.setattr(store, "write_list_snapshot", refuse)
    d = fixtures(tmp_path)
    assert crawl_lists(d, "--pages", "1,3", "--snapshot") == crawl.EXIT_DATA
    assert list_gets() == [P1]  # no retry, page 3 never sent
    (doc,) = run_docs(data_dir)
    assert doc["status"] == "stopped" and doc["stop"]["code"] == crawl.EXIT_DATA
    assert not list(store.read("observations"))  # refused before any parse
    assert crawl.pending(crawl.Run.load(doc["run_id"])) == 2  # page 1 handed back for the resume


# ── --pages: validated, bounded, and signed ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "extra",
    [
        ["--pages", "1,2", "--stride", "2"],
        ["--pages", "1,2", "--start", "1"],
        ["--pages", "1,2", "--page-count", "9"],
        ["--pages", "1,2", "--seed", "3"],
        ["--pages", "1,1"],
        ["--pages", "0,2"],
        ["--pages", "1,x"],
        ["--pages", "1,2,3", "--max-pages", "2"],
    ],
)
def test_bad_pages_are_refused_before_any_get(data_dir, tmp_path, extra):
    with pytest.raises(SystemExit):
        crawl_lists(fixtures(tmp_path), *extra)
    assert FixtureHttpClient.log == []


RUN = ["crawl-lists", "--subcategory", "widgets", "--driver", "fixture", "--fixtures", str(FIXTURES),
       "--max-tasks-per-minute", "6000", "--approve-politeness-override", "--approve-network"]  # fmt: skip


def test_pages_and_snapshot_are_signed_into_the_plan_hash(networked, capsys):  # noqa: F811
    a = plan_hash(capsys, [*RUN, "--pages", "1,2,400", "--snapshot"])
    assert a == plan_hash(capsys, [*RUN, "--pages", "1,2,400", "--snapshot"])
    assert a != plan_hash(capsys, [*RUN, "--pages", "1,2,401", "--snapshot"])
    assert a != plan_hash(capsys, [*RUN, "--pages", "2,1,400", "--snapshot"])  # the order is signed too
    assert a != plan_hash(capsys, [*RUN, "--pages", "1,2,400"])  # so is --snapshot
    plain = plan_hash(capsys, [*RUN, "--stride", "1"])
    assert plain == plan_hash(capsys, RUN)  # neither flag set: the plan hashes as before (no new keys)
    capsys.readouterr()
    main([*RUN, "--pages", "1,2,400", "--snapshot", "--plan"])
    out = json.loads(capsys.readouterr().out)["plan"]
    assert out["pages"] == [1, 2, 400] and out["snapshot"] is True
    from scrape_harness import approvals

    assert "pages 1,2,400, snapshots" in approvals.summary(out, "0" * 64, "30m")  # what Touch ID shows


def test_an_approved_pages_plan_sends_exactly_its_pages(networked, capsys, tmp_path):  # noqa: F811
    from scrape_harness import approvals

    d = fixtures(tmp_path)
    argv = [*RUN[:6], str(d), *RUN[7:], "--pages", "3,1", "--snapshot"]
    approvals.approve(plan_hash(capsys, argv))
    FixtureHttpClient.log.clear()
    assert main(argv) == 0
    assert list_gets() == [P3, P1]
    assert main(argv) == 10  # single use


def test_step_doc_and_planned_count_the_named_pages():
    from scrape_harness.cli import _planned_from, _step_doc

    doc = {"command": "crawl-lists", "pages": [1, 2, 400], "max_pages": 60, "stride": 1}
    assert _planned_from(doc) == {"list": 3, "robots": 1}
    assert _step_doc(doc)["list_pages"] == 3


def test_a_signed_list_set_binds_pages_only_when_set():
    from scrape_harness import approvals

    base = {"subcategory": "w", "region": "r", "page_count": None, "stride": 1, "start": None, "seed": None,
            "max_pages": 60}  # fmt: skip
    assert approvals.list_args(base) == base  # a set signed before --pages existed reads the same
    with_pages = approvals.list_args(base | {"pages": [1, 2, 400], "snapshot": True})
    assert with_pages == base | {"pages": [1, 2, 400], "snapshot": True}
    assert approvals.list_args(base | {"pages": None, "snapshot": False}) == base
    contracts.validate("approval.v2.schema.json#/$defs/list_set", with_pages)
    assert "pages 1, 2, 400" in approvals._list_words(with_pages)  # noqa: SLF001 - the words Touch ID shows
    assert "snapshots" in approvals._list_words(with_pages)  # noqa: SLF001
