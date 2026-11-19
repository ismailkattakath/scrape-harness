"""The one reader of `run_correction` events. A run record is never rewritten: a correction is an appended
event.v1 line (`crawl.append_correction`), and every reader that needs a run's corrected view asks here.

Pure: the caller passes the records in; nothing here reads a file or the clock.

Fields read (any other field, e.g. `stop`, is left to its own reader):
  status  reconciled {"status": ...}: the run's END, as `crawl.correct_status` writes it;
  gone    reconciled {"void": true} drops the run's gone events (an operator decision they were wrong);
          reconciled {"quarantine": true} holds them as doubtful (a breaker trip): no tombstone, no all-clear;
          any other reconciled value (e.g. {"quarantine": false}) restores them. With "scope": "weak" (a breaker
          trip, `breaker clear --resolve void`) the effect covers only the run's WEAK gones: a positive marker
          (`breaker.POSITIVE`: the site's expired notice, a 410) stands as recorded;
  breaker reconciled {"clear": true, ...}: the operator cleared the run's breaker latch (read by `breaker.latched`).
The LATEST correction of a (run, field), in file order, wins: the log is append-only, so its order (not a
second-resolution timestamp) is the authority."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .breaker import POSITIVE


def latest(events: Iterable[dict], field: str) -> dict[str, dict]:
    """run_id -> the `reconciled` object of its latest `field` correction, in file order."""
    out: dict[str, dict] = {}
    for e in events:
        c = e.get("correction") or {}
        if e.get("kind") == "run_correction" and c.get("field") == field:
            out[e["run_id"]] = c.get("reconciled") or {}
    return out


def gone_mode(reconciled: Mapping | None) -> str | None:
    """'void', 'quarantine' or None (the gone events count as recorded). Void wins: it is the stronger claim."""
    if not reconciled:
        return None
    if reconciled.get("void"):
        return "void"
    if reconciled.get("quarantine"):
        return "quarantine"
    return None


def gone_effect(fixed: Mapping | None, reason: str | None) -> str | None:
    """What a run's gone correction does to ONE of its gone events: 'void', 'quarantine' or None (it counts)."""
    if not fixed or not fixed.get("gone"):
        return None
    if fixed.get("gone_scope") == "weak" and reason in POSITIVE:
        return None
    return fixed["gone"]


def apply(runs: Iterable[Mapping], events: Iterable[dict]) -> dict[str, dict]:
    """run_id -> {status, list_status, gone, gone_scope}: each run's record with its corrections applied (read a
    gone event through `gone_effect`). A run named only by a correction (its doc absent) still gets an entry,
    with status and list_status None."""
    events = list(events)
    status = latest(events, "status")
    gone = latest(events, "gone")
    out: dict[str, dict] = {}
    for doc in runs:
        rid = doc["run_id"]
        out[rid] = {
            "status": (status.get(rid) or {}).get("status", doc.get("status")),
            "list_status": (doc.get("list") or {}).get("status"),
            "gone": gone_mode(gone.get(rid)),
            "gone_scope": (gone.get(rid) or {}).get("scope"),
        }
    for rid in status.keys() | gone.keys():
        if rid not in out:
            out[rid] = {
                "status": (status.get(rid) or {}).get("status"),
                "list_status": None,
                "gone": gone_mode(gone.get(rid)),
                "gone_scope": (gone.get(rid) or {}).get("scope"),
            }
    return out
