"""Keep the machine awake while an approved networked run lives (macOS only).

A bounded refresh once took 2 h 40 min for a 1 h plan: the Mac's idle "Maintenance Sleep" froze the
process six times for ~15 min each (pmset log, 2026-11-20), so wall time, approval expiry and politeness
all drifted. macOS ships the fix: `caffeinate -i -w PID` holds a PreventUserIdleSystemSleep assertion
until PID exits, so the hold can never outlive the run, even on a crash or kill -9 of the run.

`hold()` is called once the operator's approval has been checked (cli.need_network, refresh). It is a
no-op off macOS, when /usr/bin/caffeinate is absent, or when <PREFIX>_NO_CAFFEINATE=1. Idempotent per
process. The display may still sleep (-i, not -d): only the system is kept awake.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import pipeline

CAFFEINATE = Path("/usr/bin/caffeinate")  # the system tool (not on a nix PATH)
_held: set[int] = set()  # pids this process already holds for (a fork gets its own pid, so its own hold)


def _spawn(argv: list[str]) -> None:
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)  # fmt: skip


def hold() -> bool:
    """Hold the system awake until this process exits. True when a hold was started now."""
    pid = os.getpid()
    if pid in _held or sys.platform != "darwin" or not CAFFEINATE.exists():
        return False
    if pipeline.knob("NO_CAFFEINATE") == "1":
        return False
    _held.add(pid)
    _spawn([str(CAFFEINATE), "-i", "-w", str(pid)])
    return True
