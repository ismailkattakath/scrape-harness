"""Typed JSONL storage under the consumer's DATA root (pipeline.toml [consumer].data_env). Validate on read and on write; never outside.

Append-only files hold facts (observations, listings, findings, events). Files under
derived/ are rebuilt from them and hold no fact of their own.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from . import contracts, pipeline


def data_env() -> str:
    return pipeline.consumer().data_env


def state_env() -> str:
    return pipeline.consumer().state_env


def files() -> dict[str, tuple[str, str]]:
    """kind -> (path under DATA, record schema), from pipeline.toml's [files]."""
    return pipeline.current().files


class DataDirError(RuntimeError):
    pass


# Machine-local state (ADR-0009): never synced. Everything else under the data root is data (Drive).
STATE_PARTS = ("browser-profile", "crawlee", "plans", "approvals")
CLOUD_MARKERS = ("google drive", "googledrive", "dropbox", "onedrive", "icloud", "mobile documents")


def _in_git_tree(root: Path) -> Path | None:
    return next((p for p in (root, *root.parents) if (p / ".git").exists()), None)


def is_cloud_synced(path: Path) -> bool:
    """A folder a sync client uploads: ~/Library/CloudStorage/** or a well-known sync folder name."""
    p = path.expanduser().resolve()
    text = str(p).lower()
    return "/library/cloudstorage/" in text + "/" or any(
        m in part.lower() for part in p.parts for m in CLOUD_MARKERS
    )


def data_dir() -> Path:
    ENV = data_env()
    raw = os.environ.get(ENV)
    if not raw:
        raise DataDirError(f"{ENV} is not set. Point it at the data folder (outside any git repo).")
    root = Path(raw).expanduser().resolve()
    if repo := _in_git_tree(root):
        raise DataDirError(f"{ENV} resolves inside a git work tree ({repo}); data never lives in a repo.")
    root.mkdir(parents=True, exist_ok=True)
    _hint_unmigrated(root)
    return root


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / pipeline.consumer().state_dirname


def check_state_root(state: Path, data: Path | None) -> None:
    """The state root (browser profile, queues, approvals) must stay local and separate from data."""
    STATE_ENV, ENV = state_env(), data_env()
    if is_cloud_synced(state):
        raise DataDirError(
            f"{STATE_ENV} ({state}) is inside a cloud-sync folder; the browser profile and the "
            "approval ledger must never sync. Point it at a local folder."
        )
    if repo := _in_git_tree(state):
        raise DataDirError(f"{STATE_ENV} resolves inside a git work tree ({repo}).")
    if data is not None:
        if state == data:
            raise DataDirError(
                f"{STATE_ENV} and {ENV} are the same folder; state must be separate (and local)."
            )
        if data in state.parents:
            raise DataDirError(f"{STATE_ENV} is inside {ENV}; data may be synced, state must not be.")


def state_dir() -> Path:
    state = Path(os.environ.get(state_env()) or default_state_dir()).expanduser().resolve()
    check_state_root(state, data_dir())
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    return state


_hinted = False


def _hint_unmigrated(root: Path) -> None:
    """Old single-root layout: say so once, move nothing."""
    global _hinted
    if _hinted or os.environ.get(state_env()):
        return
    old = [p for p in STATE_PARTS if (root / p).exists()]
    if old:
        _hinted = True
        import sys

        c = pipeline.consumer()
        print(
            f"{c.cli}: {c.data_env} still holds machine state ({', '.join(old)}); run `{c.cli} migrate --to-data "
            f"<DATA> --to-state <STATE> --dry-run` (copies, never moves).",
            file=sys.stderr,
        )


def _file(kind: str) -> tuple[str, str]:
    table = files()
    if kind not in table:
        raise KeyError(f"no data file kind {kind!r} in {pipeline.current().path} [files]")
    return table[kind]


def path(kind: str) -> Path:
    return data_dir() / _file(kind)[0]


def read(kind: str) -> Iterator[dict]:
    p = path(kind)
    if not p.exists():
        return
    name = _file(kind)[1]
    with p.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if line.strip():
                yield contracts.validate(name, json.loads(line), f"{p.name}:{n}")


def append(kind: str, records: Iterable[dict]) -> int:
    """Validate EVERY record first, then append. A bad batch writes nothing."""
    name = _file(kind)[1]
    rows = [contracts.validate(name, r, f"record {i}") for i, r in enumerate(records, 1)]
    if rows:
        p = path(kind)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    return len(rows)


def replace(kind: str, records: Iterable[dict]) -> int:
    """Rewrite a derived file atomically (validate all, write a temp file, rename)."""
    name = _file(kind)[1]
    rows = [contracts.validate(name, r, f"record {i}") for i, r in enumerate(records, 1)]
    p = path(kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))
    tmp.replace(p)
    return len(rows)


def latest(records: Iterable[dict], key: Callable[[dict], object], ts: str) -> dict[object, dict]:
    """Latest record per key by an ISO-8601 UTC timestamp field (lexicographic == chronological)."""
    out: dict[object, dict] = {}
    for r in records:
        k = key(r)
        if k not in out or r[ts] >= out[k][ts]:
            out[k] = r
    return out


def runs_dir() -> Path:
    p = data_dir() / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_name(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def read_json(p: Path, name: str) -> dict:
    return contracts.validate(name, json.loads(p.read_text()), p.name)


def write_json(p: Path, name: str, doc: dict) -> None:
    contracts.validate(name, doc, p.name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    tmp.replace(p)


def write_raw(site: str, ad_id: str, fetched_at: str, html: str) -> dict:
    """Gzip the fetched ad page under raw/<site>/<ad_id>/<fetched_at>.html.gz; return listing.v1 raw_ref.
    sha256 and bytes describe the UNCOMPRESSED HTML, so they do not depend on gzip settings."""
    body = html.encode("utf-8")
    rel = f"raw/{safe_name(site)}/{safe_name(ad_id)}/{safe_name(fetched_at.replace(':', '-'))}.html.gz"
    p = data_dir() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(body, mtime=0))
    tmp.replace(p)
    return {"path": rel, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def read_raw(ref: dict) -> str:
    """The snapshot's HTML, after checking it is the exact bytes the listing was parsed from."""
    body = gzip.decompress((data_dir() / ref["path"]).read_bytes())
    if hashlib.sha256(body).hexdigest() != ref["sha256"] or len(body) != ref["bytes"]:
        raise contracts.ContractError(f"snapshot {ref['path']} does not match its raw_ref (sha256/bytes)")
    return body.decode("utf-8")
