"""A refresh's crawl-lists step passes only on exit 0 AND its run doc's list.status ok AND observations
written > 0; anything else is recorded as exit 14 (terminal: the approval is spent) and the strict doit
chain never reaches fetch-ads. The refresh's summary card shows each fetch-ads step's ads{} selection.

The steps run for real (test_refresh's `chain`); a failing list step is made by DOCTORING the run doc the
real step wrote (or by a step that prints nothing), which is what an older or broken step looks like to
the refresh. All fixtures are synthetic (example.invalid)."""

import json

import pytest
from test_approvals import KeyFileSigner, networked, operator_key, runs  # noqa: F401
from test_refresh import approved, chain, lines, make_pipeline, names, state  # noqa: F401

from scrape_harness import refresh, store
from scrape_harness.cli import main


def doctored(chain, monkeypatch, edit):  # noqa: F811
    """EXEC that runs every step for real, then applies `edit` to the crawl-lists run doc it wrote."""

    def run(argv):
        chain.append(list(argv))
        code, objs = refresh._exec(argv)
        if argv[0] == "crawl-lists" and "--dry-run" not in argv and objs:
            p = store.runs_dir() / f"{store.safe_name(objs[-1]['run_id'])}.json"
            doc = store.read_json(p, "run.v1")
            edit(doc)
            store.write_json(p, "run.v1", doc)
        return code, objs

    monkeypatch.setattr(refresh, "EXEC", run)


def no_list(d):
    d.pop("list", None)


def zero_written(d):
    d["list"] = {"status": "ok", "observations_written": 0}


def not_ok(d):
    d["list"] = d["list"] | {"status": "zero_yield"}


def other_refresh(d):
    d["approval_ref"] = d["approval_ref"] | {"refresh_run": "refresh-20000101T000000Z-000000"}


def not_completed(d):
    d["status"] = "stopped"


@pytest.mark.parametrize(
    "edit, why",
    [
        (no_list, "list.status None"),
        (zero_written, "0 observations written"),
        (not_ok, "list.status zero_yield"),
        (other_refresh, "not a crawl-lists run of refresh run"),
        (not_completed, "status stopped"),
    ],
    ids=["no-list", "zero-written", "not-ok", "other-refresh", "not-completed"],
)
def test_a_list_step_that_fails_its_postcondition_is_exit_14_and_fetch_ads_never_runs(
    chain,  # noqa: F811
    data_dir,
    capsys,
    monkeypatch,
    edit,
    why,
):
    approved(capsys)
    doctored(chain, monkeypatch, edit)
    assert main(["refresh", "--approve-network"]) == 14
    assert names(chain) == ["crawl-lists"]  # the strict chain: nothing after the failed list step
    st = state()
    step = st["steps"][0]
    assert step["status"] == "failed" and step["exit"] == 14
    assert step["reason"].startswith("list postcondition:") and why in step["reason"]
    assert [s["status"] for s in st["steps"][1:]] == ["pending"] * (len(st["steps"]) - 1)
    assert st["status"] == "spent" and "parse or yield stop (exit 14)" in st["spent"]["why"]
    assert lines(data_dir)[-1]["kind"] == "spent"
    assert not [r for r in runs(data_dir) if r["command"] == "fetch-ads"]
    chain.clear()
    assert main(["refresh", "--approve-network"]) == 10 and chain == []  # terminal: no resume


def test_a_list_step_that_prints_no_run_id_fails_closed(chain, data_dir, capsys, monkeypatch):  # noqa: F811
    approved(capsys)

    def silent(argv):
        chain.append(list(argv))
        return (0, []) if argv[0] == "crawl-lists" else refresh._exec(argv)

    monkeypatch.setattr(refresh, "EXEC", silent)
    assert main(["refresh", "--approve-network"]) == 14
    assert names(chain) == ["crawl-lists"]
    step = state()["steps"][0]
    assert step["exit"] == 14 and "printed no run_id" in step["reason"]


def test_a_failed_first_list_step_stops_the_second_and_fetch_ads(
    chain,  # noqa: F811
    tmp_path,
    monkeypatch,
    capsys,
    data_dir,
    operator_key,  # noqa: F811
):
    make_pipeline(
        tmp_path, monkeypatch, operator_key["fp"], steps=("crawl-lists", "crawl-gadgets", "fetch-ads")
    )
    approved(capsys)
    doctored(chain, monkeypatch, zero_written)
    assert main(["refresh", "--approve-network"]) == 14
    assert names(chain) == ["crawl-lists"]
    assert [s["status"] for s in state()["steps"]] == ["failed", "pending", "pending"]


def test_a_passing_chain_summary_shows_the_list_outcome_and_the_ads_selection(
    chain,  # noqa: F811
    data_dir,
    capsys,
):
    approved(capsys)
    assert main(["refresh", "--approve-network"]) == 0
    out, err = capsys.readouterr()
    s = json.loads(out.strip().splitlines()[-1])
    by = {x["name"]: x for x in s["steps"]}
    assert by["crawl-lists"]["list"]["status"] == "ok"
    assert by["crawl-lists"]["list"]["observations_written"] > 0
    ads = by["fetch-ads"]["ads"]
    assert set(ads["lanes"]) == {"new", "revisit"} and sum(ads["lanes"].values()) == ads["ad_gets"]
    assert set(ads["excluded"]) == {"tombstoned", "dormant", "quarantined", "held"}
    assert "fetch-ads selection:" in err and f"new {ads['lanes']['new']}, revisit" in err
    assert "crawl-lists list: ok" in err


def test_an_empty_pool_fetch_ads_step_still_reports_its_selection(
    chain,  # noqa: F811
    tmp_path,
    monkeypatch,
    capsys,
    data_dir,
    operator_key,  # noqa: F811
):
    """Every sighted ad already fetched: fetch-ads plans 0 and writes no run doc, but says why."""
    approved(capsys)
    assert main(["refresh", "--approve-network"]) == 0
    capsys.readouterr()
    approved(capsys)  # the same bounds again: a second refresh, the same pages, nothing new to fetch
    assert main(["refresh", "--approve-network"]) == 0
    out, err = capsys.readouterr()
    s = json.loads(out.strip().splitlines()[-1])
    ads = {x["name"]: x for x in s["steps"]}["fetch-ads"]["ads"]
    assert ads["eligible"] == 0 and ads["lanes"] == {"new": 0, "revisit": 0}
    assert "fetch-ads selection: 0 eligible" in err


def test_an_unreadable_run_doc_is_exit_14_not_a_crash(chain, data_dir, capsys, monkeypatch):  # noqa: F811
    approved(capsys)

    def corrupt(argv):
        chain.append(list(argv))
        code, objs = refresh._exec(argv)
        if argv[0] == "crawl-lists" and objs:
            (store.runs_dir() / f"{store.safe_name(objs[-1]['run_id'])}.json").write_text("{not json")
        return code, objs

    monkeypatch.setattr(refresh, "EXEC", corrupt)
    assert main(["refresh", "--approve-network"]) == 14
    st = state()
    assert st["steps"][0]["exit"] == 14 and "unreadable" in st["steps"][0]["reason"]
    assert st["status"] == "spent" and names(chain) == ["crawl-lists"]


def test_an_offline_list_step_is_not_a_list_read_of_the_refresh(
    chain,  # noqa: F811
    tmp_path,
    monkeypatch,
    capsys,
    data_dir,
    operator_key,  # noqa: F811
):
    """An offline backend spends no approval, so its run doc names no refresh run. fetch-ads would find no
    fresh sighting from it (cli._passed_lists binds on the same field), so the list step fails loudly here
    rather than letting fetch-ads plan 0 in silence."""
    make_pipeline(
        tmp_path, monkeypatch, operator_key["fp"], driver="fixture", steps=("crawl-lists", "fetch-ads")
    )
    approved(capsys)
    assert main(["refresh", "--approve-network"]) == 14
    step = state()["steps"][0]
    assert step["exit"] == 14 and "is not a crawl-lists run of refresh run" in step["reason"]
    assert names(chain) == ["crawl-lists"]


def test_a_step_that_stops_non_zero_still_shows_its_outcome(chain, data_dir, capsys, monkeypatch):  # noqa: F811
    """A Stop prints no run_id line; the summary finds the step's run doc by its approval_ref instead."""
    approved(capsys)

    def stopped(argv):
        chain.append(list(argv))
        code, objs = refresh._exec(argv)
        return (5, []) if argv[0] == "fetch-ads" else (code, objs)

    monkeypatch.setattr(refresh, "EXEC", stopped)
    assert main(["refresh", "--approve-network"]) == 5
    out, err = capsys.readouterr()
    s = json.loads(out.strip().splitlines()[-1])
    ads = {x["name"]: x for x in s["steps"]}["fetch-ads"]["ads"]
    assert ads["ad_gets"] > 0 and set(ads["lanes"]) == {"new", "revisit"}
    assert "fetch-ads selection:" in err
