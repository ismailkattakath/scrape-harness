"""`<cli> preflight <command ...>`: the offline checks a networked run needs, BEFORE the operator's Touch ID.

It exists because a bounded refresh stopped AFTER the approval, on a paid step whose secret env
var was unset. pipeline.toml now declares, per stage or utility, the env var NAMES a step needs (`secrets_env`);
preflight reports each one PRESENT or MISSING (never a value, never a length), plus the approval signers file,
the site's cooldown and breaker latch, and (only behind --egress) the egress country. `refresh` runs the
secret part itself before it plans, claims or spends anything.

Every secret in these tests is a synthetic marker; a test asserts the marker never reaches stdout or stderr.
"""

from __future__ import annotations

import json

import pytest
from conftest import FIXTURES
from test_approvals import operator_key  # noqa: F401
from test_refresh import make_pipeline

from scrape_harness import approvals, contracts, crawl, egress, pipeline, refresh
from scrape_harness.cli import main

MARKER = "synthetic-marker-value-7f3a"  # what a secret's value would be; it must never be printed
GEO, DB = "TEST_GEO_KEY", "TEST_DB_DSN"


def with_secrets(p):
    """The test pipeline's paid utilities declare the env var names they need."""
    text = p.read_text()
    text = text.replace(
        'name = "geocode"\nnetwork = "paid"\n',
        f'name = "geocode"\nnetwork = "paid"\nsecrets_env = ["{GEO}"]\n',
    )
    text = text.replace(
        'name = "export-db"\nnetwork = "paid"\n',
        f'name = "export-db"\nnetwork = "paid"\nsecrets_env = ["{DB}", "{GEO}"]\n',
    )
    p.write_text(text)
    return p


@pytest.fixture
def chain(tmp_path, monkeypatch, data_dir, operator_key):  # noqa: F811
    with_secrets(make_pipeline(tmp_path, monkeypatch, operator_key["fp"]))
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-ca.txt").as_uri())
    monkeypatch.setenv(GEO, MARKER)
    monkeypatch.setenv(DB, MARKER)
    ran = []
    real = refresh.EXEC

    def recorded(argv):
        ran.append(list(argv))
        return real(argv)

    monkeypatch.setattr(refresh, "EXEC", recorded)
    return ran


@pytest.fixture
def egress_spy(monkeypatch):
    calls = []
    real = egress.preflight

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(egress, "preflight", spy)
    return calls


def run(capsys, *argv):
    capsys.readouterr()
    code = main(["preflight", *argv])
    out, err = capsys.readouterr()
    assert MARKER not in out and MARKER not in err  # presence only, on every path
    return code, out, err


def doc(capsys, *argv):
    code, out, _ = run(capsys, "--json", *argv)
    d = json.loads(out)
    contracts.validate("preflight.v1", d)
    return code, d


def row(d, kind, name=None):
    (r,) = [c for c in d["checks"] if c["kind"] == kind and (name is None or c["name"] == name)]
    return r


# ── the declaration ───────────────────────────────────────────────────────────────────────────


def test_a_command_declares_its_secret_env_names(chain):
    pl = pipeline.current()
    assert pl.command("geocode").secrets_env == (GEO,)
    assert pl.command("export-db").secrets_env == (DB, GEO)
    assert pl.command("crawl-lists").secrets_env == ()  # absent = needs none


def test_a_secret_env_name_must_be_an_env_name(tmp_path):
    bad = pipeline._read(pipeline.locate()) | {}
    bad["utilities"] = [*bad["utilities"], {"name": "x", "network": "paid", "inputs": [], "outputs": [],
                                            "secrets_env": ["lower-case"]}]  # fmt: skip
    with pytest.raises(pipeline.PipelineError, match="not a valid pipeline.v1"):
        pipeline.parse(bad, tmp_path / "pipeline.toml")


# ── preflight refresh ─────────────────────────────────────────────────────────────────────────


def test_all_present_is_exit_0_and_names_every_secret_once(chain, capsys, egress_spy):
    code, d = doc(capsys, "refresh")
    assert code == 0 and d["ok"] is True and d["missing"] == []
    assert d["command"] == "refresh"
    assert row(d, "secret_env", GEO) == {"kind": "secret_env", "name": GEO, "status": "present",
                                         "needed_by": ["export-db", "geocode"]}  # fmt: skip
    assert row(d, "secret_env", DB)["needed_by"] == ["export-db"]
    assert row(d, "signers")["status"] == "ok"
    assert row(d, "cooldown", "example.invalid")["status"] == "ok"
    assert row(d, "breaker", "example.invalid")["status"] == "ok"
    assert row(d, "egress")["status"] == "skipped"  # behind --egress only
    assert egress_spy == []  # default: fully offline, the trace endpoint is never asked


def test_a_missing_secret_is_exit_2_and_named(chain, capsys, monkeypatch):
    monkeypatch.delenv(DB)
    code, d = doc(capsys, "refresh")
    assert code == 2 and d["ok"] is False and d["missing"] == [DB]
    assert row(d, "secret_env", DB)["status"] == "missing"
    assert row(d, "secret_env", GEO)["status"] == "present"
    code, out, _ = run(capsys, "refresh")  # the table names it too
    assert code == 2 and DB in out and "MISSING" in out


def test_an_empty_secret_is_missing(chain, capsys, monkeypatch):
    monkeypatch.setenv(GEO, "")
    code, d = doc(capsys, "refresh")
    assert code == 2 and d["missing"] == [GEO]


def test_its_own_flags_may_follow_the_command(chain, capsys, monkeypatch):
    monkeypatch.delenv(GEO)
    a = doc(capsys, "refresh")
    capsys.readouterr()
    code = main(["preflight", "refresh", "--json"])
    b = json.loads(capsys.readouterr().out)
    for x in (a[1], b):
        x.pop("checked_at")
    assert (code, b) == a


def test_the_table_is_short_and_shows_no_value(chain, capsys):
    code, out, _ = run(capsys, "refresh")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("preflight refresh: ok")
    assert any(GEO in x and "present" in x for x in lines)
    assert len(lines) < 12


def test_missing_signers_is_exit_2(chain, capsys, monkeypatch):
    monkeypatch.delenv("EX_APPROVAL_SIGNERS")
    code, d = doc(capsys, "refresh")
    assert code == 2 and d["missing"] == ["EX_APPROVAL_SIGNERS"]
    assert row(d, "signers")["status"] == "missing"


def test_an_unreadable_signers_file_is_exit_2(chain, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("EX_APPROVAL_SIGNERS", str(tmp_path / "nowhere"))
    code, d = doc(capsys, "refresh")
    assert code == 2 and row(d, "signers")["status"] == "missing"
    assert str(tmp_path) not in json.dumps(d)  # named by its env var, never by its path


def test_a_cooldown_or_a_latch_blocks(chain, capsys, monkeypatch):
    monkeypatch.setattr(crawl, "cooldown_until", lambda site, now=None: "2099-01-01T00:00:00Z")
    code, d = doc(capsys, "refresh")
    assert code == 2 and row(d, "cooldown", "example.invalid")["status"] == "blocked"
    assert "2099-01-01T00:00:00Z" in row(d, "cooldown", "example.invalid")["detail"]
    monkeypatch.setattr(crawl, "cooldown_until", lambda site, now=None: None)
    monkeypatch.setattr(crawl, "latch_stop", lambda site: crawl.Stop(14, f"refusing: {site} is latched"))
    code, d = doc(capsys, "refresh")
    assert code == 2 and row(d, "breaker", "example.invalid")["status"] == "blocked"


def test_no_data_root_is_named_not_a_traceback(chain, capsys, monkeypatch):
    monkeypatch.delenv("EXAMPLE_DATA")
    code, d = doc(capsys, "refresh")
    assert code == 2 and row(d, "data_root")["status"] == "missing" and "EXAMPLE_DATA" in d["missing"]


def test_egress_only_behind_its_flag(chain, capsys, monkeypatch, egress_spy):
    code, d = doc(capsys, "refresh", "--egress")
    assert code == 0 and len(egress_spy) == 1
    assert row(d, "egress")["status"] == "ok" and row(d, "egress")["detail"] == "CA (direct)"
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-at.txt").as_uri())
    code, d = doc(capsys, "--egress", "refresh")
    assert code == 2 and row(d, "egress")["status"] == "blocked" and len(egress_spy) == 2
    assert "ip=" not in json.dumps(d)


def test_egress_with_several_proxies_waits_for_the_plan_pick(chain, capsys, egress_spy):
    p = pipeline.locate()
    p.write_text(p.read_text().replace("[crawl.refresh]", '[crawl.proxy]\nurls = ["http://p1.example.invalid:8080", '
                 '"http://p2.example.invalid:8080"]\nuser_secret = "test-user"\npassword_secret = "test-pass"\n\n'
                 "[crawl.refresh]"))  # fmt: skip
    pipeline._cached.cache_clear()
    pipeline._doc.cache_clear()
    code, d = doc(capsys, "refresh", "--egress")
    assert code == 0 and row(d, "egress")["status"] == "skipped" and "2 proxies" in row(d, "egress")["detail"]
    assert egress_spy == []  # nothing asked: which egress to check is the plan's pick


# ── preflight <command> ───────────────────────────────────────────────────────────────────────


def test_one_command_checks_only_its_own_secrets(chain, capsys, monkeypatch):
    monkeypatch.delenv(DB)
    code, d = doc(capsys, "geocode")
    assert code == 0 and d["command"] == "geocode"
    assert [c["name"] for c in d["checks"] if c["kind"] == "secret_env"] == [GEO]
    assert not [c for c in d["checks"] if c["kind"] in ("cooldown", "breaker")]  # a paid step, not the site
    code, d = doc(capsys, "export-db")
    assert code == 2 and d["missing"] == [DB]


def test_a_site_command_checks_its_site(chain, capsys):
    code, d = doc(capsys, "crawl-lists", "--subcategory", "widgets")
    assert code == 0 and row(d, "cooldown", "example.invalid")["status"] == "ok"
    assert not [c for c in d["checks"] if c["kind"] == "secret_env"]


def test_a_two_word_command(chain, capsys):
    code, d = doc(capsys, "crawl", "resume")
    assert code == 0 and d["command"] == "crawl resume"


def test_an_unknown_command_is_refused(chain, capsys):
    with pytest.raises(SystemExit) as e:
        main(["preflight", "no-such-command"])
    assert e.value.code == 2


# ── refresh runs it first ─────────────────────────────────────────────────────────────────────


def test_refresh_refuses_before_it_plans(chain, data_dir, capsys, monkeypatch):
    monkeypatch.delenv(GEO)
    capsys.readouterr()
    assert main(["refresh"]) == 2
    out, err = capsys.readouterr()
    assert GEO in err and MARKER not in out + err
    assert not list(approvals._dir("plans").glob("*.json"))  # nothing to sign
    assert chain == []


def test_refresh_refuses_before_any_step_and_keeps_the_approval(chain, data_dir, capsys, monkeypatch):
    capsys.readouterr()
    assert main(["refresh"]) == 10
    h = json.loads(capsys.readouterr().out)["plan_hash"]
    assert list(approvals._dir("plans").glob("*.json"))  # the glob above sees a saved plan
    assert main(["approve", h]) == 0
    monkeypatch.delenv(DB)
    capsys.readouterr()
    assert main(["refresh", "--approve-network"]) == 2
    out, err = capsys.readouterr()
    assert DB in err and MARKER not in out + err
    assert chain == [] and refresh.states() == []  # no step ran, no run claimed the approval
    monkeypatch.setenv(DB, MARKER)
    assert main(["refresh", "--dry-run"]) == 0  # the same approval is still live
    assert json.loads(capsys.readouterr().out)["approval_id"]


def test_a_dry_run_warns_and_sends_nothing(chain, capsys, monkeypatch):
    monkeypatch.delenv(GEO)
    capsys.readouterr()
    assert main(["refresh", "--dry-run"]) == 0
    out, err = capsys.readouterr()
    assert GEO in err and MARKER not in out + err and chain == []


def test_abandon_needs_no_secret(chain, capsys, monkeypatch):
    monkeypatch.delenv(GEO)
    with pytest.raises(SystemExit, match="no unfinished refresh run"):
        main(["refresh", "--abandon"])
