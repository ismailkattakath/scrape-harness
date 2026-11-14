"""The site overlay (pipeline.py site_overlay): a consumer keeps its site specifics in a site file outside its
repo. The harness and the plugin's guard read the same file by the same rule and allowlist, and both FAIL
CLOSED when it is unreadable; a pipeline without site_overlay is unaffected."""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest
from conftest import PLUGIN, ROOT

from scrape_harness import claude_settings, pipeline
from scrape_harness.pipeline import PipelineError

GUARD = PLUGIN / "hooks" / "guard.py"
BASE = (ROOT / "pipeline.toml").read_text()
OVERLAY = """
[pipeline.consumer]
default_site = "other.invalid"
default_region = "other-region"
site_hosts = ["other.invalid"]

[pipeline.plugins.sources]
"other.invalid" = "example_site.source:ExampleSite"

[pipeline.crawl.proxy]
urls = ["http://proxy-a.example.invalid:8080"]
choice = "random"
user_secret = "proxy:example:user"
password_secret = "proxy:example:password"

[pipeline.crawl.refresh]
steps = [{ name = "crawl-lists", args = ["--subcategory", "gadgets", "--stride", "3"] }, { name = "reparse" }]

[region]  # the consumer's own table: never read by the harness
km = 1.0
"""


def consumer_with_overlay(tmp_path, overlay: str | None = OVERLAY):
    """A consumer dir: the example pipeline with site_overlay = true, plus (optionally) its site file."""
    d = tmp_path / "proj"
    d.mkdir()
    (d / "pipeline.toml").write_text(BASE.replace("[consumer]\n", "[consumer]\nsite_overlay = true\n", 1))
    site = tmp_path / "site.toml"
    if overlay is not None:
        site.write_text(overlay)
    return d / "pipeline.toml", site


def test_the_overlay_sets_the_site_keys_and_nothing_else(tmp_path, monkeypatch):
    p, site = consumer_with_overlay(tmp_path)
    monkeypatch.setenv("EX_SITE_CONFIG", str(site))
    pl = pipeline.load(p)
    c = pl.consumer
    assert (c.default_site, c.default_region, c.site_hosts) == (
        "other.invalid",
        "other-region",
        ("other.invalid",),
    )
    assert pl.plugins["sources"]["other.invalid"] == "example_site.source:ExampleSite"
    assert pl.proxy["urls"] == ["http://proxy-a.example.invalid:8080"]
    assert [s.name for s in pl.refresh] == ["crawl-lists", "reparse"] and "gadgets" in pl.refresh[0].args
    assert pl.overlay == site.resolve()
    repo = pipeline.load(ROOT / "pipeline.toml")
    assert (pl.stages, pl.files, c.secret_envs, c.approval_key_fingerprint) == (
        repo.stages,
        repo.files,
        repo.consumer.secret_envs,
        repo.consumer.approval_key_fingerprint,
    )


def test_a_table_value_updates_the_repo_table(tmp_path, monkeypatch):
    p, site = consumer_with_overlay(
        tmp_path, '[pipeline.crawl.proxy]\nurls = ["http://p.example.invalid:8080"]\n'
    )
    p.write_text(p.read_text() + '\n[crawl.proxy]\nuser_secret = "repo:user"\npassword_secret = "repo:pw"\n')
    monkeypatch.setenv("EX_SITE_CONFIG", str(site))
    assert pipeline.load(p).proxy == {
        "user_secret": "repo:user",
        "password_secret": "repo:pw",
        "urls": ["http://p.example.invalid:8080"],
    }


def test_the_data_root_default(tmp_path, monkeypatch):
    p, site = consumer_with_overlay(tmp_path)
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    site.rename(data / "config" / "site.toml")
    monkeypatch.delenv("EX_SITE_CONFIG", raising=False)
    monkeypatch.setenv("EXAMPLE_DATA", str(data))
    assert pipeline.load(p).consumer.default_site == "other.invalid"


@pytest.mark.parametrize("missing", ["file", "env"])
def test_an_unreadable_site_file_refuses_never_the_example(tmp_path, monkeypatch, missing):
    p, site = consumer_with_overlay(tmp_path, overlay=None)
    monkeypatch.delenv("EX_SITE_CONFIG", raising=False)
    monkeypatch.delenv("EXAMPLE_DATA", raising=False)
    if missing == "file":
        monkeypatch.setenv("EX_SITE_CONFIG", str(site))
    with pytest.raises(PipelineError, match="site_overlay"):
        pipeline.load(p)


@pytest.mark.parametrize(
    "bad",
    [
        '[pipeline.consumer]\nsecret_envs = ["X"]\n',  # not a site key
        "[pipeline.stages]\nx = 1\n",  # not an overlay table
        '[pipeline.crawl]\nblock_extensions = ["png"]\n',
    ],
)
def test_a_key_outside_the_allowlist_refuses(tmp_path, monkeypatch, bad):
    p, site = consumer_with_overlay(tmp_path, bad)
    monkeypatch.setenv("EX_SITE_CONFIG", str(site))
    with pytest.raises(PipelineError, match="may hold only"):
        pipeline.load(p)


def test_without_site_overlay_the_site_file_is_never_read(tmp_path, monkeypatch):
    monkeypatch.setenv("EX_SITE_CONFIG", str(tmp_path / "absent.toml"))
    pl = pipeline.load(ROOT / "pipeline.toml")
    assert pl.overlay is None and pl.consumer.default_site == "example.invalid"


def test_current_reloads_when_the_site_file_changes_name(tmp_path, monkeypatch):
    p, site = consumer_with_overlay(tmp_path)
    other = tmp_path / "site2.toml"
    other.write_text(OVERLAY.replace('"other.invalid"]', '"third.invalid"]'))
    monkeypatch.setenv(pipeline.ENV, str(p))
    monkeypatch.setenv("EX_SITE_CONFIG", str(site))
    assert pipeline.consumer().site_hosts == ("other.invalid",)
    monkeypatch.setenv("EX_SITE_CONFIG", str(other))
    assert pipeline.consumer().site_hosts == ("third.invalid",)


def test_settings_json_is_generic_and_the_site_rules_go_local(tmp_path, monkeypatch):
    p, site = consumer_with_overlay(tmp_path)
    monkeypatch.setenv(pipeline.ENV, str(p))
    monkeypatch.setenv("EX_SITE_CONFIG", str(site))
    committed = claude_settings.render()
    assert not any("invalid" in r for r in committed["permissions"]["deny"])
    local = json.loads(claude_settings.local_text())
    assert local == {"permissions": {"deny": ["Bash(curl *other.invalid*)", "Bash(wget *other.invalid*)"]}}
    claude_settings.write(p.parent)
    assert claude_settings.check(p.parent)
    claude_settings.local_target(p.parent).write_text("{}\n")
    assert not claude_settings.check(p.parent)  # a drifted local file is drift too
    # the example (no overlay) keeps its host rules in settings.json and writes no local file
    assert (
        "Bash(curl *example.invalid*)"
        in claude_settings.render(pipeline.load(ROOT / "pipeline.toml"))["permissions"]["deny"]
    )
    assert claude_settings.local_text(pipeline.load(ROOT / "pipeline.toml")) is None


# ── the plugin's guard reads the same file ───────────────────────────────────────────────────────


def guard_bash(cmd, project, env):
    e = {k: v for k, v in os.environ.items() if k not in ("EX_SITE_CONFIG", "EXAMPLE_DATA")}
    e |= {
        "CLAUDE_PROJECT_DIR": str(project),
        "SCRAPE_HARNESS_PIPELINE": str(project / "pipeline.toml"),
        **env,
    }
    return subprocess.run(
        [sys.executable, "-I", str(GUARD), "bash"],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
        capture_output=True,
        text=True,
        env=e,
        timeout=60,
    )


def test_guard_blocks_the_overlays_host(tmp_path):
    p, site = consumer_with_overlay(tmp_path)
    env = {"EX_SITE_CONFIG": str(site)}
    r = guard_bash("curl https://other.invalid/x", p.parent, env)
    assert r.returncode == 2 and "direct HTTP to the crawled site" in r.stderr
    assert guard_bash("curl https://unrelated.example.org/", p.parent, env).returncode == 0
    assert guard_bash("examplesite crawl-lists --dry-run", p.parent, env).returncode == 0


@pytest.mark.parametrize(
    "cmd", ["examplesite crawl-lists", "examplesite fetch-ads", "curl https://x.example.org/"]
)
def test_guard_with_an_unreadable_site_file_refuses_network_and_says_why(tmp_path, cmd):
    p, site = consumer_with_overlay(tmp_path, overlay=None)
    r = guard_bash(cmd, p.parent, {"EX_SITE_CONFIG": str(site)})
    assert r.returncode == 2 and "site file" in r.stderr and "unreadable" in r.stderr


def test_guard_with_an_unreadable_site_file_lets_offline_work_through(tmp_path):
    p, site = consumer_with_overlay(tmp_path, overlay=None)
    for cmd in ("examplesite reparse", "ls -la", "git status"):
        assert guard_bash(cmd, p.parent, {"EX_SITE_CONFIG": str(site)}).returncode == 0, cmd


def test_guard_without_site_overlay_is_unaffected(tmp_path):
    """A consumer that declares no site_overlay: no site file is looked for, curl to other hosts passes."""
    env = {"EX_SITE_CONFIG": str(tmp_path / "absent.toml")}
    assert guard_bash("curl https://unrelated.example.org/", ROOT, env).returncode == 0
    assert guard_bash("curl https://example.invalid/x", ROOT, env).returncode == 2


def test_guard_refuses_a_key_outside_the_allowlist(tmp_path):
    p, site = consumer_with_overlay(tmp_path, '[pipeline.consumer]\ncli = "other"\n')
    r = guard_bash("ls", p.parent, {"EX_SITE_CONFIG": str(site)})
    assert r.returncode == 2 and "may hold only" in r.stderr


def test_guard_and_harness_hold_the_same_allowlist_and_default():
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    assert g.OVERLAY_ALLOWED == pipeline.OVERLAY_ALLOWED
    assert g.SITE_CONFIG_DEFAULT == pipeline.SITE_CONFIG_DEFAULT


def site_with_dirs(tmp_path, body: str):
    """A site file in its OWN folder (not the project's), so a relative dir proves where it resolves."""
    sd = tmp_path / "sitedir"
    sd.mkdir()
    (sd / "site.toml").write_text(body)
    return sd


def test_overlay_dirs_add_to_the_repos_and_resolve_against_the_site_file(tmp_path, monkeypatch):
    p, _ = consumer_with_overlay(tmp_path)
    sd = site_with_dirs(
        tmp_path, '[pipeline.plugins]\npython_path = ["code"]\nschema_dirs = ["code/schemas"]\n'
    )
    monkeypatch.setenv("EX_SITE_CONFIG", str(sd / "site.toml"))
    pl, repo = pipeline.load(p), pipeline.load_repo(p)
    assert pl.python_path == (*repo.python_path, (sd / "code").resolve())
    assert pl.schema_dirs == (*repo.schema_dirs, (sd / "code" / "schemas").resolve())


def test_a_source_beside_the_site_file_loads(tmp_path, monkeypatch):
    import shutil

    p, _ = consumer_with_overlay(tmp_path)
    sd = site_with_dirs(
        tmp_path,
        '[pipeline.plugins]\npython_path = ["code"]\n\n'
        '[pipeline.plugins.sources]\n"beside.invalid" = "beside_site_src.source:ExampleSite"\n',
    )
    shutil.copytree(
        ROOT / "examples" / "example_site",
        sd / "code" / "beside_site_src",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    monkeypatch.setenv("EX_SITE_CONFIG", str(sd / "site.toml"))
    monkeypatch.setenv(pipeline.ENV, str(p))
    monkeypatch.setattr(sys, "path", list(sys.path))
    from scrape_harness import interfaces

    cls = interfaces.plugin_class("sources", "beside.invalid")
    assert cls.__name__ == "ExampleSite" and cls.__module__ == "beside_site_src.source"
    assert pathlib.Path(sys.modules[cls.__module__].__file__).is_relative_to(sd / "code")


@pytest.mark.parametrize("bad", ['python_path = "code"', "schema_dirs = [1]"])
def test_an_overlay_dir_that_is_not_a_list_of_paths_refuses(tmp_path, monkeypatch, bad):
    p, _ = consumer_with_overlay(tmp_path)
    sd = site_with_dirs(tmp_path, f"[pipeline.plugins]\n{bad}\n")
    monkeypatch.setenv("EX_SITE_CONFIG", str(sd / "site.toml"))
    with pytest.raises(PipelineError, match="list of paths"):
        pipeline.load(p)


def test_the_cli_refuses_an_unreadable_site_file_in_one_line(tmp_path, monkeypatch, capsys):
    from scrape_harness import cli

    p, site = consumer_with_overlay(tmp_path, overlay=None)
    monkeypatch.setenv("EX_SITE_CONFIG", str(site))
    monkeypatch.setenv(pipeline.ENV, str(p))
    assert cli.main(["--help"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("refusing: site_overlay:") and "unreadable" in err and "Traceback" not in err
