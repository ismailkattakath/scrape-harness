"""pipeline.toml is the one source of truth: the run card, the store and the Bash hook all derive from it.

tests/golden/pipeline.golden.json freezes what the example pipeline produces: every run-card row
(command x run/plan/dry-run, synthetic context), the stage order, the store's file table and the hook's
verdict for a fixed set of commands. A change to pipeline.toml or to the code that reads it shows up here
as a diff that must be re-recorded deliberately (conftest.check_golden).

Provenance: at 9113947 the same test proved that listing-insights' pipeline.toml reproduces the rows,
file table and verdicts of its pre-extraction hand-kept lists (captured from listing-insights 8724ad6).
Phase 3 repeats that capture against listing-insights HEAD immediately before it switches to the harness:
run runcard.make / store.FILES / its .claude/hooks/guard.py there with the CTX below, then check its new
pipeline.toml against the capture with this module's functions.
"""

import json
import os
import subprocess
import sys

import pytest
from conftest import PLUGIN, ROOT, check_golden

from scrape_harness import pipeline, runcard, store
from scrape_harness.pipeline import PipelineError

PIPELINE = ROOT / "pipeline.toml"
GUARD = PLUGIN / "hooks" / "guard.py"
CTX = {
    "queue": "2026-01-15t12-00-00z-x-desktop-abc123",
    "site": "example.invalid",
    "ads": 3,
    "ab_at": "2026-01-15T12_00_00Z",
    "sample_file": "2026-01-15T12_00_00Z-x-desktop-abc123",
}
RID = "2026-01-15T12:00:00Z-x-desktop-abc123"
HOOK_CMDS = [
    "examplesite crawl-lists --subcategory x",
    "examplesite fetch-ads",
    "examplesite fetch-ads --dry-run",
    "examplesite crawl resume",
    "examplesite ab --subcategory x",
    "examplesite reparse",
    "examplesite plan crawl-lists --subcategory x --seed 1",
    "examplesite browser open",
    "examplesite fetch-ads --approve-network",
    "EX_NETWORK_APPROVED=1 examplesite fetch-ads",
    "nix run .#default -- fetch-ads",
    "curl -s https://www.example.invalid/",
    "echo $EX_API_KEY",
    "examplesite approve " + "a" * 64,
    "examplesite cooldown clear --site s --reason r",
    "examplesite cooldown status",
    "examplesite crawl-lists --driver fixture --subcategory x",
    "nix flake check",
]


def verdict(cmd: str, env: dict | None = None) -> str:
    r = subprocess.run(
        [sys.executable, "-I", str(GUARD), "bash"],
        input=json.dumps({"tool_input": {"command": cmd}}),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(ROOT),
            "SCRAPE_HARNESS_PIPELINE": str(PIPELINE),
            **(env or {}),
        },
        timeout=60,
    )
    return "block" if r.returncode == 2 else ("ask" if '"ask"' in r.stdout else "allow")


def capture(pl: pipeline.Pipeline) -> dict:
    rows = {}
    for c in (*pl.stages, *pl.utilities):
        if not (c.inputs or c.outputs):
            continue
        for mode in ("run", "plan", "dry-run"):
            card = runcard.make(
                c.name,
                "golden",
                RID,
                ctx=CTX,
                mode=mode,
                roots={"DATA": "/DATA", "STATE": "/STATE"},
                plan_hash="a" * 64,
            )
            rows[f"{c.name}|{mode}"] = {k: card[k] for k in ("stage", "inputs", "outputs")}
    return {
        "stages": runcard.stages(),
        "runcard": rows,
        "files": {k: list(v) for k, v in store.files().items()},
        "hook": {cmd: verdict(cmd) for cmd in HOOK_CMDS},
    }


def test_the_example_pipeline_reproduces_the_golden(monkeypatch):
    monkeypatch.setenv("SCRAPE_HARNESS_PIPELINE", str(PIPELINE))  # explicit, never an inherited default
    check_golden("pipeline.golden", capture(pipeline.current()))


# ── the loader refuses a broken declaration ──────────────────────────────────────────────────────

BASE = PIPELINE.read_text()


def test_the_example_pipeline_validates():
    pl = pipeline.load(PIPELINE)
    assert pl.name == "example" and pl.stages and pl.files and pl.consumer.cli == "examplesite"


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda t: t.replace('network = "none"', 'network = "sometimes"', 1), "not a valid pipeline.v1"),
        (lambda t: t.replace('"@run"', '"@nope"', 1), "unknown group"),
        (lambda t: t.replace('name = "reparse"', 'name = "fetch-ads"', 1), "declared twice"),
        (lambda t: t.replace('schema = "pipeline.v1"', 'schema = "pipeline.v2"', 1), "not a valid"),
        (lambda t: t + "\n[[stages]\n", "not valid TOML"),
        (lambda t: t.replace("offline_flags =", "offline_flagz =", 1), "not a valid"),
        (lambda t: t.replace("[consumer]", "[consumerz]", 1), "not a valid"),
    ],
    ids=[
        "bad-network",
        "unknown-group",
        "duplicate",
        "wrong-version",
        "bad-toml",
        "missing-key",
        "no-consumer",
    ],
)
def test_the_loader_refuses_a_broken_pipeline(tmp_path, mutate, msg):
    p = tmp_path / "pipeline.toml"
    p.write_text(mutate(BASE))
    assert p.read_text() != BASE  # the mutation found its target
    with pytest.raises(PipelineError, match=msg):
        pipeline.load(p)


def test_a_missing_pipeline_is_an_error_not_an_empty_one(tmp_path, monkeypatch):
    monkeypatch.delenv("SCRAPE_HARNESS_PIPELINE")
    monkeypatch.delenv("PRJ_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PipelineError, match="no pipeline.toml"):
        pipeline.locate()
    with pytest.raises(PipelineError, match="no such pipeline file"):
        pipeline.load(tmp_path / "absent.toml")


# ── the hook fails CLOSED on every key it reads ──────────────────────────────────────────────────


def hook_with(tmp_path, text, cmd="examplesite reparse"):
    p = tmp_path / "pipeline.toml"
    if text is not None:
        p.write_text(text)
    return subprocess.run(
        [sys.executable, "-I", str(GUARD), "bash"],
        input=json.dumps({"tool_input": {"command": cmd}}),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT), "SCRAPE_HARNESS_PIPELINE": str(p)},
        timeout=60,
    )


BROKEN = [
    ("missing file", None),
    ("not toml", "[[stages]\n"),
    ("no stages", BASE.replace("[[stages]]", "[[stagez]]")),
    ("stages not a list", 'stages = "crawl-lists"\n'),
    ("no utilities", BASE.replace("[[utilities]]", "[[utilitiez]]")),
    ("a stage without name", BASE.replace('name = "reparse"', 'nam = "reparse"', 1)),
    ("a stage without network", BASE.replace('network = "none"', 'networc = "none"', 1)),
    ("an unknown network class", BASE.replace('network = "site"', 'network = "maybe"', 1)),
    ("no offline_flags", BASE.replace("offline_flags =", "offline_flagz =", 1)),
    ("offline_flags not flags", BASE.replace('"--dry-run"', '"dry-run"', 1)),
    ("no consumer", BASE.replace("[consumer]", "[consumerz]", 1)),
    ("consumer.cli malformed", BASE.replace('cli = "examplesite"', 'cli = "E X"', 1)),
    ("consumer.env_prefix missing", BASE.replace('env_prefix = "EX"', "", 1)),
    ("consumer.data_env malformed", BASE.replace('data_env = "', 'data_env = "lower', 1)),
    ("consumer.secret_envs not a list", BASE.replace("secret_envs = [", 'secret_envs = "X" #[', 1)),
    ("consumer.site_hosts missing", BASE.replace("site_hosts =", "site_hostz =", 1)),
]


@pytest.mark.parametrize("case, text", BROKEN, ids=[c for c, _ in BROKEN])
def test_the_hook_fails_closed_on_a_broken_pipeline(tmp_path, case, text):
    assert text is None or text != BASE  # the mutation found its target
    r = hook_with(tmp_path, text)
    assert r.returncode == 2 and "failing closed" in r.stderr, (case, r.stderr)


def test_the_hook_follows_the_pipeline_not_a_hidden_list(tmp_path):
    """Declare a NEW site-class command: the hook blocks it with no code change; flip it to none: allowed."""
    added = BASE + '\n[[utilities]]\nname = "probe"\nnetwork = "site"\ninputs = []\noutputs = []\n'
    assert hook_with(tmp_path, added, "examplesite probe").returncode == 2
    assert hook_with(tmp_path, added, "examplesite probe --dry-run").returncode == 0
    offline = added.replace('name = "probe"\nnetwork = "site"', 'name = "probe"\nnetwork = "none"')
    assert hook_with(tmp_path, offline, "examplesite probe").returncode == 0


def test_the_hook_takes_every_world_visible_name_from_the_consumer(tmp_path):
    """Rename the consumer: the hook follows (cli, secret env, site host, approval knob), no code change."""
    renamed = (
        BASE.replace('cli = "examplesite"', 'cli = "zz"')
        .replace('env_prefix = "EX"', 'env_prefix = "ZZ"')
        .replace('secret_envs = ["EX_API_KEY"]', 'secret_envs = ["ZZ_TOKEN"]')
        .replace('site_hosts = ["example.invalid"]', 'site_hosts = ["other.invalid"]')
    )
    want = {
        "zz fetch-ads": 2,
        "examplesite fetch-ads": 0,  # no longer this consumer's CLI
        "zz fetch-ads --dry-run": 0,
        "echo $ZZ_TOKEN": 2,
        "echo $EX_API_KEY": 0,
        "curl https://other.invalid/x": 2,
        "zz approve " + "a" * 64: 0,  # the Touch ID signature is the gate, not the hook
    }
    for cmd, code in want.items():
        assert hook_with(tmp_path, renamed, cmd).returncode == code, cmd
    asked = hook_with(tmp_path, renamed, "ZZ_NETWORK_APPROVED=1 zz fetch-ads")
    assert asked.returncode == 0 and '"ask"' in asked.stdout


def test_the_cli_takes_its_names_from_the_consumer(tmp_path, monkeypatch):
    renamed = BASE.replace('cli = "examplesite"', 'cli = "zz"').replace(
        'env_prefix = "EX"', 'env_prefix = "ZZ"'
    )
    p = tmp_path / "pipeline.toml"
    p.write_text(renamed)
    monkeypatch.setenv("SCRAPE_HARNESS_PIPELINE", str(p))
    monkeypatch.setenv("ZZ_VARIANT", "mobile")
    assert pipeline.knob_name("VARIANT") == "ZZ_VARIANT" and pipeline.knob("VARIANT") == "mobile"
    from scrape_harness import cli

    assert cli.parser().prog == "zz"


def test_a_consumer_adds_its_own_commands_and_plugin_groups(data_dir, tmp_path, monkeypatch, capsys):
    """[plugins].commands adds subcommands; any [plugins.<group>] declares a plug-in group (e.g. geocoders)."""
    code = tmp_path / "code"
    code.mkdir()
    (code / "mycmds.py").write_text(
        "from scrape_harness import cli\n"
        "class Geo:\n    provider = 'g'\n    network = False\n"
        "def run(args):\n"
        "    cli.card('hello', 'say hello')\n"
        "    print(type(cli.load('geocoders', 'synthetic')).__name__)\n    return 0\n"
        "def register(sub):\n    sub.add_parser('hello').set_defaults(fn=run)\n"
    )
    text = BASE.replace('python_path = ["examples"]', f'python_path = ["{ROOT / "examples"}", "{code}"]')
    text = text.replace('schema_dirs = ["examples/', f'schema_dirs = ["{ROOT}/examples/')
    text = text.replace(
        "[plugins.sources]",
        'commands = ["mycmds:register"]\n\n[plugins.geocoders]\nsynthetic = "mycmds:Geo"\n\n[plugins.sources]',
    )
    p = tmp_path / "pipeline.toml"
    p.write_text(text)
    monkeypatch.setenv("SCRAPE_HARNESS_PIPELINE", str(p))
    from scrape_harness import interfaces
    from scrape_harness.cli import main

    assert interfaces.available("geocoders") == ["synthetic"]
    capsys.readouterr()
    assert main(["hello"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "Geo" and "RUN CARD" in out.err


# ── plugin/harness version skew: the plugin auto-updates, the harness is pinned ──────────────────


def test_the_plugin_knows_exactly_this_harness_pipeline_v1():
    """The guard's key sets are this harness's schema at the pinned plugin rev; a drift is a release bug."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("guard", GUARD)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    schema = json.loads((ROOT / "schemas" / "pipeline.v1.schema.json").read_text())
    assert schema["title"] == g.SCHEMA
    assert set(schema["properties"]) == g.KNOWN_TOP
    assert set(schema["properties"]["consumer"]["properties"]) == g.KNOWN_CONSUMER


def test_older_plugin_meets_newer_pipeline_fails_closed_naming_the_plugin_update(tmp_path):
    newer = (
        BASE.replace("[consumer]\n", '[consumer]\nfuture_knob = "x"\n', 1) + '\n[telemetry]\nendpoint = "x"\n'
    )
    r = hook_with(tmp_path, newer, "examplesite reparse")
    assert r.returncode == 2 and "version mismatch" in r.stderr and "OLDER than the harness" in r.stderr
    assert (
        "consumer.future_knob" in r.stderr and "telemetry" in r.stderr and "claude plugin update" in r.stderr
    )
    v2 = BASE.replace('schema = "pipeline.v1"', 'schema = "pipeline.v2"', 1)
    r = hook_with(tmp_path, v2, "examplesite reparse")
    assert r.returncode == 2 and "version mismatch" in r.stderr and "'pipeline.v2'" in r.stderr


def test_newer_plugin_meets_older_pipeline_fails_closed_naming_the_harness_update(tmp_path):
    """A plugin that needs a key an older harness's pipeline does not have (simulated: drop one)."""
    older = BASE.replace('site_hosts = ["example.invalid"]', "", 1)
    r = hook_with(tmp_path, older, "examplesite reparse")
    assert r.returncode == 2 and "consumer.site_hosts is missing" in r.stderr
    assert "OLDER than this plugin" in r.stderr and "nix flake update scrape-harness" in r.stderr


def test_ships_build_legs_are_gc_roots_not_no_link():
    """A --no-link build leaves no GC root: the daemon's automatic GC then collected the ship's closures and
    the next ship rebuilt the uncached ones (>20 min, measured). Each leg must --out-link into STATE."""
    from conftest import ROOT

    nix = (ROOT / "nix" / "consumer.nix").read_text()
    ship = nix[nix.index('name = "ship"') :]
    ship = ship[: ship.index("\\n  };") if "\\n  };" in ship else len(ship)]
    builds = [ln for ln in ship.splitlines() if "nix build" in ln and not ln.strip().startswith("#")]
    assert len(builds) == 2 and not [ln for ln in builds if "--no-link" in ln]
    assert '--out-link "$roots/darwin-check"' in ship and '--out-link "$roots/linux-package"' in ship
    assert "/gcroots" in ship and "XDG_STATE_HOME" in ship


def test_ship_runs_local_checks_after_the_flake_check_and_before_the_push():
    """shipLocalChecks run on the HOST (DATA visible), so they must sit between the sandboxed
    flake check and the push; before the check they would gate nothing extra, after the push too late."""
    from conftest import ROOT

    nix = (ROOT / "nix" / "consumer.nix").read_text()
    ship = nix[nix.index('name = "ship"') :]
    lines = ship[ship.index("text = ''") :].splitlines()  # the script, not runtimeInputs
    calls = [i for i, ln in enumerate(lines) if ln.strip().startswith("ship-local-checks")]
    check = next(i for i, ln in enumerate(lines) if "nix flake check" in ln)
    push = next(i for i, ln in enumerate(lines) if "git push" in ln)
    assert len(calls) == 1 and check < calls[0] < push
