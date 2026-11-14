"""The operator's audited cooldown clear: appended, never deleted, honoured by the counter."""

import json

import pytest
from conftest import FIXTURES, LIST
from example_site.source import ExampleSite

from scrape_harness import crawl, store
from scrape_harness.cli import EXIT_APPROVAL, main

FAST = crawl.Politeness(per_minute=6000, override=True)
P1 = f"{LIST}?page=1"


def blocked_run(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    for f in FIXTURES.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    urls = json.loads((FIXTURES / "urls.json").read_text()) | {P1: {"file": "", "status": 403}}
    (d / "urls.json").write_text(json.dumps(urls))
    job = crawl.Run.new("crawl-lists", "http", "example.invalid")
    with pytest.raises(crawl.Stop) as e:
        crawl.run(
            job,
            ExampleSite(),
            lambda m, r: None,
            FAST,
            [{"url": P1, "kind": "list", "meta": {}}],
            {"backend": "fixture", "root": str(d)},
        )
    return e.value.code


def status(capsys, *extra):
    capsys.readouterr()
    assert main(["cooldown", "status", *extra]) == 0
    return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]


def test_clear_unblocks_appends_and_resets_the_count(data_dir, tmp_path, capsys):
    assert blocked_run(tmp_path, "a") == crawl.EXIT_CHALLENGE
    assert blocked_run(tmp_path, "b") == crawl.EXIT_COOLDOWN
    (cool,) = status(capsys, "--site", "example.invalid")
    assert cool["site"] == "example.invalid" and cool["reason"] == "http_403" and cool["run_id"]
    before = (data_dir / "events.jsonl").read_text()

    assert (
        main(["cooldown", "clear", "--site", "example.invalid", "--reason", "false positive, synthetic test"])
        == EXIT_APPROVAL
    )
    assert (data_dir / "events.jsonl").read_text() == before  # refused: nothing appended
    assert (
        main(
            [
                "cooldown",
                "clear",
                "--site",
                "example.invalid",
                "--reason",
                "false positive, synthetic test",
                "--approve-operator",
            ]
        )
        == 0
    )
    after = (data_dir / "events.jsonl").read_text()
    assert after.startswith(before)  # append-only: every earlier line is still there, unchanged
    cleared = [e for e in store.read("events") if e["kind"] == "cooldown_cleared"]
    assert len(cleared) == 1 and cleared[0]["clears"]["run_id"] == cool["run_id"]
    assert cleared[0]["reason"] == "false positive, synthetic test"
    assert status(capsys) == []  # a cleared cooldown no longer blocks

    assert crawl.recent_challenges("example.invalid") == 0  # challenges before the clear no longer count
    assert blocked_run(tmp_path, "c") == crawl.EXIT_CHALLENGE  # the first challenge after: counts from zero
    assert blocked_run(tmp_path, "d") == crawl.EXIT_COOLDOWN  # the second: a new cooldown


def test_clear_with_nothing_active_appends_nothing(data_dir, capsys):
    assert (
        main(["cooldown", "clear", "--site", "example.invalid", "--reason", "x", "--approve-operator"]) == 0
    )
    assert not (data_dir / "events.jsonl").exists()


def test_clear_needs_a_reason(data_dir):
    assert (
        main(["cooldown", "clear", "--site", "example.invalid", "--reason", " ", "--approve-operator"]) == 4
    )
