"""`probe robots`: ONE robots.txt GET after the egress preflight, gated like any site run, never retried.

Offline: the fixture backend stands in for impit (treated as networked so the gate, the egress preflight
and the approval apply); FixtureHttpClient.log is the instrument for what reached the "site"."""

import json
import shutil

import pytest
from conftest import FIXTURES, ORIGIN
from test_approvals import networked, operator_key, plan_hash  # noqa: F401 - pytest fixtures + helper

from scrape_harness import approvals, crawl, incidents, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureHttpClient
from scrape_harness.interfaces import FetchResult

ROBOTS = f"{ORIGIN}/robots.txt"
BASE = ["probe", "robots", "--driver", "fixture", "--approve-network"]


def argv(tmp_path, entry=None):
    """The probe's argv against a fixture copy whose robots.txt answers with `entry` (default: the real file)."""
    d = tmp_path / "fx"
    if not d.exists():
        shutil.copytree(FIXTURES, d)
    m = json.loads((d / "urls.json").read_text())
    if entry is not None:
        m[ROBOTS] = entry
    (d / "urls.json").write_text(json.dumps(m))
    return [*BASE, "--fixtures", str(d)]


def approved_run(capsys, args) -> tuple[int, dict]:
    approvals.approve(plan_hash(capsys, args))
    capsys.readouterr()
    code = main(args)
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])


def site_gets() -> list[str]:
    return [u for u in FixtureHttpClient.log if u.startswith(ORIGIN)]


def test_without_an_approval_it_refuses_and_sends_nothing(networked, tmp_path):  # noqa: F811
    assert main(argv(tmp_path)) == approvals.EXIT_UNAPPROVED
    assert site_gets() == []


def test_without_the_network_flag_it_refuses_before_spending_the_approval(networked, tmp_path, capsys):  # noqa: F811
    args = argv(tmp_path)
    approvals.approve(plan_hash(capsys, args))
    with pytest.raises(SystemExit):
        main([a for a in args if a != "--approve-network"])
    assert site_gets() == [] and not [r for r in approvals.chain() if r["kind"] == "used"]


def test_the_plan_signs_one_robots_get_and_no_retry(networked, tmp_path, capsys):  # noqa: F811
    capsys.readouterr()
    assert main([*argv(tmp_path), "--plan"]) == 0
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert plan["command"] == "probe robots" and plan["url"] == ROBOTS
    assert plan["retries"] == 0 and plan["variants"] == ["http"]
    assert site_gets() == []  # a plan sends nothing


def test_a_clean_robots_is_exactly_one_request_and_completes(networked, tmp_path, capsys):  # noqa: F811
    code, out = approved_run(capsys, argv(tmp_path))
    assert code == 0 and out["status"] == "completed"
    assert site_gets() == [ROBOTS]  # exactly one request reached the site
    p = out["probe"]
    assert p["verdict"] == "clean" and p["status"] == 200 and p["reason"] is None
    assert p["markers"]["robots_records"] >= 1 and p["markers"]["html"] is False
    assert p["egress_country"] == "CA" and p["latency_ms"] >= 0
    (rec,) = [json.loads(x.read_text()) for x in store.runs_dir().glob("*.json")]
    assert (
        rec["command"] == "probe" and rec["requests"]["robots"] == 1 and rec["requests"]["egress_trace"] == 1
    )
    assert rec["approval_ref"]["plan_hash"]
    assert main(argv(tmp_path)) == approvals.EXIT_UNAPPROVED  # single use


def test_a_timeout_is_unreachable_once_never_retried_and_no_cooldown(networked, tmp_path, capsys):  # noqa: F811
    code, out = approved_run(capsys, argv(tmp_path, {"error": "timeout"}))
    assert code == crawl.EXIT_UNREACHABLE
    assert site_gets() == [ROBOTS]  # ONE attempt: no retry on a timeout
    assert out["status"] == "stopped" and out["probe"]["verdict"] == "unreachable"
    assert out["probe"]["status"] is None
    assert crawl.recent_challenges("example.invalid") == 0 and crawl.cooldown_until("example.invalid") is None
    (inc,) = incidents.open_ones()
    assert inc["kind"] == "unreachable"


@pytest.mark.parametrize(
    "entry, reason",
    [
        ({"file": "robots.txt", "status": 403}, "http_403"),
        ({"file": "robots.txt", "status": 429}, "http_429"),
        ({"file": "robots.txt", "status": 503}, "http_503"),
        ("challenge-cf.html", "challenge_title"),  # a 200 challenge interstitial
        ("home.html", "unexpected_page"),  # a 200 HTML page where robots.txt should be
    ],
)
def test_a_block_or_challenge_is_not_clean_one_request_and_a_challenge_stop(
    networked,  # noqa: F811
    tmp_path,
    capsys,
    entry,
    reason,
):
    code, out = approved_run(capsys, argv(tmp_path, entry))
    assert code == crawl.EXIT_CHALLENGE
    assert site_gets() == [ROBOTS]
    assert out["probe"]["verdict"] == "not_clean" and out["probe"]["reason"] == reason
    assert out["status"] == "stopped"
    events = [e for e in store.read("events") if e["kind"] == "challenge"]
    assert [e["reason"] for e in events] == [reason]  # it counts toward the site cooldown, like any challenge
    assert [i["kind"] for i in incidents.open_ones()] == ["challenge"]


def test_an_egress_refusal_sends_nothing_to_the_site_and_refunds(networked, tmp_path, capsys, monkeypatch):  # noqa: F811
    args = argv(tmp_path)
    h = plan_hash(capsys, args)
    approvals.approve(h)
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-at.txt").as_uri())
    assert main(args) == crawl.EXIT_EGRESS
    assert site_gets() == [] and approvals.has_live(h)


def test_a_probe_run_is_never_picked_up_by_crawl_resume(networked, tmp_path, capsys):  # noqa: F811
    approved_run(capsys, argv(tmp_path, {"error": "timeout"}))  # a stopped probe
    with pytest.raises(SystemExit, match="no resumable run"):
        crawl.Run.load()


# ── the verdict itself, and a perturbation ─────────────────────────────────────────────────────────


def res(status=200, body="User-agent: *\nDisallow: /private\n", headers=None):
    return FetchResult(ROBOTS, status, ROBOTS, body, headers or {"content-type": "text/plain"})


def test_the_verdict_is_clean_only_for_a_robots_body_with_200():
    assert crawl.probe_verdict(res()) is None
    assert crawl.probe_verdict(res(404)) == "http_404"
    assert crawl.probe_verdict(res(body="")) == "unexpected_page"
    assert crawl.probe_verdict(res(body="<!doctype html><html>User-agent: *</html>")) == "unexpected_page"
    assert crawl.probe_verdict(res(headers={"cf-mitigated": "challenge"})) == "cf_mitigated"


def test_perturbation_with_the_verdict_disabled_a_403_reads_clean(networked, tmp_path, capsys, monkeypatch):  # noqa: F811
    """The block tests above hold only because the run consults probe_verdict(): disable it and the SAME
    403 fixture completes as "clean". If this ever stops reading clean, the block tests prove nothing."""
    monkeypatch.setattr(crawl, "probe_verdict", lambda r: None)
    code, out = approved_run(capsys, argv(tmp_path, {"file": "robots.txt", "status": 403}))
    assert code == 0 and out["probe"]["verdict"] == "clean" and out["probe"]["status"] == 403
