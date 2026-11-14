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
        ("ab.v1", EXAMPLES / "ab.example.json"),
        ("approval.v2", EXAMPLES / "approval.example.json"),
        ("sample.v1", EXAMPLES / "sample.example.json"),
        ("status.v1", EXAMPLES / "status.example.json"),
        ("refresh.v1", EXAMPLES / "refresh.example.json"),
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
            "event.v1",
            "finding.v1",
            "incident.v1",
            "pipeline.v1",
            "refresh.v1",
            "run.v1",
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
