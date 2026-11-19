"""STATE/ops/incidents.jsonl (incident.v1): the reliability protocol's record (SYNTHESIS §5).

Every stop or refusal of a run opens an incident (challenge 5, cooldown 6, politeness 7, egress 8,
approval 10, unreachable 12, incomplete 13, parse 14, or a breaker's gone_storm at 14); the plugin's monitor opens one for a stalled run. An incident stays OPEN until an `acked`
line with its id is appended (`<cli> incidents ack ID --note ...`). Append-only: nothing is edited or
deleted, so the file is the audit of who looked and what they did. Agents learn of an incident from the
monitor's doorbell line, the session's task seed and the plugin hooks, which all read this file.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

from . import contracts, store

KIND = {
    5: "challenge",
    6: "cooldown",
    7: "politeness",
    8: "egress",
    10: "approval",
    12: "unreachable",
    13: "incomplete",
    14: "parse",  # a breaker trip passes kind="gone_storm" explicitly
}


def path() -> Path:
    p = store.state_dir() / "ops"
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p / "incidents.jsonl"


def _append(rec: dict) -> dict:
    contracts.validate("incident.v1", rec)
    fd = os.open(path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)
    return rec


def read() -> list[dict]:
    p = path()
    if not p.exists():
        return []
    return [
        contracts.validate("incident.v1", json.loads(x), f"{p.name}:{n}")
        for n, x in enumerate(p.read_text().splitlines(), 1)
        if x.strip()
    ]


def open_ones() -> list[dict]:
    lines = read()
    acked = {r["id"] for r in lines if r["event"] == "acked"}
    return [r for r in lines if r["event"] == "opened" and r["id"] not in acked]


def _now() -> datetime:
    return datetime.now(UTC)


def open_incident(
    code: int | None, reason: str, run_id: str | None, stage: str | None, kind: str | None = None
) -> dict | None:
    """Open one, unless the same stop is already open (same kind, run and stage): a re-raised stop
    must not ring twice. Never raises into the caller: a stop must stay a stop."""
    try:
        kind = kind or KIND.get(code)
        if kind is None:
            return None
        if any(r["kind"] == kind and r["run_id"] == run_id and r["stage"] == stage for r in open_ones()):
            return None
        now = _now()
        return _append(
            {
                "schema": "incident.v1",
                "id": f"INC-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}",
                "event": "opened",
                "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kind": kind,
                "code": code,
                "run_id": run_id,
                "stage": stage,
                "reason": reason.splitlines()[0][:500] if reason else kind,
            }
        )
    except Exception:  # noqa: BLE001 - the record must never turn a stop into a crash
        return None


def ack(incident_id: str, note: str, by: str = "agent") -> dict:
    if not note.strip():
        raise SystemExit("an ack needs a --note saying what was done or decided")
    if incident_id not in {r["id"] for r in open_ones()}:
        known = {r["id"] for r in read()}
        raise SystemExit(
            f"{incident_id}: " + ("already acked" if incident_id in known else "no such incident")
        )
    return _append(
        {
            "schema": "incident.v1",
            "id": incident_id,
            "event": "acked",
            "at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "by": by,
            "note": note.strip(),
        }
    )
