"""Standing approvals: ONE signature over BOUNDS; every scheduled run proves it stays inside them.
The throwaway test key stands in for the Touch ID key (test_approvals' fixtures)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_approvals import KeyFileSigner, make_key, operator_key  # noqa: F401 - pytest fixtures by import

from scrape_harness import approvals
from scrape_harness.cli import main

T0 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
FMT = "%Y-%m-%dT%H:%M:%SZ"
PANEL = "a" * 64


def bounds(**kw):
    b = {
        "panel_hash": PANEL,
        "site": "example.invalid",
        "max_runs": 3,
        "max_ad_requests_per_run": 150,
        "max_list_pages_per_run": 5,
        "min_interval_s": 6 * 3600,
        "not_before": "2026-01-01T00:00:00Z",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).strftime(FMT),
        "egress_country": "CA",
        "variants": ["desktop"],
        "max_rate_per_minute": 7,
    }
    return b | kw


def run(**kw):
    return {
        "panel_hash": PANEL,
        "site": "example.invalid",
        "ad_requests": 120,
        "list_pages": 3,
        "variant": "desktop",
        "rate_per_minute": 7,
        "egress_country": "CA",
    } | kw


def now(h=0):
    return datetime.now(UTC) + timedelta(hours=h)


def test_runs_inside_the_bounds_are_allowed_spaced_and_counted(data_dir):
    a = approvals.approve_standing(bounds())
    assert (
        a["kind"] == "standing"
        and a["plan_hash"] == approvals.canonical(bounds() | {"expires_at": a["expires_at"]})[1]
    )
    r1 = approvals.consume_standing(run(), "run-1", now())
    assert r1["standing"] and r1["run"] == 1
    with pytest.raises(approvals.NotApproved, match="too soon after the last run"):
        approvals.consume_standing(run(), "run-2", now(1))
    assert approvals.consume_standing(run(), "run-2", now(7))["run"] == 2
    assert approvals.consume_standing(run(), "run-3", now(14))["run"] == 3
    with pytest.raises(approvals.NotApproved, match="all 3 runs used"):
        approvals.consume_standing(run(), "run-4", now(21))


@pytest.mark.parametrize(
    "over, msg",
    [
        ({"ad_requests": 151}, "ad pages > 150"),
        ({"list_pages": 6}, "list pages > 5"),
        ({"variant": "mobile"}, "variant mobile not signed"),
        ({"rate_per_minute": 8}, "rate 8/min > 7"),
        ({"egress_country": "US"}, "egress US is not CA"),
    ],
)
def test_a_run_outside_any_bound_is_refused(data_dir, over, msg):
    approvals.approve_standing(bounds())
    with pytest.raises(approvals.NotApproved, match=msg):
        approvals.consume_standing(run(**over), "r", now())


def test_the_window_and_the_90_day_cap(data_dir):
    with pytest.raises(approvals.NotApproved, match="at most 90 days"):
        approvals.approve_standing(bounds(expires_at=(datetime.now(UTC) + timedelta(days=91)).strftime(FMT)))
    later = (datetime.now(UTC) + timedelta(days=10)).strftime(FMT)
    approvals.approve_standing(bounds(not_before=later))
    with pytest.raises(approvals.NotApproved, match="outside its window"):
        approvals.consume_standing(run(), "r", now())


def test_a_revoked_standing_approval_is_refused(data_dir, capsys):
    a = approvals.approve_standing(bounds())
    with pytest.raises(SystemExit, match="--note"):
        approvals.revoke(a["approval_id"], " ")
    assert main(["approvals", "revoke", a["approval_id"], "--note", "operator paused the panel"]) == 0
    with pytest.raises(approvals.NotApproved, match="revoked"):
        approvals.consume_standing(run(), "r", now())
    capsys.readouterr()
    assert main(["approvals", "verify"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["standing"] == 1 and rep["revoked"] == 1 and rep["bad_signatures"] == []


def test_another_panel_or_a_key_that_is_not_pinned_is_refused(data_dir, operator_key):  # noqa: F811
    approvals.approve_standing(bounds())
    with pytest.raises(approvals.NotApproved, match="no standing approval for this panel"):
        approvals.consume_standing(run(panel_hash="b" * 64), "r", now())
    attacker, _, _ = make_key(operator_key["dir"], "attacker")
    KeyFileSigner.key = attacker
    with pytest.raises(approvals.NotApproved, match="does not verify"):
        approvals.approve_standing(bounds(panel_hash="c" * 64))


def test_the_cli_signs_bounds_from_a_file(data_dir, tmp_path, capsys):
    f = tmp_path / "bounds.json"
    f.write_text(json.dumps(bounds()))
    capsys.readouterr()
    assert main(["approvals", "standing", str(f)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["standing"] and approvals.consume_standing(run(), "r", now())["approval_id"] == out["standing"]


def test_a_dry_check_spends_nothing(data_dir):
    approvals.approve_standing(bounds(max_runs=1))
    for _ in range(3):
        assert approvals.consume_standing(run(), "r", now(), dry=True)["dry"]
    assert approvals.consume_standing(run(), "r", now())["run"] == 1
    with pytest.raises(approvals.NotApproved, match="all 1 runs used"):
        approvals.consume_standing(run(), "r", now(7), dry=True)


def test_a_standing_ref_fits_the_run_record(data_dir):
    """The ref consume_standing returns is what a run record stores, so run.v1 must accept it."""
    from jsonschema import Draft202012Validator

    from scrape_harness import contracts

    approvals.approve_standing(bounds())
    ref = approvals.consume_standing(run(), "run-1", now())
    sub = json.loads((contracts.schema_dir() / "run.v1.schema.json").read_text())["properties"][
        "approval_ref"
    ]
    assert not list(Draft202012Validator(sub).iter_errors(ref))
    assert list(Draft202012Validator(sub).iter_errors({k: v for k, v in ref.items() if k != "run"}))


def test_the_proxy_is_part_of_the_signed_bounds(data_dir):
    """Changing the proxy needs a new signature: a run through another proxy, or through one when none
    was signed (or none when one was), is refused; the same URL passes."""
    url = "http://ca-tor.proxy.example:8080"
    approvals.approve_standing(bounds() | {"proxy": url})
    for wrong in ({}, {"proxy": "http://other.proxy.example:8080"}):
        with pytest.raises(approvals.NotApproved, match="proxy"):
            approvals.consume_standing(run() | wrong, "r", now())
    assert approvals.consume_standing(run() | {"proxy": url}, "r", now())["run"] == 1


def test_an_unproxied_standing_approval_refuses_a_proxied_run(data_dir):
    approvals.approve_standing(bounds())
    with pytest.raises(approvals.NotApproved, match="proxy"):
        approvals.consume_standing(run() | {"proxy": "http://ca-tor.proxy.example:8080"}, "r", now())


def test_standing_bounds_names_the_signed_proxy_without_spending_anything(data_dir):
    url = "http://ca-mon.proxy.example:8080"
    approvals.approve_standing(bounds() | {"proxy": url})
    (b,) = approvals.standing_bounds(bounds()["panel_hash"], now())
    assert b["proxy"] == url
    assert approvals.consume_standing(run() | {"proxy": url}, "r", now())["run"] == 1  # still unspent before
    assert approvals.standing_bounds("f" * 64, now()) == []
