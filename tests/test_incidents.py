"""The incident protocol's record: every stop or refusal opens one incident in STATE/ops/incidents.jsonl
(append-only); it stays open until an acked line with its id is appended."""

import json

import pytest
from test_cooldown_clear import blocked_run

from scrape_harness import contracts, crawl, incidents
from scrape_harness.cli import main


def lines(tmp_path):
    p = tmp_path / "state" / "ops" / "incidents.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_a_challenge_stop_opens_one_incident_with_its_run_and_stage(data_dir, tmp_path):
    assert blocked_run(tmp_path, "a") == crawl.EXIT_CHALLENGE
    (inc,) = lines(tmp_path)
    contracts.validate("incident.v1", inc)
    run = json.loads(next((data_dir / "runs").glob("*.json")).read_text())
    assert inc["event"] == "opened" and inc["kind"] == "challenge" and inc["code"] == 5
    assert inc["run_id"] == run["run_id"] and inc["stage"] == "crawl-lists" and "http_403" in inc["reason"]
    assert not (data_dir / "ops").exists()  # STATE, never DATA


def test_a_second_stop_is_a_second_incident_a_cooldown(data_dir, tmp_path):
    blocked_run(tmp_path, "a")
    assert blocked_run(tmp_path, "b") == crawl.EXIT_COOLDOWN
    assert [i["kind"] for i in lines(tmp_path)] == ["challenge", "cooldown"]


def test_the_same_stop_twice_rings_once(data_dir):
    a = incidents.open_incident(5, "x", "run-1", "fetch-ads")
    assert a and incidents.open_incident(5, "x again", "run-1", "fetch-ads") is None
    assert len(incidents.open_ones()) == 1


def test_ack_is_an_appended_line_and_closes_it(data_dir, tmp_path, capsys):
    inc = incidents.open_incident(8, "refusing to start: egress XX", None, "crawl-lists")
    capsys.readouterr()
    assert main(["incidents", "list", "--open"]) == 0
    assert [json.loads(x)["id"] for x in capsys.readouterr().out.splitlines()] == [inc["id"]]
    with pytest.raises(SystemExit, match="--note"):
        main(["incidents", "ack", inc["id"]])
    assert (
        main(["incidents", "ack", inc["id"], "--note", "operator turned the VPN off", "--by", "operator"])
        == 0
    )
    assert [r["event"] for r in lines(tmp_path)] == ["opened", "acked"]  # appended, nothing edited
    assert incidents.open_ones() == []
    with pytest.raises(SystemExit, match="already acked"):
        incidents.ack(inc["id"], "again")
    with pytest.raises(SystemExit, match="no such incident"):
        incidents.ack("INC-20990101T000000Z-000000", "x")


def test_a_refused_networked_run_opens_an_approval_incident(data_dir, tmp_path, monkeypatch):
    from conftest import FIXTURES

    from scrape_harness.drivers.fixture import FixtureBackend

    monkeypatch.setattr(FixtureBackend, "network", True)
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-ca.txt").as_uri())
    argv = [
        "crawl-lists",
        "--subcategory",
        "widgets",
        "--driver",
        "fixture",
        "--fixtures",
        str(FIXTURES),
        "--approve-network",
    ]
    assert main(argv) == 10
    (inc,) = lines(tmp_path)
    assert inc["kind"] == "approval" and inc["code"] == 10 and inc["stage"] == "crawl-lists"
