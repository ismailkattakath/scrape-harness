"""The RUN CARD (operator request, 2026-11-07, ADR-0011): what this run is, where it sits in the
pipeline, what it reads and writes, how big it is, who started it, and where its live log is.

Printed to STDERR at the start of every run (stdout stays clean JSON). It renders with `rich`
(Panel + Table) on a terminal, and as plain `key: value` lines when stderr is not a TTY or NO_COLOR
is set. The same card is the first `run_card` line of the run's log, and `run.v1.card`.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import shlex
import shutil
import socket
import subprocess
import sys

from . import pipeline

# Stages, utilities and each command's (root, template) I/O come from pipeline.toml (pipeline.py).
# Templates are filled from the run's REAL context (run, queue, site, ads, ab_at, plan_hash); an entry
# whose values do not exist for this invocation (e.g. no run yet at --plan) is left out, so a card
# never shows a placeholder.


def stages() -> list[str]:
    return pipeline.current().stage_names


def stage(command: str) -> str:
    names = stages()
    if command in names:
        return f"{names.index(command) + 1}/{len(names)}"
    return "utility"


def _fill(entries: list, ctx: dict) -> list[str]:
    out = []
    for root, template in entries:
        try:
            out.append(f"{root}:{template.format(**ctx)}")
        except KeyError:
            continue  # a value this invocation does not have: leave the entry out, never a placeholder
    return out


def io(command: str, ctx: dict | None = None, mode: str = "run") -> tuple[list[str], list[str]]:
    """(inputs, outputs) with every path real. mode: run | plan | dry-run."""
    pl = pipeline.current()
    c = pl.command(command)
    ins, outs = (c.inputs, c.outputs) if c else ((), ())
    ctx = {k: v for k, v in (ctx or {}).items() if v is not None}
    if mode == "plan":
        outs = pl.plan_outputs
    elif mode == "dry-run":
        outs = []
    return _fill(ins, ctx), _fill(outs, ctx)


def parent_process() -> str:
    """'claude' when run from Claude Code (its CLAUDECODE env, or a parent process named claude),
    else the nearest parent process name."""
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    try:
        import psutil

        chain = [p.name() for p in psutil.Process().parents()]
    except Exception:  # noqa: BLE001 - a missing parent name must never break a run
        return "unknown"
    if any("claude" in n.lower() for n in chain):
        return "claude"
    return chain[0] if chain else "unknown"


def build_rev() -> str:
    """The li build's git revision: baked into the Nix wrapper, or read from the dev checkout."""
    if pipeline.knob("BUILD_REV"):
        return pipeline.knob("BUILD_REV")
    root = os.environ.get("PRJ_ROOT")
    if root:
        try:
            out = subprocess.run(
                ["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
            )
            dirty = subprocess.run(
                ["git", "-C", root, "status", "--porcelain"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            if out.returncode == 0:
                return out.stdout.strip() + ("-dirty" if dirty else "")
        except Exception:  # noqa: BLE001
            pass
    return "unknown"


def site_code() -> dict[str, str]:
    """sha256 of the code a site overlay puts on sys.path: each python_path dir OUTSIDE the consumer's repo
    (e.g. a site plugin kept in the data folder), over every *.py under it (relative path + bytes, sorted;
    __pycache__ skipped). It goes on the card so a changed or tampered copy shows. {} when there is none."""
    pl = pipeline.current()
    if not pl.overlay:
        return {}
    root, out = pl.path.parent.resolve(), {}
    for d in pl.python_path:
        if d.resolve().is_relative_to(root):
            continue
        h = hashlib.sha256()
        for f in sorted(p for p in d.rglob("*.py") if "__pycache__" not in p.parts):
            h.update(str(f.relative_to(d)).encode() + b"\0" + f.read_bytes() + b"\0")
        out[str(d)] = h.hexdigest()
    return out


def tail_line(abs_path: str) -> str:
    """shlex.quote: the DATA path has spaces ("My Drive/..."), and may hold ' or $; this survives all."""
    return f"tail -f {shlex.quote(abs_path)} | jq -c ."


def copy_to_clipboard(text: str) -> bool:
    """pbcopy when present (macOS); silently skipped elsewhere or with <prefix>_NO_CLIPBOARD=1."""
    if pipeline.knob("NO_CLIPBOARD") == "1":
        return False
    exe = shutil.which("pbcopy")
    if not exe:
        return False
    try:
        subprocess.run([exe], input=text, text=True, timeout=5, check=True)
        return True
    except Exception:  # noqa: BLE001 - a clipboard failure never stops a run
        return False


def make(
    command: str,
    purpose: str,
    run_id: str | None = None,
    *,
    ctx: dict | None = None,
    mode: str = "run",
    roots: dict | None = None,
    planned: dict | None = None,
    per_minute: float | None = None,
    variant: str | None = None,
    backend: str | None = None,
    plan_hash: str | None = None,
    approval_id: str | None = None,
    approval: str | None = None,
    egress: dict | str | None = None,
    log_abs: str | None = None,
    blocking: str | None = None,
    proxy: str | None = None,
    selection: str | None = None,
) -> dict:
    from . import store

    ctx = {
        "run": run_id,
        "run_file": store.safe_name(run_id) if run_id else None,
        "plan_hash": plan_hash,
        **(ctx or {}),
    }
    ins, outs = io(command, ctx, mode)
    total = sum((planned or {}).values())
    card = {
        "run_id": run_id,
        "command": command,
        "purpose": purpose,
        "stage": stage(command),
        "mode": mode,
        "roots": {k: str(v) for k, v in (roots or {}).items()},
        "inputs": ins,
        "outputs": outs,
        "scale": {
            "planned": planned or {},
            "per_minute": per_minute,
            "eta_s": round(total * 60 / per_minute) if planned and per_minute else None,
        },
        "triggered_by": {
            "user": getpass.getuser(),
            "host": socket.gethostname(),
            "parent": parent_process(),
            "li_rev": build_rev(),
            "plan_hash": plan_hash,
            "approval_id": approval_id,
            "approval": approval,  # a stage approved by flag, not by a signed plan (geocode)
            "variant": variant,
            "backend": backend,
            "egress": egress,
            **({"proxy": proxy} if proxy else {}),  # host:port only
            **({"site_code": sc} if (sc := site_code()) else {}),  # overlay code outside the repo
        },
        "live_log": None,
    }
    if blocking:  # browser backends only
        card["scale"]["blocking"] = blocking
    if selection:  # fetch-ads: the fresh-sighting gate and the lanes (other cards are unchanged)
        card["scale"]["selection"] = selection
    if log_abs:
        line = tail_line(log_abs)
        card["live_log"] = {"path": log_abs, "tail": line, "copied": copy_to_clipboard(line)}
    return card


def _eta(s: int | None) -> str:
    if s is None:
        return "none (no planned requests)"
    h, rem = divmod(s, 3600)
    return (f"{h} h " if h else "") + f"{rem // 60} min {rem % 60} s"


def _scale(card: dict, planned: str) -> str:
    sc = card["scale"]
    n = sum(sc["planned"].values())
    rate = f" at {sc['per_minute']}/min, one at a time" if sc["per_minute"] else ""
    if card.get("mode") in ("plan", "dry-run"):  # nothing is sent now: say what a real run WOULD send
        return f"would send {n} requests ({planned}){rate}; none sent now" if planned else "none sent now"
    return (f"{n} requests ({planned})" if planned else "no requests") + rate


def _eta_row(card: dict) -> str:
    sc = card["scale"]
    if sc["eta_s"] is None and sum(sc["planned"].values()) and not sc["per_minute"]:
        return "unknown (requests planned, no rate set)"
    if card.get("mode") in ("plan", "dry-run"):
        if sc["eta_s"] is None:
            return "none now (no request planned)"
        return f"would take ~{_eta(sc['eta_s'])} at {sc['per_minute']}/min"
    return _eta(sc["eta_s"])


def rows(card: dict) -> list[tuple[str, str]]:
    sc, tb, ll = card["scale"], card["triggered_by"], card["live_log"]
    planned = ", ".join(f"{k} {v}" for k, v in sc["planned"].items() if v)
    egress = tb["egress"]
    egress = (
        f"{egress.get('loc')} / {egress.get('colo')}"
        if isinstance(egress, dict)
        else (egress or "not checked")
    )
    out = [
        ("Run", card["run_id"] or f"none ({'plan only' if 'PLAN' in card['purpose'] else 'no crawl run'})"),
        ("What", f"{card['command']}: {card['purpose']}"),
        (
            "Stage",
            card["stage"] if card["stage"] == "utility" else f"{card['stage']}  ({' → '.join(stages())})",
        ),
        ("Roots", "\n".join(f"{k} = {v}" for k, v in card["roots"].items()) or "none"),
        ("Inputs", "\n".join(card["inputs"]) or "none"),
        ("Outputs", "\n".join(card["outputs"]) or "none"),
        ("Scale", _scale(card, planned)),
        *([("Blocking", sc["blocking"])] if sc.get("blocking") else []),
        *([("Selection", sc["selection"])] if sc.get("selection") else []),
        ("ETA", _eta_row(card)),
        (
            "Triggered by",
            f"{tb['user']}@{tb['host']} via {tb['parent']}; li {tb['li_rev']}"
            + (f"; variant {tb['variant']}, backend {tb['backend']}" if tb["variant"] else ""),
        ),
        (
            "Approval",
            f"plan {tb['plan_hash']}" + (f"\napproval {tb['approval_id']}" if tb["approval_id"] else "")
            if tb["plan_hash"]
            else tb["approval"]
            if tb.get("approval")
            else "none needed (offline)"
            if tb["backend"] in (None, "fixture")
            else "none",
        ),
        ("Egress", egress + (f" via proxy {tb['proxy']}" if tb.get("proxy") else "")),
        *(
            [("Site code", "\n".join(f"{d}  sha256 {h[:16]}" for d, h in tb["site_code"].items()))]
            if tb.get("site_code")
            else []
        ),
    ]
    if ll:
        out += [
            ("Live log", ll["path"]),
            ("Follow it", ll["tail"] + ("\n(copied to clipboard)" if ll["copied"] else "")),
        ]
    return out


def render(card: dict, stream=None, width: int | None = None) -> str:
    """Write the card to `stream` (stderr) and return the text written."""
    stream = stream or sys.stderr
    plain = os.environ.get("NO_COLOR") is not None or not getattr(stream, "isatty", lambda: False)()
    if plain:
        body = "".join(f"{k}: {v.replace(chr(10), chr(10) + ' ' * (len(k) + 2))}\n" for k, v in rows(card))
        text = f"== RUN CARD ==\n{body}"
        stream.write(text)
        stream.flush()
        return text
    from rich import box
    from rich.console import Console
    from rich.table import Table

    table = Table(title="RUN CARD", box=box.SQUARE, show_lines=True, title_style="bold")
    table.add_column("Field", style="bold", no_wrap=True)
    table.add_column("Value", overflow="fold")
    for k, v in rows(card):
        table.add_row(k, v)
    console = Console(file=stream, highlight=False, width=width, force_terminal=True)
    with console.capture() as cap:
        console.print(table)
    text = cap.get()
    stream.write(text)
    stream.flush()
    return text
