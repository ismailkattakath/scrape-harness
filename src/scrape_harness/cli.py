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
    contracts,
    crawl,
    egress,
    incidents,
    pipeline,
    plan,
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
    def handle(m: dict, res: FetchResult) -> list[dict]:
        lp = source.parse_list(
            res.html, url=res.url, subcategory=m["subcategory"], page=m["page"], seen_at=utcnow()
        )
        new: list[dict] = []
        weight = m.get("sample_weight")
        page_count = m.get("page_count") or lp.page_count
        if m.get("discover"):  # page 1 told us the page count: now plan the sample, once
            if lp.page_count is None:
                raise crawl.Stop(EXIT_CONFIG, "page count not found on page 1; pass --page-count")
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
        for o in lp.observations:
            o["crawl_id"] = job.doc["run_id"]
            o["list"]["page_count"] = page_count
            o["list"]["sample_weight"] = weight
        store.append("observations", lp.observations)
        return new

    return handle


def ad_handler(source):
    def handle(m: dict, res: FetchResult) -> None:
        fetched_at = utcnow()
        rec = source.parse_ad(res.html, url=res.url, fetched_at=fetched_at, fetch_reason=m["reason"])
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
) -> int:
    try:
        crawl.run(job, source, handle, _polite(args), requests, _opts(args), card)
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
            if plan_doc.get("page_count")
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
    bounds stops (exit 3). The same checks precede it."""
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


def cmd_crawl_lists(args: argparse.Namespace) -> int:
    source = load("sources", args.source)
    variant = _variant(args, source, "list")
    _polite(args)
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
    }
    if args.page_count:
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
    job = crawl.Run.new("crawl-lists", variant, source.site)
    if approval:
        job.doc["approval_ref"] = approval
    how = (
        "census" if args.stride == 1 else f"every {args.stride}th page, start {args.start}, seed {args.seed}"
    )
    what = f"{len(reqs)} list pages" if args.page_count else "page 1, then the sample it plans"
    card = {
        "command": "crawl-lists",
        "purpose": f"read {what} of {args.subcategory} ({how})",
        "planned": {"list": len(reqs), "robots": 1},
        # crawlee's ceiling: the planned pages, or page 1 plus the most its sample may add
        "ceiling": len(reqs) if args.page_count else 1 + args.max_pages,
    }
    return _execute(args, job, source, list_handler(job, source, args.max_pages), reqs, card)


def fetch_plan(now: datetime) -> list[tuple[str, dict]]:
    sightings: dict[tuple, list[dict]] = {}
    for o in store.read("observations"):
        if not o["list"].get("sponsored"):
            sightings.setdefault((o["site"], o["ad_id"]), []).append(o)
    latest = store.latest(store.read("listings"), lambda r: (r["source"]["site"], r["ad_id"]), "fetched_at")
    out = []
    for key, seen in sightings.items():
        reason = plan.fetch_reason(latest.get(key), seen, now)
        if reason:
            out.append((reason, max(seen, key=lambda o: o["seen_at"])))
    order = {"new": 0, "card_changed": 1, "stale_7d": 2}
    return sorted(out, key=lambda t: (order[t[0]], t[1]["seen_at"]))


def never_opened(cands: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Recruitment pool: refetch-rule candidates never fetched ("new": no listing version) that also
    have no raw snapshot, each re-tagged "recruit" so its listing and manifest row say why it exists."""
    raw = store.data_dir() / "raw"
    return [
        ("recruit", o)
        for reason, o in cands
        if reason == "new" and not (raw / store.safe_name(o["site"]) / store.safe_name(o["ad_id"])).exists()
    ]


def _sample(args: argparse.Namespace) -> tuple[list[tuple[str, dict]], dict | None, list[dict]]:
    """(todo, sample design or None, picks). Without --sample: the first --max-fetches candidates.
    --select recruit: the same stratified sampler, over never-opened ads only (needs --sample)."""
    cands = fetch_plan(datetime.now(UTC))
    recruit = getattr(args, "select", "refetch") == "recruit"
    if recruit:
        if not args.sample:
            raise SystemExit("--select recruit needs --sample N (a seeded, stratified one-off sample)")
        cands = never_opened(cands)
    if not args.sample:
        return cands[: args.max_fetches], None, []
    if args.seed is None:
        raise SystemExit("--sample needs --seed, so the approved sample is the one fetched")
    subs = args.subcategories.split(",") if args.subcategories else None
    picks, strata = plan.stratified_sample(cands, args.sample, args.seed, args.allocation, subs)
    design = {
        "n": args.sample,
        "seed": args.seed,
        "stratify": args.stratify,
        "allocation": args.allocation,
        "subcategories": subs,
        "strata": strata,
        **({"select": "recruit"} if recruit else {}),
    }
    return [(p["reason"], p["obs"]) for p in picks], design, picks


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


def _superseding(args: argparse.Namespace):
    """fetch-ads --supersede RUN: exactly that stopped run's PENDING ad requests (or, with --failed, its
    FAILED ones), as a NEW run (its own plan, approval and proxy). The old run gets an append-only
    `superseded` event naming the new one. Each scope is taken over ONCE: the old run's own queue never
    changes, so a second supersede of the same scope would GET the same ads again."""
    old = crawl.Run.load(args.supersede)
    if old.doc["command"] != "fetch-ads" or crawl.status_of(old.doc) not in ("stopped", "aborted"):
        raise SystemExit(f"--supersede needs a stopped or aborted fetch-ads run; {args.supersede} is "
                         f"{old.doc['command']} / {crawl.status_of(old.doc)}")  # fmt: skip
    scope = "failed" if getattr(args, "failed", False) else "pending"
    for e in store.read("events"):
        if e.get("run_id") == old.doc["run_id"] and e["kind"] == "superseded":
            successor = crawl.Run.load(e["reason"]).doc
            if successor.get("supersede_scope", "pending") == scope:  # runs before the field took "pending"
                raise SystemExit(f"{args.supersede}: its {scope} ads were already taken over by "
                                 f"{e['reason']}; superseding again would GET them twice")  # fmt: skip
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
    todo, design, picks = _sample(args)
    if args.dry_run:
        _card(
            "fetch-ads",
            f"DRY RUN: list the {len(todo)} ads a run would fetch; nothing is sent",
            mode="dry-run",
            planned={"ad": len(todo), "robots": 1} if todo else {},
            per_minute=args.max_tasks_per_minute,
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
        print(json.dumps({"planned": 0}))
        return 0
    source = load("sources", sites.pop())
    variant = _variant(args, source, "ad")
    _polite(args)
    approval = gate(
        args,
        run_plan(
            args,
            "fetch-ads",
            source,
            [variant],
            max_fetches=None if design else args.max_fetches,
            sample=design,
            **({"select": "recruit"} if design and design.get("select") == "recruit" else {}),
            ads=[[r, o["ad_url"]] for r, o in todo],
        ),
        [variant],
    )
    if approval and "planned" in approval:
        return 0
    _check_backend(args, variant, "fetch-ads")
    reqs = [
        {
            "url": o["ad_url"],
            "kind": "ad",
            "meta": {"reason": r, "card_city_label": o["card"].get("city_label")},
        }
        for r, o in todo
    ]
    job = crawl.Run.new("fetch-ads", variant, source.site)
    if approval:
        job.doc["approval_ref"] = approval
    if design:
        job.doc["sample"] = {**design, "manifest": _write_sample_manifest(job, source, design, picks)}
    how = (
        f"depth-stratified sample, seed {design['seed']}, {design['allocation']} allocation"
        if design
        else "first candidates of the refetch rule"
    )
    card = {
        "command": "fetch-ads",
        "purpose": f"fetch {len(todo)} ad pages ({how})",
        "planned": {"ad": len(todo), "robots": 1},
        "ceiling": len(reqs),
        "sample_file": store.safe_name(job.doc["run_id"]) if design else None,
    }
    return _execute(args, job, source, ad_handler(source), reqs, card)


def _fetch_superseding(args: argparse.Namespace) -> int:
    old, reqs, scope = _superseding(args)
    source = load("sources", old.doc["source"])
    variant = _variant(args, source, "ad")
    if args.dry_run:
        _card("fetch-ads", f"DRY RUN: the {len(reqs)} {scope} ads of {old.doc['run_id']}; nothing is sent",
              mode="dry-run", planned={"ad": len(reqs), "robots": 1}, per_minute=args.max_tasks_per_minute)  # fmt: skip
        for r in reqs:
            print(json.dumps({"reason": r["meta"].get("reason"), "url": r["url"]}))
        return 0
    _polite(args)
    approval = gate(
        args,
        run_plan(
            args,
            "fetch-ads",
            source,
            [variant],
            supersedes=old.doc["run_id"],
            ads=[[r["meta"].get("reason"), r["url"]] for r in reqs],
            **({"supersede_scope": scope} if scope != "pending" else {}),  # pending plans hash as before
        ),  # fmt: skip
        [variant],
    )
    if approval and "planned" in approval:
        return 0
    _check_backend(args, variant, "fetch-ads")
    job = crawl.Run.new("fetch-ads", variant, source.site)
    job.doc["supersedes"] = old.doc["run_id"]
    job.doc["supersede_scope"] = scope
    if approval:
        job.doc["approval_ref"] = approval
    job.save()
    crawl.event(old.doc["run_id"], "superseded", None, job.doc["run_id"], site=old.doc["source"])
    card = {
        "command": "fetch-ads",
        "purpose": f"fetch the {len(reqs)} {scope} ads of stopped run {old.doc['run_id']} (superseding it)",
        "planned": {"ad": len(reqs), "robots": 1},
        "ceiling": len(reqs),
    }
    return _execute(args, job, source, ad_handler(source), reqs, card)


def cmd_crawl_correct(args: argparse.Namespace) -> int:
    """Offline: one append-only status correction for a run (no request, no approval)."""
    ev = crawl.correct_status(args.run_id, args.status, args.reason, args.note, args.evidence)
    print(json.dumps({"correction": ev}, sort_keys=True))
    return 0


def cmd_crawl_resume(args: argparse.Namespace) -> int:
    job = crawl.Run.load(args.run_id)
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
        {
            "command": "crawl resume",
            "purpose": f"resume {job.doc['command']} run {job.doc['run_id']}",
            # what is pending (a list crawl's page 1 may still plan up to max_pages more); unknown: none
            "ceiling": None
            if (left := crawl.pending(job)) is None
            else left + (args.max_pages if job.doc["command"] == "crawl-lists" else 0),
        },
    )


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
    c.add_argument("--max-pages", type=int, default=60)
    driver(c)
    net(c)
    c.add_argument(
        "--refresh-run",
        metavar="RUN_ID",
        help="internal: run as one step of `<cli> refresh` RUN_ID, under its bounded approval (the runner adds it)",
    )
    c.set_defaults(fn=cmd_crawl_lists)

    f = sub.add_parser("fetch-ads", help="GET ad pages the refetch rule selects -> listings.jsonl")
    f.add_argument("--max-fetches", type=int, default=40, help="without --sample: the first N candidates")
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
        incidents.open_incident(s.code, str(s), None, getattr(args, "cmd", None))
        return s.code
    except contracts.ContractError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONTRACT
    except store.DataDirError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
