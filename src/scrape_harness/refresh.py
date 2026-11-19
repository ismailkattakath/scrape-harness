"""`<cli> refresh`: the ONE on-demand chain, run by doit over pipeline.toml [crawl.refresh].steps, under ONE
single-use BOUNDED Touch ID approval (approvals.py, kind "bounded").

  <cli> refresh                    no live approval for these bounds: build the bounds from [crawl.refresh]
                                   and the options, save them as the plan, print the run card, exit 10;
                                   nothing is sent. The operator signs with `<cli> approve <plan_hash>`.
  <cli> refresh --approve-network  approved: create (or continue) the refresh run's STATE file and run the
                                   steps in declared order with the SAME doit machinery as `<cli> run`.

Each networked step is its own `<cli> <step>` process, with `--approve-network --refresh-run <run_id>`
appended: the step's own gate (cli.gate) then consumes the bounded approval for exactly what that step
sends (approvals.consume_bounded), and every existing safety (egress preflight, cooldown, circuit breaker,
challenge detector, politeness) runs as it always does, inside the step.

A paid step with a paid_gate first runs its own `--dry-run`, reads its paid count from the JSON it prints,
and stops (exit 3, resumable) unless the count is 0 or the operator passed `--<step>-count N` equal to it;
it then runs with `--max-requests N` if it has that option. A paid_exempt step runs as declared.

A crawl-lists step PASSES only on exit 0 AND its run doc (named by the run_id it prints) being a completed
crawl-lists run of THIS refresh with list.status ok AND observations_written > 0 (list_postcondition).
Anything else that exited 0 is recorded as exit 14, so the strict doit chain stops there and fetch-ads never
plans from a list read that cannot be trusted. The end summary shows each crawl-lists step's list{} and each
fetch-ads step's ads{} selection (eligible, lanes, excluded), read from DATA at print time.

Ends. Complete: the approval is spent. TERMINAL (the approval is spent, the run cannot resume): a bound
breach (a networked step's exit 3), a challenge or cooldown stop (5/6), a parse or yield stop (14: what the
site sent cannot be trusted: a step's own exit 14, a breaker trip or latch, or a crawl-lists step that failed
its postcondition), the approval expiring, revoked, or `--abandon`. Every other failure (a crash, a count
mismatch, a data folder refusal (4), an egress refusal (8), politeness (7), an unreachable site (12), an
incomplete crawl (13), a step refused as unapproved (10), e.g. a fetch-ads policy digest that no longer
matches the signed one, which no resume can pass) is RESUMABLE: the next `<cli> refresh --approve-network`
continues the same run id inside the same approval and TTL, skipping the steps it already completed.
A site step's resume consumes its CEILING again (crawl-lists: 1 + max_pages; fetch-ads: its ads): only a
refusal before any site request (6/7/8) refunds the first use. So a crash or 12/13 resumes only while the
bounds leave room for the ceiling twice; otherwise the resume is a bound breach and spends the approval.
Size --max-ad-requests (and the crawl-lists step's --max-pages) with that headroom.

crawl-lists may be declared more than once (one step per list set, e.g. per subcategory): the bounds sign
each set, in step order, and each is crawled ONCE per refresh run, so a crawl-lists step that already used
its set (and was not refunded) is refused as a replay on resume, which spends the approval like any breach.
A step's identity (state file, doit) is position:name, e.g. "2:crawl-lists".

One refresh per run id: an exclusive, non-blocking flock on STATE/refresh/<run_id>.lock is held for the
whole refresh (and a claim lock while a NEW run picks its approval and writes its state); a second
`<cli> refresh` meanwhile exits 10 ("already running") and changes nothing.

The state file STATE/refresh/<run_id>.json (refresh.v1) records the run id, plan_hash, approval id, status
(running | failed | complete | spent), and per step: status, exit code, duration, the step's last JSON
stdout line, and why it failed; plus the approval's headroom (list pages and ad requests used / max).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from . import approvals, pipeline, store

RUN_ID = re.compile(r"^refresh-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
DEFAULT_TTL = "12h"
EXIT_NETWORK = 3
TERMINAL_CODES = {5: "challenge stop", 6: "site cooldown", 14: "parse or yield stop"}
REFETCH_REASONS = ("card_changed", "new", "stale_7d")  # plan.fetch_reason's values


# ── the state file ─────────────────────────────────────────────────────────────────────────────


def _dir() -> Path:
    p = store.state_dir() / "refresh"
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def state_path(run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id or ""):
        raise approvals.NotApproved(f"{run_id!r} is not a refresh run id (refresh-<UTC>-<hex>)")
    return _dir() / f"{run_id}.json"


def load_state(run_id: str) -> dict:
    p = state_path(run_id)
    if not p.is_file():
        raise approvals.NotApproved(f"no refresh run {run_id} (no state file {p.name})")
    return store.read_json(p, "refresh.v1")


def save_state(st: dict) -> None:
    st["updated_at"] = _now()
    store.write_json(state_path(st["run_id"]), "refresh.v1", st)


def states() -> list[dict]:
    out = []
    for p in sorted(_dir().glob("refresh-*.json")):
        try:
            out.append(store.read_json(p, "refresh.v1"))
        except (OSError, ValueError, Exception):  # noqa: BLE001 - a broken file never hides the others
            continue
    return out


def unfinished() -> dict | None:
    """The latest refresh run that can still continue (running or failed)."""
    left = [s for s in states() if s["status"] in ("running", "failed")]
    return left[-1] if left else None


def _now() -> str:
    return datetime.now(UTC).strftime(approvals.FMT)


def new_run_id() -> str:
    return f"refresh-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


class Busy(Exception):  # noqa: N818 - another process holds the lock: exit 10, nothing changed
    pass


@contextlib.contextmanager
def locked(run_id: str | None):
    """An exclusive, NON-blocking flock on STATE/refresh/<run_id>.lock (the run id format checked, like the
    state file's), held while the block runs. None: the claim lock, held while a NEW run picks its approval
    and writes its state, so two new refreshes cannot claim the same approval."""
    path = _dir() / "claim.lock" if run_id is None else state_path(run_id).with_suffix(".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(
                f"refresh run {run_id} is already running" if run_id else "another refresh is starting"
            ) from None
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _new_state(a: dict, steps: list[str], run_id: str | None = None) -> dict:
    run_id = run_id or new_run_id()
    st = {
        "schema": "refresh.v1",
        "run_id": run_id,
        "plan_hash": a["plan_hash"],
        "approval_id": a["approval_id"],
        "created_at": _now(),
        "updated_at": _now(),
        "status": "running",
        "steps": [{"id": step_id(i, n), "name": n, "status": "pending"} for i, n in enumerate(steps)],
    }
    save_state(st)
    return st


def step_id(pos: int, name: str) -> str:
    """A step's identity: its 1-based position + name ("1:crawl-lists", "2:crawl-lists"), since crawl-lists
    may be declared more than once."""
    return f"{pos + 1}:{name}"


def _step(st: dict, pos: int) -> dict:
    return st["steps"][pos]


# ── the bounds: what the ONE signature covers ──────────────────────────────────────────────────


def _parse(name: str, args: tuple[str, ...]) -> argparse.Namespace:
    from . import cli

    return cli.parser().parse_args([*name.split(), *args])


def subcommand_options(name: str) -> set[str]:
    """The option strings a (possibly two-word) subcommand accepts, from the real parser."""
    from . import cli

    p = cli.parser()
    for word in name.split():
        sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))  # noqa: SLF001
        p = sub.choices[word]
    return {o for a in p._actions for o in a.option_strings}  # noqa: SLF001


def configured_proxies() -> tuple[str, ...]:
    from . import proxy

    ps = proxy.proxy_set()
    return ps.urls if ps else ()


def build_bounds(ttl: str = DEFAULT_TTL, max_ad_requests: int | None = None) -> tuple[dict, bool]:
    """(bounds, networked). The bounds of THIS refresh from [crawl.refresh] and the options; the proxy is
    filled in only when exactly one is configured (with several, the plan picks one: pick_proxy)."""
    from . import cli, crawl, egress, plan, policy
    from .interfaces import load

    pl = pipeline.current()
    site = pipeline.consumer().default_site
    variants, backends, rates, concs = [], [], [], []
    override = networked = False
    lists, max_lists, max_ads, reasons, ad_policy = [], 0, 0, [], None
    for st in pl.refresh:
        if st.name not in pipeline.REFRESH_SITE_STEPS:
            continue
        ns = _parse(st.name, st.args)
        cli._polite(ns)  # noqa: SLF001 - over the ceiling without the operator's override: refuse now
        if st.name == "crawl-lists":
            source = load("sources", ns.source)
            site = source.site
            v = cli._variant(ns, source, "list")  # noqa: SLF001
            if cli._networked(ns, [v]):  # noqa: SLF001
                cli._deterministic(ns)  # noqa: SLF001 - the signed sample is the one crawled
            cli._check_pages(ns)  # noqa: SLF001
            lists.append(approvals.list_args(ns))  # one signed set per step
            if ns.pages:
                max_lists += len(ns.pages)
            elif ns.page_count:
                _, pages = plan.systematic_pages(ns.page_count, ns.stride, ns.start, ns.seed)
                max_lists += min(len(pages), ns.max_pages)
            else:
                max_lists += 1 + ns.max_pages  # page 1, then at most max_pages of the sample it plans
        else:
            if ns.supersede:
                raise SystemExit(
                    "a refresh's fetch-ads step never supersedes (--supersede is for one-off runs)"
                )
            v = cli._variant(ns, load("sources", site), "ad")  # noqa: SLF001
            max_ads = ns.sample if ns.sample else ns.max_fetches
            reasons = ["recruit"] if ns.select == "recruit" else list(REFETCH_REASONS)
            ad_policy = policy.from_args(ns).digest()  # the step's SyncPolicy: consume_bounded compares it
        if v not in variants:
            variants.append(v)
        b = cli._backend_name(ns, v)  # noqa: SLF001
        if b not in backends:
            backends.append(b)
        rates.append(ns.max_tasks_per_minute)
        concs.append(ns.concurrency)
        override = override or bool(ns.approve_politeness_override)
        networked = networked or cli._networked(ns, [v])  # noqa: SLF001
    if max_ad_requests is not None:
        if max_ad_requests < 0:
            raise SystemExit("--max-ad-requests must be >= 0")
        max_ads = max_ad_requests
    now = datetime.now(UTC)
    bounds = {
        "command": "refresh",
        "kind": "bounded",
        "site": site,
        "egress_country": egress.home_country(),
        "variants": variants,
        "backends": backends,
        "max_rate_per_minute": max(rates) if rates else crawl.DEFAULT_RATE,
        "concurrency": max(concs) if concs else crawl.CONCURRENCY,
        "politeness_override": override,
        "lists": lists,
        "max_list_pages": max_lists,
        "max_ad_requests": max_ads,
        "fetch_reasons": reasons,
        **({"policy_digest": ad_policy} if ad_policy else {}),  # bounds with no fetch-ads hash as before
        "steps": [s.name for s in pl.refresh],
        "not_before": now.strftime(approvals.FMT),
        "expires_at": (now + approvals.parse_ttl(ttl)).strftime(approvals.FMT),
    }
    proxies = configured_proxies() if networked else ()
    if len(proxies) == 1:
        bounds["proxy"] = proxies[0]
    return bounds, networked


def pick_proxy(bounds: dict, proxies: tuple[str, ...]) -> dict:
    """With several proxies configured, a new plan picks ONE ([crawl.proxy] choice); the signature binds it."""
    if len(proxies) > 1:
        from . import proxy

        bounds = bounds | {"proxy": proxy.choose(proxy.proxy_set())}
    return bounds


# ── running a step ─────────────────────────────────────────────────────────────────────────────


def _exec(argv: list[str]) -> tuple[int, list[dict]]:
    """Run one `<cli> ...` process; stream its stdout through, keep the JSON objects it printed."""
    cmd = [sys.executable, "-m", "scrape_harness.cli", *argv]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    objs: list[dict] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return proc.wait(), objs


EXEC = _exec  # the one seam a test may replace


def _record(run_id: str, pos: int, **kw) -> None:
    st = load_state(run_id)
    _step(st, pos).update({k: v for k, v in kw.items() if v is not None})
    save_state(st)


def _action(run_id: str, pos: int, step: pipeline.RefreshStep, counts: dict[str, int | None]):
    pl = pipeline.current()
    cmd = pl.command(step.name)

    def run() -> bool:
        opts = subcommand_options(step.name)
        argv = [*step.name.split(), *step.args]
        if cmd.network != "none" and "--approve-network" in opts:
            argv.append("--approve-network")
        if step.name in pipeline.REFRESH_SITE_STEPS:
            argv += ["--refresh-run", run_id]
        paid = None
        if step.count_field:
            code, objs = EXEC([*step.name.split(), *step.args, "--dry-run"])
            found = [o[step.count_field] for o in objs if isinstance(o.get(step.count_field), int)]
            if code != 0 or not found:
                why = (
                    f"its --dry-run exited {code}" if code else f"its --dry-run printed no {step.count_field}"
                )
                _record(run_id, pos, status="failed", exit=code or 4, reason=f"paid gate: {why}")
                print(f"refresh STOPPED at {step.name}: paid gate: {why}", file=sys.stderr)
                return False
            paid, given = found[-1], counts.get(step.name)
            if paid != 0 and given != paid:
                why = (
                    f"{step.name} would make {paid} paid requests ({step.count_field}); confirm with "
                    f"`{pl.consumer.cli} refresh --approve-network {step.count_flag} {paid}`"
                    + (f" (you passed {given})" if given is not None else "")
                )
                _record(run_id, pos, status="failed", exit=EXIT_NETWORK, reason=f"paid gate: {why}",
                        paid_count=paid)  # fmt: skip
                print(f"refresh STOPPED at {step.name}: {why}", file=sys.stderr)
                return False
            if "--max-requests" in opts:
                argv += ["--max-requests", str(paid)]
        _record(run_id, pos, status="running")
        t0 = time.monotonic()
        code, objs = EXEC(argv)
        why = list_postcondition(run_id, objs) if step.name == "crawl-lists" and code == 0 else None
        if why:
            from . import crawl

            code = crawl.EXIT_PARSE  # terminal (TERMINAL_CODES): the strict chain never reaches fetch-ads
        _record(
            run_id,
            pos,
            status="done" if code == 0 else "failed",
            exit=code,
            duration_s=round(time.monotonic() - t0, 3),
            summary=objs[-1] if objs else None,
            paid_count=paid,
            reason=None if code == 0 else f"list postcondition: {why}" if why else f"exit {code}",
        )
        if code != 0:
            print(f"refresh STOPPED at {step.name}: exit {code}" + (f" ({why})" if why else ""),
                  file=sys.stderr)  # fmt: skip
        return code == 0

    return run


def _run_doc(objs: list[dict]) -> dict | None:
    """The run doc a site step names in its last stdout object (`{"run_id": ...}`), or None."""
    rid = objs[-1].get("run_id") if objs else None
    if not isinstance(rid, str) or not rid:
        return None
    p = store.runs_dir() / f"{store.safe_name(rid)}.json"
    if not p.is_file():
        return None
    doc = store.read_json(p, "run.v1")
    return doc if doc.get("run_id") == rid else None


def list_postcondition(run_id: str, objs: list[dict]) -> str | None:
    """Why a crawl-lists step that exited 0 still FAILS this refresh, or None. It passes only when its run doc
    is a completed crawl-lists run of THIS refresh with list.status ok and at least one observation written;
    anything else (an older step, a doc that names no list outcome) fails closed."""
    rid = objs[-1].get("run_id") if objs else None
    if not rid:
        return "the step printed no run_id"
    try:
        doc = _run_doc(objs)
    except Exception as e:  # noqa: BLE001 - fail closed to 14: a crash here would leave the step resumable
        return f"run doc of {rid} is unreadable ({type(e).__name__})"
    if doc is None:
        return f"no run doc for {rid}"
    if doc.get("command") != "crawl-lists" or (doc.get("approval_ref") or {}).get("refresh_run") != run_id:
        return f"{rid} is not a crawl-lists run of refresh run {run_id}"
    if doc.get("status") != "completed":
        return f"{rid} has status {doc.get('status')}"
    lst = doc.get("list") or {}
    if lst.get("status") != "ok":
        return f"{rid} has list.status {lst.get('status')}"
    if not (lst.get("observations_written") or 0) > 0:
        return f"{rid} has 0 observations written"
    return None


def _terminal(st: dict, failed: dict | None) -> str | None:
    """Why this failure ends the refresh for good (its approval is then spent), or None (resumable)."""
    if failed is None:
        return None
    code, reason = failed.get("exit"), failed.get("reason") or ""
    if code in TERMINAL_CODES:
        return f"{failed['name']}: {TERMINAL_CODES[code]} (exit {code})"
    if (
        code == EXIT_NETWORK
        and failed["name"] in pipeline.REFRESH_SITE_STEPS
        and not reason.startswith("paid gate")
    ):
        return f"{failed['name']}: a bound of the approval was exceeded (exit 3)"
    if why := approvals.bounded_status(st["approval_id"]):
        return f"the approval is {why}"
    return None


def _headroom(st: dict) -> dict:
    a = approvals.bounded_record(st["approval_id"])
    used = approvals.bounded_usage(st["approval_id"], st["run_id"])
    return {
        "list_pages": {"used": used["list_pages"], "max": a["bounds"]["max_list_pages"]},
        "ad_requests": {"used": used["ad_requests"], "max": a["bounds"]["max_ad_requests"]},
    }


def _spend(st: dict, why: str, status: str = "spent") -> dict:
    approvals.mark_spent(st["approval_id"], st["run_id"], why)
    st["status"] = status
    st["spent"] = {"at": _now(), "why": why}
    st["headroom"] = _headroom(st)
    save_state(st)
    return st


def _step_run(st: dict, pos: int) -> dict | None:
    """The run doc of step `pos`: the one its stdout named, else (a Stop prints no run_id line) the latest
    run of that command whose approval_ref names this refresh (for crawl-lists, the step's own list set)."""
    s = _step(st, pos)
    if s.get("summary") and (doc := _run_doc([s["summary"]])):
        return doc
    if s["status"] != "failed":
        return None
    from . import crawl

    nth = sum(1 for x in st["steps"][:pos] if x["name"] == s["name"])
    docs = [
        d
        for d in crawl.run_docs()
        if d.get("command") == s["name"]
        and (ref := d.get("approval_ref") or {}).get("refresh_run") == st["run_id"]
        and (s["name"] != "crawl-lists" or ref.get("list_set", 0) == nth)
    ]
    return max(docs, key=lambda d: d.get("created_at") or "") if docs else None


def _outcome(st: dict, pos: int) -> dict:
    """A site step's outcome, read at print time from DATA (its run doc; for a fetch-ads step that planned
    nothing and so has no run doc, the ads{} its stdout carried): crawl-lists' list{}, fetch-ads' ads{}."""
    s = _step(st, pos)
    key = {"crawl-lists": "list", "fetch-ads": "ads"}.get(s["name"])
    if not key or s["status"] == "pending":
        return {}
    try:
        doc = _step_run(st, pos)
    except Exception:  # noqa: BLE001 - a broken run doc never hides the summary
        doc = None
    got = (doc or {}).get(key) or (s.get("summary") or {}).get(key)
    return {key: got} if isinstance(got, dict) else {}


def summary(st: dict) -> dict:
    paid = sum(s.get("paid_count") or 0 for s in st["steps"])
    digests = {
        s["name"]: s["summary"]["digest"]
        for s in st["steps"]
        if isinstance((s.get("summary") or {}).get("digest"), str)
    }
    return {
        "refresh": st["run_id"],
        "status": st["status"],
        "plan_hash": st["plan_hash"],
        "approval_id": st["approval_id"],
        "steps": [
            {
                k: s.get(k)
                for k in ("id", "name", "status", "exit", "duration_s", "summary", "reason", "paid_count")
                if s.get(k) is not None
            }
            | _outcome(st, i)
            for i, s in enumerate(st["steps"])
        ],
        "paid_requests": paid,
        "headroom": st.get("headroom") or _headroom(st),
        **({"digests": digests} if digests else {}),
        **({"spent": st["spent"]} if st.get("spent") else {}),
    }


def _print_summary(st: dict) -> None:
    s = summary(st)
    h = s["headroom"]
    lines = [f"REFRESH {st['run_id']}: {st['status']}"]
    for x in s["steps"]:
        lines.append(f"  {x.get('id', x['name']):<16} {x['status']:<8} exit {x.get('exit', '-')}  {x.get('duration_s', '-')} s"
                     + (f"  {json.dumps(x['summary'], sort_keys=True)}" if x.get("summary") else "")
                     + (f"  ({x['reason']})" if x.get("reason") else ""))  # fmt: skip
        if lst := x.get("list"):
            lines.append(f"    crawl-lists list: {lst.get('status')}, "
                         f"{lst.get('observations_written', '-')} observations written")  # fmt: skip
        if ads := x.get("ads"):
            from . import cli

            lines.append(f"    fetch-ads selection: {cli.selection_words(ads)}"
                         + (f"; ad GETs {ads['ad_gets']}, ok {ads.get('ok')}, gone {ads.get('gone')}"
                            if "ad_gets" in ads else ""))  # fmt: skip
    lines.append(f"  paid requests {s['paid_requests']}; headroom: ad requests {h['ad_requests']['used']}/"
                 f"{h['ad_requests']['max']}, list pages {h['list_pages']['used']}/{h['list_pages']['max']}")  # fmt: skip
    for k, v in (s.get("digests") or {}).items():
        lines.append(f"  {k} digest {v}")
    print("\n".join(lines), file=sys.stderr)
    print(json.dumps(s, sort_keys=True))


# ── the command ────────────────────────────────────────────────────────────────────────────────


def _card(
    bounds: dict, plan_hash: str | None, purpose: str, mode: str, approval_id: str | None = None
) -> None:
    from . import cli

    cli._card(  # noqa: SLF001
        "refresh",
        purpose,
        state=True,
        mode=mode,
        plan_hash=plan_hash,
        approval_id=approval_id,
        approval=None
        if plan_hash
        else "needed: ONE bounded Touch ID approval of these bounds (refresh, then approve)",
        planned={"list": bounds["max_list_pages"], "ad": bounds["max_ad_requests"]},
        per_minute=bounds["max_rate_per_minute"],
        proxy=bounds.get("proxy"),
        egress="checked by each networked step at its start (its own egress preflight), never the site",
    )
    print(f"steps: {' -> '.join(bounds['steps'])}", file=sys.stderr)
    print(approvals.bounded_text(bounds), file=sys.stderr)


def counts_from(args: argparse.Namespace) -> dict[str, int | None]:
    return {s.name: getattr(args, count_dest(s), None) for s in pipeline.current().refresh if s.count_field}


def count_dest(step: pipeline.RefreshStep) -> str:
    return "count_" + step.name.replace(" ", "_").replace("-", "_")


def cmd(args: argparse.Namespace) -> int:
    with contextlib.ExitStack() as stack:
        try:
            return _cmd(args, stack)
        except Busy as e:
            print(f"refusing: {e}; nothing was changed", file=sys.stderr)
            return approvals.EXIT_UNAPPROVED


def _cmd(args: argparse.Namespace, stack: contextlib.ExitStack) -> int:
    pl = pipeline.current()
    cli_name = pl.consumer.cli
    if not pl.refresh:
        raise SystemExit(f"{pl.path} declares no [crawl.refresh] steps; there is nothing to refresh")
    st = unfinished()
    if st:  # held for the whole refresh: a second refresh of this run id refuses (exit 10)
        stack.enter_context(locked(st["run_id"]))
        st = load_state(st["run_id"])  # what the lock holder left, never a pre-lock read
        if st["status"] not in ("running", "failed"):
            st = None
    if args.abandon:
        if not st:
            raise SystemExit("no unfinished refresh run to abandon")
        _spend(st, "abandoned by the operator (--abandon)")
        _print_summary(st)
        return 0
    bounds, networked = build_bounds(args.ttl or DEFAULT_TTL, args.max_ad_requests)
    from . import crawl

    if stop := crawl.latch_stop(bounds["site"]):  # before a plan is printed or an approval claimed or spent
        print(str(stop), file=sys.stderr)
        if not args.dry_run:
            return stop.code
    proxies = configured_proxies() if networked else ()
    core = approvals.bounds_core(bounds, proxies)
    if st:
        a = approvals.bounded_record(st["approval_id"])
        if why := approvals.bounded_status(st["approval_id"]):
            _spend(st, f"the approval is {why}")
            _print_summary(st)
            print(f"refusing: refresh run {st['run_id']}'s approval is {why}; plan a new `{cli_name} refresh`",
                  file=sys.stderr)  # fmt: skip
            return approvals.EXIT_UNAPPROVED
        if approvals.bounds_core(a["bounds"], proxies) != core:
            print(f"refusing: the refresh's bounds changed since run {st['run_id']} was approved; finish it as "
                  f"declared, or `{cli_name} refresh --abandon` it and plan a new one", file=sys.stderr)  # fmt: skip
            return approvals.EXIT_UNAPPROVED
        bounds = a["bounds"]
    else:
        claim = stack.enter_context(contextlib.ExitStack())
        claim.enter_context(locked(None))  # until this new run's state (and its own lock) exist
        claimed = {s["approval_id"] for s in states()}
        live = [a for a in approvals.bounded_live(core, proxies) if a["approval_id"] not in claimed]
        if not live:
            bounds = pick_proxy(bounds, proxies)
            if args.dry_run:
                _card(
                    bounds,
                    None,
                    "DRY RUN: the bounds a refresh would ask the operator to sign; nothing sent",
                    "dry-run",
                )
                print(json.dumps({"bounds": bounds}, sort_keys=True, indent=1))
                return 0
            h = approvals.save_plan(bounds)
            _card(bounds, h, f"PLAN only: nothing is sent (plan_hash {h[:12]}...)", "plan")
            print(json.dumps({"plan": bounds, "plan_hash": h}, sort_keys=True, indent=1))
            print(f"Operator approves with: {cli_name} approve {h}", file=sys.stderr)
            return approvals.EXIT_UNAPPROVED
        a = live[0]
        bounds = a["bounds"]
    if args.dry_run:
        _card(bounds, a["plan_hash"], "DRY RUN: approved; this refresh would run now; nothing sent", "dry-run",
              a["approval_id"])  # fmt: skip
        print(json.dumps({"bounds": bounds, "approval_id": a["approval_id"],
                          **({"resumes": st["run_id"]} if st else {})}, sort_keys=True, indent=1))  # fmt: skip
        return 0
    needs_net = networked or any(pl.command(s.name).network != "none" for s in pl.refresh)
    if needs_net and not (args.approve_network or pipeline.knob("NETWORK_APPROVED") == "1"):
        print(f"refusing: this refresh touches the network. Re-run with --approve-network (or "
              f"{pipeline.knob_name('NETWORK_APPROVED')}=1) only when the operator has approved it.", file=sys.stderr)  # fmt: skip
        return EXIT_NETWORK
    if st is None:
        if a["approval_id"] in {s["approval_id"] for s in states()}:
            raise Busy(f"approval {a['approval_id']} was just claimed by another refresh run")
        run_id = new_run_id()
        stack.enter_context(locked(run_id))
        st = _new_state(a, list(bounds["steps"]), run_id)
        claim.close()
    else:
        st["status"] = "running"
        save_state(st)
    _card(bounds, a["plan_hash"], f"refresh run {st['run_id']}: {' -> '.join(bounds['steps'])}", "run",
          a["approval_id"])  # fmt: skip
    from . import tasks

    done = {step_id(i, s["name"]) for i, s in enumerate(st["steps"]) if s["status"] == "done"}
    counts = counts_from(args)
    found = tasks.step_tasks(
        [(step_id(i, s.name), _action(st["run_id"], i, s, counts)) for i, s in enumerate(pl.refresh)], done
    )
    deps = _dir() / "doit"
    deps.mkdir(exist_ok=True)
    tasks.run([], found=found, dep_file=deps / f"{st['run_id']}.json")
    st = load_state(st["run_id"])
    st["headroom"] = _headroom(st)
    if all(s["status"] == "done" for s in st["steps"]):
        st = _spend(st, "complete", status="complete")
        _print_summary(st)
        return 0
    failed = next((s for s in st["steps"] if s["status"] == "failed"), None)
    if why := _terminal(st, failed):
        st = _spend(st, why)
    else:
        st["status"] = "failed"
        save_state(st)
    _print_summary(st)
    if st["status"] == "failed":
        print(f"resumable: `{cli_name} refresh --approve-network` continues {st['run_id']} at "
              f"{failed['name'] if failed else 'its next step'}", file=sys.stderr)  # fmt: skip
    return (failed or {}).get("exit") or 1
