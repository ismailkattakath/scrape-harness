"""Step 4: STATE/status.json (live mirror), the status line, the monitor's doorbells, and `<cli> run` (doit)."""

import importlib.util
import json
import os
import subprocess
import sys
import time

from conftest import FIXTURES, PLUGIN, ROOT

from scrape_harness import contracts, status, store, tasks
from scrape_harness.cli import main

FIX = [
    "--driver",
    "fixture",
    "--fixtures",
    str(FIXTURES),
    "--max-tasks-per-minute",
    "6000",
    "--approve-politeness-override",
]
GUARD = PLUGIN / "hooks" / "guard.py"


def guard():
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_run_mirrors_its_record_into_status_json(data_dir, tmp_path):
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    st = json.loads((tmp_path / "state" / "status.json").read_text())
    contracts.validate("status.v1", st)
    run = json.loads(next((data_dir / "runs").glob("*.json")).read_text())
    assert st["run_id"] == run["run_id"] and st["status"] == "completed" and st["stage"] == "1/3"
    assert st["requests"] == run["requests"] and st["log"] == run["card"]["live_log"]["path"]
    assert not (data_dir / "status.json").exists()  # STATE, never DATA
    line = status.line(st, "examplesite")
    assert line.startswith("examplesite · crawl-lists 1/3 · completed · ") and "0 challenges" in line


def test_status_line_without_any_run_or_with_a_broken_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EXAMPLE_STATE", str(tmp_path / "s"))
    assert main(["statusline"]) == 0 and capsys.readouterr().out.strip() == "examplesite · no run yet"
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "status.json").write_text("{not json")
    assert main(["statusline"]) == 0 and capsys.readouterr().out.strip() == "examplesite · no run yet"


def sample(**kw):
    st = json.loads((ROOT / "schemas" / "examples" / "status.example.json").read_text())
    return st | kw


def test_status_line_eta_and_stale():
    now = time.time()
    fresh = sample(updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), pid=os.getpid())
    assert "70/600 pages" in status.line(fresh, "examplesite", now) and "~1 h 16 min left" in status.line(
        fresh, "examplesite", now
    )
    gone = sample(updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1200)), pid=2**22 + 12345)
    assert "stale (20 min, process gone)" in status.line(gone, "examplesite", now)


def test_monitor_rings_on_transitions_only():
    g = guard()
    os.environ["SCRAPE_HARNESS_PIPELINE"] = str(ROOT / "pipeline.toml")
    now = time.time()
    t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    a = sample(status="running", updated_at=t, challenges=0)
    assert len(g.transitions(None, dict(a), now)) == 1  # a new run
    assert g.transitions(dict(a), dict(a), now) == []  # nothing new: silence
    ch = g.transitions(dict(a), dict(a, challenges=1), now)
    assert len(ch) == 1 and "challenge #1" in ch[0]
    stop = g.transitions(dict(a), dict(a, status="stopped"), now)
    assert "running -> stopped" in stop[0] and "runbook-stop" in stop[0] and "status.json" in stop[0]
    old = dict(a, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1200)))
    first = g.transitions(dict(a), old, now)
    assert len(first) == 2 and "no progress for 20 min" in first[0]  # + the stall incident's doorbell
    assert first[1].startswith("scrape-harness INCIDENT INC-") and "stall" in first[1]
    assert g.transitions(dict(old), dict(old), now) == []  # stale is said once


def test_monitor_survives_a_broken_pipeline(tmp_path):
    p = tmp_path / "pipeline.toml"
    p.write_text("[[stages]\n")
    env = {**os.environ, "SCRAPE_HARNESS_PIPELINE": str(p), "CLAUDE_PROJECT_DIR": str(ROOT)}
    try:
        r = subprocess.run(
            [sys.executable, "-I", str(GUARD), "watch"], capture_output=True, text=True, env=env, timeout=3
        )
        out = r.stdout
    except subprocess.TimeoutExpired as e:  # still running: the monitor did not die
        out = (e.stdout or b"").decode()
    assert "watching paused until fixed" in out


def test_run_tasks_follow_the_stages(data_dir):
    ts = tasks.tasks(extra=["--driver", "fixture"])
    assert [t["name"] for t in ts] == ["crawl-lists", "fetch-ads", "reparse"]
    assert [t["task_dep"] for t in ts] == [[], ["crawl-lists"], ["fetch-ads"]]
    crawl, fetch, reparse = ts
    assert crawl["actions"][0][3:7] == ["crawl-lists", "--subcategory", "widgets", "--driver"]
    assert "--driver" not in reparse["actions"][0]  # extra args go to networked stages only
    for t in ts:
        assert not any(a.startswith("--approve") for a in t["actions"][0])  # never an approval flag
        assert t["uptodate"] == [False]  # networked always run; reparse reads a directory (raw/)


def test_run_executes_the_pipeline_offline_through_doit(data_dir, tmp_path, monkeypatch):
    """The stage args come from pipeline.toml; the fixture set has list pages 1 and 3 only, so this test's
    pipeline crawls with stride 2 (a page-2 request would 404 and stop the run, which is correct)."""
    text = (ROOT / "pipeline.toml").read_text()
    text = text.replace(
        'args = ["--subcategory", "widgets"]',
        'args = ["--subcategory", "widgets", "--stride", "2", "--start", "1"]',
    )
    text = text.replace('python_path = ["examples"]', f'python_path = ["{ROOT / "examples"}"]')
    text = text.replace('schema_dirs = ["examples/', f'schema_dirs = ["{ROOT}/examples/')
    pl = tmp_path / "pipeline.toml"
    pl.write_text(text)
    monkeypatch.setenv("SCRAPE_HARNESS_PIPELINE", str(pl))
    assert main(["run", "--", *FIX]) == 0
    obs = (data_dir / "observations.jsonl").read_text().splitlines()
    lst = (data_dir / "listings.jsonl").read_text().splitlines()
    assert len(obs) == 4 and len(lst) == 3
    assert (tmp_path / "state" / "doit.json").exists() and not (data_dir / "doit.json").exists()
    assert json.loads((tmp_path / "state" / "status.json").read_text())["command"] == "fetch-ads"
    assert store.data_dir() == data_dir


def test_the_status_line_counts_open_incidents(data_dir, tmp_path, capsys):
    from scrape_harness import incidents

    assert main(["statusline"]) == 0 and "incident" not in capsys.readouterr().out
    inc = incidents.open_incident(5, "STOPPED", "r", "fetch-ads")
    assert main(["statusline"]) == 0 and capsys.readouterr().out.strip().endswith("⚠ 1 open incident")
    incidents.ack(inc["id"], "reported")
    assert main(["statusline"]) == 0 and "incident" not in capsys.readouterr().out
