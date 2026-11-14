"""Egress preflight, offline: the trace endpoint is configuration, so tests point it at file://
fixture bodies (synthetic; the IP is from the TEST-NET-1 documentation block)."""

import json

import pytest
from conftest import FIXTURES, LIST
from example_site.source import ExampleSite

from scrape_harness import crawl, egress, store
from scrape_harness.drivers.fixture import FixtureBackend, FixtureHttpClient

FAST = crawl.Politeness(per_minute=6000, override=True)


def use_trace(monkeypatch, name):
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / name).as_uri())


def test_home_country_passes_and_the_ip_is_never_kept(monkeypatch):
    use_trace(monkeypatch, "trace-ca.txt")
    e = egress.preflight()
    assert (e["loc"], e["colo"], e["warp"], e["gateway"]) == ("CA", "YYZ", "off", "off")
    assert "ip" not in e and "192.0.2.10" not in json.dumps(e)


def test_foreign_egress_is_refused_with_no_override(monkeypatch):
    use_trace(monkeypatch, "trace-at.txt")
    with pytest.raises(egress.EgressRefused, match="egress is AT .*not CA.*no override"):
        egress.preflight()


def test_home_country_is_configuration(monkeypatch):
    use_trace(monkeypatch, "trace-at.txt")
    monkeypatch.setenv("EX_HOME_COUNTRY", "at")
    assert egress.preflight()["loc"] == "AT"


def test_unreachable_trace_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (tmp_path / "missing.txt").as_uri())
    with pytest.raises(egress.EgressRefused, match="preflight failed"):
        egress.preflight()


def test_garbage_trace_fails_closed():
    with pytest.raises(egress.EgressRefused):
        egress.preflight(get=lambda url: "<html>not a trace</html>")


@pytest.fixture
def networked_fixture(monkeypatch):
    """The offline backend, pretending to be networked so the preflight runs (it sends nothing)."""
    monkeypatch.setattr(FixtureBackend, "network", True)
    FixtureHttpClient.log.clear()
    return {"backend": "fixture", "root": str(FIXTURES)}


def test_run_refuses_before_any_request_on_a_vpn(data_dir, monkeypatch, networked_fixture):
    use_trace(monkeypatch, "trace-at.txt")
    job = crawl.Run.new("crawl-lists", "http", "example.invalid")
    with pytest.raises(crawl.Stop) as e:
        crawl.run(
            job,
            ExampleSite(),
            lambda m, r: None,
            FAST,
            [{"url": f"{LIST}?page=1", "kind": "list", "meta": {}}],
            networked_fixture,
        )
    assert e.value.code == crawl.EXIT_EGRESS == 8
    assert FixtureHttpClient.log == []  # not even robots.txt was requested
    assert crawl.Run.load(job.doc["run_id"]).doc["status"] == "stopped"


def test_run_records_egress_and_challenge_evidence_carries_it(
    data_dir, monkeypatch, networked_fixture, tmp_path
):
    use_trace(monkeypatch, "trace-ca.txt")
    fx = tmp_path / "fx"
    fx.mkdir()
    for f in FIXTURES.iterdir():
        (fx / f.name).write_bytes(f.read_bytes())
    urls = json.loads((FIXTURES / "urls.json").read_text())
    urls[f"{LIST}?page=1"] = {"file": "challenge-cf.html", "status": 403, "headers": {"server": "cloudflare"}}
    (fx / "urls.json").write_text(json.dumps(urls))
    job = crawl.Run.new("crawl-lists", "http", "example.invalid")
    with pytest.raises(crawl.Stop):
        crawl.run(
            job,
            ExampleSite(),
            lambda m, r: None,
            FAST,
            [{"url": f"{LIST}?page=1", "kind": "list", "meta": {}}],
            {"backend": "fixture", "root": str(fx)},
        )
    assert crawl.Run.load(job.doc["run_id"]).doc["egress"]["loc"] == "CA"
    (ev,) = [x for x in store.read("events") if x["kind"] == "challenge"]
    assert ev["evidence"]["egress"]["colo"] == "YYZ" and ev["evidence"]["server"] == "cloudflare"
