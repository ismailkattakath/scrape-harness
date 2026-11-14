"""`<cli> run`: the declared stages as doit tasks (pydoit), generated from pipeline.toml.

doit gives dependency order, up-to-date checks and partial runs without a second list of stages. The
CLI stays the only network gate: a networked stage run by doit still needs its plan approved, and
refuses (exit 3/10) otherwise; doit then reports that stage as failed and runs nothing after it.

- Order: each stage depends on the one before (task_dep).
- Up-to-date: only OFFLINE stages can be skipped as up to date, and only when every input is a concrete
  DATA file (no template, no directory) and every output a concrete DATA file. Networked stages always
  run when asked: whether the site changed is not something a file timestamp can know.
- doit's own state lives in STATE (never synced): STATE/doit.json; timestamps, not hashes, so a large
  DATA root on a sync client is not re-read every time.

`<cli> refresh` (refresh.py) runs its declared steps through the SAME loader (run(found=..., dep_file=...)):
step_tasks() makes one task per step (its id: position:name), in order, each depending on the one before; a step this refresh
run already completed is up to date (uptodate=[True]), so a rerun resumes at the step that failed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import pipeline, store


def _concrete(entries, data: Path) -> list[str] | None:
    """DATA paths without templates or directories, or None if any entry is not one."""
    out = []
    for root, tpl in entries:
        if root != "DATA" or "{" in tpl or tpl.endswith("/") or " " in tpl or "," in tpl:
            return None
        out.append(str(data / tpl))
    return out


def tasks(pl: pipeline.Pipeline | None = None, extra: list[str] | None = None) -> list[dict]:
    """doit task dicts, one per stage. `extra` is appended to every NETWORKED stage (e.g. a driver)."""
    pl = pl or pipeline.current()
    data = store.data_dir()
    out, prev = [], None
    for c in pl.stages:
        argv = [sys.executable, "-m", "scrape_harness.cli", c.name, *c.args]
        if c.network != "none":
            argv += list(extra or [])
        t = {
            "name": c.name,
            "actions": [argv],
            "task_dep": [prev] if prev else [],
            "verbosity": 2,
            "doc": f"{runcard_stage(pl, c.name)} {c.name} ({c.network})",
        }
        ins, outs = _concrete(c.inputs, data), _concrete(c.outputs, data)
        if c.network == "none" and ins and outs:
            t["file_dep"], t["targets"] = ins, outs
        else:
            t["uptodate"] = [False]
        out.append(t)
        prev = c.name
    return out


def step_tasks(steps: list[tuple[str, object]], done: set[str]) -> list[dict]:
    """doit task dicts for a refresh: (name, python action) in declared order; `done` steps are up to date."""
    out, prev = [], None
    for name, action in steps:
        out.append(
            {
                "name": name.replace(" ", "-").replace(":", "-"),  # doit reserves ':' for subtasks
                "actions": [action],
                "task_dep": [prev] if prev else [],
                "verbosity": 2,
                "doc": f"refresh step {name}",
                "uptodate": [name in done],
            }
        )
        prev = out[-1]["name"]
    return out


def runcard_stage(pl: pipeline.Pipeline, name: str) -> str:
    names = pl.stage_names
    return f"{names.index(name) + 1}/{len(names)}"


def run(
    doit_args: list[str],
    extra: list[str] | None = None,
    found: list[dict] | None = None,
    dep_file: Path | None = None,
) -> int:
    from doit.cmd_base import TaskLoader2
    from doit.doit_cmd import DoitMain

    found = tasks(extra=extra) if found is None else found

    class Loader(TaskLoader2):
        def setup(self, opt_values):
            pass

        def load_doit_config(self):
            return {
                "dep_file": str(dep_file or store.state_dir() / "doit.json"),
                "backend": "json",
                "check_file_uptodate": "timestamp",
                "verbosity": 2,
                # doit's default is the sys.stdout of its IMPORT time; a later run in the same process
                # (a test, or a refresh after another) would write to a stream that may be closed by then
                "outfile": sys.stdout,
            }

        def load_tasks(self, cmd, pos_args):
            from doit.task import dict_to_task

            return [dict_to_task(dict(t)) for t in found]

    return DoitMain(Loader()).run(doit_args)
