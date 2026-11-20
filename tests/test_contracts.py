import json

import pytest
from conftest import EXAMPLE, EXAMPLES

from scrape_harness import contracts

GEO = "listing.v1.schema.json#/$defs/geo"  # a sub-schema of the example consumer's listing.v1
REF = EXAMPLE / "schemas" / "examples"


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("finding.v1", EXAMPLES / "finding.example.json"),
        ("event.v1", EXAMPLES / "event.example.json"),
        ("run.v1", EXAMPLES / "run.example.json"),
        ("run.v1", EXAMPLES / "run.list-stop.example.json"),  # list{} + stop{} (exit 14)
        ("run.v1", EXAMPLES / "run.fetch-ads-sync.example.json"),  # ads{} + breaker{} + stop{}
        ("event.v1", EXAMPLES / "event.parse-failed.example.json"),
        ("incident.v1", EXAMPLES / "incident.example.json"),
        ("incident.v1", EXAMPLES / "incident.parse.example.json"),
        ("ab.v1", EXAMPLES / "ab.example.json"),
        ("approval.v2", EXAMPLES / "approval.example.json"),
        ("sample.v1", EXAMPLES / "sample.example.json"),
        ("status.v1", EXAMPLES / "status.example.json"),
        ("refresh.v1", EXAMPLES / "refresh.example.json"),
        ("list_snapshot.v1", EXAMPLES / "list_snapshot.example.json"),
        ("cli.v1", EXAMPLES / "cli.example.json"),
        ("preflight.v1", EXAMPLES / "preflight.example.json"),
        ("run_report.v1", EXAMPLES / "run_report.example.json"),
        ("diagnosis.v1", EXAMPLES / "diagnosis.example.json"),
        ("listing.v1", REF / "listing.example.json"),  # a consumer's schemas, via add_schema_dir
        ("observation.v1", REF / "observation.example.json"),
    ],
)
def test_examples_validate(name, path):
    contracts.validate(name, json.loads(path.read_text()))


@pytest.mark.parametrize(
    "path", sorted([*(EXAMPLES / "invalid").glob("*.json"), *(REF / "invalid").glob("*.json")])
)
def test_invalid_examples_fail(path):
    with pytest.raises(contracts.ContractError):
        name = next(n for n in contracts.names() if n.split(".")[0] == path.name.split(".")[0])
        contracts.validate(name, json.loads(path.read_text()))


def test_records_written_before_the_sync_fields_still_validate():
    """Additive contracts: a run, event or incident written before list/ads/breaker/stop and ad_id existed
    is still valid, so every JSONL line already on disk still reads."""
    run = json.loads((EXAMPLES / "run.example.json").read_text())
    assert not {"list", "ads", "breaker", "stop"} & run.keys()
    contracts.validate("run.v1", run)
    ev = json.loads((EXAMPLES / "event.example.json").read_text())
    assert "ad_id" not in ev
    contracts.validate("event.v1", ev)
    gone = {k: v for k, v in ev.items() if k != "evidence"} | {"kind": "gone", "reason": "expired_notice"}
    contracts.validate("event.v1", gone)


def test_a_gone_event_may_name_its_ad():
    ev = json.loads((EXAMPLES / "event.example.json").read_text())
    gone = {k: v for k, v in ev.items() if k != "evidence"} | {"kind": "gone", "reason": "http_410"}
    contracts.validate("event.v1", gone | {"ad_id": "x-1"})
    contracts.validate("event.v1", gone | {"ad_id": None})  # a legacy-shaped writer may not know it
    with pytest.raises(contracts.ContractError):
        contracts.validate("event.v1", gone | {"ad_id": ""})


@pytest.mark.parametrize("status", ["ok", "zero_yield", "failed", "blocked", "parse_drift"])
def test_every_list_status_validates(status):
    run = json.loads((EXAMPLES / "run.list-stop.example.json").read_text())
    contracts.validate("run.v1", run | {"list": {"status": status, "observations_written": 3}})


@pytest.mark.parametrize("kind", ["parse", "gone_storm"])
def test_the_new_incident_kinds_validate(kind):
    inc = json.loads((EXAMPLES / "incident.parse.example.json").read_text())
    contracts.validate("incident.v1", inc | {"kind": kind})


def test_date_time_format_is_enforced():
    """Same behaviour as check-jsonschema: a malformed date-time is an error, not a pass."""
    rec = json.loads((EXAMPLES / "finding.example.json").read_text()) | {"tested_at": "yesterday"}
    assert any("tested_at" in e for e in contracts.errors("finding.v1", rec))


def test_subschema_reference_into_a_consumer_schema():
    contracts.validate(GEO, {"lat": 10.5, "lon": -29.5})
    with pytest.raises(contracts.ContractError):
        contracts.validate(GEO, {"lat": 91, "lon": 0})


def test_the_core_ships_only_its_own_contracts():
    """Record schemas (observation, listing, ...) belong to a CONSUMER; one in the core's schemas/ would ship
    in the wheel and shadow the consumer's own (it happened: a template copy landed there)."""
    from conftest import ROOT

    core = {p.name for p in (ROOT / "schemas").glob("*.schema.json")}
    assert core == {
        f"{n}.schema.json"
        for n in (
            "ab.v1",
            "approval.v2",
            "cli.v1",
            "diagnosis.v1",
            "event.v1",
            "finding.v1",
            "incident.v1",
            "list_snapshot.v1",
            "pipeline.v1",
            "preflight.v1",
            "refresh.v1",
            "run.v1",
            "run_report.v1",
            "sample.v1",
            "status.v1",
        )
    }


@pytest.fixture
def cold_registry(monkeypatch):
    """No consumer schema registered yet, as in a fresh process whose FIRST lookup is a sub-ref."""
    from scrape_harness import pipeline

    def clear():
        contracts._registry.cache_clear()
        contracts._validator.cache_clear()
        pipeline._cached.cache_clear()

    monkeypatch.setattr(contracts, "_EXTRA_DIRS", [])
    clear()
    yield
    monkeypatch.undo()
    clear()


def test_a_subschema_reference_is_the_first_lookup(cold_registry):
    """A sub-ref into a consumer schema must load the consumer's schema dirs itself (it once only
    resolved when a by-title lookup had loaded them first, so `pytest one_file.py` failed)."""
    assert "listing.v1" not in contracts._registry()[1]
    contracts.validate(GEO, {"lat": 10.5, "lon": -29.5})
    with pytest.raises(contracts.ContractError):
        contracts.validate(GEO, {"lat": 91, "lon": 0})


def test_the_sync_fields_round_trip_through_the_store(data_dir):
    """Written by the store's own writers (which validate) and read back unchanged: a run doc with
    list/stop and ads/breaker, and a parse_failed event naming its ad, beside a legacy event without one."""
    from scrape_harness import store

    for name in ("run.list-stop.example.json", "run.fetch-ads-sync.example.json"):
        doc = json.loads((EXAMPLES / name).read_text())
        p = store.runs_dir() / f"{store.safe_name(doc['run_id'])}.json"
        store.write_json(p, "run.v1", doc)
        assert store.read_json(p, "run.v1") == doc
    new = json.loads((EXAMPLES / "event.parse-failed.example.json").read_text())
    old = json.loads((EXAMPLES / "event.example.json").read_text())
    assert store.append("events", [old, new]) == 2
    assert list(store.read("events")) == [old, new]


# ── the agents' answers: run_report.v1 (run-steward) and diagnosis.v1 (pipeline-engineer) ────────────


def _report(**kw):
    return json.loads((EXAMPLES / "run_report.example.json").read_text()) | kw


def _diagnosis(**kw):
    return json.loads((EXAMPLES / "diagnosis.example.json").read_text()) | kw


@pytest.mark.parametrize(
    "change",
    [
        {"state": "running", "exit_code": None, "stop_reason": None, "next_action": "none"},
        {
            "state": "finished",
            "exit_code": 0,
            "stop_reason": None,
            "next_action": "none",
            "open_incidents": [],
        },
        {"state": "finished", "exit_code": None, "stop_reason": None, "next_action": "none"},
        {
            "state": "refused",
            "exit_code": 10,
            "stop_reason": "no approval for this plan",
            "next_action": "approve",
        },
        {"state": "stopped", "exit_code": 6, "stop_reason": "cooldown", "next_action": "wait_cooldown"},
        {"evidence": ["STATE:status.json", "DATA:events.jsonl"]},
        {"run_ids": ["a-run", "another-run"]},
    ],
)
def test_a_run_report_validates(change):
    contracts.validate("run_report.v1", _report(**change))


@pytest.mark.parametrize(
    "change",
    [
        {"state": "running", "exit_code": 5},  # a running run has no exit code yet
        {"state": "running", "stop_reason": "x", "exit_code": None},
        {"state": "finished", "exit_code": 5, "stop_reason": None},  # a non-zero exit is a stop
        {"state": "stopped", "exit_code": 0},  # a stop is a non-zero exit
        {"state": "stopped", "stop_reason": ""},
        {"state": "done"},
        {"next_action": "retry"},  # closed enum: a stop is never retried in a loop
        {"counts": {"list_pages": -1}},
        {"counts": {"list_pages": 1.5}},
        {"open_incidents": ["INC-1"]},
        {"evidence": ["DATA:../outside.json"]},
        {"evidence": ["DATA:/abs.json"]},
        {"evidence": ["runs/x.json"]},  # a ref names its root
        {"evidence": []},
        {"run_ids": []},
        {"extra": 1},
    ],
)
def test_a_malformed_run_report_fails(change):
    with pytest.raises(contracts.ContractError):
        contracts.validate("run_report.v1", _report(**change))


@pytest.mark.parametrize(
    "change",
    [
        {"blocked_on": "operator_approval", "fix": None, "confidence": "low"},
        {"blocked_on": "operator_decision"},  # blocked with a fix proposed is fine
        {"evidence": [{"file": "DATA:events.jsonl", "quote": "x" * 200}]},
        {"evidence": [{"file": "STATE:status.json", "line": 1, "quote": "running"}]},
        {"tests": []},
    ],
)
def test_a_diagnosis_validates(change):
    contracts.validate("diagnosis.v1", _diagnosis(**change))


@pytest.mark.parametrize(
    "change",
    [
        {"fix": None},  # not blocked: a fix is proposed
        {"confidence": "certain"},
        {"evidence": []},
        {"evidence": [{"file": "/abs/source.py", "quote": "x"}]},
        {"evidence": [{"file": "~/source.py", "quote": "x"}]},
        {"evidence": [{"file": "../source.py", "quote": "x"}]},
        {"evidence": [{"file": "DATA:/abs.json", "quote": "x"}]},
        {"evidence": [{"file": "src/../../x.py", "quote": "x"}]},
        {"evidence": [{"file": "a.py", "line": 0, "quote": "x"}]},
        {"evidence": [{"file": "a.py", "quote": "x", "extra": 1}]},
        {"evidence": [{"file": "a.py"}]},
        {"fix": {"change": "x", "files": ["/abs.py"]}},
        {"fix": {"change": "x", "files": [], "extra": 1}},
        {"symptom": ""},
        {"extra": 1},
    ],
)
def test_a_malformed_diagnosis_fails(change):
    with pytest.raises(contracts.ContractError):
        contracts.validate("diagnosis.v1", _diagnosis(**change))


@pytest.mark.parametrize("name", ["run_report", "diagnosis"])
def test_the_cli_validates_an_agent_answer(name, capsys, tmp_path):
    from scrape_harness import cli

    good = EXAMPLES / f"{name}.example.json"
    assert cli.main(["validate", f"{name}.v1", str(good)]) == 0
    bad = next((EXAMPLES / "invalid").glob(f"{name}.*.json"))
    assert cli.main(["validate", f"{name}.v1", str(bad)]) != 0
