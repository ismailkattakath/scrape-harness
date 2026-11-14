"""The plugin is well-formed, its hooks point at real files, and the project's settings are its render.

Formats per the Claude Code docs (plugins manifest reference, hooks, sub-agents): plugin.json needs
`name`; hooks/hooks.json needs the top-level "hooks" wrapper; a plugin agent's SubagentStop matcher is
the plugin-scoped name; plugin agents ignore hooks / mcpServers / permissionMode frontmatter.
"""

import ast
import json
import os
import re

import pytest
from conftest import PLUGIN, ROOT

from scrape_harness import claude_settings, pipeline

AGENT_FIELDS = {
    "name", "description", "tools", "disallowedTools", "model", "maxTurns", "skills", "memory",
    "background", "effort", "isolation", "color",
}  # fmt: skip


def frontmatter(path):
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, f"{path} has no frontmatter"
    return dict(line.split(":", 1) for line in m.group(1).splitlines() if line.strip()), text[m.end() :]


def test_the_plugin_is_the_pinned_marketplace_copy_and_the_harness_holds_none():
    """ONE source: the marketplace repo's plugins/scrape-harness, pinned as this flake's marketplace input."""
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "scrape-harness"
    assert claude_settings.PLUGIN.split("@") == ["scrape-harness", claude_settings.MARKETPLACE_NAME]
    assert manifest["repository"] == f"https://github.com/{claude_settings.MARKETPLACE_REPO}"
    assert not (ROOT / "plugin").exists() and not (ROOT / ".claude-plugin").exists()  # no second copy here


def test_hook_commands_point_at_real_executables_and_agents():
    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    assert set(doc["hooks"]) == {"PreToolUse", "SessionStart", "SubagentStop", "TeammateIdle", "PreCompact"}
    gates = []
    for groups in doc["hooks"].values():
        for g in groups:
            for h in g["hooks"]:
                m = re.fullmatch(
                    r'"\$\{CLAUDE_PLUGIN_ROOT\}"/(\S+) (bash|write|subagent|session|steward|teammate|precompact)',
                    h["command"],
                )
                assert m, h["command"]
                script = PLUGIN / m.group(1)
                assert script.is_file() and os.access(script, os.X_OK), script
                gates.append(m.group(2))
    assert sorted(gates) == ["bash", "precompact", "session", "steward", "subagent", "teammate", "write"]
    for sub in doc["hooks"]["SubagentStop"]:
        agent = re.fullmatch(r"\^scrape-harness:([a-z-]+)\$", sub["matcher"]).group(1)
        assert (PLUGIN / "agents" / f"{agent}.md").is_file()


@pytest.mark.parametrize("path", sorted((PLUGIN / "agents").glob("*.md")), ids=lambda p: p.stem)
def test_agents_use_only_supported_frontmatter(path):
    fm, body = frontmatter(path)
    assert set(fm) <= AGENT_FIELDS, set(fm) - AGENT_FIELDS
    assert fm["name"].strip() == path.stem and ":" not in fm["name"].strip()
    assert len(fm["description"].strip()) > 40 and body.strip()
    for skill in filter(None, (s.strip() for s in fm.get("skills", "").split(","))):
        assert (PLUGIN / "skills" / skill / "SKILL.md").is_file(), skill


def test_named_agents_exist():
    assert {p.stem for p in (PLUGIN / "agents").glob("*.md")} == {
        "pipeline-engineer",
        "analyst",
        "run-steward",
    }


@pytest.mark.parametrize("path", sorted((PLUGIN / "skills").glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_skills_are_named_after_their_folder(path):
    fm, body = frontmatter(path)
    assert fm["name"].strip() == path.parent.name and len(fm["description"].strip()) > 40 and body.strip()


def test_read_only_agents_cannot_write_or_fetch():
    for name in ("analyst", "run-steward"):
        fm, _ = frontmatter(PLUGIN / "agents" / f"{name}.md")
        denied = {t.strip() for t in fm["disallowedTools"].split(",")}
        assert {"Edit", "Write", "NotebookEdit", "WebFetch"} <= denied, name


def test_the_project_has_no_hooks_of_its_own():
    """The plugin provides the gates; a project hook would make each fire twice."""
    assert not (ROOT / ".claude" / "hooks").exists()
    assert "hooks" not in json.loads((ROOT / ".claude" / "settings.json").read_text())


def test_hook_and_settings_agree_on_data_file_types():
    tree = ast.parse((PLUGIN / "hooks" / "guard.py").read_text())
    (node,) = [n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "DATA_EXT"]
    assert ast.literal_eval(node.value) == set(claude_settings.DATA_EXT)


def test_project_settings_are_the_render_of_pipeline_toml():
    """Drift gate. Fix by editing pipeline.toml and running `<cli> claude-settings --write`."""
    assert claude_settings.check(ROOT), "run `examplesite claude-settings --write`"


def test_the_render_follows_the_consumer(tmp_path):
    base = (ROOT / "pipeline.toml").read_text()
    p = tmp_path / "pipeline.toml"
    p.write_text(
        base.replace('secret_envs = ["EX_API_KEY"]', 'secret_envs = ["ZZ_TOKEN"]').replace(
            'site_hosts = ["example.invalid"]', 'site_hosts = ["other.invalid"]'
        )
    )
    deny = claude_settings.render(pipeline.load(p))["permissions"]["deny"]
    assert "Bash(curl *other.invalid*)" in deny and "Bash(printenv ZZ_TOKEN)" in deny
    assert not any("example.invalid" in r or "EX_API_KEY" in r for r in deny)
    assert claude_settings.render(pipeline.load(p))["env"]["CLAUDE_CODE_ENABLE_TODO_TOOLS"] == "1"


# ── task seeding (SessionStart) ──────────────────────────────────────────────────────────────


def session_context(pipeline_path):
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-I", str(PLUGIN / "hooks" / "guard.py"), "session"],
        input="{}",
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT), "SCRAPE_HARNESS_PIPELINE": str(pipeline_path)},
        timeout=60,
    )
    assert r.returncode == 0  # SessionStart never blocks
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    return out["additionalContext"]


def test_session_start_seeds_one_task_per_stage_in_run_card_order():
    from scrape_harness import runcard

    ctx = session_context(ROOT / "pipeline.toml")
    seed = json.loads(ctx.split("\n", 1)[1])
    names = runcard.stages()
    assert [t["subject"] for t in seed] == [f"Stage {runcard.stage(n)}: {n}" for n in names]
    assert seed[0]["blockedBy"] == [] and all(
        t["blockedBy"] == [seed[i]["subject"]] for i, t in enumerate(seed[1:])
    )
    net = {c.name: c.network for c in pipeline.current().stages}
    for t, n in zip(seed, names, strict=True):
        assert ("--plan" in t["description"]) == (net[n] != "none"), t  # networked stages are planned first
        assert "approval flag yourself" in t["description"] or net[n] == "none"
    assert "TaskCreate" in ctx and "split, merge, fan out" in ctx


def test_session_start_says_so_when_the_pipeline_is_broken(tmp_path):
    p = tmp_path / "pipeline.toml"
    p.write_text("[[stages]\n")
    assert "missing or invalid" in session_context(p) and "Fix pipeline.toml first" in session_context(p)
    assert "missing or invalid" in session_context(tmp_path / "absent.toml")


def test_the_seed_follows_the_pipeline(tmp_path):
    base = (ROOT / "pipeline.toml").read_text()
    p = tmp_path / "pipeline.toml"
    p.write_text(
        base + '\n[[stages]]\nname = "publish"\nnetwork = "none"\ninputs = []\noutputs = ["stdout:report"]\n'
    )
    seed = json.loads(session_context(p).split("\n", 1)[1])
    assert seed[-1]["subject"] == "Stage 4/4: publish" and seed[-1]["blockedBy"] == ["Stage 3/4: reparse"]
    assert "Offline: run `examplesite publish`" in seed[-1]["description"]


def test_no_generated_rule_is_an_inert_write_rule():
    """Claude Code 2.1.318: "Write(...) is not matched by file permission checks — only Edit(path) rules
    are". Every file rule must be Edit(...) (covers all editing tools) or Read(...)."""
    deny = claude_settings.render()["permissions"]["deny"]
    assert not [r for r in deny if r.startswith("Write(")]
    assert all(r.split("(", 1)[0] in {"Edit", "Read", "Bash"} for r in deny), deny
    assert {f"Edit(**/*{e})" for e in claude_settings.DATA_EXT} <= set(deny)


def test_the_monitor_runs_the_watch_gate():
    """monitors/monitors.json (experimental): strict keys name/command/description/when."""
    (mon,) = json.loads((PLUGIN / "monitors" / "monitors.json").read_text())
    assert set(mon) <= {"name", "command", "description", "when"} and mon["when"] == "always"
    m = re.fullmatch(r'"\$\{CLAUDE_PLUGIN_ROOT\}"/(\S+) watch', mon["command"])
    assert m and os.access(PLUGIN / m.group(1), os.X_OK)


def test_the_status_line_is_the_consumers_cli():
    sl = claude_settings.render()["statusLine"]
    assert sl == {"type": "command", "command": claude_settings.STATUSLINE_COMMAND, "refreshInterval": 5}


BARE = "/usr/bin:/bin:/usr/sbin:/sbin"


def test_the_consumer_cli_does_not_collide_with_a_system_command():
    """`ex` was the example's CLI and is POSIX vi's line editor: on a bare PATH it resolved to /usr/bin/ex."""
    import shutil

    assert shutil.which(pipeline.consumer().cli, path=BARE) is None


def run_launcher(env):
    import subprocess

    py = next((p for p in ("/usr/bin/python3", "/bin/python3") if os.path.exists(p)), None) or sys_python()
    return subprocess.run(
        [py, str(ROOT / claude_settings.STATUSLINE)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def sys_python():
    import sys

    return sys.executable


def test_the_statusline_launcher_runs_with_no_dev_shell(tmp_path):
    """Bare PATH, no PYTHONPATH, no package installed: the generated launcher alone renders the bar."""
    env = {"PATH": BARE, "HOME": str(tmp_path), "EXAMPLE_STATE": str(tmp_path / "state")}
    r = run_launcher(env)
    assert r.returncode == 0 and r.stdout.strip() == "examplesite · no run yet", r.stderr
    st = json.loads((ROOT / "schemas" / "examples" / "status.example.json").read_text())
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "status.json").write_text(json.dumps(st))
    out = run_launcher(env).stdout.strip()
    assert out.startswith("examplesite · fetch-ads 2/3 · ") and "70/600 pages" in out


def test_the_launcher_finds_the_same_default_state_root_as_the_cli(tmp_path, monkeypatch):
    from scrape_harness import status, store

    monkeypatch.delenv("EXAMPLE_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    (store.default_state_dir()).mkdir(parents=True)
    st = (ROOT / "schemas" / "examples" / "status.example.json").read_text()
    (store.default_state_dir() / "status.json").write_text(st)
    env = {"PATH": BARE, "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "xdg")}
    assert run_launcher(env).stdout.strip().startswith("examplesite · fetch-ads 2/3")  # it found the file
    assert status.read_path() == store.default_state_dir() / "status.json"


def test_the_consumers_allow_rules_are_merged(tmp_path):
    base = (ROOT / "pipeline.toml").read_text()
    p = tmp_path / "pipeline.toml"
    p.write_text(
        base.replace(
            "[groups]", '[claude]\nallow = ["Bash(examplesite *)", "Bash(nix flake check*)"]\n\n[groups]', 1
        )
    )
    perms = claude_settings.render(pipeline.load(p))["permissions"]
    assert perms["allow"] == ["Bash(examplesite *)", "Bash(nix flake check*)"] and perms["deny"]
    assert "allow" not in claude_settings.render()["permissions"]  # the example declares none
