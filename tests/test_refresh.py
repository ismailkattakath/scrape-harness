"""`<cli> refresh`: ONE bounded Touch ID approval over a declared chain of steps, run by doit.

The throwaway test key stands in for the Touch ID key (test_approvals' fixtures). Bound checks are unit
tests of approvals.consume_bounded; the chain itself runs for real: each step is its own `<cli>` process
(refresh.EXEC is only WRAPPED, to record what ran), against a test pipeline.toml whose steps use a
networked stand-in of the fixture backend (tests/refresh_helpers.py), so every gate applies and nothing
opens a socket."""

import json
import re
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIXTURES, ROOT
from test_approvals import KeyFileSigner, ledger, make_key, networked, operator_key, runs  # noqa: F401

from scrape_harness import approvals, cli, pipeline, refresh
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureHttpClient

BASE = (ROOT / "pipeline.toml").read_text()
TESTS = ROOT / "tests"
FMT = approvals.FMT


def fix(driver, root=FIXTURES):
    return ["--driver", driver, "--fixtures", str(root), "--max-tasks-per-minute", "6000",
            "--approve-politeness-override"]  # fmt: skip


def two_subcategories(tmp_path):
    """The fixture set plus a synthetic second list subcategory, "gadgets", served by the same pages."""
    import shutil

    root = tmp_path / "fixtures-two"
    if not root.exists():
        shutil.copytree(FIXTURES, root)
        urls = json.loads((root / "urls.json").read_text())
        for k in [k for k in urls if "/listings/widgets/sample-region?page=" in k]:
            urls[k.replace("/widgets/", "/gadgets/")] = urls[k]
        (root / "urls.json").write_text(json.dumps(urls))
    return root


def toml_list(xs):
    return "[" + ", ".join(json.dumps(x) for x in xs) + "]"


FULL = ("crawl-lists", "fetch-ads", "reparse", "geocode", "export-db")


def make_pipeline(tmp_path, monkeypatch, fp, driver="netfixture", steps=FULL):
    lines = {
        "crawl-lists": f'{{ name = "crawl-lists", args = {toml_list(["--subcategory", "widgets", "--stride", "2", "--start", "1", *fix(driver)])} }}',
        "crawl-gadgets": f'{{ name = "crawl-lists", args = {toml_list(["--subcategory", "gadgets", "--stride", "2", "--start", "1", "--max-pages", "5", *fix(driver, two_subcategories(tmp_path))])} }}',
        "fetch-ads": f'{{ name = "fetch-ads", args = {toml_list(fix(driver))} }}',
        "reparse": '{ name = "reparse" }',
        "geocode": '{ name = "geocode", paid_gate = { count_field = "pending" } }',
        "export-db": '{ name = "export-db", paid_exempt = "writes our own rows to our own database; no metered API" }',
    }
    text = BASE.replace('python_path = ["examples"]', f'python_path = ["{ROOT / "examples"}", "{TESTS}"]')
    text = text.replace('schema_dirs = ["examples/', f'schema_dirs = ["{ROOT}/examples/')
    text = re.sub(r'approval_key_fingerprint = "[^"]+"', f'approval_key_fingerprint = "{fp}"', text)
    text = text.replace(
        "[plugins.sources]",
        'commands = ["refresh_helpers:register"]\n\n[plugins.drivers]\nnetfixture = "refresh_helpers:NetFixture"\n\n[plugins.sources]',
    )
    start, end = text.index("[crawl.refresh]"), text.index("# ── Typed data files")
    block = "[crawl.refresh]\nsteps = [\n" + "".join(f"  {lines[s]},\n" for s in steps) + "]\n\n"
    text = text[:start] + block + text[end:]
    text += (
        '\n[[utilities]]\nname = "geocode"\nnetwork = "paid"\ninputs = []\noutputs = []\n'
        '\n[[utilities]]\nname = "export-db"\nnetwork = "paid"\ninputs = []\noutputs = []\n'
    )
    p = tmp_path / "pipeline.toml"
    p.write_text(text)
    monkeypatch.setenv("SCRAPE_HARNESS_PIPELINE", str(p))
    return p


@pytest.fixture
def chain(tmp_path, monkeypatch, data_dir, operator_key):  # noqa: F811
    """The full five-step chain on the networked stand-in, egress CA, ready for subprocess steps."""
    make_pipeline(tmp_path, monkeypatch, operator_key["fp"])
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-ca.txt").as_uri())
    monkeypatch.delenv("TEST_GEOCODE_PENDING", raising=False)
    monkeypatch.delenv("TEST_EXPORT_FAIL", raising=False)
    ran = []
    real = refresh.EXEC

    def recorded(argv):
        ran.append(list(argv))
        return real(argv)

    monkeypatch.setattr(refresh, "EXEC", recorded)
    return ran


def plan(capsys, *opts):
    capsys.readouterr()
    assert main(["refresh", *opts]) == 10
    return json.loads(capsys.readouterr().out)["plan_hash"]


def approved(capsys, *opts):
    h = plan(capsys, *opts)
    assert main(["approve", h]) == 0
    capsys.readouterr()
    return h


def state():
    (st,) = refresh.states()
    return st


def lines(data_dir):
    p = ledger(data_dir)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def names(ran):
    return [a[0] + (" --dry-run" if "--dry-run" in a else "") for a in ran]


# ── the bounds: each one bites (unit tests of consume_bounded) ───────────────────────────────────


def bounds(**kw):
    now = datetime.now(UTC)
    b = {
        "command": "refresh",
        "kind": "bounded",
        "site": "example.invalid",
        "egress_country": "CA",
        "variants": ["desktop"],
        "backends": ["browser"],
        "max_rate_per_minute": 7,
        "concurrency": 1,
        "politeness_override": False,
        "lists": {
            "subcategory": "widgets",
            "region": "sample-region",
            "page_count": None,
            "stride": 2,
            "start": 1,
            "seed": None,
            "max_pages": 5,
        },  # fmt: skip
        "max_list_pages": 6,
        "max_ad_requests": 40,
        "fetch_reasons": ["card_changed", "new", "stale_7d"],
        "steps": ["crawl-lists", "fetch-ads", "reparse"],
        "not_before": now.strftime(FMT),
        "expires_at": (now + timedelta(hours=12)).strftime(FMT),
    }
    return b | kw


def sign(b, steps=None):
    rec = approvals.approve_bounded(approvals.save_plan(b))
    return rec, refresh._new_state(rec, steps or b["steps"])


def crawl_doc(**kw):
    d = {"command": "crawl-lists", "site": "example.invalid", "variants": ["desktop"], "backends": ["browser"],
         "concurrency": 1, "max_tasks_per_minute": 7, "politeness_override": False, "egress_country": "CA",
         "subcategory": "widgets", "region": "sample-region", "page_count": None, "stride": 2, "start": 1,
         "seed": None, "max_pages": 5, "list_pages": 6, "ad_requests": 0, "fetch_reasons": []}  # fmt: skip
    return d | kw


def fetch_doc(n=30, **kw):
    d = {"command": "fetch-ads", "site": "example.invalid", "variants": ["desktop"], "backends": ["browser"],
         "concurrency": 1, "max_tasks_per_minute": 7, "politeness_override": False, "egress_country": "CA",
         "list_pages": 0, "ad_requests": n, "fetch_reasons": ["new"]}  # fmt: skip
    return d | kw


@pytest.mark.parametrize(
    "doc, msg",
    [
        (fetch_doc(41), "0 \\+ 41 ad requests > 40"),
        (crawl_doc(list_pages=7), "0 \\+ 7 list pages > 6"),
        (fetch_doc(site="other.invalid"), "site other.invalid is not example.invalid"),
        (fetch_doc(egress_country="US"), "egress US is not CA"),
        (
            fetch_doc(proxy="http://p.example.invalid:8080"),
            "proxy http://p.example.invalid:8080 is not the signed none",
        ),
        (fetch_doc(variants=["mobile"]), "variants \\['mobile'\\] are not all signed"),
        (fetch_doc(backends=["impit"]), "backends \\['impit'\\] are not all signed"),
        (fetch_doc(max_tasks_per_minute=8), "rate 8/min > 7"),
        (fetch_doc(concurrency=2), "concurrency 2 > 1"),
        (fetch_doc(politeness_override=True), "politeness override that the bounds do not sign"),
        (crawl_doc(start=2), "crawl-lists arguments .* are not one of the signed"),
        (crawl_doc(max_pages=60), "crawl-lists arguments .* are not one of the signed"),
        (crawl_doc(subcategory="gadgets"), "crawl-lists arguments .* are not one of the signed"),
        (fetch_doc(fetch_reasons=["new", "recruit"]), "fetch reasons recruit not signed"),
    ],
    ids=[
        "ad-requests",
        "list-pages",
        "site",
        "egress",
        "proxy",
        "variant",
        "backend",
        "rate",
        "concurrency",
        "politeness",
        "lists-start",
        "lists-max-pages",
        "lists-subcategory",
        "fetch-reason",
    ],  # fmt: skip
)
def test_each_bound_bites_and_spends_nothing(data_dir, doc, msg):
    rec, st = sign(bounds())
    before = lines(data_dir)
    with pytest.raises(approvals.BoundExceeded, match=msg):
        approvals.consume_bounded(doc, st["run_id"], doc["command"])
    assert lines(data_dir) == before  # nothing appended, nothing spent
    assert approvals.consume_bounded(fetch_doc(), st["run_id"], "fetch-ads")["bounded"]  # still usable


def test_a_step_that_is_not_signed_is_refused(data_dir):
    _, st = sign(bounds(steps=["fetch-ads"]))
    with pytest.raises(approvals.BoundExceeded, match="step crawl-lists is not one of the signed steps"):
        approvals.consume_bounded(crawl_doc(), st["run_id"], "crawl-lists")


def test_counts_are_cumulative_over_the_run(data_dir):
    _, st = sign(bounds())
    approvals.consume_bounded(fetch_doc(30), st["run_id"], "fetch-ads")
    with pytest.raises(approvals.BoundExceeded, match="30 \\+ 11 ad requests > 40"):
        approvals.consume_bounded(fetch_doc(11), st["run_id"], "fetch-ads")
    ref = approvals.consume_bounded(fetch_doc(10), st["run_id"], "fetch-ads")  # exactly the max
    assert ref == ref | {"refresh_run": st["run_id"], "step": "fetch-ads", "ad_requests": 10}
    assert approvals.bounded_usage(rec_id(st), st["run_id"]) == {"list_pages": 0, "ad_requests": 40}


def rec_id(st):
    return st["approval_id"]


def test_expiry_refuses(data_dir):
    _, st = sign(bounds())
    later = datetime.now(UTC) + timedelta(hours=13)
    with pytest.raises(approvals.NotApproved, match="is expired"):
        approvals.consume_bounded(fetch_doc(), st["run_id"], "fetch-ads", now=later)
    with pytest.raises(approvals.NotApproved, match="at most 24 h"):
        approvals.approve_bounded(approvals.save_plan(bounds(
            expires_at=(datetime.now(UTC) + timedelta(hours=25)).strftime(FMT))))  # fmt: skip


def test_reuse_across_refresh_run_ids_is_refused_and_the_same_run_may_resume(data_dir):
    rec, a = sign(bounds())
    approvals.consume_bounded(crawl_doc(), a["run_id"], "crawl-lists")
    b = refresh._new_state(rec, rec["bounds"]["steps"])  # a second run naming the same approval
    with pytest.raises(approvals.NotApproved, match=f"already used by refresh run {a['run_id']}"):
        approvals.consume_bounded(fetch_doc(), b["run_id"], "fetch-ads")
    assert approvals.consume_bounded(fetch_doc(), a["run_id"], "fetch-ads")  # a's resume: fine


def test_a_spent_or_revoked_approval_refuses(data_dir):
    rec, st = sign(bounds())
    approvals.mark_spent(rec["approval_id"], st["run_id"], "complete")
    assert approvals.mark_spent(rec["approval_id"], st["run_id"], "again") is None  # one terminal line
    with pytest.raises(approvals.NotApproved, match="spent \\(complete\\)"):
        approvals.consume_bounded(fetch_doc(), st["run_id"], "fetch-ads")
    rec2, st2 = sign(bounds(max_ad_requests=39))
    approvals.revoke(rec2["approval_id"], "operator changed their mind")
    with pytest.raises(approvals.NotApproved, match="is revoked"):
        approvals.consume_bounded(fetch_doc(), st2["run_id"], "fetch-ads")


def test_an_unsigned_or_forged_run_id_refuses(data_dir):
    with pytest.raises(approvals.NotApproved, match="not a refresh run id"):
        approvals.consume_bounded(fetch_doc(), "../../etc/passwd", "fetch-ads")
    with pytest.raises(approvals.NotApproved, match="no refresh run"):
        approvals.consume_bounded(fetch_doc(), "refresh-20260101T000000Z-abcdef", "fetch-ads")


def test_a_bounded_approval_is_signed_by_the_pinned_key_only(data_dir, operator_key):  # noqa: F811
    attacker, _, _ = make_key(operator_key["dir"], "attacker")
    KeyFileSigner.key = attacker
    with pytest.raises(approvals.NotApproved, match="does not verify"):
        approvals.approve_bounded(approvals.save_plan(bounds()))
    assert not [r for r in lines(data_dir) if r["kind"] == "bounded"]


def test_verify_and_the_chain_cover_bounded_used_and_spent_lines(data_dir, capsys):
    rec, st = sign(bounds())
    approvals.consume_bounded(crawl_doc(), st["run_id"], "crawl-lists")
    approvals.mark_spent(rec["approval_id"], st["run_id"], "complete")
    capsys.readouterr()
    assert main(["approvals", "verify"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep == rep | {"lines": 3, "approved": 1, "bounded": 1, "used": 1, "spent": 1, "bad_signatures": []}
    p = ledger(data_dir)
    raw = p.read_text().splitlines()
    tampered = json.loads(raw[0])
    tampered["bounds"]["max_ad_requests"] = 4000  # widen the signed bounds
    p.write_text("\n".join([json.dumps(tampered, sort_keys=True), *raw[1:]]) + "\n")
    assert main(["approvals", "verify"]) == 10  # the chain breaks at line 2
    assert "chain is broken" in capsys.readouterr().err
    # the same edit with the chain re-computed: the SIGNATURE no longer verifies
    import hashlib

    out, prev = [], approvals.GENESIS
    for x in [json.dumps(tampered, sort_keys=True), *raw[1:]]:
        r = json.loads(x)
        r["prev"] = prev
        line = json.dumps(r, sort_keys=True)
        out.append(line)
        prev = hashlib.sha256(line.encode()).hexdigest()
    p.write_text("\n".join(out) + "\n")
    assert main(["approvals", "verify"]) == 10
    assert json.loads(capsys.readouterr().out)["bad_signatures"] == [rec["approval_id"]]


def test_the_approval_screen_says_the_bounds_in_plain_words(data_dir, capsys):
    capsys.readouterr()
    h = approvals.save_plan(bounds())
    assert main(["approve", h]) == 0
    out = capsys.readouterr().out
    assert "BOUNDED approval for ONE refresh of example.invalid (single use)" in out
    assert "at most 6 list pages + 40 ad pages in all" in out and "egress CA, proxy none" in out
    with pytest.raises(SystemExit, match="carries its own TTL"):
        main(["approve", h, "--ttl", "1h"])


# ── the step gate: --refresh-run (in-process, the fixture backend treated as networked) ──────────


def test_the_step_gate_checks_the_network_flag_before_it_spends(
    networked,  # noqa: F811
    tmp_path,
    monkeypatch,
    capsys,
    operator_key,  # noqa: F811
):
    make_pipeline(tmp_path, monkeypatch, operator_key["fp"], driver="fixture", steps=("crawl-lists",))
    approved(capsys)
    rec = next(r for r in lines(networked) if r["kind"] == "bounded")
    st = refresh._new_state(rec, ["crawl-lists"])
    argv = ["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *fix("fixture"),
            "--refresh-run", st["run_id"]]  # fmt: skip
    with pytest.raises(SystemExit) as e:  # no --approve-network: refused BEFORE the approval is used
        main(argv)
    assert e.value.code == 3
    assert [r["kind"] for r in lines(networked)] == ["bounded"] and FixtureHttpClient.log == []
    assert main([*argv, "--approve-network"]) == 0
    (run,) = runs(networked)
    ref = run["approval_ref"]
    assert ref == ref | {
        "bounded": True,
        "refresh_run": st["run_id"],
        "step": "crawl-lists",
        "list_pages": 61,
    }
    other = [*argv, "--approve-network"]
    other[other.index("--start") + 1] = "2"  # a different sample than the signed one: a breach
    assert main(other) == 3
    assert "outside its bounded approval" in capsys.readouterr().err


def test_without_refresh_run_the_gate_is_unchanged(networked, tmp_path, monkeypatch, capsys, operator_key):  # noqa: F811
    make_pipeline(tmp_path, monkeypatch, operator_key["fp"], driver="fixture", steps=("crawl-lists",))
    approved(capsys)  # a bounded approval exists, but a plain run still needs its own exact plan
    argv = ["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *fix("fixture"),
            "--approve-network"]  # fmt: skip
    assert main(argv) == 10 and FixtureHttpClient.log == []


# ── the chain (doit, real step processes) ────────────────────────────────────────────────────────


def test_no_approval_prints_the_plan_and_sends_nothing(chain, data_dir, capsys):
    capsys.readouterr()
    assert main(["refresh", "--approve-network"]) == 10
    out, err = capsys.readouterr()
    doc = json.loads(out)
    assert doc["plan"]["kind"] == "bounded" and doc["plan"]["steps"] == list(FULL)
    assert doc["plan_hash"] == approvals.canonical(doc["plan"])[1]
    assert "approve " + doc["plan_hash"] in err and "RUN CARD" in err
    assert chain == [] and refresh.states() == [] and runs(data_dir) == []
    assert main(["refresh", "--dry-run"]) == 0 and chain == []  # dry run: nothing saved, nothing sent


def test_approved_without_the_network_flag_is_refused(chain, data_dir, capsys):
    approved(capsys)
    assert main(["refresh"]) == 3 and chain == [] and refresh.states() == []


def test_the_chain_runs_in_declared_order_and_spends_the_approval(chain, data_dir, capsys):
    approved(capsys)
    time.sleep(1.1)  # a later clock: the run must still find the approval (its bounds hash their own times)
    assert main(["refresh", "--approve-network"]) == 0
    assert names(chain) == [
        "crawl-lists",
        "fetch-ads",
        "reparse",
        "geocode --dry-run",
        "geocode",
        "export-db",
    ]
    crawl_argv = chain[0]
    assert crawl_argv[-3:] == ["--approve-network", "--refresh-run", state()["run_id"]]
    assert "--refresh-run" not in chain[2] and "--approve-network" not in chain[2]  # reparse is offline
    assert chain[4][-2:] == ["--max-requests", "0"]  # N == 0 passes without --geocode-count
    st = state()
    assert st["status"] == "complete" and [s["status"] for s in st["steps"]] == ["done"] * 5
    assert st["headroom"] == {"list_pages": {"used": 61, "max": 61}, "ad_requests": {"used": 3, "max": 40}}
    kinds = [r["kind"] for r in lines(data_dir)]
    assert kinds == ["bounded", "used", "used", "spent"]
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["digests"] == {"export-db": "sha256:" + "d" * 64} and summary["paid_requests"] == 0
    assert summary["headroom"]["ad_requests"] == {"used": 3, "max": 40}
    assert [x["exit"] for x in summary["steps"]] == [0] * 5
    # single use: the same bounds now need a NEW signature
    assert main(["refresh", "--approve-network"]) == 10


def test_a_paid_count_must_be_confirmed_and_a_resume_skips_done_steps(chain, data_dir, capsys, monkeypatch):
    approved(capsys)
    monkeypatch.setenv("TEST_GEOCODE_PENDING", "5")
    assert main(["refresh", "--approve-network"]) == 3
    st = state()
    assert st["status"] == "failed" and [s["status"] for s in st["steps"]] == [
        "done",
        "done",
        "done",
        "failed",
        "pending",
    ]
    geo = st["steps"][3]
    assert geo["paid_count"] == 5 and "--geocode-count 5" in geo["reason"]
    assert not (data_dir / "geocode.log").exists()  # the paid step never ran for real
    chain.clear()
    assert main(["refresh", "--approve-network", "--geocode-count", "4"]) == 3  # a different N: still stopped
    assert names(chain) == ["geocode --dry-run"]
    chain.clear()
    assert main(["refresh", "--approve-network", "--geocode-count", "5"]) == 0
    assert names(chain) == ["geocode --dry-run", "geocode", "export-db"]  # crawl/fetch/reparse not re-run
    assert chain[1][-2:] == ["--max-requests", "5"]
    assert json.loads((data_dir / "geocode.log").read_text()) == {"max_requests": 5, "approve_network": True}
    assert state()["status"] == "complete" and len(refresh.states()) == 1


def test_a_failed_step_resumes_in_the_same_run(chain, data_dir, capsys, monkeypatch):
    approved(capsys)
    monkeypatch.setenv("TEST_EXPORT_FAIL", "1")
    assert main(["refresh", "--approve-network"]) == 1
    st = state()
    assert st["status"] == "failed" and st["steps"][-1] == st["steps"][-1] | {"status": "failed", "exit": 1}
    chain.clear()
    assert main(["refresh", "--approve-network"]) == 0
    assert names(chain) == ["export-db"] and state()["status"] == "complete"  # only the failed step re-ran


def test_a_bound_breach_stops_the_chain_and_ends_the_refresh(chain, data_dir, capsys):
    approved(capsys, "--max-ad-requests", "2")  # fetch-ads would GET 3 ads
    assert main(["refresh", "--approve-network", "--max-ad-requests", "2"]) == 3
    assert names(chain) == ["crawl-lists", "fetch-ads"]
    st = state()
    assert st["status"] == "spent" and "bound of the approval was exceeded" in st["spent"]["why"]
    assert [r["kind"] for r in lines(data_dir)] == ["bounded", "used", "spent"]  # the breach used nothing
    assert len(runs(data_dir)) == 1  # the breaching step never started a run
    chain.clear()
    assert main(["refresh", "--approve-network", "--max-ad-requests", "2"]) == 10 and chain == []


def test_a_challenge_stop_ends_the_refresh(chain, data_dir, capsys, monkeypatch):
    approved(capsys)

    def challenged(argv):
        chain.append(list(argv))
        return (5, []) if argv[0] == "fetch-ads" else refresh._exec(argv)

    monkeypatch.setattr(refresh, "EXEC", challenged)
    assert main(["refresh", "--approve-network"]) == 5
    assert state()["status"] == "spent" and "challenge stop" in state()["spent"]["why"]
    assert lines(data_dir)[-1]["kind"] == "spent"


def test_abandon_spends_the_unfinished_run(chain, data_dir, capsys, monkeypatch):
    approved(capsys)
    monkeypatch.setenv("TEST_GEOCODE_PENDING", "5")
    assert main(["refresh", "--approve-network"]) == 3
    assert main(["refresh", "--abandon"]) == 0
    assert (
        state()["status"] == "spent"
        and lines(data_dir)[-1]["note"] == "abandoned by the operator (--abandon)"
    )


def test_an_egress_refusal_is_resumable_and_not_double_counted(
    chain,
    tmp_path,
    monkeypatch,
    capsys,
    data_dir,
    operator_key,  # noqa: F811
):
    """max_list_pages equals the crawl-lists step's own count: only a refund makes the resume fit."""
    make_pipeline(tmp_path, monkeypatch, operator_key["fp"], steps=("crawl-lists",))
    approved(capsys)
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-at.txt").as_uri())
    assert main(["refresh", "--approve-network"]) == 8
    st = state()
    assert st["status"] == "failed" and st["steps"][0]["exit"] == 8
    assert [r["kind"] for r in lines(data_dir)] == ["bounded", "used", "refunded"]
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-ca.txt").as_uri())
    assert main(["refresh", "--approve-network"]) == 0
    assert state()["status"] == "complete" and state()["run_id"] == st["run_id"]
    assert state()["headroom"]["list_pages"] == {"used": 61, "max": 61}


# ── the declaration ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "edit, msg",
    [
        (
            lambda t: t.replace(', paid_gate = { count_field = "pending" }', ""),
            "must declare paid_gate or paid_exempt",
        ),
        (lambda t: t.replace('{ name = "reparse" }', '{ name = "ab" }'), "a site step must be one of"),
        (lambda t: t.replace('{ name = "reparse" }', '{ name = "nope" }'), "not a declared stage or utility"),
        (
            lambda t: t.replace('{ name = "reparse" }', '{ name = "reparse", args = ["--plan"] }'),
            "the runner's to add",
        ),
        (lambda t: t.replace('{ name = "reparse" }', '{ name = "fetch-ads" }'), "declared twice"),
        (
            lambda t: t.replace('{ name = "reparse" }', '{ name = "reparse", paid_exempt = "x" }'),
            "for paid steps only",
        ),
    ],
    ids=["paid-ungated", "other-site-step", "unknown", "runner-flag", "twice", "exempt-offline"],
)
def test_the_refresh_declaration_is_checked(tmp_path, monkeypatch, edit, msg, operator_key):  # noqa: F811
    p = make_pipeline(tmp_path, monkeypatch, operator_key["fp"])
    p.write_text(edit(p.read_text()))
    with pytest.raises(pipeline.PipelineError, match=msg):
        pipeline.load(p)


def test_every_paid_step_of_the_example_declares_its_gate():
    for pl in (pipeline.load(ROOT / "pipeline.toml"),):
        for s in pl.refresh:
            if pl.command(s.name).network == "paid":
                assert s.count_field or s.paid_exempt
        assert [s.name for s in pl.refresh] == ["crawl-lists", "fetch-ads", "reparse"]


def test_refresh_and_run_are_in_help(capsys):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["--help"])
    out = capsys.readouterr().out
    assert re.search(r"\brefresh\b", out) and re.search(r"\brun\b", out)
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["crawl-lists", "--help"])
    assert "--refresh-run" in capsys.readouterr().out


# ── crawl-lists more than once: one signed list set per step, each crawled once ──────────────────

GADGETS = {"subcategory": "gadgets", "region": "sample-region", "page_count": None, "stride": 2, "start": 1,
           "seed": None, "max_pages": 5}  # fmt: skip


def two_sets(**kw):
    return bounds(lists=[bounds()["lists"], GADGETS], max_list_pages=12,
                  steps=["crawl-lists", "crawl-lists", "fetch-ads"], **kw)  # fmt: skip


def test_each_signed_set_is_crawled_once_and_an_unsigned_set_is_refused(data_dir):
    rec, st = sign(two_sets())
    rid = st["run_id"]
    with pytest.raises(approvals.BoundExceeded, match="are not one of the signed"):
        approvals.consume_bounded(crawl_doc(subcategory="other"), rid, "crawl-lists")
    first = approvals.consume_bounded(crawl_doc(), rid, "crawl-lists")
    assert first["list_set"] == 0
    with pytest.raises(approvals.BoundExceeded, match="set 1 was already crawled in refresh run"):
        approvals.consume_bounded(crawl_doc(), rid, "crawl-lists")  # a replay of the same set
    second = approvals.consume_bounded(crawl_doc(**GADGETS), rid, "crawl-lists")
    assert second["list_set"] == 1
    with pytest.raises(approvals.BoundExceeded, match="set 2 was already crawled"):
        approvals.consume_bounded(crawl_doc(**GADGETS), rid, "crawl-lists")
    used = [r for r in lines(data_dir) if r["kind"] == "used"]
    assert [r["list_set"] for r in used] == [0, 1]


def test_a_refunded_set_may_be_crawled_again_in_the_same_run(data_dir):
    _, st = sign(two_sets())
    ref = approvals.consume_bounded(crawl_doc(**GADGETS), st["run_id"], "crawl-lists")
    approvals.refund_bounded(ref, "refused before any site request (exit 8)")
    assert approvals.consume_bounded(crawl_doc(**GADGETS), st["run_id"], "crawl-lists")["list_set"] == 1


def test_a_bounds_doc_signed_with_one_list_object_still_matches_and_consumes(data_dir):
    """Approvals signed before crawl-lists could repeat hold `lists` as one object: same meaning."""
    rec, st = sign(bounds())  # the object form
    built = bounds() | {"lists": [bounds()["lists"]]}  # what build_bounds now produces
    assert approvals.bounds_core(rec["bounds"]) == approvals.bounds_core(built)
    assert approvals.consume_bounded(crawl_doc(), st["run_id"], "crawl-lists")["list_set"] == 0
    with pytest.raises(approvals.BoundExceeded, match="already crawled"):
        approvals.consume_bounded(crawl_doc(), st["run_id"], "crawl-lists")


def test_the_screen_names_each_list_set_and_the_robots_checks(data_dir, capsys):
    capsys.readouterr()
    assert main(["approve", approvals.save_plan(two_sets())]) == 0
    out = capsys.readouterr().out
    assert (
        "crawl-lists (1) widgets in sample-region (every 2th page, start 1, seed None; page 1, then at most 5 more pages)"
        in out
    )
    assert (
        "(2) gadgets in sample-region (every 2th page, start 1, seed None; page 1, then at most 5 more pages), each set crawled once"
        in out
    )
    assert "at most 12 list pages + 40 ad pages in all, plus one robots.txt check per site step (3)" in out
    assert approvals.bounded_text(bounds(lists={})).count("robots.txt check per site step (2)") == 1


def test_two_crawl_lists_steps_run_in_order_under_one_approval(
    chain,
    tmp_path,
    monkeypatch,
    capsys,
    data_dir,
    operator_key,  # noqa: F811
):
    make_pipeline(
        tmp_path, monkeypatch, operator_key["fp"], steps=("crawl-lists", "crawl-gadgets", "fetch-ads")
    )
    h = plan(capsys)
    doc = approvals.load_plan(h)
    assert [x["subcategory"] for x in doc["lists"]] == ["widgets", "gadgets"]
    assert doc["max_list_pages"] == 61 + 6 and doc["steps"] == ["crawl-lists", "crawl-lists", "fetch-ads"]
    assert main(["approve", h]) == 0
    assert main(["refresh", "--approve-network"]) == 0
    assert [(a[0], a[a.index("--subcategory") + 1] if "--subcategory" in a else None) for a in chain] == [
        ("crawl-lists", "widgets"), ("crawl-lists", "gadgets"), ("fetch-ads", None)]  # fmt: skip
    st = state()
    assert [s["id"] for s in st["steps"]] == ["1:crawl-lists", "2:crawl-lists", "3:fetch-ads"]
    assert st["status"] == "complete" and st["headroom"]["list_pages"] == {"used": 67, "max": 67}
    used = [r for r in lines(data_dir) if r["kind"] == "used"]
    assert [(r["step"], r.get("list_set")) for r in used] == [
        ("crawl-lists", 0),
        ("crawl-lists", 1),
        ("fetch-ads", None),
    ]
    assert {r["approval_ref"]["list_set"] for r in runs(data_dir) if r["command"] == "crawl-lists"} == {0, 1}


def test_a_failure_in_the_second_crawl_lists_resumes_there(
    chain,
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
    failed_once = []

    def flaky(argv):
        chain.append(list(argv))
        if "gadgets" in argv and not failed_once:
            failed_once.append(1)
            return 1, []  # step 2 dies before its gate: nothing consumed
        return refresh._exec(argv)

    monkeypatch.setattr(refresh, "EXEC", flaky)
    assert main(["refresh", "--approve-network"]) == 1
    st = state()
    assert [s["status"] for s in st["steps"]] == ["done", "failed", "pending"] and st["status"] == "failed"
    chain.clear()
    assert main(["refresh", "--approve-network"]) == 0
    assert [a[a.index("--subcategory") + 1] if "--subcategory" in a else a[0] for a in chain] == [
        "gadgets",
        "fetch-ads",
    ]
    assert state()["status"] == "complete"


def test_only_crawl_lists_may_repeat(tmp_path, monkeypatch, operator_key):  # noqa: F811
    p = make_pipeline(
        tmp_path, monkeypatch, operator_key["fp"], steps=("crawl-lists", "crawl-gadgets", "fetch-ads")
    )
    assert [s.name for s in pipeline.load(p).refresh] == ["crawl-lists", "crawl-lists", "fetch-ads"]
    p.write_text(
        p.read_text().replace(
            '{ name = "fetch-ads", args', '{ name = "fetch-ads" },\n  { name = "fetch-ads", args'
        )
    )
    with pytest.raises(pipeline.PipelineError, match="only crawl-lists may repeat"):
        pipeline.load(p)


# ── one refresh per run id: an exclusive lock ────────────────────────────────────────────────────


def test_a_second_refresh_of_a_locked_run_id_refuses_and_changes_nothing(
    chain, data_dir, capsys, monkeypatch
):
    import fcntl
    import os

    approved(capsys)
    monkeypatch.setenv("TEST_GEOCODE_PENDING", "5")
    assert main(["refresh", "--approve-network"]) == 3  # a resumable stop at geocode
    st = state()
    held = os.open(refresh.state_path(st["run_id"]).with_suffix(".lock"), os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # "another refresh process" holds it
    before_state, before_ledger = refresh.state_path(st["run_id"]).read_text(), ledger(data_dir).read_text()
    chain.clear()
    capsys.readouterr()
    try:
        assert main(["refresh", "--approve-network", "--geocode-count", "5"]) == 10
        assert "is already running" in capsys.readouterr().err
        assert main(["refresh", "--abandon"]) == 10
    finally:
        os.close(held)
    assert chain == [] and refresh.state_path(st["run_id"]).read_text() == before_state
    assert ledger(data_dir).read_text() == before_ledger  # nothing spent, nothing used
    assert main(["refresh", "--approve-network", "--geocode-count", "5"]) == 0  # released: it resumes


def test_the_lock_keeps_the_run_id_format_check():
    with pytest.raises(approvals.NotApproved, match="not a refresh run id"), refresh.locked("../../x"):
        pass
