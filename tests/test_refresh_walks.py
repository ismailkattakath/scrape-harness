"""A refresh with walk steps (ADR-0017 §A, §C.4, Migration step 5): a census (`--mode walk`) and a frontier
(`--mode frontier`) declared as crawl-lists steps in [crawl.refresh], signed in the bounds, each judged by
its OWN mode's postcondition, feeding the fetch-ads gate of the same refresh.

- A census step passes only on a positive END (`end`, a clamp included: A.1 names it an end_method). Its
  ceiling, a short census and an unknown end are exit 14 (terminal) and fetch-ads never runs.
- A frontier step passes on `anchored`, `end` or `ceiling_hit`; a ceiling_hit shows its GAP.
- A walk step enters a refresh only when its source declares `end_selectors`: refused at plan time, exit 2.

The chain runs for real (each step its own process) over SYNTHETIC example.invalid pages; the source with an
end marker is tests/refresh_helpers.py's EndSite, since a monkeypatch never reaches a step subprocess."""

import json
import re

import pytest
from test_approvals import networked, operator_key, runs  # noqa: F401 - fixtures, used by name
from test_loud_failures import fixtures
from test_refresh import approved, chain, make_pipeline, names, state, toml_list  # noqa: F401

from scrape_harness import approvals, liveness, refresh
from scrape_harness.cli import main

ORIGIN = "https://www.example.invalid"
END_HTML = (
    "<!doctype html><html><head><title>Not found | Example</title></head><body>"
    '<div id="no-results">No widgets match</div></body></html>'
)
SLUG = {1000001: "blue-widget", 1000002: "red-widget", 1000003: "green-widget"}  # the fixture ad pages


def lurl(sub: str, n: int) -> str:
    return f"{ORIGIN}/listings/{sub}/sample-region?page={n}"


def page(ids) -> str:
    """A SYNTHETIC list page, one card per id, linking the fixture set's ad pages; no pager."""
    cards = "".join(
        f'<article class="result" data-id="{i}"><a class="result__link" '
        f'href="/listings/widgets/sample-region/{SLUG.get(i, "widget")}-{i}"><h2 class="result__title">'
        f'Widget {i}</h2></a><span class="result__city">Sampletown</span>'
        '<span class="result__price">10</span></article>'
        for i in ids
    )
    return (
        "<!doctype html><html><head><title>Widgets | Example</title></head><body>"
        f'<main id="results">{cards}</main></body></html>'
    )


def walk_site(tmp_path, lists: dict[str, list[list[int]]]):
    """lists[sub][i] = the ids on page i+1 of that subcategory; the next page answers the END template (404)."""
    over, files = {}, [("walk-end.html", END_HTML)]
    for sub, pages in lists.items():
        for n, ids in enumerate(pages, 1):
            over[lurl(sub, n)] = f"walk-{sub}-{n}.html"
            files.append((f"walk-{sub}-{n}.html", page(ids)))
        over[lurl(sub, len(pages) + 1)] = {"file": "walk-end.html", "status": 404}
    return fixtures(tmp_path, over, files)


def fx(root) -> list[str]:
    return ["--driver", "netfixture", "--fixtures", str(root), "--max-tasks-per-minute", "6000",
            "--approve-politeness-override"]  # fmt: skip


def walk_pipeline(tmp_path, monkeypatch, fp, root, steps: list[list[str]], end_source=True, fetch=True):
    """The test pipeline with these crawl-lists steps (their args), then fetch-ads; the source is EndSite
    (end_selectors declared) unless end_source is False (the example source, which declares none)."""
    p = make_pipeline(tmp_path, monkeypatch, fp, steps=("crawl-lists",))
    text = p.read_text()
    rows = [f'  {{ name = "crawl-lists", args = {toml_list([*a, *fx(root)])} }},\n' for a in steps]
    if fetch:
        rows.append(f'  {{ name = "fetch-ads", args = {toml_list(fx(root))} }},\n')
    text = re.sub(r"\[crawl\.refresh\]\nsteps = \[\n.*?\n\]\n", "[crawl.refresh]\nsteps = [\n" + "".join(rows)
                  + "]\n", text, count=1, flags=re.S)  # fmt: skip
    if end_source:
        text = text.replace('"example_site.source:ExampleSite"', '"refresh_helpers:EndSite"')
    p.write_text(text)
    return p


CENSUS = ["--subcategory", "widgets", "--mode", "walk", "--max-pages", "4"]
FRONTIER = ["--subcategory", "gadgets", "--mode", "frontier", "--max-pages", "1"]


@pytest.fixture
def walks(chain, tmp_path, monkeypatch, operator_key):  # noqa: F811
    """A census of widgets (2 pages, then a marked 404 END) and a frontier of gadgets (2 pages, ceiling 1)."""
    root = walk_site(tmp_path, {"widgets": [[1000001], [1000002]], "gadgets": [[1000003], [2000009]]})
    walk_pipeline(tmp_path, monkeypatch, operator_key["fp"], root, [CENSUS, FRONTIER])
    return chain


def summary_of(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


# ── the chain ────────────────────────────────────────────────────────────────────────────────────────
def test_a_census_and_a_frontier_feed_the_fetch_ads_gate_of_the_same_refresh(walks, data_dir, capsys):
    approved(capsys)
    assert main(["refresh", "--approve-network"]) == 0
    out, err = capsys.readouterr()
    assert names(walks) == ["crawl-lists", "crawl-lists", "fetch-ads"]
    st = state()
    assert [s["status"] for s in st["steps"]] == ["done"] * 3 and st["status"] == "complete"
    by = {d["run_id"]: d for d in runs(data_dir)}
    census, front = (by[s["summary"]["run_id"]]["list"] for s in st["steps"][:2])
    assert census["mode"] == "walk" and census["status"] == "end" and census["end_page"] == 2
    assert (
        front["mode"] == "frontier" and front["status"] == "ceiling_hit" and front["gap"]["after_page"] == 1
    )
    # the gate took the sightings of BOTH passed list steps: every id they read, and nothing else
    (fetch,) = [d for d in by.values() if d["command"] == "fetch-ads"]
    assert fetch["ads"]["ad_gets"] == 3
    # the summary: each walk step's mode, pages read, end_page, and the frontier's GAP
    s = {x["id"]: x for x in summary_of(out)["steps"]}
    assert s["1:crawl-lists"]["list"]["pages_read"] == 3 and s["2:crawl-lists"]["list"]["pages_read"] == 1
    assert "crawl-lists list (census walk): end, end_page 2 (end_marker), 3 pages read of 4" in err
    assert "crawl-lists list (frontier walk): ceiling_hit, 1 pages read of 1" in err
    assert "GAP after page 1 (no reference before it)" in err


def test_the_refresh_card_shows_each_walk_step(walks, capsys):
    capsys.readouterr()
    assert main(["refresh"]) == 10
    err = capsys.readouterr().err
    assert "list step (1) widgets: census walk, at most 4 list GETs; passes only on a positive END" in err
    assert "list step (2) gadgets: frontier walk, list order unordered: no early stop" in err
    assert "passes on an anchor, a positive END or the ceiling (a GAP)" in err


def test_a_census_at_its_ceiling_is_terminal_and_fetch_ads_never_runs(
    chain,  # noqa: F811
    tmp_path,
    monkeypatch,
    data_dir,
    capsys,
    operator_key,  # noqa: F811
):
    """A census that hit its ceiling did not read the whole list: in a refresh that is exit 14, terminal."""
    root = walk_site(tmp_path, {"widgets": [[1000001], [1000002], [1000003]]})
    walk_pipeline(tmp_path, monkeypatch, operator_key["fp"], root,
                  [["--subcategory", "widgets", "--mode", "walk", "--max-pages", "2"]])  # fmt: skip
    approved(capsys)
    assert main(["refresh", "--approve-network"]) == 14
    assert names(chain) == ["crawl-lists"]
    st = state()
    step = st["steps"][0]
    assert step["exit"] == 14 and "list.status ceiling_hit" in step["reason"]
    assert "a census walk passes only on" in step["reason"]
    assert st["status"] == "spent" and not [r for r in runs(data_dir) if r["command"] == "fetch-ads"]


# ── the per-mode postcondition ──────────────────────────────────────────────────────────────────────
def _post(monkeypatch, lst: dict, mode=None):
    doc = {"run_id": "c1", "command": "crawl-lists", "status": "completed", "approval_ref": {"refresh_run": "R"},
           "list": {"observations_written": 3, **lst}}  # fmt: skip
    monkeypatch.setattr(refresh, "_run_doc", lambda objs: doc)
    return refresh.list_postcondition("R", [{"run_id": "c1"}], mode=mode)


@pytest.mark.parametrize(
    "mode, status, passes",
    [("walk", "end", True), ("walk", "ceiling_hit", False), ("walk", "short_census", False),
     ("walk", "end_unknown", False), ("walk", "anchored", False), ("walk", "ok", False),
     ("frontier", "anchored", True), ("frontier", "end", True), ("frontier", "ceiling_hit", True),
     ("frontier", "ok", False), ("frontier", "end_unknown", False),
     (None, "ok", True), (None, "end", True), (None, "zero_yield", False)],
)  # fmt: skip
def test_each_mode_passes_only_its_own_outcomes(monkeypatch, mode, status, passes):
    why = _post(monkeypatch, {"mode": mode, "status": status}, mode=mode)
    assert (why is None) is passes, why


def test_a_census_clamp_is_an_end_and_passes(monkeypatch):
    assert _post(monkeypatch, {"mode": "walk", "status": "end", "end_method": "clamped", "end_page": 3},
                 mode="walk") is None  # fmt: skip


def test_a_run_doc_of_another_mode_than_the_step_declares_fails_closed(monkeypatch):
    assert "list.mode None" in _post(monkeypatch, {"status": "end"}, mode="walk")
    assert "list.mode walk" in _post(monkeypatch, {"mode": "walk", "status": "end"}, mode="frontier")


def test_every_mode_pass_set_is_a_good_crawl_set():
    """One rule: a refresh never passes a list read that liveness would not count as a good crawl."""
    for mode, ok in refresh.PASS_STATUS.items():
        assert ok <= liveness.DEFAULT.good_list_status, mode


# ── Migration step 5: a walk step needs the site's end marker ───────────────────────────────────────
@pytest.mark.parametrize("step", [CENSUS, FRONTIER], ids=["walk", "frontier"])
def test_a_walk_step_without_end_selectors_is_refused_at_plan_time(
    tmp_path,
    monkeypatch,
    data_dir,
    capsys,
    operator_key,  # noqa: F811
    step,
):
    root = walk_site(tmp_path, {"widgets": [[1000001]], "gadgets": [[1000003]]})
    walk_pipeline(tmp_path, monkeypatch, operator_key["fp"], root, [step], end_source=False)
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        main(["refresh"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "declares no end_selectors" in err and f"--mode {step[3]}" in err
    assert not list(approvals._dir("plans").glob("*.json"))  # noqa: SLF001  # nothing saved to sign


def test_a_step_without_a_mode_needs_no_end_selectors(tmp_path, monkeypatch, data_dir, operator_key):  # noqa: F811
    root = walk_site(tmp_path, {"widgets": [[1000001]]})
    walk_pipeline(tmp_path, monkeypatch, operator_key["fp"], root, [["--subcategory", "widgets"]],
                  end_source=False)  # fmt: skip
    b, _ = refresh.build_bounds()
    assert "mode" not in approvals.signed_lists(b)[0]
