"""Mechanical network approval: plan -> the operator's key signs it (Touch ID via Secretive) -> exactly
that run, once. Tests stand in a throwaway Ed25519 SSH key for the Secure Enclave key; one test drives
the REAL agent path (AgentSigner) through a throwaway ssh-agent, the same protocol Secretive speaks."""

import dataclasses
import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIXTURES

from scrape_harness import approvals, pipeline
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureBackend, FixtureHttpClient

RUN = [
    "crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", "--driver", "fixture",
    "--fixtures", str(FIXTURES), "--max-tasks-per-minute", "6000", "--approve-politeness-override",
    "--approve-network",
]  # fmt: skip
KEYGEN = shutil.which("ssh-keygen")


def make_key(d, name):
    key = d / name
    subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(key)], check=True)
    pub = " ".join(key.with_suffix(".pub").read_text().split()[:2])
    fp = subprocess.run(
        [KEYGEN, "-l", "-E", "sha256", "-f", str(key)], capture_output=True, text=True
    ).stdout.split()[1]
    return key, pub, fp


def signers(path, pub):
    path.write_text(f'{approvals.PRINCIPAL} namespaces="{approvals.NAMESPACE}" {pub}\n')
    return path


class KeyFileSigner:
    """Stands in for the Secure Enclave: signs with a private key FILE (tests only)."""

    key = None

    def sign(self, body, pubkey):
        msg = KeyFileSigner.key.parent / "msg"
        msg.write_bytes(body)
        subprocess.run(
            [KEYGEN, "-Y", "sign", "-f", str(KeyFileSigner.key), "-n", approvals.NAMESPACE, str(msg)],
            check=True,
            capture_output=True,
        )
        sig = msg.with_suffix(".sig").read_text()
        msg.with_suffix(".sig").unlink()
        return sig


@pytest.fixture(autouse=True)
def operator_key(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("keys")
    key, pub, fp = make_key(d, "operator")
    monkeypatch.setenv("EX_APPROVAL_SIGNERS", str(signers(d / "allowed_signers", pub)))
    real = pipeline.consumer()
    monkeypatch.setattr(pipeline, "consumer", lambda: dataclasses.replace(real, approval_key_fingerprint=fp))
    KeyFileSigner.key = key
    monkeypatch.setattr(approvals, "SIGNER", KeyFileSigner)
    return {"dir": d, "key": key, "pub": pub, "fp": fp}


@pytest.fixture
def networked(monkeypatch, data_dir):
    """The offline fixture backend, treated as networked so the gate applies (it still sends nothing)."""
    monkeypatch.setattr(FixtureBackend, "network", True)
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-ca.txt").as_uri())
    FixtureHttpClient.log.clear()
    return data_dir


def plan_hash(capsys, argv):
    capsys.readouterr()
    assert main([*argv, "--plan"]) == 0
    return json.loads(capsys.readouterr().out)["plan_hash"]


def ledger(d):
    return d.parent / "state" / "approvals" / "ledger.jsonl"


def runs(d):
    return (
        [json.loads(p.read_text()) for p in sorted((d / "runs").glob("*.json"))]
        if (d / "runs").exists()
        else []
    )


def test_no_approval_refuses_with_zero_requests(networked):
    assert main(RUN) == approvals.EXIT_UNAPPROVED == 10
    assert FixtureHttpClient.log == [] and runs(networked) == []


def test_plan_is_stable_and_plan_prefix_is_the_same(networked, capsys):
    a = plan_hash(capsys, RUN)
    capsys.readouterr()
    assert main(["plan", *RUN]) == 0
    assert json.loads(capsys.readouterr().out)["plan_hash"] == a == plan_hash(capsys, RUN)


def test_approved_plan_runs_once_and_the_ledger_is_a_chain(networked, capsys):
    h = plan_hash(capsys, RUN)
    assert main(["approve", h]) == 0  # no TTY, no typing: the signature (Touch ID) is the approval
    assert main(RUN) == 0
    (run,) = runs(networked)
    assert run["approval_ref"]["plan_hash"] == h
    assert main(RUN) == 10  # single use
    lines = ledger(networked).read_text().splitlines()
    recs = [json.loads(x) for x in lines]
    assert [r["kind"] for r in recs] == ["approved", "used"]
    import hashlib

    assert (
        recs[0]["prev"] == approvals.GENESIS
        and recs[1]["prev"] == hashlib.sha256(lines[0].encode()).hexdigest()
    )
    assert recs[0]["sig"].startswith("-----BEGIN SSH SIGNATURE-----") and "sig" not in recs[1]
    assert stat.S_IMODE(ledger(networked).stat().st_mode) == 0o600
    capsys.readouterr()
    assert main(["approvals", "verify"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == report | {"lines": 2, "approved": 1, "used": 1, "chain": "intact", "bad_signatures": []}


def test_a_different_plan_is_refused(networked, capsys):
    approvals.approve(plan_hash(capsys, RUN))
    other = [*RUN]
    other[other.index("--start") + 1] = "2"
    assert main(other) == 10 and FixtureHttpClient.log == []


def test_an_expired_approval_is_refused(networked, capsys):
    h = plan_hash(capsys, RUN)
    approvals.approve(h, ttl="1s")
    with pytest.raises(approvals.NotApproved, match="expired or already used"):
        approvals.consume(h, now=datetime.now(UTC) + timedelta(seconds=5))


def test_offline_runs_need_no_approval(data_dir):
    assert main(RUN) == 0


def test_a_forged_unsigned_line_breaks_the_ledger(networked, capsys):
    h = plan_hash(capsys, RUN)
    forged = {
        "schema": "approval.v2", "kind": "approved", "approval_id": "f" * 16, "plan_hash": h,
        "at": "2026-01-01T00:00:00Z", "expires_at": "2999-01-01T00:00:00Z", "run_id": None,
        "key_fp": "SHA256:" + "A" * 43, "prev": approvals.GENESIS,
    }  # fmt: skip
    ledger(networked).parent.mkdir(parents=True, exist_ok=True)
    ledger(networked).write_text(json.dumps(forged) + "\n")
    capsys.readouterr()
    assert main(RUN) == 10 and "not a valid approval.v2" in capsys.readouterr().err
    assert FixtureHttpClient.log == [] and runs(networked) == []


def test_a_line_signed_by_another_key_is_refused(networked, capsys, operator_key):
    attacker, _, _ = make_key(operator_key["dir"], "attacker")
    h = plan_hash(capsys, RUN)
    KeyFileSigner.key = attacker  # signs with a key that is not the pinned one...
    with pytest.raises(approvals.NotApproved, match="does not verify"):
        approvals.approve(h)  # ...so approve refuses to record it at all
    assert not ledger(networked).exists() and main(RUN) == 10


def test_swapping_the_drive_pin_alone_is_refused(networked, capsys, operator_key):
    """The operator's accepted risk: whoever can write the pin's folder could swap it. A swap of the
    allowed_signers file without ALSO changing pipeline.toml's fingerprint refuses everything."""
    attacker, apub, _ = make_key(operator_key["dir"], "attacker")
    signers(operator_key["dir"] / "allowed_signers", apub)
    KeyFileSigner.key = attacker
    h = plan_hash(capsys, RUN)
    with pytest.raises(approvals.NotApproved, match="needs both files changed"):
        approvals.approve(h)
    assert main(RUN) == 10 and FixtureHttpClient.log == []


def test_a_tampered_plan_hash_is_refused(networked, capsys):
    h = plan_hash(capsys, RUN)
    other = [*RUN]
    other[other.index("--start") + 1] = "2"
    h2 = plan_hash(capsys, other)
    approvals.approve(h)
    ledger(networked).write_text(ledger(networked).read_text().replace(h, h2))  # retarget the signed line
    assert main(other) == 10 and FixtureHttpClient.log == []


@pytest.mark.parametrize("damage", ["delete", "edit", "reorder"])
def test_a_damaged_chain_refuses_everything(networked, capsys, damage):
    h = plan_hash(capsys, RUN)
    approvals.approve(h)
    approvals.approve(h)  # two approvals for the same plan: one can be burned and one left
    assert main(RUN) == 0  # uses the first
    lines = ledger(networked).read_text().splitlines()
    if damage == "delete":
        lines = [lines[0], lines[2]]  # drop the second approval: hide it
    elif damage == "edit":
        lines[0] = lines[0].replace('"expires_at": "20', '"expires_at": "29', 1)  # valid, but changed
    else:
        lines = [lines[0], lines[2], lines[1]]
    ledger(networked).write_text("\n".join(lines) + "\n")
    capsys.readouterr()
    assert main(RUN) == 10 and "chain is broken" in capsys.readouterr().err
    assert main(["approvals", "verify"]) == 10


@pytest.mark.parametrize("how", ["unset", "unreadable"])
def test_no_readable_signers_pin_refuses(networked, capsys, monkeypatch, tmp_path, how):
    h = plan_hash(capsys, RUN)
    if how == "unset":
        monkeypatch.delenv("EX_APPROVAL_SIGNERS")
    else:
        monkeypatch.setenv("EX_APPROVAL_SIGNERS", str(tmp_path / "missing"))
    capsys.readouterr()
    assert main(["approve", h]) == 10 and "EX_APPROVAL_SIGNERS" in capsys.readouterr().err
    assert main(RUN) == 10


def test_a_declined_touch_records_nothing(networked, capsys, monkeypatch):
    class Declined:
        def sign(self, body, pubkey):
            raise approvals.NotApproved("not signed (Touch ID declined, timed out or the key was absent)")

    monkeypatch.setattr(approvals, "SIGNER", Declined)
    assert main(["approve", plan_hash(capsys, RUN)]) == 10
    assert not ledger(networked).exists()


def test_the_agent_path_signs_through_an_ssh_agent(
    networked, capsys, monkeypatch, operator_key, tmp_path_factory
):
    """AgentSigner, unmodified, through a throwaway ssh-agent holding the key: only the .pub is ever on
    disk for the signing call, exactly as with Secretive (whose agent adds the Touch ID prompt)."""
    agent = shutil.which("ssh-agent")
    sock = tmp_path_factory.mktemp("agent") / "s"
    proc = subprocess.Popen(
        [agent, "-D", "-a", str(sock)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        for _ in range(100):
            if sock.exists():
                break
            import time

            time.sleep(0.05)
        subprocess.run(
            [shutil.which("ssh-add"), str(operator_key["key"])],
            env={**os.environ, "SSH_AUTH_SOCK": str(sock)},
            check=True,
            capture_output=True,
        )
        monkeypatch.setattr(approvals, "SIGNER", approvals.AgentSigner)
        monkeypatch.setenv("EX_APPROVAL_AGENT", str(sock))
        h = plan_hash(capsys, RUN)
        rec = approvals.approve(h)
        assert rec["key_fp"] == operator_key["fp"] and main(RUN) == 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_no_agent_socket_refuses(networked, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(approvals, "SIGNER", approvals.AgentSigner)
    monkeypatch.setenv("EX_APPROVAL_AGENT", str(tmp_path / "nope.sock"))
    with pytest.raises(approvals.NotApproved, match="no SSH agent socket"):
        approvals.approve(plan_hash(capsys, RUN))


def test_pre_v2_lines_are_kept_but_never_honoured(networked, capsys):
    h = plan_hash(capsys, RUN)
    old = {"schema": "approval.v1", "kind": "approved", "approval_id": "e" * 16, "plan_hash": h,
           "at": "2026-01-01T00:00:00Z", "expires_at": "2999-01-01T00:00:00Z", "run_id": None, "sig": "x"}  # fmt: skip
    ledger(networked).parent.mkdir(parents=True, exist_ok=True)
    ledger(networked).write_text(json.dumps(old) + "\n")
    assert main(RUN) == 10
    approvals.approve(h)  # a v2 approval starts the chain after the old line
    assert (
        main(RUN) == 0
        and json.loads(ledger(networked).read_text().splitlines()[0])["schema"] == "approval.v1"
    )


def test_truncating_the_trailing_used_line_does_not_replay(networked, capsys):
    """The chain cannot anchor its last line; the run record (DATA) still names the approval."""
    h = plan_hash(capsys, RUN)
    approvals.approve(h)
    assert main(RUN) == 0
    lines = ledger(networked).read_text().splitlines()
    ledger(networked).write_text(lines[0] + "\n")  # drop the `used` line: the chain is still intact
    approvals.chain()
    assert main(RUN) == 10 and len(runs(networked)) == 1


def test_a_missing_network_flag_refuses_before_the_approval_is_spent(networked, capsys):
    """2026-11-08: a run without --approve-network was refused AFTER gate() consumed the signed approval,
    so the operator had to touch the sensor again. The flag check now comes first."""
    approvals.approve(plan_hash(capsys, RUN))
    no_flag = [a for a in RUN if a != "--approve-network"]
    with pytest.raises(SystemExit):
        main(no_flag)
    assert FixtureHttpClient.log == []
    assert not [r for r in approvals.chain() if r["kind"] == "used"]  # nothing spent
    assert main(RUN) == 0  # the same approval still runs


def test_a_refusal_before_any_site_request_refunds_the_approval(networked, capsys, monkeypatch):
    """Egress refused (exit 8) after the approval was spent, before anything reached the site: the approval
    comes back (a refunded ledger line AND the run record's flag), and the same approval then runs."""
    h = plan_hash(capsys, RUN)
    approvals.approve(h)
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-at.txt").as_uri())  # not the home country
    assert main(RUN) == 8 and FixtureHttpClient.log == []
    kinds = [r["kind"] for r in approvals.chain()]
    assert kinds == ["approved", "used", "refunded"] and approvals.has_live(h)
    (rec,) = [json.loads(p.read_text()) for p in approvals.store.runs_dir().glob("*.json")]
    assert rec["approval_ref"]["refunded"] is True and rec["requests"]["egress_trace"] == 1
    with pytest.raises(approvals.NotApproved, match="nothing to refund"):
        approvals.refund(rec["approval_ref"]["approval_id"], rec["run_id"], "again")  # one refund per use
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", (FIXTURES / "trace-ca.txt").as_uri())
    assert main(RUN) == 0 and not approvals.has_live(h)  # it ran, and is spent now


def test_a_stop_after_a_site_request_is_never_refunded(networked, capsys, tmp_path):
    """A challenge stop happens AFTER requests reached the site: the approval stays spent (no refund)."""
    from conftest import LIST

    d = tmp_path / "fx"
    shutil.copytree(FIXTURES, d)
    m = json.loads((d / "urls.json").read_text())
    m[f"{LIST}?page=1"] = "challenge-cf.html"  # the first list page is a challenge
    (d / "urls.json").write_text(json.dumps(m))
    argv = [*RUN]
    argv[argv.index("--fixtures") + 1] = str(d)
    h = plan_hash(capsys, argv)
    approvals.approve(h)
    assert main(argv) == 5  # stopped on the challenge, after robots.txt and the page reached the "site"
    assert [r["kind"] for r in approvals.chain()] == ["approved", "used"] and not approvals.has_live(h)
