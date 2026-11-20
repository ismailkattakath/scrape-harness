"""`<cli> preflight <command ...>`: what a run needs, checked BEFORE the operator's Touch ID (preflight.v1).

A bounded refresh once stopped AFTER the approval, on a paid step whose secret env var was
unset. So pipeline.toml declares, per stage or utility, the env var NAMES a command needs (`secrets_env`), and
this module checks, offline and read-only:

  secret_env  each needed env var is set and non-empty: PRESENT or MISSING. By NAME only: a value is never
              printed, logged, hashed or measured (no length either).
  signers     the approval signers file (<PREFIX>_APPROVAL_SIGNERS) names a readable file with the signer line;
              named by its env var, never by its path. Checked for networked commands and for refresh.
  data_root   the DATA root is set (the cooldown and breaker readers need it).
  cooldown    the site has no active cooldown (crawl.cooldown_until, the existing reader). Site commands only.
  breaker     the site has no breaker latch (crawl.latch_stop, the existing reader). Site commands only.
  egress      ONLY with --egress: the existing egress preflight (a request to the trace endpoint, never the
              site). Default OFF, so a preflight is fully offline unless asked.

`preflight refresh` checks every declared [crawl.refresh] step (secrets of each, and the sites of its site
steps). Exit 0 when every check passes, 2 when any does not; `missing` names each failing check. `refresh`
itself runs `secret_gaps()` before it plans, claims or spends anything (refresh.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from . import approvals, contracts, crawl, pipeline, store

EXIT_MISSING = 2
OWN_FLAGS = ("--json", "--egress")  # preflight's own, wherever they appear (no networked command takes them)
PASS = ("present", "ok", "skipped")


def needed_secrets(names: list[str]) -> dict[str, list[str]]:
    """env var name -> the commands (sorted) that declare it, for these declared command names."""
    pl, out = pipeline.current(), {}
    for n in names:
        c = pl.command(n)
        for env in c.secrets_env if c else ():
            out.setdefault(env, [])
            if n not in out[env]:
                out[env].append(n)
    return {k: sorted(v) for k, v in out.items()}


def secret_rows(needed: dict[str, list[str]]) -> list[dict]:
    """Presence only: set and non-empty. Nothing about the value leaves this function."""
    return [
        {
            "kind": "secret_env",
            "name": k,
            "status": "present" if os.environ.get(k) else "missing",
            "needed_by": v,
        }
        for k, v in needed.items()
    ]


def secret_gaps(names: list[str]) -> list[str]:
    """The needed env var names that are unset or empty (refresh refuses on any)."""
    return [r["name"] for r in secret_rows(needed_secrets(names)) if r["status"] == "missing"]


def _signers() -> dict:
    name = pipeline.knob_name("APPROVAL_SIGNERS")
    try:
        approvals.signer_key()  # the file is readable AND holds the signer line; never its path in a message
    except approvals.NotApproved as e:
        return {"kind": "signers", "name": name, "status": "missing", "detail": str(e)}
    return {"kind": "signers", "name": name, "status": "ok"}


def _site_rows(sites: list[str]) -> list[dict]:
    env = store.data_env()
    try:
        store.data_dir()
    except store.DataDirError as e:
        return [{"kind": "data_root", "name": env, "status": "missing", "detail": str(e).split(". ")[0]}]
    rows = [{"kind": "data_root", "name": env, "status": "ok"}]
    for site in sites:
        until = crawl.cooldown_until(site)
        rows.append(
            {"kind": "cooldown", "name": site, "status": "blocked", "detail": f"cooling down until {until}"}
            if until
            else {"kind": "cooldown", "name": site, "status": "ok"}
        )
        stop = crawl.latch_stop(site)
        rows.append(
            {"kind": "breaker", "name": site, "status": "blocked", "detail": str(stop)}
            if stop
            else {"kind": "breaker", "name": site, "status": "ok"}
        )
    return rows


def _egress(asked: bool) -> dict:
    if not asked:
        return {"kind": "egress", "name": "egress", "status": "skipped", "detail": "not asked (--egress)"}
    from . import egress, proxy

    ps = proxy.proxy_set()
    if ps and len(ps.urls) > 1:  # the run's egress is the proxy its plan picks; none is picked yet
        return {"kind": "egress", "name": "egress", "status": "skipped",
                "detail": f"[crawl.proxy] lists {len(ps.urls)} proxies; the plan picks one and the run checks it"}  # fmt: skip
    via = "via [crawl.proxy]" if ps else "direct"
    try:
        rec = egress.preflight()  # through the one configured proxy, as a run's own preflight is
    except Exception as e:  # noqa: BLE001 - any failure to learn the egress is a refusal (fails closed)
        msg = str(e) if isinstance(e, egress.EgressRefused) else f"egress check failed ({type(e).__name__})"
        return {"kind": "egress", "name": "egress", "status": "blocked", "detail": f"{msg} ({via})"}
    return {"kind": "egress", "name": "egress", "status": "ok", "detail": f"{rec.get('loc', '')} ({via})"}


def _site_of(name: str, args: list[str]) -> str:
    """The site a site command would send to: its --source's site, else the consumer's default."""
    from . import refresh

    ns = refresh._parse(name, tuple(args))  # noqa: SLF001 - the real parser; a bad argument exits 2 here
    src = getattr(ns, "source", None)
    if not src:
        return pipeline.consumer().default_site
    try:
        from .interfaces import load

        return load("sources", src).site
    except Exception:  # noqa: BLE001 - an unloadable source still names a site to check
        return src


def resolve(words: list[str]) -> tuple[str, list[str]]:
    """(command name, its args) from `preflight <command ...>`: a two-word declared name wins."""
    pl = pipeline.current()
    if len(words) >= 2 and pl.command(" ".join(words[:2])):
        return " ".join(words[:2]), words[2:]
    return words[0], words[1:]


def check(name: str, args: list[str] | None = None, egress: bool = False) -> dict:
    """The preflight.v1 result for `<cli> <name> <args>` (name "refresh": the whole declared chain)."""
    pl, args = pipeline.current(), list(args or [])
    if name == "refresh":
        if not pl.refresh:
            raise SystemExit(f"{pl.path} declares no [crawl.refresh] steps; there is nothing to preflight")
        from . import refresh

        try:  # a resume: only the secrets of its unfinished steps, as `refresh` checks them
            st = refresh.unfinished()
        except store.DataDirError:  # no roots: the data_root row below names it
            st = None
        names = refresh.steps_to_run(pl, st)
        sites = sorted(
            {_site_of(s.name, list(s.args)) for s in pl.refresh if pl.command(s.name).network == "site"}
        )
        networked = True  # a refresh always asks for one bounded approval
    else:
        c = pl.command(name)
        network = c.network if c else "none"
        site = _site_of(name, args)  # parses with the real parser: an unknown command or argument exits 2
        sites = [site] if network == "site" else []
        names, networked = [name], network != "none"
    rows = secret_rows(needed_secrets(names))
    if networked:
        rows.append(_signers())
    if sites:
        rows += _site_rows(sites)
    rows.append(_egress(egress))
    failed = [r["name"] for r in rows if r["status"] not in PASS]
    return contracts.validate(
        "preflight.v1",
        {
            "schema": "preflight.v1",
            "command": name,
            "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ok": not failed,
            "missing": list(dict.fromkeys(failed)),
            "checks": rows,
        },
    )


def table(d: dict) -> str:
    head = f"preflight {d['command']}: " + ("ok" if d["ok"] else f"MISSING {', '.join(d['missing'])}")
    lines = [head]
    for r in d["checks"]:
        status = r["status"] if r["status"] in PASS else r["status"].upper()
        tail = f"needed by {', '.join(r['needed_by'])}" if r.get("needed_by") else r.get("detail", "")
        lines.append(f"  {r['kind']:<10} {r['name']:<28} {status:<8} {tail}".rstrip())
    return "\n".join(lines)


def cmd(args: argparse.Namespace) -> int:
    words = list(args.command)
    as_json = args.json or "--json" in words
    egress = args.egress or "--egress" in words
    words = [w for w in words if w not in OWN_FLAGS]
    if not words:
        print("preflight needs a command, e.g. `preflight refresh`", file=sys.stderr)
        return EXIT_MISSING
    name, rest = resolve(words)
    d = check(name, rest, egress)
    print(json.dumps(d, sort_keys=True) if as_json else table(d))
    return 0 if d["ok"] else EXIT_MISSING
