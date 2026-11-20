"""`<cli> claude-md`: the route table between the scrape-harness markers in a consumer's CLAUDE.md.

Generated from the plugin's skills and agents (at the ONE pinned plugin path, $SCRAPE_HARNESS_PLUGIN) and
from `cli spec` rows rendered repo-only, so no site file value can reach the committed block. Every test
plugin here is synthetic (tmp_path); one test checks the example's committed CLAUDE.md against the pin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from conftest import EXAMPLE, ROOT
from test_cli_spec import overlay_consumer  # noqa: F401 - the same site-overlay consumer `cli spec` uses

from scrape_harness import claude_md, cli, pipeline

START, END = claude_md.START, claude_md.END


def _md(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n"


@pytest.fixture
def fake_plugin(tmp_path, monkeypatch):
    """A synthetic plugin: two skills, two agents, every trigger form the extractor reads."""
    p = tmp_path / "plugin"
    (p / ".claude-plugin").mkdir(parents=True)
    (p / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "scrape-harness"}))
    for name, desc in {
        "zeta-skill": "Does zeta. Use whenever a run | pipe shows up in the output. Never anything else.",
        "alpha-skill": '"Does alpha. Use before any widget run that is not offline."',
    }.items():
        (p / "skills" / name).mkdir(parents=True)
        (p / "skills" / name / "SKILL.md").write_text(_md(name, desc))
    (p / "agents").mkdir()
    (p / "agents" / "watcher.md").write_text(
        "---\nname: watcher\ndescription: >-\n  Watches a run. Use when a\n  run is started.\ntools: Read\n---\nbody\n"
    )
    (p / "agents" / "fixer.md").write_text(
        _md("fixer", "Fixes. Use when a run stops, or a field goes missing.")
    )
    monkeypatch.setenv(claude_md.PLUGIN_ENV, str(p))
    return p


def block(capsys, *extra: str) -> str:
    capsys.readouterr()
    assert cli.main(["claude-md", *extra]) == 0
    return capsys.readouterr().out


def doc(tmp_path, body: str, name: str = "CLAUDE.md"):
    f = tmp_path / name
    f.write_text(body)
    return f


def test_the_block_routes_every_skill_agent_and_declared_command(capsys, fake_plugin):
    out = block(capsys)
    assert "| whenever a run \\| pipe shows up in the output | skill `scrape-harness:zeta-skill` |" in out
    assert "| before any widget run that is not offline | skill `scrape-harness:alpha-skill` |" in out
    assert "| when a run is started | agent `scrape-harness:watcher` |" in out  # a folded YAML scalar
    assert "| when a run stops, or a field goes missing | agent `scrape-harness:fixer` |" in out
    declared = [c.name for c in (*pipeline.current().stages, *pipeline.current().utilities)]
    for name in declared:
        assert f"`examplesite {name}`" in out, name
    assert "| `examplesite crawl-lists` (stage 1/" in out and "| site |" in out
    assert "`examplesite claude-md`" not in out.split("|---|", 1)[1]  # undeclared commands get no row
    rule = next(line for line in out.splitlines() if line.startswith("Networked commands"))
    assert "--plan" in rule and "examplesite approve <plan_hash>" in rule and "--approve-network" in rule
    for name in ("crawl-lists", "fetch-ads", "ab", "crawl resume", "probe robots"):  # each takes --plan
        assert f"`{name}`" in rule
    assert "`browser open`" not in rule  # operator-only, no --plan: the operator runs it headed
    assert "| `examplesite browser open` (operator only: the hook asks) | site |" in out


def test_rows_are_sorted_and_every_table_row_has_three_cells(capsys, fake_plugin):
    out = block(capsys)
    rows = [line for line in out.splitlines() if line.startswith("| ") and "`scrape-harness:" in line]
    uses = [r.split(" | ")[1] for r in rows]
    assert uses == sorted(uses)  # agents, then skills, each by name
    for line in out.splitlines():
        if line.startswith("|"):
            assert line.count("|") - line.count("\\|") == 4, line


def test_the_block_is_the_same_under_every_hash_seed(fake_plugin):
    def run(seed):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        r = subprocess.run(
            [sys.executable, "-m", "scrape_harness.cli", "claude-md"], env=env, capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr
        return r.stdout

    assert run("1") == run("2") == run("12345")


def test_no_site_file_value_reaches_the_block(capsys, fake_plugin, overlay_consumer, monkeypatch):  # noqa: F811
    bare = block(capsys)  # no site file at all (the Nix sandbox)
    _, site, _ = overlay_consumer
    monkeypatch.setenv("EX_SITE_CONFIG", str(site))
    assert pipeline.current().overlay is not None  # the rest of the CLI reads the site file ...
    got = block(capsys)
    assert got == bare  # ... the block never does: one render on every machine
    assert "overlay-only.invalid" not in got and "overlay-region" not in got
    assert claude_md.render() == bare  # the module is repo-only by itself, not only through main()


def test_a_description_without_a_use_clause_refuses(capsys, fake_plugin):
    (fake_plugin / "agents" / "mute.md").write_text(_md("mute", "Does things, quietly."))
    capsys.readouterr()
    assert cli.main(["claude-md"]) == 4
    assert "mute.md" in capsys.readouterr().err


def test_an_unset_or_missing_plugin_refuses(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv(claude_md.PLUGIN_ENV, raising=False)
    assert cli.main(["claude-md"]) == 4
    assert claude_md.PLUGIN_ENV in capsys.readouterr().err
    monkeypatch.setenv(claude_md.PLUGIN_ENV, str(tmp_path / "nope"))
    assert cli.main(["claude-md"]) == 4
    assert cli.main(["claude-md", "--plugin", str(tmp_path / "nope")]) == 4


def test_write_fills_only_the_block_and_a_second_write_changes_nothing(capsys, fake_plugin, tmp_path):
    f = doc(tmp_path, f"# Project\n\nmine, above\n\n{START}\nstale\n{END}\n\nmine, below\n")
    capsys.readouterr()
    assert cli.main(["claude-md", "--write", "--file", str(f)]) == 0
    assert json.loads(capsys.readouterr().out) == {"path": str(f), "written": True}
    first = f.read_text()
    assert first.startswith("# Project\n\nmine, above\n\n" + START + "\n")
    assert first.endswith(END + "\n\nmine, below\n") and "stale" not in first
    assert first.split(START + "\n", 1)[1].split(END, 1)[0] == block(capsys)
    mtime = f.stat().st_mtime_ns
    capsys.readouterr()
    assert cli.main(["claude-md", "--write", "--file", str(f)]) == 0
    assert json.loads(capsys.readouterr().out) == {"path": str(f), "written": False}
    assert f.read_text() == first and f.stat().st_mtime_ns == mtime


def test_check_exit_codes_and_diff(capsys, fake_plugin, tmp_path):
    f = doc(tmp_path, f"top\n{START}\n{END}\n")
    assert cli.main(["claude-md", "--check", "--file", str(f)]) == 1  # an empty block is drift
    assert cli.main(["claude-md", "--write", "--file", str(f)]) == 0
    capsys.readouterr()
    assert cli.main(["claude-md", "--check", "--file", str(f)]) == 0
    assert capsys.readouterr().out == ""
    f.write_text(f.read_text().replace("top\n", "edited outside the block\n"))
    assert cli.main(["claude-md", "--check", "--file", str(f)]) == 0  # outside the block is the consumer's
    f.write_text(f.read_text().replace("| skill `scrape-harness:zeta-skill`", "| skill `zeta`"))
    capsys.readouterr()
    assert cli.main(["claude-md", "--check", "--file", str(f)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("--- ") and "-| whenever" in out and "+| whenever" in out


@pytest.mark.parametrize(
    "body",
    [
        "no markers at all\n",
        f"{START}\nonly a start\n",
        f"only an end\n{END}\n",
        f"{START}\n{START}\n{END}\n",  # duplicated
        f"{START}\n{END}\n{START}\n{END}\n",
        f"{END}\n{START}\n",  # end before start
        f"inline {START}\n{END}\n",  # a marker is a line of its own
    ],
)
@pytest.mark.parametrize("mode", ["--write", "--check"])
def test_missing_duplicated_or_misplaced_markers_refuse(capsys, fake_plugin, tmp_path, body, mode):
    f = doc(tmp_path, body)
    capsys.readouterr()
    assert cli.main(["claude-md", mode, "--file", str(f)]) == 4
    assert "marker" in capsys.readouterr().err
    assert f.read_text() == body  # a refusal writes nothing


def test_an_absent_file_refuses(capsys, fake_plugin, tmp_path):
    for mode in ("--write", "--check"):
        assert cli.main(["claude-md", mode, "--file", str(tmp_path / "absent.md")]) == 4
    assert not (tmp_path / "absent.md").exists()


def test_write_and_check_are_exclusive(fake_plugin):
    with pytest.raises(SystemExit):
        cli.main(["claude-md", "--write", "--check"])


def test_the_default_file_is_claude_md_beside_pipeline_toml(fake_plugin):
    assert claude_md.default_file() == ROOT / "CLAUDE.md"


def test_the_example_consumers_claude_md_matches_the_pinned_plugin():
    """examples/example_site/CLAUDE.md is the example's committed block, against the plugin pinned in
    flake.lock. Regenerate it after a plugin pin bump:
    `examplesite claude-md --write --file examples/example_site/CLAUDE.md`."""
    assert cli.main(["claude-md", "--check", "--file", str(EXAMPLE / "CLAUDE.md")]) == 0
