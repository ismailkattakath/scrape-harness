"""`<cli> cli spec`: the whole argparse tree (core + the consumer's [plugins].commands) as cli.v1 JSONL.

One row per command, positional and option, joined with the command's pipeline.toml declaration (stage or
utility, network class, inputs, outputs). It is the ONE source the route table, the docs and the settings
are generated from, so it must be deterministic and drift-checked (docs/cli.md is the example's render).
"""

from __future__ import annotations

import argparse
import json

import pytest
from conftest import ROOT

from scrape_harness import cli, contracts

PIPELINE = ROOT / "pipeline.toml"
DOC = ROOT / "docs" / "cli.md"


def spec(capsys, *extra: str) -> str:
    capsys.readouterr()
    assert cli.main(["cli", "spec", *extra]) == 0
    return capsys.readouterr().out


def rows(capsys, *extra: str) -> list[dict]:
    return [json.loads(line) for line in spec(capsys, *extra).splitlines()]


@pytest.fixture
def consumer_cmd(tmp_path, monkeypatch):
    """A consumer plug-in command that reuses the harness's network flags and is declared in pipeline.toml."""
    code = tmp_path / "code"
    code.mkdir()
    (code / "speccmds.py").write_text(
        "from scrape_harness import cli\n"
        "def register(sub):\n"
        "    g = sub.add_parser('geo-test', help='test: a paid stand-in')\n"
        "    g.add_argument('--dry-run', action='store_true')\n"
        "    cli.network_args(g)\n"
        "    t = sub.add_parser('token-test', help='test: one approval flag only')\n"
        "    cli.approve_network_arg(t)\n"
    )
    text = PIPELINE.read_text()
    text = text.replace('python_path = ["examples"]', f'python_path = ["{ROOT / "examples"}", "{code}"]')
    text = text.replace('schema_dirs = ["examples/', f'schema_dirs = ["{ROOT}/examples/')
    text = text.replace("[plugins.sources]", 'commands = ["speccmds:register"]\n\n[plugins.sources]')
    text = text.replace(
        "# ── `examplesite refresh`",
        '[[utilities]]\nname = "geo-test"\nnetwork = "paid"\ninputs = ["DATA:observations.jsonl"]\n'
        "outputs = []\n\n# ── `examplesite refresh`",
    )
    p = tmp_path / "pipeline.toml"
    p.write_text(text)
    monkeypatch.setenv("SCRAPE_HARNESS_PIPELINE", str(p))
    return p


def test_every_row_is_a_valid_cli_v1_record(capsys, tmp_path):
    out = spec(capsys)
    got = [json.loads(line) for line in out.splitlines()]
    assert got and {r["kind"] for r in got} == {"command", "option", "positional"}
    for r in got:
        contracts.validate("cli.v1", r)
    f = tmp_path / "spec.jsonl"
    f.write_text(out)
    assert cli.main(["validate", "cli.v1", str(f)]) == 0
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps(got[0] | {"kind": "flag"}) + "\n")
    assert cli.main(["validate", "cli.v1", str(bad)]) == cli.EXIT_CONTRACT


def test_two_runs_are_byte_identical_and_sorted(capsys):
    a, b = spec(capsys), spec(capsys)
    assert a == b and a.endswith("\n")
    got = [json.loads(line) for line in a.splitlines()]
    cmds = [r["cmd"] for r in got]
    assert cmds == sorted(cmds)
    order = {"command": 0, "positional": 1, "option": 2}
    for cmd in set(cmds):
        mine = [r for r in got if r["cmd"] == cmd]
        assert [order[r["kind"]] for r in mine] == sorted(order[r["kind"]] for r in mine)
        opts = [r["opt"] for r in mine if r["kind"] == "option"]
        assert opts == sorted(opts)
    assert spec(capsys, "--json") == json.dumps(got, sort_keys=True, ensure_ascii=False) + "\n"


def test_the_tree_is_complete_nested_and_skips_help(capsys):
    got = rows(capsys)
    cmds = {r["cmd"] for r in got if r["kind"] == "command"}
    assert {"crawl-lists", "crawl", "crawl resume", "probe robots", "cli", "cli spec", "refresh"} <= cmds
    assert not [r for r in got if r["opt"] in ("-h", "--help")]
    by = {(r["cmd"], r["opt"]): r for r in got if r["kind"] != "command"}
    assert by[("crawl-lists", "--subcategory")] | {"help": None} == {
        "kind": "option",
        "cmd": "crawl-lists",
        "opt": "--subcategory",
        "takes_value": True,
        "required": True,
        "default": None,
        "choices": None,
        "help": None,
        "declared": None,
    }
    assert by[("crawl-lists", "--stride")]["default"] == "1"
    flag = by[("crawl-lists", "--approve-network")]
    assert flag["takes_value"] is False and flag["default"] is None
    pos = by[("cooldown", "action")]
    assert pos["kind"] == "positional" and pos["choices"] == ["status", "clear"]


def test_the_declared_network_class_is_carried(capsys):
    got = {r["cmd"]: r for r in rows(capsys) if r["kind"] == "command"}
    cl = got["crawl-lists"]["declared"]
    assert cl["role"] == "stage" and cl["position"] == 1 and cl["network"] == "site"
    assert cl["outputs"][0] == "DATA:observations.jsonl" and cl["inputs"] == []
    assert got["reparse"]["declared"]["network"] == "none"
    assert got["probe robots"]["declared"] | {"inputs": [], "outputs": []} == {
        "role": "utility",
        "position": None,
        "network": "site",
        "operator_only": False,
        "inputs": [],
        "outputs": [],
    }
    assert got["browser open"]["declared"]["operator_only"] is True
    assert got["cli spec"]["declared"] is None  # offline and undeclared


def test_a_consumer_plugin_command_appears_with_the_shared_network_flags(capsys, consumer_cmd):
    got = rows(capsys)
    geo = {r["opt"]: r for r in got if r["cmd"] == "geo-test"}
    cmd = geo[None]
    assert cmd["help"] == "test: a paid stand-in"
    assert cmd["declared"]["network"] == "paid" and cmd["declared"]["inputs"] == ["DATA:observations.jsonl"]
    shared = {r["opt"]: r for r in got if r["cmd"] == "crawl-lists"}
    for opt in (
        "--approve-network",
        "--proxy-exclude",
        "--concurrency",
        "--max-tasks-per-minute",
        "--approve-politeness-override",
        "--plan",
    ):
        assert geo[opt] == shared[opt] | {"cmd": "geo-test"}, opt  # same flag, default and help
    token = {r["opt"] for r in got if r["cmd"] == "token-test" and r["kind"] == "option"}
    assert token == {"--approve-network"}
    assert {r["opt"]: r for r in got if r["cmd"] == "token-test"}["--approve-network"]["help"] == (
        shared["--approve-network"]["help"]
    )


def test_network_args_is_the_one_definition_the_core_uses():
    p = argparse.ArgumentParser()
    cli.network_args(p)
    mine = [a.option_strings for a in p._actions if a.option_strings and a.dest != "help"]  # noqa: SLF001
    assert mine == [
        ["--approve-network"],
        ["--proxy-exclude"],
        ["--concurrency"],
        ["--max-tasks-per-minute"],
        ["--approve-politeness-override"],
        ["--plan"],
    ]


def test_check_exit_codes_and_diff(capsys, tmp_path):
    same = tmp_path / "same.jsonl"
    same.write_text(spec(capsys))
    assert cli.main(["cli", "spec", "--check", str(same)]) == 0
    drift = tmp_path / "drift.jsonl"
    drift.write_text(same.read_text().replace('"--stride"', '"--strode"', 1))
    capsys.readouterr()
    assert cli.main(["cli", "spec", "--check", str(drift)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("--- ") and '-{"choices"' in out and '"--strode"' in out
    assert cli.main(["cli", "spec", "--check", str(tmp_path / "missing.jsonl")]) == 1


def test_markdown_is_stable_and_escapes_pipes(capsys):
    md = spec(capsys, "--format", "md")
    assert md == spec(capsys, "--format", "md")
    assert "| `crawl-lists` | stage 1 | site |" in md
    assert "### `crawl-lists`" in md and "| `--approve-network` |" in md
    assert "`status\\|clear`" in md  # choices are joined by an ESCAPED pipe
    for line in md.splitlines():
        if line.startswith("|"):  # every table has 4 columns: 5 unescaped pipes on every row
            assert line.count("|") - line.count("\\|") == 5, line


def test_docs_cli_md_is_in_sync_with_the_tree():
    """docs/cli.md is the example consumer's render; regenerate with
    `examplesite cli spec --format md > docs/cli.md` when a command or flag changes."""
    assert cli.main(["cli", "spec", "--format", "md", "--check", str(DOC)]) == 0
