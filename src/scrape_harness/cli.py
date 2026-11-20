"""`<cli>` — the pipeline's only entry point. Each subcommand reads and writes typed JSONL under
the DATA root ($<data_env>), validating at the boundary.

Network-touching runs (a driver whose `network` is true) refuse to start without
`--approve-network` or LI_NETWORK_APPROVED=1: every ad-page GET adds a view to a real person's ad.
All page GETs go through crawl.run: one request at a time, robots.txt obeyed, challenge = stop.

`<cli> refresh` (refresh.py) runs pipeline.toml [crawl.refresh] as ONE chain under ONE bounded Touch ID
approval; its networked steps are these same subcommands with `--refresh-run RUN_ID`, so gate() checks each
step against the signed bounds instead of a single-use plan approval.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import (
    approvals,
    breaker,
    contracts,
    crawl,
    egress,
    incidents,
    liveness,
    pipeline,
    plan,
    policy,
    refresh,
    runcard,
    schemaorg,
    store,
    variants,
)
from .interfaces import FetchResult, available, load, plugin_class

EXIT_CONTRACT, EXIT_NETWORK, EXIT_CONFIG = 2, 3, 4
utcnow = crawl.utcnow


def need_network(args: argparse.Namespace, plugin: object, what: str) -> None:
    if not getattr(plugin, "network", True):
        return
    if getattr(args, "approve_network", False) or pipeline.knob("NETWORK_APPROVED") == "1":
        return
    sys.stderr.write(
        f"refusing: {what} touches the network. Re-run with --approve-network (or {pipeline.knob_name('NETWORK_APPROVED')}=1)\n"
        "only when the operator has approved THIS run.\n"
    )
    raise SystemExit(EXIT_NETWORK)


def _backend_name(args: argparse.Namespace, variant: str) -> str:
    return args.driver or variants.get(variant).backend


def _check_backend(args: argparse.Namespace, variant: str, what: str) -> None:
    need_network(args, plugin_class("drivers", _backend_name(args, variant)), what)  # before any fetch


def _opts(args: argparse.Namespace) -> dict:
    return {"backend": args.driver, "root": args.fixtures} if args.driver else {}


def _polite(args: argparse.Namespace) -> crawl.Politeness:
    return crawl.Politeness(
        args.concurrency, args.max_tasks_per_minute, args.approve_politeness_override
    ).checked()


def list_handler(job: crawl.Run, source, max_pages: int):
    seen: dict[str, int] | None = None  # a walk's ad id -> the lowest page it was kept on: this crawl's rows
    walk_pages: dict[int, list[str]] = {}  # a walk's page -> its kept ids in parsed order (frontier input)
    ref: list = []  # the frontier's reference (plan.reference_of), loaded once per attempt

    def walk_seen() -> dict[str, int]:
        nonlocal seen
        if seen is None:  # once per attempt, from DATA: a resume sees the ids of the pages read before it
            rid, seen = job.doc["run_id"], {}
            rows = sorted((o["list"]["page"], o["list"]["position"], o["ad_id"]) for o in store.read("observations")
                          if o.get("crawl_id") == rid)  # fmt: skip
            for p, _, a in rows:
                seen[a] = min(seen.get(a, p), p)
                if a not in walk_pages.setdefault(p, []):
                    walk_pages[p].append(a)
        return seen

    def reference(lst: dict) -> dict | None:
        """The frontier's reference crawl, the one recorded at its start (a resume never chooses again)."""
        if not ref:
            doc = next((d for d in crawl.run_docs() if d["run_id"] == lst.get("ref_crawl_id")), None)
            ref.append(plan.reference_of(doc, store.read("observations")) if doc else None)
        return ref[0]

    def ended(lst: dict, m: dict, method: str) -> None:
        """Record an END read on page m["page"]: end_read_page (the lowest), and in a walk end_page = the page
        before it (exact: pages were read contiguously from 1). Elsewhere end_page is exact only when page 1
        ENDED (0, an empty subcategory)."""
        p = m["page"]
        if lst.get("end_read_page") is not None and p >= lst["end_read_page"]:
            return
        lst["end_read_page"], lst["end_method"] = p, method
        if m.get("mode") in plan.WALK_MODES or p == 1:
            lst["end_page"] = p - 1

    def walked(lst: dict, m: dict) -> list[dict]:
        """A walk page is handled: count it, save the walk's state, and enqueue the NEXT page only while the
        signed ceiling (every list GET sent, the END page and retries included: `settled`, counted by the crawl
        before each GET) leaves room. Never two pages ahead."""
        lst["pages_read"] = lst.get("pages_read", 0) + 1
        job.save()  # the walk's state after every page: a resume continues from it
        if lst.get("anchor"):  # a confirmed anchor: the rest of the list is what the reference read
            return []
        if lst.get("end_read_page") is not None:
            if lst["end_page"] < (lst.get("min_pages") or 0):
                lst["status"] = "short_census"
                raise crawl.Stop(crawl.EXIT_PARSE, f"STOPPED: short_census: the list ENDED after {lst['end_page']} "
                                 f"pages ({lst['end_method']}), fewer than the signed --min-pages "
                                 f"{lst['min_pages']}")  # fmt: skip
            return []
        if lst["settled"] >= lst["ceiling"]:
            return []
        p = m["page"] + 1
        return [
            {
                "url": source.list_url(m["subcategory"], m["region"], p),
                "kind": "list",
                "meta": {**m, "page": p},
            }
        ]

    def handle(m: dict, res: FetchResult) -> list[dict]:
        # this page's END nomination (crawl's seam), taken out so no page planned from `m` can inherit it
        m = dict(m)
        marked, reason = m.pop("end_marker", False), m.pop("reason", None)
        lp = source.parse_list(
            res.html, url=res.url, subcategory=m["subcategory"], page=m["page"], seen_at=utcnow()
        )
        if m.get("snapshot"):  # the counts, before any stop below: the sidecar was saved before the parse
            store.update_list_sidecar(source.site, job.doc["run_id"], m["page"], parsed=len(lp.observations),
                                      skipped=getattr(lp, "skipped", None))  # fmt: skip
        # A positive END (ADR-0017 A.1), decided FIRST: before the page-count check (a past-end template has no
        # pager) and before any drift guard (a past-end page may still render promoted slots the parser skips).
        lst = job.doc.setdefault("list", {})
        walk = m.get("mode") in plan.WALK_MODES
        if marked and not lp.observations and not crawl.redirected(res):
            ended(lst, m, "end_marker")
            return walked(lst, m) if walk else []
        if marked and reason:  # it would have stopped the run: none of it is kept
            lst["status"] = "end_unknown"
            raise crawl.Stop(crawl.EXIT_PARSE, f"STOPPED: end_unknown: list page {m['page']} carries the end "
                             f"marker ({reason}) but is not an END (" + ("served at another URL"
                             if crawl.redirected(res) else f"{len(lp.observations)} cards") + ")")  # fmt: skip
        # a walk's 0-card page with no positive END: the list's end is unknown, or the parser broke, and the walk
        # would lose its next page. Samples and named pages read every planned page as before (an empty page
        # writes no row; list_outcome's zero-yield backstop judges the whole run)
        if walk and not lp.observations:
            lst["status"] = "zero_yield" if m["page"] == 1 else "end_unknown"
            raise crawl.Stop(crawl.EXIT_PARSE, f"STOPPED: {lst['status']}: list page {m['page']} gave 0 cards "
                             "and no end marker")  # fmt: skip
        # a walk: no pager is read (a windowed pager gives no count), one page ahead. A census row weighs 1; a
        # frontier row has no weight (the walk is neither a census nor a sample of the list)
        if walk and crawl.redirected(res):  # cards served at another URL are not this page's: never kept, and a
            lst["status"] = "end_unknown"  # redirect is never an END (A.1), clamp or not; the site changed
            raise crawl.Stop(crawl.EXIT_PARSE, f"STOPPED: end_unknown: list page {m['page']} was served at another "
                             f"URL ({len(lp.observations)} cards); a walk keeps no page it did not ask for")  # fmt: skip
        if walk:
            ids, known = [o["ad_id"] for o in lp.observations], walk_seen()
            # the clamp guard: ids kept on an EARLIER page (a page read again by a resume is not its own repeat)
            if 2 * sum(1 for i in ids if known.get(i, m["page"]) < m["page"]) > len(ids):
                ended(lst, m, "clamped")  # its cards are earlier pages' cards again: none is kept
                return walked(lst, m)
            frontier = m["mode"] == "frontier"
            keep(lp.observations, None, None if frontier else 1)
            for i in ids:
                known.setdefault(i, m["page"])
            walk_pages[m["page"]] = list(dict.fromkeys(ids))
            if frontier:  # a confirmed anchor (ADR-0017 A.2), from the walk's rows and the reference's order
                pol = dataclasses.replace(policy.DEFAULT, frontier_k=lst["k"])
                walk_now = {"order": lst.get("order"), "page": m["page"], "pages": walk_pages}
                if anchor := plan.frontier_stop(walk_now, reference(lst), pol):
                    lst["anchor"] = anchor
            return walked(lst, m)
        new: list[dict] = []
        weight = m.get("sample_weight")
        page_count = m.get("page_count") or lp.page_count
        if m.get("discover"):  # page 1 told us the page count: now plan the sample, once
            if lp.page_count is None:  # its rows are kept first: the page was read, its view is spent
                keep(lp.observations, None, None)
                job.doc.setdefault("list", {})["status"] = "parse_drift"
                raise crawl.Stop(crawl.EXIT_PARSE, "STOPPED: page count not found on page 1 (parser drift?); "
                                 "plan a new crawl with --page-count")  # fmt: skip
            start, pages = plan.systematic_pages(lp.page_count, m["stride"], m.get("start"), m.get("seed"))
            weight = plan.sample_weight(lp.page_count, pages)
            new = [
                {
                    "url": source.list_url(m["subcategory"], m["region"], p),
                    "kind": "list",
                    "meta": {
                        **m,
                        "page": p,
                        "page_count": lp.page_count,
                        "sample_weight": weight,
                        "start": start,
                        "discover": False,
                    },
                }
                for p in pages[:max_pages]
                if p != 1
            ]
            if 1 not in pages:  # page 1 was read only to learn the count: its sightings are out of sample
                weight = None
        keep(lp.observations, page_count, weight)
        return new

    def keep(observations: list[dict], page_count: int | None, weight: float | None) -> None:
        for o in observations:
            o["crawl_id"] = job.doc["run_id"]
            o["list"]["page_count"] = page_count
            o["list"]["sample_weight"] = weight
        store.append("observations", observations)

    return handle


def list_outcome(job: crawl.Run, stop: crawl.Stop | None) -> crawl.Stop | None:
    """crawl-lists' postcheck: record the observations this run wrote (recounted by its crawl_id, across
    every attempt). List pages that answered but left no observation are a zero yield: exit 14, terminal,
    never a `completed` reference crawl."""
    n = sum(1 for o in store.read("observations") if o.get("crawl_id") == job.doc["run_id"])
    lst = job.doc.setdefault("list", {})
    lst["observations_written"] = n
    if stop is not None:
        return None
    snap = lst.get("snapshot") or {}
    end = lst.get("end_read_page")
    # an END page is saved as past_end (it answered 404/410) and was handed to the parser; a bare past-end page
    # (no end marker) is evidence only
    bare = [p for p in snap.get("past_end", ()) if p != end]
    unlisted = len(bare) + len(snap.get("redirected", ()))
    # pages that were only past-end or redirected wrote no rows by design; any page handed to the parser
    # (listed) that left no row is still a zero yield, unless the list positively ENDED (an empty subcategory)
    if (n == 0 and end is None and job.doc.get("requests", {}).get("list_pages", 0) > 0
            and snap.get("saved", 1) > unlisted):  # fmt: skip
        lst["status"] = "zero_yield"
        job.doc["status"] = "stopped"
        return crawl.Stop(crawl.EXIT_PARSE, f"STOPPED: zero yield: {job.doc['requests']['list_pages']} list "
                          "page requests and no observation written (no cards, or the parser drifted)")  # fmt: skip
    if unlisted:  # an evidence run: completed, never a good crawl (liveness.Rules.good_list_status)
        lst["status"] = "past_end" if bare else "redirected"
        return None
    if lst.get("mode") in plan.WALK_MODES and end is None:
        if lst.get("anchor"):  # a frontier confirmed its anchor: the reference's GAP, if any, is still open
            lst["status"] = "anchored"
            ref = next((d for d in crawl.run_docs() if d["run_id"] == lst.get("ref_crawl_id")), None)
            if gap := ((ref or {}).get("list") or {}).get("gap"):
                lst["gap"] = gap
            return _gap_line(job)
        if lst.get("settled", 0) >= lst.get("ceiling", 0):
            lst["status"] = "ceiling_hit"  # read to its signed ceiling: good, and the list goes on past it
            lst["gap"] = _ceiling_gap(job, lst)  # the pages past the ceiling are unread until a census ENDs
            return _gap_line(job)
        # drained with neither an END nor the ceiling: the walk lost its next page; never an `ok` census
        lst["status"] = "end_unknown"
        job.doc["status"] = "stopped"
        return crawl.Stop(crawl.EXIT_PARSE, f"STOPPED: end_unknown: the walk ended after {lst.get('pages_read', 0)} "
                          "pages with neither a positive END nor its ceiling")  # fmt: skip
    lst["status"] = "end" if end is not None else "ok"
    return None


def _ceiling_gap(job: crawl.Run, lst: dict) -> dict:
    """The GAP a walk at its ceiling leaves open. A census has no reference: nothing past its ceiling was read by
    it (since_ref null). A frontier whose reference still has an open GAP keeps that GAP's origin (since_ref and
    opened_by: the list past it has been unread since then, not since this reference) and the smaller
    after_page; only a reference with no GAP (a census that ENDED, an anchor on one) starts a new one from it."""
    new = {"after_page": lst["ceiling"], "since_ref": None, "opened_by": job.doc["run_id"]}
    if lst["mode"] != "frontier":
        return new
    ref = next((d for d in crawl.run_docs() if d["run_id"] == lst.get("ref_crawl_id")), None)
    if old := ((ref or {}).get("list") or {}).get("gap"):
        return old | {"after_page": min(old["after_page"], lst["ceiling"])}
    return new | {"since_ref": lst.get("ref_crawl_id")}


def _gap_line(job: crawl.Run) -> None:
    """Say a frontier's open GAP out loud (the run card's and the refresh summary's GAP line)."""
    if gap := job.doc["list"].get("gap"):
        print(f"GAP: {gap_words(gap)}", file=sys.stderr)
    return None


def gap_words(gap: dict) -> str:
    since = gap.get("since_ref")
    return (f"after page {gap['after_page']} (since reference {since})" if since else
            f"after page {gap['after_page']} (no reference before it)") + ", unread until a census of this list"  # fmt: skip


def ad_handler(source):
    def handle(m: dict, res: FetchResult) -> None:
        fetched_at = utcnow()
        try:
            rec = source.parse_ad(res.html, url=res.url, fetched_at=fetched_at, fetch_reason=m["reason"])
        except Exception:
            if m.get("ad_id"):  # keep the page that could not be parsed: a fixed parser reparses it, no view
                store.write_raw(m.get("site") or source.site, m["ad_id"], fetched_at, res.html)
            raise  # the crawl records ONE parse_failed event and never retries it
        if not rec["location"]["city_label"]:
            rec["location"]["city_label"] = m.get("card_city_label")
        # The snapshot first: a later selector can then be applied by `<cli> reparse`, with no new view.
        rec["raw_ref"] = store.write_raw(source.site, rec["ad_id"], fetched_at, res.html)
        store.append("listings", [rec])

    return handle


COMPARED = ("source", "content", "time", "location", "site")


def cmd_reparse(args: argparse.Namespace) -> int:
    """Re-extract listing fields from saved snapshots, OFFLINE: no driver is loaded, nothing is fetched.
    Appends a `reparse` version only where the current parser's fields differ from the latest version
    parsed from that same snapshot."""
    _card("reparse", "re-extract listings from saved snapshots, offline")
    latest_per_raw: dict[str, dict] = {}
    for r in store.read("listings"):
        if r.get("raw_ref"):
            latest_per_raw[r["raw_ref"]["path"]] = r  # file order: the last version for a snapshot wins
    out, unchanged = [], 0
    for old in latest_per_raw.values():
        if args.site and old["source"]["site"] != args.site:
            continue
        if args.since and old["fetched_at"] < args.since:
            continue
        source = load("sources", old["source"]["site"])
        html = store.read_raw(old["raw_ref"])
        rec = source.parse_ad(
            html, url=old["source"]["url"], fetched_at=old["fetched_at"], fetch_reason="reparse"
        )
        if not rec["location"]["city_label"]:
            rec["location"]["city_label"] = old["location"].get("city_label")  # the card's fallback, kept
        if all(rec.get(k) == old.get(k) for k in COMPARED):
            unchanged += 1
            continue
        rec["raw_ref"] = old["raw_ref"]
        rec["derived"] = {"method": source.parser}
        out.append(rec)
    n = store.append("listings", out)
    print(json.dumps({"snapshots": len(latest_per_raw), "reparsed": n, "unchanged": unchanged}))
    return 0


def card(command: str, purpose: str, state: bool = False, **kw) -> None:
    """Public for consumer commands ([plugins].commands): print the run card of an offline command."""
    _card(command, purpose, state, **kw)


def _card(command: str, purpose: str, state: bool = False, **kw) -> None:
    """The run card for a command with no crawl run (offline stages, utilities, dry runs, plans)."""
    roots = kw.pop("roots", None) or {
        "DATA": store.data_dir(),
        **({"STATE": store.state_dir()} if state else {}),
    }
    runcard.render(runcard.make(command, purpose, roots=roots, **kw))


def _execute(
    args: argparse.Namespace,
    job: crawl.Run,
    source,
    handle,
    requests: list[dict] | None,
    card: dict | None = None,
    postcheck=None,
    pol: policy.SyncPolicy | None = None,
) -> int:
    try:
        crawl.run(job, source, handle, _polite(args), requests, _opts(args), card, postcheck, pol)
    except crawl.Stop as s:
        print(str(s), file=sys.stderr)
        _refund_if_unsent(job, s)
        return s.code
    print(
        json.dumps({"run_id": job.doc["run_id"], "status": job.doc["status"], "variant": job.doc["variant"]})
    )
    return 0


REFUNDABLE = (crawl.EXIT_COOLDOWN, crawl.EXIT_EGRESS, crawl.EXIT_POLITENESS)  # refusals before any send


def _refund_if_unsent(job: crawl.Run, s: crawl.Stop) -> None:
    """A run refused before ANY site request (cooldown, egress, politeness) gives its approval back, so the
    operator's Touch ID is not burned by a refusal. Never after a site request (a challenge stop included)."""
    ref = job.doc.get("approval_ref") or {}
    sent = sum(job.doc.get("requests", {}).get(k, 0) for k in ("list_pages", "ad_pages", "robots", "other"))
    if (
        s.code not in REFUNDABLE
        or sent
        or not ref.get("approval_id")
        or ref.get("standing")
        or ref.get("refunded")
    ):
        return
    note = f"refused before any site request (exit {s.code})"
    if ref.get("bounded"):  # this step's counts back, so the same refresh run's resume is not double counted
        approvals.refund_bounded(ref, f"{note}, crawl run {job.doc['run_id']}")
    else:
        approvals.refund(ref["approval_id"], job.doc["run_id"], note)
    job.doc["approval_ref"] = ref | {"refunded": True}
    job.save()
    print(f"approval {ref['approval_id']} refunded: nothing was sent to the site", file=sys.stderr)


def _variant(args: argparse.Namespace, source, kind: str) -> str:
    return variants.get(args.variant or pipeline.knob("VARIANT") or source.default_variant.get(kind)).name


# ── subcommands ──────────────────────────────────────────────────────────────────────────────


def _networked(args: argparse.Namespace, variant_names: list[str]) -> bool:
    return any(plugin_class("drivers", _backend_name(args, v)).network for v in variant_names)


def run_plan(args: argparse.Namespace, command: str, source, variant_names: list[str], **what) -> dict:
    """The canonical description of a networked run: everything that decides what is sent, and to whom."""
    doc = {
        "command": command,
        "site": source.site,
        "variants": variant_names,
        "backends": [_backend_name(args, v) for v in variant_names],
        "concurrency": args.concurrency,
        "max_tasks_per_minute": args.max_tasks_per_minute,
        "politeness_override": bool(args.approve_politeness_override),
        "egress_country": egress.home_country(),
        **what,
    }
    # an offline backend (fixtures) sends nothing: no proxy in its plan, and no approval lookup to pick one
    return doc | (_proxy_plan(args, doc) if _networked(args, variant_names) else {})


def _proxy_plan(args: argparse.Namespace, doc: dict) -> dict:
    """The run's ONE proxy URL (never its credentials) is part of what an approval signs, and is
    activated for this process. `--plan` CHOOSES it ([crawl.proxy] choice); the run then rebuilds the same
    plan for each configured URL and takes the one whose plan holds a live approval, so the signature
    decides the proxy (no extra state). Absent when no proxy is configured: those plans hash as before."""
    from . import proxy

    ps = proxy.proxy_set()
    if not ps:
        return {}
    kept = getattr(args, "kept_proxy", None)  # a resume: the run's recorded proxy, never a new pick
    if kept:
        if kept not in ps.urls:
            raise SystemExit(f"the run's proxy {kept} is no longer configured in [crawl.proxy]")
        url = kept
    elif len(ps.urls) == 1:
        url = ps.urls[0]
    elif getattr(args, "plan", False):
        out = tuple(getattr(args, "proxy_exclude", None) or ())
        keep = tuple(u for u in ps.urls if not any(x in u for x in out))
        if not keep:
            raise SystemExit(f"--proxy-exclude {' '.join(out)} leaves no proxy of {len(ps.urls)}")
        url = proxy.choose(dataclasses.replace(ps, urls=keep))  # the run finds it among all, by signature
    else:
        signed = [u for u in ps.urls if approvals.has_live(approvals.canonical(doc | {"proxy": u})[1])]
        url = signed[0] if signed else ps.urls[0]  # none signed: the gate refuses this exact plan anyway
    proxy.activate(url)
    return {"proxy": url}


def _planned_from(plan_doc: dict) -> dict:
    """What a plan WOULD send, where the plan itself decides it (else {}: decided at run time)."""
    cmd, n_var = plan_doc["command"], len(plan_doc.get("variants") or [1])
    if cmd == "fetch-ads":
        return {"ad": len(plan_doc.get("ads") or []), "robots": 1}
    if cmd == "probe robots":
        return {"robots": 1}
    if cmd == "ab":
        return {"list": plan_doc["pages"] * n_var, "robots": n_var}
    if cmd == "crawl resume" and plan_doc.get("pending") is not None:
        kind = "ad" if plan_doc.get("resumes") == "fetch-ads" else "list"
        return {kind: plan_doc["pending"], "robots": 1}
    if cmd == "crawl-lists" and plan_doc.get("pages"):
        return {"list": len(plan_doc["pages"]), "robots": 1}
    if cmd == "crawl-lists" and _walk(plan_doc):  # at most: the walk stops at its END
        return {"list": plan_doc["max_pages"], "robots": 1}
    if cmd == "crawl-lists" and plan_doc.get("page_count"):
        _, pages = plan.systematic_pages(
            plan_doc["page_count"], plan_doc["stride"], plan_doc.get("start"), plan_doc.get("seed")
        )
        return {"list": min(len(pages), plan_doc["max_pages"]), "robots": 1}
    return {}


def _step_doc(plan_doc: dict) -> dict:
    """A refresh step's plan plus what it may send, as consume_bounded checks it against the bounds."""
    if plan_doc["command"] == "crawl-lists":
        lp = (
            _planned_from(plan_doc).get("list", 0)
            if plan_doc.get("page_count") or plan_doc.get("pages") or _walk(plan_doc)
            else 1 + plan_doc["max_pages"]  # page 1, then at most max_pages of the sample it plans
        )
        return plan_doc | {"list_pages": lp, "ad_requests": 0, "fetch_reasons": []}
    ads = plan_doc.get("ads") or []
    return plan_doc | {"list_pages": 0, "ad_requests": len(ads), "fetch_reasons": sorted({r for r, _ in ads})}


def _refresh_step(args: argparse.Namespace) -> None:
    """--refresh-run RUN_ID: this run is one step of `<cli> refresh`; its proxy is the one the bounds sign."""
    if not getattr(args, "refresh_run", None):
        return
    if args.plan:
        raise SystemExit("--refresh-run never takes --plan: `<cli> refresh` plans the whole chain's bounds")
    if getattr(args, "supersede", None):
        raise SystemExit("--refresh-run never supersedes: a refresh's fetch-ads takes the refetch rule's ads")
    args.kept_proxy = approvals.refresh_bounds(args.refresh_run).get("proxy")


def gate(args: argparse.Namespace, plan_doc: dict, variant_names: list[str]) -> dict | None:
    """--plan: print the plan and its hash, save it for `<cli> approve`, and stop (returns {"planned": ...}).
    Networked run: consume a matching operator approval or refuse (exit 10). Offline backends: no gate.
    --refresh-run RUN_ID (a `<cli> refresh` step): consume that refresh's BOUNDED approval for exactly what
    this step sends (approvals.consume_bounded) instead of a single-use plan approval; a step outside the
    bounds stops (exit 3). The same checks precede it. FIRST: a site latched by the gone-storm breaker refuses
    (exit 14) every plan and run, so no approval is asked for or spent."""
    if stop := crawl.latch_stop(plan_doc["site"]):
        raise stop
    if args.plan:
        h = approvals.save_plan(plan_doc)
        _card(
            plan_doc["command"],
            f"PLAN only: nothing is sent (plan_hash {h[:12]}...)",
            state=True,
            mode="plan",
            plan_hash=h,
            planned=_planned_from(plan_doc),
            per_minute=args.max_tasks_per_minute,
            egress="checked at run start: 1 request to the egress-check endpoint, never the site"
            if _networked(args, variant_names)
            else None,
        )
        print(json.dumps({"plan": plan_doc, "plan_hash": h}, sort_keys=True, indent=1))
        print(f"Operator approves with: {pipeline.consumer().cli} approve {h}", file=sys.stderr)
        return {"planned": h}
    if not _networked(args, variant_names):
        return None
    # every refusal that needs no approval comes BEFORE the approval is spent: a missing --approve-network
    # once burned an operator's Touch ID (2026-11-08: consumed, then refused on the flag; nothing sent)
    for v in variant_names:
        _check_backend(args, v, plan_doc["command"])
    if getattr(args, "refresh_run", None):
        return approvals.consume_bounded(_step_doc(plan_doc), args.refresh_run, plan_doc["command"])
    return approvals.consume(approvals.canonical(plan_doc)[1])


def _deterministic(args: argparse.Namespace) -> None:
    if args.stride > 1 and args.start is None and args.seed is None:
        raise SystemExit(
            "a networked sample needs --start or --seed, so the approved plan is the run that happens"
        )


def page_list(text: str) -> list[int]:
    """--pages 1,2,400: positive page numbers, each once, in the order given."""
    try:
        pages = [int(p) for p in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError(f"--pages {text!r}: comma-separated page numbers") from None
    if not pages or min(pages) < 1 or len(set(pages)) != len(pages):
        raise argparse.ArgumentTypeError(f"--pages {text!r}: page numbers >= 1, each named once")
    return pages


def _check_pages(args: argparse.Namespace) -> None:
    """--pages reads exactly the named pages: no page-count discovery and no sample, so the sample's options
    are refused with it (never silently ignored), and the pages count toward --max-pages (never truncated)."""
    if not getattr(args, "pages", None):
        return
    clash = [f for f, on in (("--stride", args.stride != 1), ("--start", args.start is not None),
             ("--seed", args.seed is not None), ("--page-count", args.page_count is not None)) if on]  # fmt: skip
    if clash:
        raise SystemExit(f"--pages names its pages: it cannot take {', '.join(clash)}")
    if len(args.pages) > args.max_pages:
        raise SystemExit(f"--pages names {len(args.pages)} pages, more than --max-pages {args.max_pages}")


def _mode(x) -> str | None:
    return x.get("mode") if isinstance(x, dict) else getattr(x, "mode", None)


def _walk(x) -> bool:
    """A walk (ADR-0017 A), the census (mode walk) or the frontier: a plan doc, a signed list set or parsed
    arguments. Both read 1, 2, 3, ... from page 1, one page ahead, under a ceiling of every list GET."""
    return _mode(x) in plan.WALK_MODES


def _walk_signed(args: argparse.Namespace) -> dict:
    """What a walk's plan and its refresh list set sign beyond the arguments: the policy digest its outcome is
    judged by, and a frontier's K (policy.frontier_k; the confirmation thresholds ride in the digest)."""
    if not _walk(args):
        return {}
    return {"policy_digest": policy.DEFAULT.digest(),
            **({"k": policy.DEFAULT.frontier_k} if _mode(args) == "frontier" else {})}  # fmt: skip


def _check_walk(args: argparse.Namespace) -> None:
    """--mode walk|frontier reads 1, 2, 3, ... to a positive END (or a frontier's confirmed anchor): the
    sample's and the named pages' options are refused with it (never silently ignored). Stride k (the H4 depth
    walk) is not built yet."""
    mode = _mode(args)
    if getattr(args, "min_pages", None) is not None and mode != "walk":
        raise SystemExit("--min-pages guards a census: it needs --mode walk")
    if not _walk(args):
        return
    if args.stride != 1:
        raise SystemExit(f"--mode {mode} reads every page (stride 1): the stride-{args.stride} depth walk (H4) "
                         "is deferred; use --page-count with --stride for a sample")  # fmt: skip
    clash = [f for f, on in (("--pages", bool(args.pages)), ("--page-count", args.page_count is not None),
             ("--start", args.start is not None), ("--seed", args.seed is not None)) if on]  # fmt: skip
    if clash:
        raise SystemExit(f"--mode {mode} reads from page 1 to its END: it cannot take {', '.join(clash)}")
    if args.max_pages < 1 or (args.min_pages is not None and not 1 <= args.min_pages <= args.max_pages):
        raise SystemExit(f"--mode walk needs 1 <= --min-pages <= --max-pages ({args.min_pages} and "
                         f"{args.max_pages}): a census that can never pass is refused")  # fmt: skip


def cmd_crawl_lists(args: argparse.Namespace) -> int:
    source = load("sources", args.source)
    variant = _variant(args, source, "list")
    _polite(args)
    _check_pages(args)
    _check_walk(args)
    _refresh_step(args)
    if _networked(args, [variant]):
        _deterministic(args)
    approval = gate(
        args,
        run_plan(
            args,
            "crawl-lists",
            source,
            [variant],
            subcategory=args.subcategory,
            region=args.region,
            page_count=args.page_count,
            stride=args.stride,
            start=args.start,
            seed=args.seed,
            max_pages=args.max_pages,
            # signed only when set, so every plan without them hashes as before
            **({"pages": args.pages} if args.pages else {}),
            **({"snapshot": True} if args.snapshot else {}),
            **({"mode": args.mode} if args.mode else {}),
            **({"min_pages": args.min_pages} if args.min_pages is not None else {}),
            # a walk's outcome is judged by the good-crawl rules, which the policy digest covers (a frontier's
            # anchor rules too); a frontier signs its K
            **_walk_signed(args),
        ),
        [variant],
    )
    if approval and "planned" in approval:
        return 0
    _check_backend(args, variant, "crawl-lists")
    m = {
        "subcategory": args.subcategory,
        "region": args.region,
        "stride": args.stride,
        "start": args.start,
        "seed": args.seed,
        **({"snapshot": True} if args.snapshot else {}),  # in each request: a resume keeps saving
        **({"mode": args.mode} if _walk(args) else {}),
    }
    job = crawl.Run.new("crawl-lists", variant, source.site)
    ref = None
    if _walk(args):  # the walk's signed state, on the run doc from the start (a resume reads it back)
        job.doc["list"] = {"mode": args.mode, "stride": 1, "ceiling": args.max_pages, "pages_read": 0,
                           "settled": 0, "subcategory": args.subcategory, "region": args.region,
                           **({"min_pages": args.min_pages} if args.min_pages is not None else {})}  # fmt: skip
    if _mode(args) == "frontier":  # its reference, chosen ONCE from DATA (a resume keeps it), and its order
        ref = plan.frontier_reference(crawl.run_docs(), store.read("events"), store.read("observations"),
                                      site=source.site, subcategory=args.subcategory, region=args.region)  # fmt: skip
        job.doc["list"] |= {"k": policy.DEFAULT.frontier_k, "order": getattr(source, "order", "unordered"),
                            "ref_crawl_id": ref["crawl_id"] if ref else None}  # fmt: skip
        job.save()
        # page 1 only: the handler enqueues each next page after it handles the current one
        reqs = [
            {
                "url": source.list_url(args.subcategory, args.region, 1),
                "kind": "list",
                "meta": {**m, "page": 1},
            }
        ]
    elif args.pages:  # exactly these pages, in order: no discovery, no sample weight
        reqs = [
            {
                "url": source.list_url(args.subcategory, args.region, p),
                "kind": "list",
                "meta": {**m, "page": p},
            }
            for p in args.pages
        ]
    elif args.page_count:
        start, pages = plan.systematic_pages(args.page_count, args.stride, args.start, args.seed)
        weight = plan.sample_weight(args.page_count, pages)
        reqs = [
            {
                "url": source.list_url(args.subcategory, args.region, p),
                "kind": "list",
                "meta": {
                    **m,
                    "page": p,
                    "page_count": args.page_count,
                    "sample_weight": weight,
                    "start": start,
                },
            }
            for p in pages[: args.max_pages]
        ]
    else:
        reqs = [
            {
                "url": source.list_url(args.subcategory, args.region, 1),
                "kind": "list",
                "meta": {**m, "page": 1, "discover": True},
            }
        ]
    if approval:
        job.doc["approval_ref"] = approval
    how = (
        "census" if args.stride == 1 else f"every {args.stride}th page, start {args.start}, seed {args.seed}"
    )
    what = (
        f"pages {','.join(map(str, args.pages))}"
        if args.pages
        else f"{len(reqs)} list pages"
        if args.page_count
        else f"pages 1, 2, 3, ... to a positive END, at most {args.max_pages}"
        if _walk(args)
        else "page 1, then the sample it plans"
    )
    if args.pages:
        how = "named pages" + (", snapshots saved" if args.snapshot else "")
    if _mode(args) == "walk":
        how = "census walk" + (f", at least {args.min_pages} pages" if args.min_pages else "")
    if _mode(args) == "frontier":
        how = frontier_words(job.doc["list"], ref)
    card = {
        "command": "crawl-lists",
        "purpose": f"read {what} of {args.subcategory} ({how})",
        "planned": {"list": args.max_pages if _walk(args) else len(reqs), "robots": 1},
        # crawlee's ceiling: the planned pages, a walk's signed ceiling (every list GET, the END page included),
        # or page 1 plus the most its sample may add
        "ceiling": len(reqs)
        if args.page_count or args.pages
        else args.max_pages
        if _walk(args)
        else 1 + args.max_pages,  # fmt: skip
    }
    return _execute(args, job, source, list_handler(job, source, args.max_pages), reqs, card, list_outcome)


def frontier_words(lst: dict, ref: dict | None) -> str:
    """The run card's words for a frontier walk: its reference, whether it may stop early, an open GAP."""
    if lst.get("order") != "bump_desc":
        stop = f"list order {lst.get('order')}: no early stop, to a positive END or the ceiling"
    elif ref is None:
        stop = "no reference crawl yet: to a positive END or the ceiling"
    else:
        stop = f"to a confirmed anchor against reference {ref['crawl_id']} (K {lst['k']}), a positive END or the ceiling"
    gap = f"; GAP {gap_words(ref['gap'])}" if ref and ref.get("gap") else ""
    return f"frontier walk, {stop}{gap}"


def _now() -> datetime:
    """The one clock fetch-ads plans by (a test seam). A plan's as_of is this, to the second, and is signed."""
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_docs() -> list[dict]:
    return crawl.run_docs()  # read only: a dry run creates nothing


def _records() -> tuple[list, list, list, list]:
    return (
        list(store.read("observations")),
        list(store.read("listings")),
        list(store.read("events")),
        _run_docs(),
    )


def _passed_lists(
    runs: list[dict], events: list[dict], refresh_run: str, rules: liveness.Rules = liveness.DEFAULT
) -> set[str]:
    """The crawl-lists runs of THIS refresh that passed: liveness.good_crawl, with nothing taken on trust."""
    good = liveness.good_crawl(runs, events, rules, legacy=False)
    return {
        d["run_id"]
        for d in runs
        if d.get("command") == "crawl-lists"
        and (d.get("approval_ref") or {}).get("refresh_run") == refresh_run
        and good(d["run_id"])
    }


def fetch_pool(
    recs: tuple, pol: policy.SyncPolicy, as_of: str, refresh_run: str | None = None
) -> tuple[list[dict], dict[str, int], str]:
    """(candidates, excluded, as_of): the fresh-sighting gate over DATA (plan.eligible).

    Sightings are the GOOD, non-sponsored list cards (liveness.good_crawl); states are the liveness ledger's.
    Fresh: inside a refresh, sighted by a list step of THIS refresh that passed, and as_of is the latest such
    sighting (DATA, not the clock); standalone, sighted within the policy window of `as_of`, the plan time."""
    observations, listings, events, runs = recs
    rules = pol.rules()
    ledger = liveness.build(observations, listings, events, runs, rules)
    good = liveness.good_crawl(runs, events, rules)
    sightings: dict[tuple, list[dict]] = {}
    for o in observations:
        if not o["list"].get("sponsored") and good(o.get("crawl_id")):
            sightings.setdefault((o["site"], o["ad_id"]), []).append(o)
    latest = store.latest(listings, lambda r: (r["source"]["site"], r["ad_id"]), "fetched_at")
    if refresh_run:
        passed = _passed_lists(runs, events, refresh_run, rules)
        mine = [o["seen_at"] for o in observations if o.get("crawl_id") in passed]
        as_of = max(mine) if mine else as_of

        def fresh(o: dict) -> bool:
            return o.get("crawl_id") in passed
    else:
        lo = plan.ts(as_of) - pol.window

        def fresh(o: dict) -> bool:
            return plan.ts(o["seen_at"]) >= lo

    cands, excluded = plan.eligible(
        sightings, latest, lambda k: ledger.status(*k), fresh, plan.ts(as_of), pol
    )
    return cands, excluded, as_of


def never_opened(cands: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Recruitment pool: refetch-rule candidates never fetched ("new": no listing version) that also
    have no raw snapshot, each re-tagged "recruit" so its listing and manifest row say why it exists."""
    raw = store.data_dir() / "raw"
    return [
        ("recruit", o)
        for reason, o in cands
        if reason == "new" and not (raw / store.safe_name(o["site"]) / store.safe_name(o["ad_id"])).exists()
    ]


@dataclasses.dataclass
class Selection:
    todo: list[tuple[str, dict]]
    design: dict | None
    picks: list[dict]
    ads: dict  # the run doc's ads{}: as_of, policy_digest, eligible, excluded, lanes (select_ads only)


def _select(args: argparse.Namespace, pol: policy.SyncPolicy, recs: tuple, as_of: str) -> Selection:
    """Without --sample: plan.select_ads over the gated pool, budget --max-fetches. --sample (and --select
    recruit, which needs it): the stratified sampler, over the SAME gated pool (recruit: its never-opened),
    under the same lanes and HARD revisit cap with B = N (plan.sample_lanes)."""
    recruit = getattr(args, "select", "refetch") == "recruit"
    if recruit and not args.sample:
        raise SystemExit("--select recruit needs --sample N (a seeded, stratified one-off sample)")
    if args.sample and args.seed is None:
        raise SystemExit("--sample needs --seed, so the approved sample is the one fetched")
    pool, excluded, as_of = fetch_pool(recs, pol, as_of, getattr(args, "refresh_run", None))
    ads = {"as_of": as_of, "policy_digest": pol.digest(), "eligible": len(pool), "excluded": excluded}
    if not args.sample:
        todo, lanes = plan.select_ads(pool, args.max_fetches, pol)
        return Selection(todo, None, [], ads | {"lanes": lanes})
    cands = [(c["reason"], c["obs"]) for c in pool]
    if recruit:
        cands = never_opened(cands)
    subs = args.subcategories.split(",") if args.subcategories else None
    picks, strata, lanes = plan.sample_lanes(cands, args.sample, args.seed, args.allocation, subs, pol)
    design = {
        "n": args.sample,
        "seed": args.seed,
        "stratify": args.stratify,
        "allocation": args.allocation,
        "subcategories": subs,
        "strata": strata,
        **({"select": "recruit"} if recruit else {}),
    }
    return Selection([(p["reason"], p["obs"]) for p in picks], design, picks, ads | {"lanes": lanes})


def _fetch_plan_doc(args: argparse.Namespace, sel: Selection, source, variant: str) -> dict:
    design = sel.design
    return run_plan(
        args,
        "fetch-ads",
        source,
        [variant],
        max_fetches=None if design else args.max_fetches,
        sample=design,
        **({"select": "recruit"} if design and design.get("select") == "recruit" else {}),
        as_of=sel.ads["as_of"],  # signed: the run re-selects at THIS time, so it reproduces the plan
        policy_digest=sel.ads["policy_digest"],
        ads=[[r, o["ad_url"]] for r, o in sel.todo],
    )


def _signed_selection(args: argparse.Namespace, pol: policy.SyncPolicy, recs: tuple) -> Selection | None:
    """A networked run re-selects at the as_of its approved plan signed (newest approval first) and keeps the
    selection whose rebuilt plan holds a live approval: the signature decides as_of, like the proxy. None:
    no such plan; the run then selects at now and the gate refuses an unapproved plan (exit 10)."""
    for doc in approvals.approved_plans("fetch-ads"):
        as_of = doc.get("as_of")
        if not as_of or doc.get("policy_digest") != pol.digest() or plan.ts(as_of) > _now():
            continue
        sel = _select(args, pol, recs, as_of)
        if {o["site"] for _, o in sel.todo} != {doc.get("site")}:
            continue
        source = load("sources", doc["site"])
        variant = _variant(args, source, "ad")
        if _networked(args, [variant]) and approvals.has_live(
            approvals.canonical(_fetch_plan_doc(args, sel, source, variant))[1]
        ):
            return sel
    return None


def selection_words(ads: dict) -> str:
    """The run card's Selection row: the gate and the lanes, in plain words."""
    lanes = ads.get("lanes")
    ex = ads.get("excluded") or {}
    return (
        f"{ads.get('eligible', 0)} eligible"
        + (f"; new {lanes['new']}, revisit {lanes['revisit']}" if lanes else "")
        + "; excluded: "
        + ", ".join(f"{k} {ex.get(k, 0)}" for k in plan.EXCLUDED)
        + f"; as_of {ads.get('as_of')}, policy {str(ads.get('policy_digest'))[:19]}"
    )


def ads_outcome(planned: set[tuple], pol: policy.SyncPolicy = policy.DEFAULT):
    """fetch-ads' postcheck: the run's ads{} outcome (ad GETs sent, listings written, gone events) and the
    site's cross-run parse streak (breaker.parse_streak). A run that parsed nothing, failed at least one ad
    page, and brings the streak to breaker_floor is exit 14, terminal (policy.parse_streak); a run's own stop
    stands, and one failed ad alone never rings."""

    def check(job: crawl.Run, stop: crawl.Stop | None) -> crawl.Stop | None:
        a = job.doc.setdefault("ads", {})
        rid, since = job.doc["run_id"], job.doc["created_at"]
        a["ad_gets"] = int(job.doc.get("requests", {}).get("ad_pages", 0))
        a["ok"] = len(
            {
                (r["source"]["site"], r["ad_id"])
                for r in store.read("listings")
                if r.get("fetch_reason") != "reparse"
                and r["fetched_at"] >= since
                and (r["source"]["site"], r["ad_id"]) in planned
            }
        )
        events = list(store.read("events"))
        mine = [e["kind"] for e in events if e.get("run_id") == rid]
        a["gone"] = mine.count("gone")
        runs = [d for d in _run_docs() if d["run_id"] != rid] + [job.doc]
        a["parse_streak"] = breaker.parse_streak(job.doc["source"], runs, events)
        if stop is not None or not pol.parse_streak or a["ok"] or "parse_failed" not in mine:
            return None
        if a["parse_streak"] < pol.breaker_floor:
            return None
        job.doc["status"] = "stopped"
        return crawl.Stop(crawl.EXIT_PARSE, f"STOPPED: {a['parse_streak']} ad pages of {job.doc['source']} failed "
                          "to parse, across runs, with no ad parsed since (the ad parser is broken?); fix it, "
                          "then plan a new fetch-ads (this run is not resumed)")  # fmt: skip

    return check


def _postcondition(
    planned: list[dict], recs: tuple, as_of: str, pol: policy.SyncPolicy, budget: int | None = None
) -> None:
    """plan.assert_admissible over the RAW records, before the gate: a plan naming an ad the records say must
    not get a view, or more revisits than ceil(rho * budget), is a selector bug, refused (exit 14) before any
    approval is spent or any GET is sent."""
    observations, listings, events, _ = recs
    bad = plan.assert_admissible(planned, observations, listings, events, as_of, pol, budget)
    if bad:
        raise crawl.Stop(crawl.EXIT_PARSE, f"refusing: the plan names {len(bad)} ad(s) that must not get a view "
                         f"(postcondition): {'; '.join(bad[:5])}{' ...' if len(bad) > 5 else ''}")  # fmt: skip


def _write_sample_manifest(job: crawl.Run, source, design: dict, picks: list[dict]) -> str:
    rel = f"derived/samples/{store.safe_name(job.doc['run_id'])}.jsonl"
    rows = [
        contracts.validate(
            "sample.v1",
            {
                "schema": "sample.v1",
                "run_id": job.doc["run_id"],
                "site": source.site,
                "ad_id": p["obs"]["ad_id"],
                "url": p["obs"]["ad_url"],
                "reason": p["reason"],
                "subcategory": p["subcategory"],
                "decile": p["decile"],
                "weight": p["weight"],
                "seed": design["seed"],
                "allocation": design["allocation"],
            },
        )
        for p in picks
    ]
    out = store.data_dir() / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    return rel


def _dry_run_gates(args: argparse.Namespace, todo: list[tuple[str, dict]]) -> dict:
    """What a REAL run of this dry run would need, for its card: the approval and the egress check.
    Offline backends (fixtures) need neither, so the card keeps saying offline."""
    sites = {o["site"] for _, o in todo}
    if len(sites) != 1:
        return {}
    variant = _variant(args, load("sources", sites.pop()), "ad")
    if not _networked(args, [variant]):
        return {}
    out = {
        "approval": "needed for a real run: --plan, then the operator approves its plan_hash",
        "egress": "checked at run start: 1 request to the egress-check endpoint, never the site",
    }
    if _backend_name(args, variant) == "browser":
        from . import browser

        out["blocking"] = browser.blocking(variants.get(variant))
    return out


def _taken_over(run_id: str, scope: str) -> str | None:
    """The run that already took over `scope` (pending | failed) of fetch-ads run `run_id`, or None. The ONE
    check behind --supersede and `crawl resume`: each scope is taken over once, and a successor written before
    the supersede_scope field took the pending scope."""
    for e in store.read("events"):
        if (
            e.get("run_id") == run_id
            and e["kind"] == "superseded"
            and crawl.Run.load(e["reason"]).doc.get("supersede_scope", "pending") == scope
        ):
            return e["reason"]
    return None


def _superseding(args: argparse.Namespace, statuses: tuple[str, ...] = ("stopped", "aborted")):
    """fetch-ads --supersede RUN: that stopped run's PENDING ad requests (or, with --failed, its FAILED ones),
    as a NEW run (its own plan, approval and proxy), each admitted through the gate again (plan.admit, in
    _fetch_superseding). The old run gets an append-only `superseded` event naming the new one. Each scope is
    taken over ONCE: the old run's own queue never changes, so a second supersede of the same scope would GET
    the same ads again. A run whose breaker tripped is refused (exit 14): the operator clears it first."""
    old = crawl.Run.load(args.supersede)
    if (old.doc.get("breaker") or {}).get("tripped"):
        raise crawl.Stop(crawl.EXIT_PARSE, f"refusing: run {args.supersede} tripped the gone-storm breaker; its "
                         "queue is not taken over (plan a new fetch-ads once the operator has cleared it)")  # fmt: skip
    if old.doc["command"] != "fetch-ads" or crawl.status_of(old.doc) not in statuses:
        raise SystemExit(f"--supersede needs a {' or '.join(statuses)} fetch-ads run; {args.supersede} is "
                         f"{old.doc['command']} / {crawl.status_of(old.doc)}")  # fmt: skip
    scope = "failed" if getattr(args, "failed", False) else "pending"
    if succ := _taken_over(old.doc["run_id"], scope):
        raise SystemExit(f"{args.supersede}: its {scope} ads were already taken over by {succ}; superseding "
                         "again would GET them twice")  # fmt: skip
    if scope == "failed":
        reqs = [r for r in crawl.failed_requests(old) if r["kind"] == "ad"]
        logged = {e["url"] for e in store.read("events")
                  if e.get("run_id") == old.doc["run_id"] and e["kind"] == "failed_request"}  # fmt: skip
        if {r["url"] for r in reqs} - logged:  # the queue's ERROR state and the run's own events must agree
            raise SystemExit(f"{args.supersede}: its queue holds failed requests its events never logged")
    else:
        reqs = [r for r in crawl.pending_requests(old) if r["kind"] == "ad"]
    if not reqs:
        raise SystemExit(f"{args.supersede}: nothing {scope} to take over")
    return old, reqs, scope


def cmd_fetch_ads(args: argparse.Namespace) -> int:
    if getattr(args, "refresh_run", None) and not args.dry_run:
        _refresh_step(args)
    if getattr(args, "supersede", None):
        return _fetch_superseding(args)
    pol = policy.from_args(args)
    recs = _records()
    sel = None
    if not args.plan and not args.dry_run and not getattr(args, "refresh_run", None):
        sel = _signed_selection(args, pol, recs)
    sel = sel or _select(args, pol, recs, _iso(_now()))
    todo, design, picks = sel.todo, sel.design, sel.picks
    if args.dry_run:
        for site in sorted({o["site"] for _, o in todo} | {d["source"] for d in recs[3]}):
            if stop := crawl.latch_stop(site):
                print(f"breaker latch: {stop}", file=sys.stderr)  # a dry run sends nothing: it still looks
        _card(
            "fetch-ads",
            f"DRY RUN: list the {len(todo)} ads a run would fetch; nothing is sent",
            mode="dry-run",
            planned={"ad": len(todo), "robots": 1} if todo else {},
            per_minute=args.max_tasks_per_minute,
            selection=selection_words(sel.ads),
            **_dry_run_gates(args, todo),
        )
        if design:
            for p in picks:
                o = p["obs"]
                print(
                    json.dumps(
                        {
                            "reason": p["reason"],
                            "site": o["site"],
                            "ad_id": o["ad_id"],
                            "url": o["ad_url"],
                            "subcategory": p["subcategory"],
                            "decile": p["decile"],
                            "weight": p["weight"],
                        }
                    )
                )
            print(json.dumps({"summary": True, "sampled": len(picks), "strata": design["strata"]}))
            return 0
        for reason, o in todo:
            print(json.dumps({"reason": reason, "site": o["site"], "ad_id": o["ad_id"], "url": o["ad_url"]}))
        return 0
    sites = {o["site"] for _, o in todo}
    if len(sites) > 1:
        raise SystemExit("fetch-ads: one site per run; pass observations of one site")
    if not todo:
        print(json.dumps({"planned": 0, "ads": sel.ads}))  # no run doc: the selection says why
        return 0
    source = load("sources", sites.pop())
    variant = _variant(args, source, "ad")
    _polite(args)
    if stop := crawl.latch_stop(source.site):  # before the postcondition: a latched site is the louder fact
        raise stop
    _postcondition([{"site": o["site"], "ad_id": o["ad_id"], "url": o["ad_url"]} for _, o in todo], recs,
                   sel.ads["as_of"], pol, args.sample or args.max_fetches)  # fmt: skip
    approval = gate(args, _fetch_plan_doc(args, sel, source, variant), [variant])
    if approval and "planned" in approval:
        return 0
    _check_backend(args, variant, "fetch-ads")
    reqs = [
        {
            "url": o["ad_url"],
            "kind": "ad",
            # ad_id and site travel with the request, so a parse_failed event names its ad
            "meta": {
                "reason": r,
                "card_city_label": o["card"].get("city_label"),
                "ad_id": o["ad_id"],
                "site": o["site"],
            },
        }
        for r, o in todo
    ]
    job = crawl.Run.new("fetch-ads", variant, source.site)
    if approval:
        job.doc["approval_ref"] = approval
    job.doc["ads"] = dict(sel.ads)
    if design:
        job.doc["sample"] = {**design, "manifest": _write_sample_manifest(job, source, design, picks)}
    how = (
        f"depth-stratified sample, seed {design['seed']}, {design['allocation']} allocation"
        if design
        else "freshly sighted ads, by select_ads"
    )
    card = {
        "command": "fetch-ads",
        "purpose": f"fetch {len(todo)} ad pages ({how})",
        "planned": {"ad": len(todo), "robots": 1},
        "ceiling": len(reqs),
        "sample_file": store.safe_name(job.doc["run_id"]) if design else None,
        "selection": selection_words(sel.ads),
    }
    planned = {(o["site"], o["ad_id"]) for _, o in todo}
    return _execute(args, job, source, ad_handler(source), reqs, card, ads_outcome(planned, pol), pol)


def _admitted(args, old, reqs, scope, recs, pol, source, variant, as_of: str):
    """(plan doc, kept requests, dropped counts, as_of): the old queue admitted at `as_of`, as a plan."""
    cands, _, as_of = fetch_pool(recs, pol, as_of)
    kept, dropped = plan.admit(reqs, cands, old.doc["created_at"])
    doc = run_plan(
        args,
        "fetch-ads",
        source,
        [variant],
        supersedes=old.doc["run_id"],
        ads=[[r["meta"]["reason"], r["url"]] for r in kept],
        as_of=as_of,  # signed: the run admits at THIS time again, so it reproduces the plan
        policy_digest=pol.digest(),
        **({"supersede_scope": scope} if scope != "pending" else {}),
    )
    return doc, kept, dropped, as_of


def _fetch_superseding(args: argparse.Namespace, statuses: tuple[str, ...] = ("stopped", "aborted")) -> int:
    old, reqs, scope = _superseding(args, statuses)
    source = load("sources", old.doc["source"])
    variant = _variant(args, source, "ad")
    pol = policy.from_args(args)
    recs = _records()
    dry = getattr(args, "dry_run", False)
    found = None
    if not args.plan and not dry and _networked(args, [variant]):
        # the as_of its approved plan signed (newest approval first), like fetch-ads' own selection
        for d in approvals.approved_plans("fetch-ads"):
            if (d.get("supersedes") != old.doc["run_id"] or not d.get("as_of")
                    or d.get("policy_digest") != pol.digest() or plan.ts(d["as_of"]) > _now()):  # fmt: skip
                continue
            got = _admitted(args, old, reqs, scope, recs, pol, source, variant, d["as_of"])
            if approvals.has_live(approvals.canonical(got[0])[1]):
                found = got
                break
    doc, kept, dropped, as_of = found or _admitted(
        args, old, reqs, scope, recs, pol, source, variant, _iso(_now())
    )
    if dry:
        _card("fetch-ads", f"DRY RUN: the {len(kept)} admitted {scope} ads of {old.doc['run_id']} (of "
              f"{len(reqs)} queued); nothing is sent", mode="dry-run", planned={"ad": len(kept), "robots": 1}
              if kept else {}, per_minute=args.max_tasks_per_minute)  # fmt: skip
        for r in kept:
            print(json.dumps({"reason": r["meta"]["reason"], "url": r["url"]}))
        print(json.dumps({"summary": True, "queued": len(reqs), "admitted": len(kept), "dropped": dropped}))
        return 0
    if not kept:
        print(json.dumps({"planned": 0, "queued": len(reqs), "dropped": dropped}))
        return 0
    if stop := crawl.latch_stop(source.site):
        raise stop
    _postcondition([{"site": r["meta"]["site"], "ad_id": r["meta"]["ad_id"], "url": r["url"]} for r in kept],
                   recs, as_of, pol)  # fmt: skip
    _polite(args)
    approval = gate(args, doc, [variant])
    if approval and "planned" in approval:
        return 0
    _check_backend(args, variant, "fetch-ads")
    job = crawl.Run.new("fetch-ads", variant, source.site)
    job.doc["supersedes"] = old.doc["run_id"]
    job.doc["supersede_scope"] = scope
    job.doc["ads"] = {"as_of": as_of, "policy_digest": pol.digest(), "eligible": len(kept)}
    if approval:
        job.doc["approval_ref"] = approval
    job.save()
    crawl.event(old.doc["run_id"], "superseded", None, job.doc["run_id"], site=old.doc["source"])
    card = {
        "command": "fetch-ads",
        "purpose": f"fetch the {len(kept)} admitted {scope} ads of stopped run {old.doc['run_id']} (superseding "
        f"it; {len(reqs) - len(kept)} dropped)",
        "planned": {"ad": len(kept), "robots": 1},
        "ceiling": len(kept),
    }
    planned = {(r["meta"]["site"], r["meta"]["ad_id"]) for r in kept}
    return _execute(args, job, source, ad_handler(source), kept, card, ads_outcome(planned, pol), pol)


def cmd_crawl_correct(args: argparse.Namespace) -> int:
    """Offline: one append-only status correction for a run (no request, no approval)."""
    ev = crawl.correct_status(args.run_id, args.status, args.reason, args.note, args.evidence)
    print(json.dumps({"correction": ev}, sort_keys=True))
    return 0


def cmd_crawl_resume(args: argparse.Namespace) -> int:
    job = crawl.Run.load(args.run_id)
    stop, lst = job.doc.get("stop") or {}, job.doc.get("list") or {}
    terminal = ("zero_yield", "parse_drift", "end_unknown", "short_census")
    if stop.get("code") == crawl.EXIT_PARSE or lst.get("status") in terminal:
        # its stopping request is DONE: a resume would drain to `completed` and pass for a reference crawl
        print(f"refusing: run {job.doc['run_id']} stopped terminally (exit {crawl.EXIT_PARSE}: "
              f"{stop.get('reason') or lst.get('status')}); plan a new run instead", file=sys.stderr)  # fmt: skip
        return crawl.EXIT_PARSE
    if job.doc["command"] == "fetch-ads":
        return _resume_fetch_ads(args, job)
    if _walk(lst) and lst["settled"] >= lst["ceiling"]:  # a failed last GET spent it: nothing may be sent
        print(f"refusing: walk {job.doc['run_id']} settled its signed ceiling of {lst['ceiling']} list GETs; "
              "plan a new crawl instead", file=sys.stderr)  # fmt: skip
        return crawl.EXIT_INCOMPLETE
    source = load("sources", job.doc["source"])
    # the plan signs the proxy the run will actually use: its own (a resume keeps it), not a new choice
    args.kept_proxy = (job.doc.get("proxy") or {}).get("url")
    _polite(args)
    approval = gate(
        args,
        run_plan(
            args,
            "crawl resume",
            source,
            [job.doc["variant"]],
            run_id=job.doc["run_id"],
            max_pages=args.max_pages,
            resumes=job.doc["command"],
            pending=crawl.pending(job),  # what is left to send: the card shows it, the approval binds it
            # a walk's signed ceiling and what it already settled: the approval binds what is left of it
            **({k: lst[k] for k in ("mode", "ceiling", "settled")} if _walk(lst) else {}),
            # a frontier's reference, recorded at its start: never chosen again
            **({"ref_crawl_id": lst.get("ref_crawl_id")} if _mode(lst) == "frontier" else {}),
        ),
        [job.doc["variant"]],
    )
    if approval and "planned" in approval:
        return 0
    if approval:
        job.doc["approval_ref"] = approval
    _check_backend(args, job.doc["variant"], "crawl resume")
    handle = (
        list_handler(job, source, args.max_pages)
        if job.doc["command"] == "crawl-lists"
        else ad_handler(source)
    )
    return _execute(
        args,
        job,
        source,
        handle,
        None,
        card={
            "command": "crawl resume",
            "purpose": f"resume {job.doc['command']} run {job.doc['run_id']}",
            # a walk: what its signed ceiling leaves; else what is pending (a list crawl's page 1 may still plan
            # up to max_pages more); unknown: none
            "ceiling": max(lst["ceiling"] - lst["settled"], 0)
            if _walk(lst)
            else None
            if (left := crawl.pending(job)) is None
            else left + (args.max_pages if job.doc["command"] == "crawl-lists" else 0),
        },
        postcheck=list_outcome if job.doc["command"] == "crawl-lists" else None,
    )


def _resume_fetch_ads(args: argparse.Namespace, job: crawl.Run) -> int:
    """A fetch-ads run is never resumed in place: its persisted queue was planned against older records. It is
    rebuilt as a NEW run of its pending ads, each admitted through the gate again (the supersede path), and
    refused (exit 14, before any gate) when the run is aborted, its pending scope was already taken over
    (`_taken_over`), or it is older than the policy window W (its sightings are no longer fresh)."""
    pol = policy.from_args(args)
    rid, now = job.doc["run_id"], _now()
    succ = _taken_over(
        rid, "pending"
    )  # a --failed successor took only the failed scope: pending is still its own
    why = (
        "it tripped the gone-storm breaker" if (job.doc.get("breaker") or {}).get("tripped")
        else "it was aborted" if crawl.status_of(job.doc) == "aborted"
        else f"it was superseded by {succ}" if succ
        else f"it is older than the {pol.window_h} h window" if plan.ts(job.doc["created_at"]) < now - pol.window
        else None
    )  # fmt: skip
    if why:
        print(
            f"refusing: fetch-ads run {rid} is not resumed: {why}; plan a new fetch-ads instead",
            file=sys.stderr,
        )
        return crawl.EXIT_PARSE
    args.supersede, args.failed = rid, False
    args.dry_run = getattr(args, "dry_run", False)
    return _fetch_superseding(args, statuses=("stopped", "running"))


def cmd_probe_robots(args: argparse.Namespace) -> int:
    """ONE robots.txt GET through the run's proxy, after the egress preflight: is the site answering
    normally from here? Gated like any site run (plan, the operator's Touch ID, --approve-network);
    `--proxy-only TEXT` makes the plan pick the configured proxy URL containing TEXT."""
    source = load("sources", args.source)
    variant = "http"  # plain HTTP (impit): one request, no browser, no sub-resources
    if args.proxy_only:
        from . import proxy

        ps = proxy.proxy_set()
        keep = [u for u in (ps.urls if ps else ()) if args.proxy_only in u]
        if len(keep) != 1:
            raise SystemExit(f"--proxy-only {args.proxy_only} matches {len(keep)} configured proxies, not 1")
        args.proxy_exclude = [u for u in ps.urls if u != keep[0]]
    _polite(args)
    approval = gate(
        args,
        run_plan(args, "probe robots", source, [variant], url=source.robots_url, retries=0,
                 timeout_s=crawl.PROBE_TIMEOUT),
        [variant],
    )  # fmt: skip
    if approval and "planned" in approval:
        return 0
    _check_backend(args, variant, "probe robots")
    job = crawl.Run.new("probe", variant, source.site)
    if approval:
        job.doc["approval_ref"] = approval
        job.save()
    card = {"command": "probe robots", "purpose": "ONE robots.txt GET: is the site answering normally?",
            "planned": {"robots": 1}, "ceiling": 0}  # fmt: skip
    try:
        crawl.probe(job, source, _polite(args), _opts(args), card)
    except crawl.Stop as s:
        print(str(s), file=sys.stderr)
        _refund_if_unsent(job, s)
        print(
            json.dumps(
                {"run_id": job.doc["run_id"], "status": job.doc["status"], "probe": job.doc.get("probe")}
            )
        )
        return s.code
    print(json.dumps({"run_id": job.doc["run_id"], "status": job.doc["status"], "probe": job.doc["probe"]}))
    return 0


def cmd_browser_open(args: argparse.Namespace) -> int:
    """HEADED, same executable and same persistent profile, so the operator clears a challenge ONCE."""
    from . import browser

    source = load("sources", args.source)
    print(
        f"Opening the dedicated profile headed at {source.home_url}. Clear what is needed, then CLOSE the window."
    )
    return browser.open_headed(source.home_url)


def _at(o: dict, path: str):
    for part in path.split("."):
        o = o.get(part) if isinstance(o, dict) else None
    return o


def coverage(obs: list[dict], fields: tuple[str, ...]) -> dict[str, float]:
    """Share of sightings with a non-empty value per field. The SOURCE declares the fields (dotted paths
    into observation.v1, `coverage_fields`); each is reported under its last segment."""
    n = len(obs)
    return {
        f.rsplit(".", 1)[-1]: round(sum(1 for o in obs if _at(o, f) not in (None, False, "")) / n, 3)
        if n
        else 0.0
        for f in fields
    }


def cmd_ab(args: argparse.Namespace) -> int:
    """The SAME N list pages in every variant; never an ad page; nothing written but the report.
    A challenge stops THAT variant only (recorded), and the comparison moves on; a cooldown stops all."""
    source = load("sources", args.source)
    names = (
        args.variants.split(",")
        if args.variants
        else [n for n, v in variants.VARIANTS.items() if v.in_default_ab]
    )
    _polite(args)
    approval = gate(
        args,
        run_plan(
            args, "ab", source, names, subcategory=args.subcategory, region=args.region, pages=args.pages
        ),
        names,
    )
    if approval and "planned" in approval:
        return 0
    for name in names:
        _check_backend(args, variants.get(name).name, "ab")
    pages = list(range(1, args.pages + 1))
    report = {
        "schema": "ab.v1",
        "at": utcnow(),
        "site": source.site,
        "subcategory": args.subcategory,
        "pages": pages,
        "variants": {},
    }
    code = 0
    for name in names:
        reqs = [
            {
                "url": source.list_url(args.subcategory, args.region, p),
                "kind": "list",
                "meta": {"subcategory": args.subcategory, "page": p},
            }
            for p in pages
        ]
        job = crawl.Run.new("ab", name, source.site)
        if approval:
            job.doc["approval_ref"] = approval  # one approval covers the whole experiment
        seen: list[dict] = []
        pins = [0]

        def handle(m: dict, res: FetchResult, seen=seen, pins=pins) -> None:
            lp = source.parse_list(
                res.html, url=res.url, subcategory=args.subcategory, page=m["page"], seen_at=utcnow()
            )
            seen.extend(o for o in lp.observations if not o["list"]["sponsored"])
            pins[0] += len(schemaorg.pins(schemaorg.extract(res.html, res.url)))

        stopped = None
        try:
            crawl.run(
                job,
                source,
                handle,
                _polite(args),
                reqs,
                _opts(args),
                {
                    "command": "ab",
                    "purpose": f"A/B variant {name}: {len(pages)} list pages, no ad pages",
                    "planned": {"list": len(pages), "robots": 1},
                    "ceiling": len(pages),
                    "ab_at": store.safe_name(report["at"]),
                },
            )
        except crawl.Stop as s:
            stopped = next(
                (
                    e["reason"]
                    for e in reversed(list(store.read("events")))
                    if e["run_id"] == job.doc["run_id"] and e["kind"] == "challenge"
                ),
                {crawl.EXIT_COOLDOWN: "cooldown", crawl.EXIT_EGRESS: "egress_refused"}.get(s.code, "stopped"),
            )
            code = max(code, s.code)  # a cooldown (6) outranks a single stop (5)
            if s.code in (crawl.EXIT_COOLDOWN, crawl.EXIT_EGRESS):  # nothing else may start
                report["variants"][name] = {
                    "cards": len(seen),
                    "coverage": coverage(seen, source.coverage_fields),
                    "pins_on_pages": pins[0],
                    "stopped": stopped,
                    "requests": job.doc.get("requests") or dict(crawl.ZERO),
                }
                break
        report["variants"][name] = {
            "cards": len(seen),
            "coverage": coverage(seen, source.coverage_fields),
            "pins_on_pages": pins[0],
            "stopped": stopped,
            "requests": job.doc.get("requests") or dict(crawl.ZERO),
        }
    contracts.validate("ab.v1", report)
    out = store.data_dir() / "derived" / f"ab-{store.safe_name(report['at'])}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps(report))
    return code


def latest_listings() -> list[dict]:
    return list(
        store.latest(
            store.read("listings"), lambda r: (r["source"]["site"], r["ad_id"]), "fetched_at"
        ).values()
    )


def _load_json_or_jsonl(src: str) -> list:
    text = (sys.stdin.read() if src == "-" else Path(src).read_text()).strip()
    if text.startswith("```"):  # tolerate one fenced block, nothing else
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, list) else [obj]
    except json.JSONDecodeError:
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def cmd_validate(args: argparse.Namespace) -> int:
    bad = 0
    for i, rec in enumerate(_load_json_or_jsonl(args.file), 1):
        for err in contracts.errors(args.contract, rec):
            bad += 1
            print(f"record {i}: {err}", file=sys.stderr)
    return EXIT_CONTRACT if bad else 0


def cmd_findings(args: argparse.Namespace) -> int:
    if args.action == "validate":
        args.contract = "finding.v1"
        return cmd_validate(args)
    if args.action == "append":
        print(json.dumps({"appended": store.append("findings", _load_json_or_jsonl(args.file))}))
        return 0
    current = store.latest(store.read("findings"), lambda f: f["id"], "tested_at")
    for f in sorted(current.values(), key=lambda f: int(f["id"][1:])):
        print(json.dumps(f, ensure_ascii=False))
    return 0


EXIT_APPROVAL = 9  # an operator-only action was attempted without --approve-operator


def cmd_cooldown(args: argparse.Namespace) -> int:
    _card("cooldown", f"cooldown {args.action}" + (f" for {args.site}" if args.site else ""))
    if args.action == "status":
        sites = (
            [args.site] if args.site else sorted({e["site"] for e in store.read("events") if e.get("site")})
        )
        for site in sites:
            for e in crawl.active_cooldowns(site):
                print(
                    json.dumps(
                        {
                            "site": site,
                            "until": e["until"],
                            "reason": e["reason"],
                            "run_id": e["run_id"],
                            "at": e["at"],
                        }
                    )
                )
        return 0
    if not args.approve_operator:
        print(
            "refusing: clearing a cooldown is the operator's decision. Re-run with --approve-operator.",
            file=sys.stderr,
        )
        return EXIT_APPROVAL
    if not args.site or not (args.reason or "").strip():
        print("cooldown clear needs --site and a non-empty --reason (it is recorded).", file=sys.stderr)
        return EXIT_CONFIG
    e = crawl.clear_cooldown(args.site, args.reason.strip())
    if e is None:
        print(
            json.dumps({"site": args.site, "cleared": None, "note": "no active cooldown; nothing appended"})
        )
        return 0
    print(
        json.dumps({"site": args.site, "cleared": e["clears"], "cleared_at": e["at"], "reason": e["reason"]})
    )
    return 0


def cmd_breaker(args: argparse.Namespace) -> int:
    """`breaker status [--site S]` (JSONL), or the operator's `breaker clear --site S --resolve keep|void --note
    ... --approve-operator`: for each run latching S, one `gone` correction (keep: its quarantined gones are
    real deaths, tombstones again; void: its weak gones were wrong, dropped; a positive marker stands either
    way) and one `breaker` correction {clear: true} that lifts the latch. Appended, never rewritten."""
    _card("breaker", f"breaker {args.action}" + (f" for {args.site}" if args.site else ""))
    runs, events = crawl.run_docs(), list(store.read("events"))
    if args.action == "status":
        tripped = {d["source"] for d in runs if (d.get("breaker") or {}).get("tripped")}
        for site in [args.site] if args.site else sorted(tripped):
            print(json.dumps({"site": site, "latched_by": breaker.latched(site, runs, events)}))
        return 0
    if not args.approve_operator:
        print("refusing: clearing a breaker latch is the operator's decision. Re-run with --approve-operator.",
              file=sys.stderr)  # fmt: skip
        return EXIT_APPROVAL
    if not args.site or not args.resolve or not (args.note or "").strip():
        print("breaker clear needs --site, --resolve keep|void and a non-empty --note (it is recorded).",
              file=sys.stderr)  # fmt: skip
        return EXIT_CONFIG
    cleared = []
    for rid in breaker.latched(args.site, runs, events):
        ev = [f"DATA:runs/{store.safe_name(rid)}.json", "DATA:events.jsonl"]
        gone = {"quarantine": False} if args.resolve == "keep" else {"void": True, "scope": "weak"}
        crawl.append_correction(run_id=rid, field="gone", recorded={"quarantine": True, "scope": "weak"},
                                reconciled=gone | {"by": "breaker clear"}, note=args.note.strip(), evidence=ev,
                                site=args.site)  # fmt: skip
        crawl.append_correction(run_id=rid, field="breaker", recorded={"tripped": True},
                                reconciled={"clear": True, "resolve": args.resolve,
                                            **({"plugin_version": args.plugin_version} if args.plugin_version else {})},
                                note=args.note.strip(), evidence=ev, site=args.site)  # fmt: skip
        cleared.append(rid)
    print(json.dumps({"site": args.site, "resolve": args.resolve, "cleared": cleared}))
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Copy (never move) an old single root into DATA + STATE, verifying every file's sha256.
    stdout is pure JSONL: one manifest line per file, then ONE summary line (`"summary": true`), all
    on the same stream so no buffering interleave can split a line."""
    _card(
        "migrate",
        "copy an old single root into DATA + STATE (copies only)",
        roots={
            "OLD": Path(args.source or os.environ.get(store.data_env(), "")).expanduser().resolve(),
            "DATA": Path(args.to_data).expanduser().resolve(),
            "STATE": Path(args.to_state).expanduser().resolve(),
        },
    )
    from . import migrate

    src = Path(args.source or os.environ.get(store.data_env(), "")).expanduser()
    if not str(src) or not src.is_dir():
        print(
            f"{pipeline.consumer().cli} migrate: pass --from <old root> (default: ${store.data_env()})",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    rows, code = migrate.run(src, Path(args.to_data), Path(args.to_state), args.dry_run)
    tally: dict = {}
    for r in rows:
        key = r["action"] if r["action"] != "skip" else f"skip:{r.get('reason')}"
        tally[key] = tally.get(key, 0) + 1
    out = [json.dumps(r, sort_keys=True) for r in rows]
    out.append(
        json.dumps(
            {
                "summary": True,
                "dry_run": args.dry_run,
                "files": len(rows),
                "actions": tally,
                "exit": code,
                "next": "check, then delete the old copy yourself"
                if not args.dry_run and code == 0
                else None,
            },
            sort_keys=True,
        )
    )
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()
    return code


def cmd_approve(args: argparse.Namespace) -> int:
    plan_doc = approvals.load_plan(args.plan_hash)
    if plan_doc.get("command") == "refresh" and plan_doc.get("kind") == "bounded":
        if args.ttl is not None:
            raise SystemExit(
                "a refresh plan carries its own TTL (`refresh --ttl`); `approve --ttl` does not apply"
            )
        rec = approvals.approve_bounded(args.plan_hash)
        print(json.dumps({"approved": rec["plan_hash"], "approval_id": rec["approval_id"],
                          "expires_at": rec["expires_at"], "bounded": True}))  # fmt: skip
        return 0
    rec = approvals.approve(args.plan_hash, args.ttl or approvals.DEFAULT_TTL)
    print(
        json.dumps(
            {"approved": rec["plan_hash"], "approval_id": rec["approval_id"], "expires_at": rec["expires_at"]}
        )
    )
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    """`approvals verify` (chain + signatures), `approvals standing BOUNDS.json` (Touch ID signs bounds
    for scheduled runs), `approvals revoke ID --note ...` (unsigned; it only reduces authority)."""
    if args.action == "standing":
        if not args.arg:
            raise SystemExit("approvals standing needs a bounds JSON file")
        bounds = json.loads(Path(args.arg).read_text())
        rec = approvals.approve_standing(bounds)
        print(
            json.dumps(
                {
                    "standing": rec["approval_id"],
                    "plan_hash": rec["plan_hash"],
                    "expires_at": rec["expires_at"],
                }
            )
        )
        return 0
    if args.action == "revoke":
        if not args.arg:
            raise SystemExit("approvals revoke needs the approval id")
        print(json.dumps(approvals.revoke(args.arg, args.note or ""), sort_keys=True))
        return 0
    report = approvals.audit()
    print(json.dumps(report, sort_keys=True))
    return 0 if not report["bad_signatures"] else approvals.EXIT_UNAPPROVED


def _reclassify(args: argparse.Namespace) -> int:
    """Record that a past STOP was misclassified, e.g. a gone ad stopped as signin_wall: one run_correction
    (field "stop", recorded vs reconciled, linked to the incident), then the incident is acked with the
    note. The run record is never rewritten; the challenge counted then stays counted (cooldown unchanged)."""
    if not (args.id and args.as_ and (args.note or "").strip()):
        raise SystemExit("incidents reclassify needs the incident id, --as gone and a --note")
    opened = {r["id"]: r for r in incidents.read() if r["event"] == "opened"}
    inc = opened.get(args.id)
    if inc is None:
        raise SystemExit(f"{args.id}: no such incident (list them with `incidents list`)")
    if inc.get("kind") not in ("challenge", "cooldown") or not inc.get("run_id"):
        raise SystemExit(
            f"{args.id}: not a run's challenge stop (kind {inc.get('kind')}); nothing to reclassify"
        )
    if any(e["kind"] == "run_correction" and (e.get("correction") or {}).get("incident") == args.id
           for e in store.read("events")):  # fmt: skip
        raise SystemExit(f"{args.id}: already reclassified")
    # the stop it records: the run's last challenge at or before the incident (a cooldown incident is
    # opened by the challenge that tripped it)
    ch = [
        e for e in store.read("events")
        if e.get("run_id") == inc["run_id"] and e["kind"] == "challenge" and e["at"] <= inc["at"]
    ]  # fmt: skip
    if not ch:
        raise SystemExit(f"{args.id}: run {inc['run_id']} logged no challenge before it")
    # the incident's reason names the stopped URL ("... at <url>"): the exact match beats a timestamp
    named = [e for e in ch if e.get("url") and f" at {e['url']}" in (inc.get("reason") or "")]
    last = (named or ch)[-1]
    ev = crawl.append_correction(
        run_id=inc["run_id"],
        field="stop",
        recorded={"reason": last.get("reason"), "url": last.get("url"), "at": last.get("at")},
        reconciled={"reason": args.as_, "detail": args.detail},
        note=args.note,
        evidence=args.evidence,
        site=last.get("site"),
        incident=args.id,
    )
    is_open = args.id in {r["id"] for r in incidents.open_ones()}
    if is_open:  # an incident already acked keeps its ack; the correction is the new record
        incidents.ack(args.id, f"reclassified as {args.as_} ({args.detail}): {args.note}", args.by)
    print(json.dumps({"correction": ev, "acked_now": is_open}, sort_keys=True))
    return 0


def cmd_incidents(args: argparse.Namespace) -> int:
    """`incidents list [--open]` (JSONL) / `incidents ack ID --note ...` / `incidents reclassify ID --as gone
    --note ...` (all append-only)."""
    if args.action == "reclassify":
        return _reclassify(args)
    if args.action == "ack":
        if not args.id:
            raise SystemExit("incidents ack needs the incident id")
        print(json.dumps(incidents.ack(args.id, args.note or "", args.by), sort_keys=True))
        return 0
    rows = incidents.open_ones() if args.open else incidents.read()
    for r in rows:
        print(json.dumps(r, sort_keys=True))
    return 0


def cmd_plugins(_: argparse.Namespace) -> int:
    print(
        json.dumps({g: available(g) for g in ("sources", "drivers")} | {"variants": list(variants.VARIANTS)})
    )
    return 0


# ── argument parsing ─────────────────────────────────────────────────────────────────────────


def cmd_claude_settings(args: argparse.Namespace) -> int:
    """Render the project's .claude/settings.json from pipeline.toml (--write), or check it (default)."""
    from . import claude_settings

    if args.write:
        print(json.dumps({"written": str(claude_settings.write())}))
        return 0
    ok = claude_settings.check()
    print(json.dumps({"path": str(claude_settings.target()), "in_sync": ok}))
    return 0 if ok else EXIT_CONFIG


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=pipeline.consumer().cli, description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def net(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--approve-network", action="store_true", help="the operator approved THIS networked run"
        )
        sp.add_argument(
            "--proxy-exclude",
            action="append",
            metavar="TEXT",
            help="--plan: never choose a [crawl.proxy] url containing TEXT (repeatable); the plan signs the pick",
        )
        sp.add_argument(
            "--concurrency",
            type=int,
            default=crawl.CONCURRENCY,
            help=f"parallel requests (default and ceiling {crawl.CONCURRENCY})",
        )
        sp.add_argument(
            "--max-tasks-per-minute",
            type=float,
            default=crawl.DEFAULT_RATE,
            help=f"rate limit (default {crawl.DEFAULT_RATE}; ceiling {crawl.MAX_RATE})",
        )
        sp.add_argument(
            "--approve-politeness-override",
            action="store_true",
            help="the OPERATOR approved exceeding the concurrency/rate ceiling for this run",
        )
        sp.add_argument(
            "--plan",
            action="store_true",
            help=f"print this run's canonical plan and plan_hash for `{pipeline.consumer().cli} approve`; no network",
        )

    def driver(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--driver", help="force a backend (fixture = offline); default: the variant's backend"
        )
        sp.add_argument("--fixtures", help="fixture backend: directory with urls.json")
        sp.add_argument(
            "--variant", help=f"page variant: {', '.join(variants.VARIANTS)} (default {variants.DEFAULT})"
        )

    def site(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--source", default=pipeline.consumer().default_site)
        sp.add_argument("--region", default=pipeline.consumer().default_region)

    c = sub.add_parser("crawl-lists", help="read list pages -> observations.jsonl (depth-stratified)")
    site(c)
    c.add_argument("--subcategory", required=True)
    c.add_argument("--page-count", type=int, help="total pages; read from page 1 when omitted")
    c.add_argument("--stride", type=int, default=1, help="every k-th page (1 = census)")
    c.add_argument("--start", type=int, help="first page in [1, stride]; random from --seed when omitted")
    c.add_argument("--seed", type=int)
    c.add_argument(
        "--max-pages",
        type=int,
        default=60,
        help="the most list pages to read; with --mode walk, every list GET the walk may send (the END page "
        "included)",
    )
    c.add_argument(
        "--mode",
        choices=list(plan.WALK_MODES),
        help="walk: read pages 1, 2, 3, ..., the next only after the current one is handled, to a positive END "
        "or --max-pages (no page-count discovery); stride 1 only (a census). frontier: the same, and it may stop "
        "early at an anchor against the latest reference crawl, confirmed by one more page (only for a source "
        "whose order is bump_desc)",
    )
    c.add_argument(
        "--min-pages",
        type=int,
        help="with --mode walk: an END before this many pages is short_census (exit 14)",
    )
    c.add_argument(
        "--pages",
        type=page_list,
        metavar="N,N,...",
        help="read exactly these list pages, in order (no page-count discovery); not with --stride/--start/"
        "--seed/--page-count; counts toward --max-pages",
    )
    c.add_argument(
        "--snapshot",
        action="store_true",
        help="save each answered list page (gzip) and a sidecar {status, final_url, ...} under "
        "raw/lists/<site>/<crawl_id>/, before any parse or stop; a 404/410 list page is recorded, not a stop",
    )
    driver(c)
    net(c)
    c.add_argument(
        "--refresh-run",
        metavar="RUN_ID",
        help="internal: run as one step of `<cli> refresh` RUN_ID, under its bounded approval (the runner adds it)",
    )
    c.set_defaults(fn=cmd_crawl_lists)

    f = sub.add_parser("fetch-ads", help="GET ad pages the refetch rule selects -> listings.jsonl")
    f.add_argument(
        "--max-fetches",
        type=int,
        default=40,
        help="without --sample: the budget B, a ceiling (select_ads; an empty pool sends nothing)",
    )
    f.add_argument("--window-h", type=int, help=f"a sighting is fresh within H hours of the plan (default "
                   f"{policy.DEFAULT.window_h}); older ones are dormant")  # fmt: skip
    f.add_argument("--rho", type=float, help=f"the revisit lane's hard cap, a share of the budget (default "
                   f"{policy.DEFAULT.rho})")  # fmt: skip
    f.add_argument("--stale-after-d", type=int, help=f"a stored copy older than D days is due a revisit "
                   f"(default {policy.DEFAULT.stale_after_d})")  # fmt: skip
    f.add_argument("--sample", type=int, help="pick N candidates, stratified (see --stratify)")
    f.add_argument(
        "--stratify", choices=["depth"], default="depth", help="strata: (subcategory, depth decile)"
    )
    f.add_argument("--seed", type=int, help="seed of the sample's RNG (required with --sample)")
    f.add_argument("--subcategories", help="comma-separated subcategories to sample from (default: all)")
    f.add_argument(
        "--allocation",
        choices=["equal", "proportional"],
        default="equal",
        help="across subcategories (default equal: the comparison design); within one: proportional",
    )
    f.add_argument(
        "--select",
        choices=["refetch", "recruit"],
        default="refetch",
        help="refetch (default): the refetch rule; recruit: a one-off --sample of ads seen on list cards "
        "but never opened (no listing, no snapshot), fetch_reason recruit",
    )
    f.add_argument("--dry-run", action="store_true", help="print the plan; no network")
    f.add_argument(
        "--supersede",
        metavar="RUN_ID",
        help="a NEW run of exactly that stopped fetch-ads run's pending ads (its own plan, approval, proxy)",
    )
    f.add_argument(
        "--failed",
        action="store_true",
        help="with --supersede: take its FAILED ads (retries exhausted) instead of its pending ones",
    )
    driver(f)
    net(f)
    f.add_argument(
        "--refresh-run",
        metavar="RUN_ID",
        help="internal: run as one step of `<cli> refresh` RUN_ID, under its bounded approval (the runner adds it)",
    )
    f.set_defaults(fn=cmd_fetch_ads)

    rf = sub.add_parser(
        "refresh",
        help="the ONE on-demand chain ([crawl.refresh] steps, by doit) under ONE bounded Touch ID approval",
        description=(refresh.__doc__ or "").split("\n\n")[0],
        epilog="Without a live approval it prints the plan (bounds + plan_hash) and exits 10, sending nothing; "
        "`approve <plan_hash>` signs it. Exit codes: 0 complete; 10 not approved, or already running; 3 a bound exceeded, a paid "
        "count not confirmed, or --approve-network missing; otherwise the failing step's own code (5/6 end "
        "the refresh for good; the others resume with the same command, but a site step's resume counts its "
        "ceiling again, so it fits only if the bounds leave that headroom).",
    )
    rf.add_argument("--approve-network", action="store_true", help="the operator approved THIS refresh")
    rf.add_argument("--ttl", help=f"the bounded approval's lifetime (default {refresh.DEFAULT_TTL}, max 24h)")
    rf.add_argument(
        "--max-ad-requests", type=int, help="the most ad pages in all (default: the fetch-ads step's own cap)"
    )
    for st in pipeline.current().refresh:
        if st.count_field:
            rf.add_argument(
                st.count_flag,
                type=int,
                metavar="N",
                dest=refresh.count_dest(st),
                help=f"confirm the {st.name} step's paid count ({st.count_field} from its --dry-run); 0 needs none",
            )
    rf.add_argument(
        "--abandon", action="store_true", help="end the unfinished refresh run and spend its approval"
    )
    rf.add_argument(
        "--dry-run", action="store_true", help="print the run card and the bounds; nothing is sent or saved"
    )
    rf.set_defaults(fn=refresh.cmd)
    # `run` is handled before parsing (main); this entry only lists it in --help
    sub.add_parser(
        "run", help="the declared stages as doit tasks: `run [doit args] [-- args for networked stages]`"
    )

    cr = sub.add_parser("crawl", help="crawl run control")
    crs = cr.add_subparsers(dest="action", required=True)
    rs = crs.add_parser("resume", help="continue a stopped run from its persisted queue (no duplicate GETs)")
    rs.add_argument("--run-id", help="default: the latest unfinished run")
    rs.add_argument("--max-pages", type=int, default=60)
    driver(rs)
    net(rs)
    rs.set_defaults(fn=cmd_crawl_resume)
    cc = crs.add_parser(
        "correct",
        help="record a run's real END (append-only run_correction; the run record is not rewritten)",
    )
    cc.add_argument("--run-id", required=True)
    cc.add_argument("--status", required=True, choices=["stopped", "aborted", "completed"])
    cc.add_argument("--reason", required=True, help="why it ended, e.g. unreachable or interrupted")
    cc.add_argument("--note", required=True, help="what the evidence showed")
    cc.add_argument("--evidence", action="append", default=[], help="a further path the decision read")
    cc.set_defaults(fn=cmd_crawl_correct)

    mg = sub.add_parser(
        "migrate", help="COPY an old single root into DATA (synced) and STATE (local); never moves"
    )
    mg.add_argument("--from", dest="source", help=f"the old root (default: ${store.data_env()})")
    mg.add_argument("--to-data", required=True)
    mg.add_argument("--to-state", required=True)
    mg.add_argument("--dry-run", action="store_true")
    mg.set_defaults(fn=cmd_migrate)

    ap = sub.add_parser(
        "approve", help="approve one exact run plan, once: the operator's Touch ID signs it (Secretive)"
    )
    ap.add_argument("plan_hash")
    ap.add_argument(
        "--ttl",
        help="how long it stays usable (default 30m, max 24h); a refresh plan carries its own (refresh --ttl)",
    )
    ap.set_defaults(fn=cmd_approve)

    co = sub.add_parser(
        "cooldown", help="site cooldowns: status, or an audited operator clear (appends, never deletes)"
    )
    co.add_argument("action", choices=["status", "clear"])
    co.add_argument("--site", help=f"e.g. {pipeline.consumer().default_site} (status: default all sites)")
    co.add_argument("--reason", help="clear: why (recorded in the event log)")
    co.add_argument("--approve-operator", action="store_true", help="clear: the OPERATOR decided this")
    co.set_defaults(fn=cmd_cooldown)

    bk = sub.add_parser(
        "breaker", help="the gone-storm breaker's site latch: status, or an audited operator clear (appends)"
    )
    bk.add_argument("action", choices=["status", "clear"])
    bk.add_argument("--site", help="the latched site")
    bk.add_argument("--resolve", choices=["keep", "void"],
                    help="clear: keep (the quarantined gones were real deaths) or void (they were wrong)")  # fmt: skip
    bk.add_argument("--note", help="clear: what the evidence showed (recorded)")
    bk.add_argument(
        "--plugin-version", help="clear: the source plug-in version that fixes it, if any (recorded)"
    )
    bk.add_argument("--approve-operator", action="store_true", help="clear: the OPERATOR decided this")
    bk.set_defaults(fn=cmd_breaker)

    pr = sub.add_parser("probe", help="one-request checks of the site")
    prs = pr.add_subparsers(dest="action", required=True)
    po = prs.add_parser(
        "robots", help="ONE robots.txt GET (no retry) after the egress preflight: clean or not"
    )
    po.add_argument("--source", default=pipeline.consumer().default_site)
    po.add_argument(
        "--proxy-only", metavar="TEXT", help="--plan: the one configured proxy URL containing TEXT"
    )
    driver(po)
    net(po)
    po.set_defaults(fn=cmd_probe_robots)

    b = sub.add_parser("browser", help="the dedicated browser profile")
    bs = b.add_subparsers(dest="action", required=True)
    bo = bs.add_parser("open", help="open the dedicated profile HEADED to clear a challenge or gate once")
    bo.add_argument("--source", default=pipeline.consumer().default_site)
    bo.set_defaults(fn=cmd_browser_open)

    ab = sub.add_parser(
        "ab", help="same N list pages in every page variant -> derived/ab-*.json (no ad pages)"
    )
    site(ab)
    ab.add_argument("--subcategory", required=True)
    ab.add_argument("--pages", type=int, default=2)
    ab.add_argument(
        "--variants", help="comma-separated (default: all but desktop-unblocked, an explicit A/B arm)"
    )
    driver(ab)
    net(ab)
    ab.set_defaults(fn=cmd_ab)

    rp = sub.add_parser("reparse", help="re-extract listings from saved ad snapshots, OFFLINE (no new views)")
    rp.add_argument("--site", help="only this source site")
    rp.add_argument("--since", help="only versions fetched at or after this ISO-8601 UTC time")
    rp.set_defaults(fn=cmd_reparse)

    fi = sub.add_parser("findings", help="the findings register (finding.v1)")
    fi.add_argument("action", choices=["validate", "append", "current"])
    fi.add_argument("file", nargs="?", default="-", help="JSON or JSONL file, or - for stdin")
    fi.set_defaults(fn=cmd_findings)
    inc = sub.add_parser("incidents", help="run stops and refusals (STATE/ops/incidents.jsonl): list, ack")
    inc.add_argument("action", choices=["list", "ack", "reclassify"])
    inc.add_argument("--as", dest="as_", choices=["gone"], help="reclassify: what the stop really was")
    inc.add_argument(
        "--detail", default="redirected_to_list", help="reclassify: why (default: redirected_to_list)"
    )
    inc.add_argument("--evidence", action="append", default=[], help="reclassify: a path the decision read")
    inc.add_argument("id", nargs="?")
    inc.add_argument("--open", action="store_true", help="list: only incidents not yet acked")
    inc.add_argument("--note", help="ack: what was done or decided (required)")
    inc.add_argument("--by", choices=["agent", "operator"], default="agent")
    inc.set_defaults(fn=cmd_incidents)
    av = sub.add_parser("approvals", help="audit the approval ledger: hash chain + signatures (read-only)")
    av.add_argument("action", choices=["verify", "standing", "revoke"])
    av.add_argument("arg", nargs="?", help="standing: the bounds JSON file; revoke: the approval id")
    av.add_argument("--note", help="revoke: why (required)")
    av.set_defaults(fn=cmd_approvals)
    cs = sub.add_parser("claude-settings", help="render/check .claude/settings.json from pipeline.toml")
    cs.add_argument("--write", action="store_true", help="write it (default: check, exit 4 on drift)")
    cs.set_defaults(fn=cmd_claude_settings)

    v = sub.add_parser("validate", help="validate JSON/JSONL against a named contract")
    v.add_argument("contract", help="e.g. listing.v1, observation.v1, finding.v1")
    v.add_argument("file", nargs="?", default="-")
    v.set_defaults(fn=cmd_validate)

    sub.add_parser("plugins", help="list registered sources, drivers, variants").set_defaults(fn=cmd_plugins)
    # The consumer's own subcommands (e.g. analysis stages), declared in pipeline.toml [plugins].commands.
    for spec in pipeline.current().commands:
        pipeline.resolve(spec)(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["statusline"]:  # Claude Code's status line: one fast line, never a failure
        from . import status

        return status.statusline()
    if argv[:1] == [
        "run"
    ]:  # the stages as doit tasks: `<cli> run [doit args] [-- args for networked stages]`
        from . import tasks

        rest = argv[1:]
        extra = rest[rest.index("--") + 1 :] if "--" in rest else []
        return tasks.run(rest[: rest.index("--")] if "--" in rest else rest, extra)
    if argv[:1] == ["plan"]:  # `<cli> plan <command> <args>` == `<cli> <command> <args> --plan`
        argv = [*argv[1:], "--plan"]
    try:
        args = parser().parse_args(argv)
    except pipeline.PipelineError as e:  # e.g. a declared site file is unreadable: one line, not a traceback
        print(f"refusing: {e}", file=sys.stderr)
        return 1
    try:
        return args.fn(args)
    except (
        approvals.BoundExceeded
    ) as e:  # a refresh step outside its bounds: it stops the chain, nothing spent
        print(f"STOPPED: {e}", file=sys.stderr)
        incidents.open_incident(EXIT_NETWORK, str(e), None, getattr(args, "cmd", None))
        return EXIT_NETWORK
    except approvals.NotApproved as e:
        print(f"refusing: {e}", file=sys.stderr)
        if getattr(args, "cmd", None) not in ("approve", "approvals"):  # a refused RUN, not a refused approve
            incidents.open_incident(approvals.EXIT_UNAPPROVED, str(e), None, getattr(args, "cmd", None))
        return approvals.EXIT_UNAPPROVED
    except crawl.Stop as s:
        print(str(s), file=sys.stderr)
        incidents.open_incident(s.code, str(s), None, getattr(args, "cmd", None), kind=s.kind)
        return s.code
    except contracts.ContractError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONTRACT
    except store.DataDirError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
