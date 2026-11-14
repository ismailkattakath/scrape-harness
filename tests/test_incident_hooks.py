"""The incident protocol in the plugin: doorbells by id, the steward kept on unacked incidents,
compaction keeping their ids, and the task seed blocking the stopped stage."""

import json
import os
import subprocess
import sys
import time

from conftest import PLUGIN, ROOT

from scrape_harness import incidents

GUARD = PLUGIN / "hooks" / "guard.py"


def hook(gate, payload, tmp_path, timeout=30):
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(ROOT),
        "SCRAPE_HARNESS_PIPELINE": str(ROOT / "pipeline.toml"),
        "EXAMPLE_STATE": str(tmp_path / "state"),
    }
    return subprocess.run(
        [sys.executable, "-I", str(GUARD), gate],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def test_the_steward_is_kept_working_until_every_incident_is_acked(data_dir, tmp_path):
    ev = {
        "hook_event_name": "SubagentStop",
        "agent_type": "scrape-harness:run-steward",
        "stop_hook_active": False,
    }
    assert hook("steward", ev, tmp_path).returncode == 0  # nothing open: it may stop
    inc = incidents.open_incident(5, "STOPPED: http_403 at x", "run-1", "fetch-ads")
    r = hook("steward", ev | {"stop_hook_active": True}, tmp_path)
    assert r.returncode == 2 and inc["id"] in r.stderr and "incidents ack" in r.stderr
    incidents.ack(inc["id"], "reported to the operator; he clears it in browser open")
    assert hook("steward", ev, tmp_path).returncode == 0


def test_teammate_idle_holds_only_the_run_steward(data_dir, tmp_path):
    incidents.open_incident(8, "egress refused", None, "crawl-lists")
    assert (
        hook(
            "teammate", {"hook_event_name": "TeammateIdle", "teammate_name": "researcher"}, tmp_path
        ).returncode
        == 0
    )
    held = hook("teammate", {"hook_event_name": "TeammateIdle", "teammate_name": "run-steward"}, tmp_path)
    assert held.returncode == 2 and "egress" in held.stderr


def test_compaction_keeps_open_incident_ids(data_dir, tmp_path):
    assert hook("precompact", {"hook_event_name": "PreCompact", "trigger": "auto"}, tmp_path).stdout == ""
    inc = incidents.open_incident(6, "cooling down", "run-2", "crawl-lists")
    r = hook(
        "precompact",
        {"hook_event_name": "PreCompact", "trigger": "auto", "custom_instructions": None},
        tmp_path,
    )
    assert r.returncode == 0 and inc["id"] in r.stdout and "Preserve verbatim" in r.stdout


def test_the_seed_makes_an_incident_task_that_blocks_its_stage(data_dir, tmp_path):
    inc = incidents.open_incident(5, "STOPPED", "run-3", "fetch-ads")
    ctx = json.loads(hook("session", {}, tmp_path).stdout)["hookSpecificOutput"]["additionalContext"]
    seed = json.loads(ctx.split("\n", 1)[1])
    task = next(t for t in seed if t["subject"].startswith(f"Incident {inc['id']}"))
    stage = next(t for t in seed if t["subject"] == "Stage 2/3: fetch-ads")
    assert task["subject"] in stage["blockedBy"] and "incidents ack" in task["description"]


def test_the_monitor_rings_a_new_incident_by_id_once(data_dir, tmp_path):
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(ROOT),
        "SCRAPE_HARNESS_PIPELINE": str(ROOT / "pipeline.toml"),
        "EXAMPLE_STATE": str(tmp_path / "state"),
        "SCRAPE_HARNESS_WATCH_EVERY": "0.2",
    }
    old = incidents.open_incident(7, "politeness", None, "crawl-lists")  # already there: not news
    proc = subprocess.Popen(
        [sys.executable, "-I", str(GUARD), "watch"], stdout=subprocess.PIPE, text=True, env=env
    )
    try:
        time.sleep(1)
        new = incidents.open_incident(5, "STOPPED: http_403", "run-4", "fetch-ads")
        time.sleep(1.5)
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0]
    assert out.count(f"INCIDENT {new['id']}") == 1 and old["id"] not in out
    assert "incidents ack" in out and "runbook-stop" in out


def test_a_stalled_run_opens_a_stall_incident_once(data_dir, tmp_path, monkeypatch):
    import importlib.util

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ROOT))
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    now = time.time()
    st = json.loads((ROOT / "schemas" / "examples" / "status.example.json").read_text())
    stale = dict(st, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1200)))
    lines = g.transitions(dict(st), dict(stale), now)
    assert any("INCIDENT" in ln and "stall" in ln for ln in lines)
    (inc,) = incidents.open_ones()
    assert (
        inc["kind"] == "stall" and inc["run_id"] == st["run_id"] and inc["code"] is None
    )  # schema-valid (read validates)
    g.transitions(dict(st), dict(stale), now)
    assert len(incidents.open_ones()) == 1  # once per run while open
