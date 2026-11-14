"""Live per-run JSON-lines log (ADR-0007) and the import/ boundary."""

import json
import logging

import pytest
from conftest import FIXTURES, LIST
from example_site.source import ExampleSite

from scrape_harness import crawl, runlog, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureBackend

FIX = [
    "--driver",
    "fixture",
    "--fixtures",
    str(FIXTURES),
    "--max-tasks-per-minute",
    "6000",
    "--approve-politeness-override",
]
KIND = {"list": "list_pages", "ad": "ad_pages", "robots": "robots", "other": "other"}


def runs(d):
    return [json.loads(p.read_text()) for p in sorted((d / "runs").glob("*.json"))]


def log_lines(d, run):
    return [json.loads(x) for x in (d / run["log_ref"]).read_text().splitlines()]


def test_request_lines_equal_the_run_counts(data_dir):
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    assert main(["fetch-ads", *FIX]) == 0
    for run in runs(data_dir):
        lines = log_lines(data_dir, run)
        req = [x for x in lines if x.get("event") == "request"]
        tally = {k: 0 for k in KIND.values()}
        for x in req:
            tally[KIND[x["kind"]]] += 1
        assert tally == {k: run["requests"][k] for k in KIND.values()}
        assert all("?" not in x["path"] and x["status"] == 200 and x["ms"] >= 0 for x in req)
        assert (
            lines[0]["event"] == "run_card" and lines[0]["card"]["run_id"] == run["run_id"]
        )  # the card leads
        assert [x["event"] for x in lines if x.get("event") in ("run_card", "stop")] == ["run_card", "stop"]
        assert any(x["logger"].startswith("crawlee") for x in lines)  # crawlee's own logger is captured
    list_req = [x for x in log_lines(data_dir, runs(data_dir)[0]) if x.get("kind") == "list"]
    assert sorted(x["page"] for x in list_req) == [1, 3]  # the page number, not the query string


def test_preflight_refusal_is_logged(data_dir, monkeypatch):
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-at.txt").as_uri())
    monkeypatch.setattr(FixtureBackend, "network", True)
    job = crawl.Run.new("crawl-lists", "http", "example.invalid")
    with pytest.raises(crawl.Stop):
        crawl.run(
            job,
            ExampleSite(),
            lambda m, r: None,
            crawl.Politeness(),
            [{"url": f"{LIST}?page=1", "kind": "list", "meta": {}}],
            {"backend": "fixture", "root": str(FIXTURES)},
        )
    (run,) = runs(data_dir)
    lines = log_lines(data_dir, run)
    assert any(x.get("event") == "preflight" and x.get("reason") == "egress_refused" for x in lines)
    assert lines[-1]["event"] == "stop" and lines[-1]["code"] == 8
    assert not [x for x in lines if x.get("event") == "request"]


def test_lines_are_flushed_live_and_the_handler_is_detached(data_dir):
    with runlog.attached("synthetic-run") as rel:
        runlog.LOG.info("hello", extra={"event": "start"})
        assert json.loads((data_dir / rel).read_text().splitlines()[-1])["msg"] == "hello"  # readable mid-run
    before = (data_dir / rel).read_text()
    logging.getLogger("scrape_harness.run").info("after the run")
    assert (data_dir / rel).read_text() == before


def test_redaction():
    out = runlog.redact("GET https://x.example/a/b?page=2&token=abc Cookie: sid=123 key=" + "Z" * 40)
    assert "?[query]" in out and "token=abc" not in out and "sid=123" not in out and "Z" * 40 not in out


def test_no_command_reads_import(data_dir, capsys):
    """import/ holds a non-li scrape: nothing in li may treat it as input."""
    imp = data_dir / "import" / "2026-01-01-synthetic"
    imp.mkdir(parents=True)
    for name in ("listings.jsonl", "observations.jsonl", "findings.jsonl", "events.jsonl"):
        (imp / name).write_text("not li data\n")
    snapshot = {p: p.read_bytes() for p in imp.iterdir()}
    for argv in (
        ["reparse"],
        ["findings", "current"],
        ["cooldown", "status"],
        ["fetch-ads", "--dry-run"],
    ):
        assert main(argv) == 0, argv
    assert {p: p.read_bytes() for p in imp.iterdir()} == snapshot
    assert not any(path.startswith("import/") for path, _ in store.files().values())
