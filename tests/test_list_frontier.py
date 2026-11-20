"""The frontier walk end to end (ADR-0017 §A.2, §A.3, `crawl-lists --mode frontier`): pages 1, 2, 3, ... like
the census walk, plus an early stop at a CONFIRMED anchor against the reference crawl, only when the source
declares `order = "bump_desc"`. Without it (the default, `unordered`) or without a reference, the walk reads to
a positive END or its signed ceiling (`ceiling_hit`, with a GAP). Every page is SYNTHETIC and every GET is
counted on the offline fixture backend.
"""

import dataclasses
import json

import pytest
from example_site.source import ExampleSite
from test_approvals import networked, operator_key, plan_hash  # noqa: F401 - fixtures, used by name
from test_list_snapshots import crawl_lists, list_gets
from test_list_walk import BASE, MARK, P, site
from test_loud_failures import fix

from scrape_harness import approvals, cli, contracts, crawl, plan, policy, refresh, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureHttpClient


def ids(start: int, n: int = 6) -> list[int]:
    return list(range(start, start + n))


REF = [ids(3000001), ids(3000007), ids(3000013), ids(3000019)]  # the reference census: 4 pages, END on 5
TODAY = [ids(4000001)] + REF  # one page of new bumps, then the reference's content again


@pytest.fixture(autouse=True)
def fresh_log():
    FixtureHttpClient.log.clear()


@pytest.fixture
def marker(monkeypatch):
    monkeypatch.setattr(ExampleSite, "end_selectors", (MARK,), raising=False)


@pytest.fixture
def ordered(monkeypatch):
    monkeypatch.setattr(ExampleSite, "order", "bump_desc", raising=False)


def stamp() -> None:
    """created_at has one-second resolution, and these runs follow each other within a second: give each new
    run doc a distinct, increasing created_at, so "the latest crawl" is the one run last."""
    docs = crawl.run_docs()
    done = sum(1 for d in docs if d["created_at"].startswith("2000-"))
    for d in docs:
        if not d["created_at"].startswith("2000-"):
            job = crawl.Run.load(d["run_id"])
            done += 1
            job.doc["created_at"] = f"2000-01-01T00:00:{done:02d}Z"
            job.save()


def frontier(d, *extra) -> int:
    code = crawl_lists(d, "--mode", "frontier", *extra)
    stamp()
    return code


def census(tmp_path, pages=REF, *extra) -> str:
    """A reference crawl: a census walk to its END. Returns its run_id."""
    assert crawl_lists(site(tmp_path / "ref", pages), "--mode", "walk", *extra) == 0
    stamp()
    FixtureHttpClient.log.clear()
    return latest_doc()["run_id"]


def latest_doc() -> dict:
    return contracts.validate("run.v1", max(crawl.run_docs(), key=lambda d: d["created_at"]))


def doc_of(rid: str) -> dict:
    (d,) = [d for d in crawl.run_docs() if d["run_id"] == rid]
    return contracts.validate("run.v1", d)


def rows(doc) -> list[dict]:
    return [o for o in store.read("observations") if o["crawl_id"] == doc["run_id"]]


# ── the anchor: candidate on page 2, confirmed on page 3, nothing read after ─────────────────────────────
def test_frontier_stops_at_a_confirmed_anchor(data_dir, tmp_path, marker, ordered):
    ref = census(tmp_path)
    assert frontier(site(tmp_path / "today", TODAY), "--max-pages", "20") == 0
    assert list_gets() == [P(1), P(2), P(3)]  # page 3 is the ONE confirmation page; page 4 is never read
    doc = latest_doc()
    lst = doc["list"]
    assert doc["status"] == "completed" and lst["status"] == "anchored" and lst["mode"] == "frontier"
    assert lst["anchor"] == {"page": 2, "confirm_page": 3, "ref_crawl_id": ref}
    assert lst["ref_crawl_id"] == ref and lst["order"] == "bump_desc" and lst["k"] == 5
    assert lst["pages_read"] == 3 and lst["settled"] == 3 and "gap" not in lst and "end_page" not in lst
    got = rows(doc)
    assert sorted({o["list"]["page"] for o in got}) == [1, 2, 3]  # the confirmation page's rows are kept
    # frontier rows carry no weight: they are not a census or a sample of the list
    assert {(o["list"]["page_count"], o["list"]["sample_weight"]) for o in got} == {(None, None)}
    # an anchored frontier is a good crawl, and itself a reference for the next walk
    assert plan.frontier_reference(crawl.run_docs(), [], store.read("observations"), site=doc["source"],
                                   subcategory="widgets", region=lst["region"])["crawl_id"] == doc["run_id"]  # fmt: skip


def test_unordered_never_stops_early(data_dir, tmp_path, marker):
    """The default order: the same pages, a reference, a true boundary, and still a walk to the END."""
    ref = census(tmp_path)
    assert frontier(site(tmp_path / "today", TODAY), "--max-pages", "20") == 0
    assert list_gets() == [P(n) for n in range(1, 7)]
    lst = latest_doc()["list"]
    assert lst["status"] == "end" and lst["end_page"] == 5 and lst["order"] == "unordered"
    assert lst["ref_crawl_id"] == ref and "anchor" not in lst and "gap" not in lst


def test_a_frontier_with_no_reference_walks_to_its_end(data_dir, tmp_path, marker, ordered):
    assert frontier(site(tmp_path, TODAY), "--max-pages", "20") == 0
    assert list_gets() == [P(n) for n in range(1, 7)]
    lst = latest_doc()["list"]
    assert lst["status"] == "end" and lst["ref_crawl_id"] is None and "gap" not in lst


def test_bootstrap_ceiling_records_a_gap(data_dir, tmp_path, marker, ordered):
    """No reference and a ceiling before the END: ceiling_hit (a good crawl), GAP since no reference."""
    assert frontier(site(tmp_path, TODAY), "--max-pages", "3") == 0
    assert list_gets() == [P(1), P(2), P(3)]
    doc = latest_doc()
    assert doc["list"]["status"] == "ceiling_hit"
    assert doc["list"]["gap"] == {"after_page": 3, "since_ref": None, "opened_by": doc["run_id"]}


def test_ceiling_gap_recorded(data_dir, tmp_path, marker, ordered, capsys):
    """A walk that hits its ceiling against a reference records the GAP since that reference. A later frontier
    anchored on it carries the GAP on; a census of the list closes it."""
    ref = census(tmp_path)
    # 4 pages of bumps: more than a ceiling of 3
    many = [ids(5000001), ids(5000007), ids(5000013), ids(5000019)] + REF
    capsys.readouterr()
    assert frontier(site(tmp_path / "busy", many), "--max-pages", "3") == 0
    first = latest_doc()
    assert first["list"]["status"] == "ceiling_hit"
    assert first["list"]["gap"] == {"after_page": 3, "since_ref": ref, "opened_by": first["run_id"]}
    assert "GAP" in capsys.readouterr().err
    FixtureHttpClient.log.clear()
    # the next day: one page of new bumps above the first frontier's pages; it anchors on them
    nxt = [ids(6000001)] + many
    assert frontier(site(tmp_path / "next", nxt), "--max-pages", "20") == 0
    assert f"GAP after page 3 (since reference {ref})" in capsys.readouterr().err  # the run card names it
    second = latest_doc()
    assert second["list"]["status"] == "anchored" and second["list"]["ref_crawl_id"] == first["run_id"]
    assert second["list"]["gap"] == first["list"]["gap"]  # still open: the pages past the ceiling are unread
    FixtureHttpClient.log.clear()
    census(tmp_path / "later", nxt)
    FixtureHttpClient.log.clear()
    assert frontier(site(tmp_path / "after", [ids(7000001)] + nxt), "--max-pages", "20") == 0
    third = latest_doc()
    assert third["list"]["status"] == "anchored" and "gap" not in third["list"]


def test_chained_ceiling_frontiers_keep_the_oldest_gap(data_dir, tmp_path, marker, ordered, capsys):
    """Two ceiling-bound frontiers in a row: the second's reference is the first (the latest valid crawl), whose
    GAP is still open. The bumps past the ceiling are unread since the CENSUS, not since the first frontier, so
    the GAP keeps its oldest since_ref (and its opener) and never shortens with each ceiling."""
    ref = census(tmp_path)
    many = [ids(5000001), ids(5000007), ids(5000013), ids(5000019)] + REF
    assert frontier(site(tmp_path / "f1", many), "--max-pages", "3") == 0
    first = latest_doc()
    assert first["list"]["gap"] == {"after_page": 3, "since_ref": ref, "opened_by": first["run_id"]}
    FixtureHttpClient.log.clear()
    more = [ids(6000001), ids(6000007), ids(6000013), ids(6000019)] + many
    capsys.readouterr()
    assert frontier(site(tmp_path / "f2", more), "--max-pages", "3") == 0
    second = latest_doc()
    assert second["list"]["status"] == "ceiling_hit" and second["list"]["ref_crawl_id"] == first["run_id"]
    assert second["list"]["gap"] == first["list"]["gap"]
    assert f"GAP: after page 3 (since reference {ref})" in capsys.readouterr().err


def test_a_bootstrap_gap_survives_a_second_ceiling(data_dir, tmp_path, marker, ordered):
    """The bootstrap GAP (since_ref null: nothing was ever read past the ceiling) stays null through the next
    ceiling-bound frontier, instead of becoming "since the bootstrap"."""
    many = [ids(5000001), ids(5000007), ids(5000013), ids(5000019)] + REF
    assert frontier(site(tmp_path / "f1", many), "--max-pages", "3") == 0
    first = latest_doc()
    FixtureHttpClient.log.clear()
    assert (
        frontier(site(tmp_path / "f2", [ids(6000001), ids(6000007), ids(6000013)] + many), "--max-pages", "2")
        == 0
    )
    gap = latest_doc()["list"]["gap"]
    # the smaller ceiling widens the GAP: pages after 2 are now unread, still since no reference
    assert gap == {"after_page": 2, "since_ref": None, "opened_by": first["run_id"]}


def test_a_census_at_its_ceiling_records_a_gap_that_a_frontier_carries_on(
    data_dir, tmp_path, marker, ordered
):
    """A census that hits its ceiling is a good crawl and a valid reference, so it records its own GAP (the
    pages past its ceiling were not read); a frontier anchored on it carries that GAP on, never `gap: None`."""
    assert crawl_lists(site(tmp_path / "ref", REF), "--mode", "walk", "--max-pages", "2") == 0
    stamp()
    cen = latest_doc()
    assert cen["list"]["status"] == "ceiling_hit"
    assert cen["list"]["gap"] == {"after_page": 2, "since_ref": None, "opened_by": cen["run_id"]}
    FixtureHttpClient.log.clear()
    assert frontier(site(tmp_path / "today", TODAY), "--max-pages", "20") == 0
    lst = latest_doc()["list"]
    assert lst["status"] == "anchored" and lst["ref_crawl_id"] == cen["run_id"]
    assert lst["gap"] == cen["list"]["gap"]


def test_steady_state_anchors_deeper_than_the_reference_read(data_dir, tmp_path, marker, ordered, capsys):
    """An ANCHORED reference read only 3 pages; the next day brings 2 pages of new bumps, so the boundary
    sits deeper than the reference's own last page. The candidate is judged by its place in the REFERENCE
    (before the reference's last page read), not by the walk's page number, so the walk still anchors."""
    census(tmp_path)
    assert frontier(site(tmp_path / "day1", TODAY), "--max-pages", "20") == 0
    day1 = latest_doc()
    assert day1["list"]["status"] == "anchored" and day1["list"]["pages_read"] == 3
    FixtureHttpClient.log.clear()
    capsys.readouterr()
    day2 = [ids(8000001), ids(8000007)] + TODAY
    assert frontier(site(tmp_path / "day2", day2), "--max-pages", "20") == 0
    assert list_gets() == [P(1), P(2), P(3), P(4)]  # candidate on page 3 (day 1's page 1), confirmed on 4
    lst = latest_doc()["list"]
    assert lst["status"] == "anchored" and lst["ref_crawl_id"] == day1["run_id"]
    assert lst["anchor"] == {"page": 3, "confirm_page": 4, "ref_crawl_id": day1["run_id"]}
    assert "gap" not in lst
    assert f"to a confirmed anchor against reference {day1['run_id']}" in capsys.readouterr().err  # run card


def test_a_failed_frontier_page_settles_and_resumes_within_the_ceiling(data_dir, tmp_path, marker):
    """A failed list GET of a frontier settles too, so the ceiling counts it, and the resume reads the lost
    page (as for the census walk)."""
    d = site(tmp_path, REF)
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(2): {"file": "walk-2.html", "status": 500}}))
    assert frontier(d, "--max-pages", "5") == crawl.EXIT_INCOMPLETE
    doc = latest_doc()
    # page 2 and its retry: every GET sent settles
    assert doc["status"] == "stopped" and doc["list"]["settled"] == 3 and doc["list"]["pages_read"] == 1
    (d / "urls.json").write_text(json.dumps(urls))
    assert main(["crawl", "resume", "--run-id", doc["run_id"], *fix(d)]) == 0
    # the full log: the lost page, then one more: 5 GETs in all, the ceiling
    assert list_gets() == [P(1), P(2), P(2), P(2), P(3)]
    lst = doc_of(doc["run_id"])["list"]
    assert lst["status"] == "ceiling_hit" and lst["settled"] == 5 and lst["pages_read"] == 3
    # the GAP starts after the last page READ (3), not after the ceiling (5): a retry spent a GET, not a page
    assert lst["gap"]["after_page"] == 3


def test_a_candidate_on_the_ceiling_page_is_ceiling_hit(data_dir, tmp_path, marker, ordered):
    """The confirmation page is a list GET under the ceiling: with no room for it, no anchor."""
    ref = census(tmp_path)
    assert frontier(site(tmp_path / "today", TODAY), "--max-pages", "2") == 0
    assert list_gets() == [P(1), P(2)]
    lst = latest_doc()["list"]
    assert lst["status"] == "ceiling_hit" and "anchor" not in lst
    assert lst["gap"]["since_ref"] == ref


def test_an_end_on_the_confirmation_page_is_end(data_dir, tmp_path, marker, ordered):
    census(tmp_path)
    assert frontier(site(tmp_path / "today", [ids(4000001), REF[0]]), "--max-pages", "20") == 0
    assert list_gets() == [P(1), P(2), P(3)]
    lst = latest_doc()["list"]
    assert lst["status"] == "end" and lst["end_page"] == 2 and "anchor" not in lst


def test_pinned_block_on_page_1_never_stops_the_walk(data_dir, tmp_path, marker, ordered):
    """Page 1 repeats the reference's top block (adjacent there), then today's bumps fill page 2: the
    confirmation fails, and the walk goes on to the true boundary."""
    census(tmp_path)
    pinned = [REF[0][:5] + [4000001], ids(4000002), ids(4000008), REF[1], REF[2], REF[3]]
    assert frontier(site(tmp_path / "today", pinned), "--max-pages", "20") == 0
    assert list_gets() == [P(1), P(2), P(3), P(4), P(5)]
    lst = latest_doc()["list"]
    assert lst["status"] == "anchored" and lst["anchor"]["page"] == 4 and lst["anchor"]["confirm_page"] == 5


# ── resume: the anchor is a function of the walk's rows, so a stop between candidate and confirmation ────
def test_resume_between_candidate_and_confirmation(data_dir, tmp_path, marker, ordered, monkeypatch):
    plans = []
    real = cli.gate
    monkeypatch.setattr(cli, "gate", lambda args, doc, v: plans.append(doc) or real(args, doc, v))
    ref = census(tmp_path)
    d = site(tmp_path / "today", TODAY)
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(3): {"file": "walk-3.html", "status": 403}}))
    assert frontier(d, "--max-pages", "20") == crawl.EXIT_CHALLENGE
    rid = latest_doc()["run_id"]
    (d / "urls.json").write_text(json.dumps(urls))
    FixtureHttpClient.log.clear()
    assert main(["crawl", "resume", "--run-id", rid, *fix(d)]) == 0
    assert list_gets() == [P(3)]
    lst = doc_of(rid)["list"]
    assert lst["status"] == "anchored" and lst["anchor"] == {
        "page": 2,
        "confirm_page": 3,
        "ref_crawl_id": ref,
    }
    # the resume keeps the recorded reference (never re-chosen), and its plan binds it
    assert plans[-1]["ref_crawl_id"] == ref and plans[-1]["mode"] == "frontier"


# ── refusals and signing ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "extra, word",
    [(("--min-pages", "2"), "--min-pages"), (("--stride", "2"), "stride"), (("--pages", "1,2"), "--pages"),
     (("--page-count", "9"), "--page-count")],
)  # fmt: skip
def test_frontier_refuses_what_it_cannot_honour(data_dir, tmp_path, capsys, extra, word):
    with pytest.raises(SystemExit) as e:
        frontier(site(tmp_path, REF), *extra)
    assert word in str(e.value)


def test_a_frontier_plan_signs_mode_k_and_the_policy_digest(networked, capsys, monkeypatch):  # noqa: F811
    base = plan_hash(capsys, BASE)
    got = plan_hash(capsys, [*BASE, "--mode", "frontier"])
    assert got != base
    saved = approvals.load_plan(got)
    assert (
        saved["mode"] == "frontier" and saved["k"] == 5 and saved["policy_digest"] == policy.DEFAULT.digest()
    )
    assert "min_pages" not in saved
    monkeypatch.setattr(policy, "DEFAULT", dataclasses.replace(policy.DEFAULT, frontier_k=6))
    assert plan_hash(capsys, [*BASE, "--mode", "frontier"]) != got  # K moves the hash
    assert plan_hash(capsys, BASE) == base  # a plan without --mode hashes as before
    assert "k" not in approvals.load_plan(base)


def test_a_refresh_frontier_step_signs_mode_and_k(tmp_path, monkeypatch, data_dir, operator_key, marker):  # noqa: F811
    # marker: a walk step enters a refresh only with the site's end marker (Migration step 5)
    from test_refresh import make_pipeline

    p = make_pipeline(tmp_path, monkeypatch, operator_key["fp"], steps=("crawl-lists",))
    p.write_text(p.read_text().replace('"--stride", "2", "--start", "1"', '"--mode", "frontier"'))
    b, _ = refresh.build_bounds()
    (lst,) = approvals.signed_lists(b)
    assert lst["mode"] == "frontier" and lst["k"] == 5 and lst["policy_digest"] == policy.DEFAULT.digest()
    assert b["max_list_pages"] == lst["max_pages"]
    assert "a frontier walk" in approvals.bounded_text(b)
    step = cli._step_doc({"command": "crawl-lists", **lst})  # noqa: SLF001
    assert approvals.list_args(step) == lst and step["list_pages"] == lst["max_pages"]


def test_the_refresh_summary_shows_the_gap(capsys):
    st = {"run_id": "R", "status": "completed", "plan_hash": "h", "approval_id": "a",
          "headroom": {"ad_requests": {"used": 0, "max": 0}, "list_pages": {"used": 2, "max": 2}},
          "steps": [{"id": "s1", "name": "crawl-lists", "status": "done", "exit": 0,
                     "summary": {"list": {"status": "ceiling_hit", "observations_written": 12,
                                          "gap": {"after_page": 2, "since_ref": "c0", "opened_by": "c1"}}}}]}  # fmt: skip
    refresh._print_summary(st)  # noqa: SLF001
    err = capsys.readouterr().err
    assert "GAP after page 2 (since reference c0)" in err


def test_refresh_postcondition_passes_an_anchored_frontier(monkeypatch):
    doc = {"run_id": "c1", "command": "crawl-lists", "status": "completed", "approval_ref": {"refresh_run": "R"},
           "list": {"mode": "frontier", "status": "anchored", "observations_written": 12}}  # fmt: skip
    monkeypatch.setattr(refresh, "_run_doc", lambda objs: doc)
    assert refresh.list_postcondition("R", [{"run_id": "c1"}]) is None


# ── what a walk signs beyond its arguments: the list order and the policy digest ─────────────────────────
def test_a_frontier_plan_signs_the_sources_order(networked, capsys, monkeypatch):  # noqa: F811
    """Early stop depends on Source.order, read from the plug-in at run start: a plug-in edit after the approval
    (unordered -> bump_desc) must be another plan, never the same hash."""
    got = plan_hash(capsys, [*BASE, "--mode", "frontier"])
    assert approvals.load_plan(got)["order"] == "unordered"
    monkeypatch.setattr(ExampleSite, "order", "bump_desc", raising=False)
    assert plan_hash(capsys, [*BASE, "--mode", "frontier"]) != got
    # only a frontier signs it: a census and a plan without --mode hash as before
    assert "order" not in approvals.load_plan(plan_hash(capsys, [*BASE, "--mode", "walk"]))
    assert "order" not in approvals.load_plan(plan_hash(capsys, BASE))


def test_a_refresh_frontier_step_signs_the_order(tmp_path, monkeypatch, data_dir, operator_key, marker):  # noqa: F811
    from test_refresh import make_pipeline

    p = make_pipeline(tmp_path, monkeypatch, operator_key["fp"], steps=("crawl-lists",))
    p.write_text(p.read_text().replace('"--stride", "2", "--start", "1"', '"--mode", "frontier"'))
    b, _ = refresh.build_bounds()
    (lst,) = approvals.signed_lists(b)
    assert lst["order"] == "unordered"
    step = cli._step_doc({"command": "crawl-lists", **lst})  # noqa: SLF001
    assert approvals.list_args(step) == lst  # the step's run_plan carries it: consume_bounded binds it


def stopped_walk(tmp_path, mode: str) -> str:
    d = site(tmp_path, REF)
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(2): {"file": "walk-2.html", "status": 403}}))
    assert crawl_lists(d, "--mode", mode, "--max-pages", "10") == crawl.EXIT_CHALLENGE
    (d / "urls.json").write_text(json.dumps(urls))
    FixtureHttpClient.log.clear()
    return d


@pytest.mark.parametrize("mode", ["walk", "frontier"])
def test_a_walk_resume_refuses_a_policy_that_changed_since_its_start(
    data_dir, tmp_path, marker, monkeypatch, mode
):
    """The walk records the policy digest it was approved under; a resume under other rules (a harness bump
    between the run and its resume) is refused, exit 10, with no GET, as a fetch-ads resume is."""
    d = stopped_walk(tmp_path, mode)
    doc = latest_doc()
    assert doc["list"]["policy_digest"] == policy.DEFAULT.digest()
    monkeypatch.setattr(policy, "DEFAULT", dataclasses.replace(policy.DEFAULT, confirm_share=0.5))
    assert main(["crawl", "resume", "--run-id", doc["run_id"], *fix(d)]) == approvals.EXIT_UNAPPROVED
    assert list_gets() == []


def test_a_walk_resume_plan_binds_the_policy_digest(data_dir, tmp_path, marker, monkeypatch):
    plans = []
    real = cli.gate
    monkeypatch.setattr(cli, "gate", lambda args, doc, v: plans.append(doc) or real(args, doc, v))
    d = stopped_walk(tmp_path, "frontier")
    rid = latest_doc()["run_id"]
    assert main(["crawl", "resume", "--run-id", rid, *fix(d)]) == 0
    assert plans[-1]["policy_digest"] == policy.DEFAULT.digest() and plans[-1]["order"] == "unordered"


def test_a_challenge_on_the_last_allowed_get_is_still_a_challenge_stop(data_dir, tmp_path, marker):
    """The GET that reaches the ceiling is marked no-retry; a challenge on it is still exit 5 (not a failed
    page), and the walk, its ceiling spent, is not resumed."""
    d = site(tmp_path, REF)
    urls = json.loads((d / "urls.json").read_text())
    (d / "urls.json").write_text(json.dumps(urls | {P(2): {"file": "walk-2.html", "status": 403}}))
    assert crawl_lists(d, "--mode", "frontier", "--max-pages", "2") == crawl.EXIT_CHALLENGE
    assert list_gets() == [P(1), P(2)]
    doc = latest_doc()
    assert doc["list"]["settled"] == 2 and doc["list"].get("status") != "failed" and doc["challenges"] == 1
    FixtureHttpClient.log.clear()
    assert main(["crawl", "resume", "--run-id", doc["run_id"], *fix(d)]) == crawl.EXIT_INCOMPLETE
    assert list_gets() == []
