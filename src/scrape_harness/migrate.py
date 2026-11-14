"""`<cli> migrate`: split an old single root into DATA (synced) and STATE (local), by COPYING.

Never moves, never deletes, never overwrites: every regular file is copied (mode and mtime kept)
to its new home and its sha256 verified against the source; a destination file that already exists
is left alone (identical: skipped; different: a conflict that fails the run). The operator removes
the old copy himself after checking the manifest.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from . import store

SKIP_NAMES = ("SingletonLock", "SingletonSocket", "SingletonCookie")  # a running Chromium's live locks


def os_junk(name: str) -> bool:
    """macOS Finder litter: never data, never copied (a real dry-run planned 8 .DS_Store into Drive)."""
    return name in (".DS_Store", "Icon\r") or name.startswith("._")


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan(src: Path, to_data: Path, to_state: Path) -> list[dict]:
    rows = []
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if p.is_dir() and not p.is_symlink():
            continue
        if os_junk(p.name):
            rows.append({"from": str(rel), "to": None, "action": "skip", "reason": "os_junk"})
            continue
        if not p.is_file() or p.is_symlink() or p.name in SKIP_NAMES:
            rows.append({"from": str(rel), "to": None, "action": "skip", "reason": "not_regular"})
            continue
        dest_root = to_state if rel.parts[0] in store.STATE_PARTS else to_data
        dest = dest_root / rel
        row = {
            "from": str(rel),
            "to": str(dest),
            "root": "state" if dest_root == to_state else "data",
            "bytes": p.stat().st_size,
            "sha256": _sha(p),
        }
        if dest.exists():
            row["action"] = "exists-identical" if _sha(dest) == row["sha256"] else "CONFLICT"
        else:
            row["action"] = "copy"
        rows.append(row)
    return rows


def run(src: Path, to_data: Path, to_state: Path, dry_run: bool) -> tuple[list[dict], int]:
    to_data, to_state, src = to_data.expanduser().resolve(), to_state.expanduser().resolve(), src.resolve()
    store.check_state_root(to_state, to_data)
    if store.is_cloud_synced(to_state):
        raise store.DataDirError("refusing: --to-state is a cloud-sync folder")
    if src in (to_data, to_state) or src in to_data.parents or src in to_state.parents:
        raise store.DataDirError("refusing: a destination is the source or inside it")
    rows = plan(src, to_data, to_state)
    if any(r["action"] == "CONFLICT" for r in rows):
        return rows, 2
    if dry_run:
        return rows, 0
    for r in rows:
        if r["action"] != "copy":
            continue
        dest = Path(r["to"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        if r["root"] == "state":
            (to_state).chmod(0o700)
        shutil.copy2(src / r["from"], dest)
        got = _sha(dest)
        r["verified"] = got == r["sha256"]
        if not r["verified"]:
            return rows, 2
    return rows, 0


def manifest(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r, sort_keys=True) for r in rows)
