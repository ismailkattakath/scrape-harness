"""The hook gates, driven exactly as Claude Code drives them: JSON on stdin, exit code + stderr out.
Each gate has an allowed case, a denied case, and a garbage-input case (which must fail CLOSED)."""

import json
import os
import subprocess
import sys

import pytest
from conftest import EXAMPLES, PLUGIN, ROOT

GUARD = PLUGIN / "hooks" / "guard.py"


def run(gate, payload, env=None):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    e = {**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT), **(env or {})}
    return subprocess.run(
        [sys.executable, "-I", str(GUARD), gate],
        input=data,
        capture_output=True,
        text=True,
        env=e,
        timeout=60,
    )


def bash(cmd, **kw):
    return run("bash", {"tool_name": "Bash", "tool_input": {"command": cmd}}, **kw)


def write(path, content="x", **kw):
    return run(
        "write", {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}}, **kw
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "examplesite reparse",
        "examplesite fetch-ads --dry-run",
        "examplesite crawl-lists --driver fixture --subcategory x",
        "nix flake check",
        "examplesite ab --driver fixture --subcategory x",
        "examplesite breaker status",
    ],
)
def test_bash_allows_offline(cmd):
    assert bash(cmd).returncode == 0


@pytest.mark.parametrize(
    "cmd",
    [
        "examplesite fetch-ads",
        "examplesite crawl-lists --subcategory widgets",
        "cd x && examplesite fetch-ads",
        "examplesite crawl resume",
        "examplesite probe robots",
        "examplesite ab --subcategory x",
        "echo $EX_API_KEY",
        "secret reveal gcp:geocoding:apikey",
        "curl -s https://www.example.invalid/listings",
    ],
)
def test_bash_blocks_network_and_secrets(cmd):
    r = bash(cmd)
    assert r.returncode == 2 and "blocked" in r.stderr


@pytest.mark.parametrize(
    "cmd",
    [
        "examplesite fetch-ads --approve-network",
        "nix run . -- fetch-ads --approve-network",
        "./some-wrapper.sh --approve-network",
        "EX_NETWORK_APPROVED=1 make crawl",
    ],
)
def test_bash_approved_network_run_still_asks_the_human(cmd):
    r = bash(cmd)
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    "cmd",
    [
        "examplesite cooldown clear --site example.invalid --reason x --approve-operator",
        "examplesite cooldown clear --site example.invalid --reason x",
        "examplesite breaker clear --site example.invalid --resolve keep --note x --approve-operator",
    ],
)
def test_bash_cooldown_clear_asks(cmd):
    r = bash(cmd)
    assert r.returncode == 0 and json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    "cmd",
    [
        "examplesite approve " + "a" * 64,
        "script -q /dev/null examplesite approve abc",
        "nix run .#default -- approve abc",
        "python3 -m scrape_harness.cli approve abc",
    ],
)
def test_bash_lets_an_agent_invoke_approve_because_touch_id_is_the_gate(cmd):
    """Since step 5 the operator's Secure Enclave key signs approvals (Secretive, Touch ID): an agent
    may START `approve`; without the operator's finger it records nothing (tests/test_approvals.py)."""
    assert bash(cmd).returncode == 0


def test_bash_plan_is_allowed():
    assert bash("examplesite plan crawl-lists --subcategory x --seed 1").returncode == 0


def test_bash_cooldown_status_is_allowed():
    r = bash("examplesite cooldown status")
    assert r.returncode == 0 and r.stdout == ""


def test_bash_browser_open_asks():
    r = bash("examplesite browser open")
    assert r.returncode == 0 and json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_bash_blocks_unapproved_nix_run():
    assert bash("nix run .#default -- crawl-lists --subcategory x").returncode == 2


@pytest.mark.parametrize("gate", ["bash", "write", "subagent"])
def test_garbage_input_fails_closed(gate):
    r = run(gate, "not json")
    assert r.returncode == 2 and "failing closed" in r.stderr


def test_write_allows_source_and_fixtures():
    assert write(ROOT / "src" / "scrape_harness" / "x.py").returncode == 0
    assert write(ROOT / "tests" / "fixtures" / "ad-x.html").returncode == 0
    assert (
        write(ROOT / "tests" / "fixtures" / "a.html", "call 416-555-0123").returncode == 0
    )  # fictional block


@pytest.mark.parametrize("rel", ["observations.jsonl", "data/x.json", "export.csv", "qa.html"])
def test_write_blocks_data_in_repo(rel):
    r = write(ROOT / rel)
    assert r.returncode == 2 and "blocked" in r.stderr


def test_write_blocks_data_dir_and_real_phone(tmp_path):
    r = write(tmp_path / "d" / "notes.md", env={"EXAMPLE_DATA": str(tmp_path / "d")})
    assert r.returncode == 2 and "data folder" in r.stderr
    r = write(ROOT / "docs" / "x.md", "reach me at 416 700 1234")
    assert r.returncode == 2 and "phone" in r.stderr


def test_write_blocks_forbidden_literal_without_echoing_it(tmp_path):
    lits = tmp_path / "lits.txt"
    lits.write_text("\nsynthetic-forbidden-token\n")
    r = write(
        ROOT / "docs" / "x.md", "hello Synthetic-Forbidden-Token", env={"EX_FORBIDDEN_LITERALS": str(lits)}
    )
    assert r.returncode == 2 and "forbidden literal" in r.stderr
    assert "synthetic-forbidden-token" not in r.stderr.lower()


def _li_on_path(tmp_path):
    """Put the installed `examplesite` (this interpreter's) on PATH for the subagent gate."""
    shim = tmp_path / "bin" / "examplesite"
    shim.parent.mkdir()
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} -m scrape_harness.cli "$@"\n')
    shim.chmod(0o755)
    return {"PATH": f"{shim.parent}:{os.environ['PATH']}"}


def test_subagent_output_gate(tmp_path):
    env = _li_on_path(tmp_path)
    good = (EXAMPLES / "finding.example.json").read_text()
    ok = run(
        "subagent",
        {"agent_type": "finding-analyst", "last_assistant_message": f"```json\n{good}\n```"},
        env=env,
    )
    assert ok.returncode == 0, ok.stderr
    bad = run(
        "subagent", {"agent_type": "finding-analyst", "last_assistant_message": "I think H1 holds."}, env=env
    )
    assert bad.returncode == 2 and "finding.v1" in bad.stderr
    retry = run(
        "subagent",
        {"agent_type": "finding-analyst", "stop_hook_active": True, "last_assistant_message": "still prose"},
        env=env,
    )
    assert retry.returncode == 0  # one retry only; `examplesite findings append` is the final gate


def test_write_blocks_the_state_root(tmp_path):
    state = tmp_path / "state"
    r = write(state / "approvals" / "ledger.jsonl", env={"EXAMPLE_STATE": str(state)})
    assert r.returncode == 2 and "state folder" in r.stderr
    r = write(
        tmp_path / "xdg" / "scrape-harness-example" / "approvals" / "approve.pub",
        env={"XDG_STATE_HOME": str(tmp_path / "xdg"), "EXAMPLE_STATE": ""},
    )
    assert r.returncode == 2  # the default (XDG) state root is guarded too


def test_forbidden_literals_is_a_list_of_files_and_every_one_counts(tmp_path):
    a, b = tmp_path / "identity.txt", tmp_path / "site-words.txt"
    a.write_text("synthetic-identity\n")
    b.write_text("synthetic-site-word\n")
    env = {"EX_FORBIDDEN_LITERALS": f"{a}:{b}"}
    assert write(ROOT / "docs" / "x.md", "a synthetic-site-word here", env=env).returncode == 2
    assert write(ROOT / "docs" / "x.md", "a synthetic-identity here", env=env).returncode == 2
    assert write(ROOT / "docs" / "x.md", "nothing forbidden", env=env).returncode == 0


def test_an_unreadable_literals_file_fails_closed(tmp_path):
    a = tmp_path / "identity.txt"
    a.write_text("synthetic-identity\n")
    r = write(
        ROOT / "docs" / "x.md", "harmless", env={"EX_FORBIDDEN_LITERALS": f"{a}:{tmp_path / 'typo.txt'}"}
    )
    assert r.returncode == 2 and "unreadable" in r.stderr and "failing closed" in r.stderr
