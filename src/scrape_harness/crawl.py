"""Crawl runs on crawlee-python (operator decision, ADR-0003). crawlee owns the request queue
(persisted, dedup by unique_key), retries, robots.txt and the concurrency/rate limits. What stays
ours, by design:

  - the politeness CEILING: concurrency 1 and <= 60 requests/minute unless the operator passes an
    explicit override; robots.txt Crawl-delay lowers the rate further;
  - challenge STOP: crawlee's own block handling is switched off (retry_on_blocked=False, the block
    statuses are passed to our handler), so the first 403/429/503/captcha/wall/gate stops the run;
  - the SITE cooldown after two challenges for one site within an hour (across runs and A/B
    variants), challenge evidence, the run record (run.v1) and events (event.v1);
  - the gone-storm and ad-failure BREAKER (breaker.py): a run whose ad pages look gone (or fail to parse)
    far more often than chance trips, quarantines its doubtful gones, stops (exit 14) and latches its site;
  - refetch-on-change (plan.py): crawlee's queue dedups, it does not decide WHAT to fetch.

Every backend (impit HTTP, Playwright browser, offline fixture) is a crawlee crawler behind the
same shape, so the handler sees one FetchResult whatever fetched it.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from . import (
    breaker,
    browser,
    challenge,
    contracts,
    corrections,
    egress,
    incidents,
    pipeline,
    policy,
    proxy,
    runcard,
    runlog,
    status,
    store,
    variants,
)
from .interfaces import FetchResult, load, plugin_class

DEFAULT_RATE = 7  # requests per minute (~8.6 s apart), the default for every networked run
MAX_RATE = 60  # ~1 request per second: the ceiling without an explicit override
CONCURRENCY = 1  # the ceiling without an explicit override
MAX_RETRIES = 1  # one retry for transient errors
CHALLENGES_BEFORE_COOLDOWN = 2  # per SITE, within CHALLENGE_WINDOW, across runs and A/B variants
CHALLENGE_WINDOW = timedelta(hours=1)
COOLDOWN = timedelta(hours=24)
BLOCK_STATUSES = [
    401,
    403,
    404,
    410,
    429,
    503,
]  # handed to OUR detectors instead of crawlee's retry/backoff (410: "gone")

EXIT_CHALLENGE, EXIT_COOLDOWN, EXIT_POLITENESS = 5, 6, 7
EXIT_EGRESS = egress.EXIT_EGRESS  # 8: the egress preflight refused (e.g. a VPN is on)
# 12: the site stopped answering. 13: the crawl ended with requests still pending (a ceiling, a signal, a
# stop). Both record "stopped" (resumable), never "completed"; neither is a challenge, so neither counts
# toward the cooldown. (9-11 are taken by the CLI, the approval gate and a consumer's own commands.)
EXIT_UNREACHABLE, EXIT_INCOMPLETE = 12, 13
# 14: a parse or yield stop (zero_yield, parse_drift, a breaker trip, a postcondition): the site answered but
# what came back cannot be trusted. Terminal, never resumed into a reference crawl; it opens a `parse`
# incident (a breaker trip a `gone_storm` one). Not a challenge, so it never counts toward the cooldown.
EXIT_PARSE = 14
# 4: the DATA root refused a write mid-run (store.DataDirError, or an OSError from a store write: a full disk,
# a permission error, a sync mount gone away; the CLI's EXIT_CONFIG). Resumable: the page whose rows could not be written is handed back to the queue, so a resume reads it again.
EXIT_DATA = 4
UNREACHABLE_AFTER = 5  # consecutive page attempts with no response at all (timeouts, refused connections)


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Stop(Exception):  # noqa: N818 - a control-flow signal, not an error
    def __init__(self, code: int, message: str, kind: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind  # the incident kind when the code alone does not say it (a breaker's gone_storm)


class ChallengeDetected(Exception):  # noqa: N818 - raised in the handler so crawlee keeps the request
    pass


@dataclass(frozen=True)
class Politeness:
    concurrency: int = CONCURRENCY
    per_minute: float = DEFAULT_RATE
    override: bool = False  # the operator's explicit approval to exceed the ceiling

    def checked(self) -> Politeness:
        if self.concurrency < 1 or self.per_minute <= 0:
            raise Stop(EXIT_POLITENESS, "concurrency and rate must be positive")
        if (self.concurrency > CONCURRENCY or self.per_minute > MAX_RATE) and not self.override:
            raise Stop(
                EXIT_POLITENESS,
                f"refusing: concurrency {self.concurrency} / {self.per_minute} per minute exceeds the ceiling "
                f"({CONCURRENCY} / {MAX_RATE}). Only the operator may pass --approve-politeness-override.",
            )
        return self


def event(
    run_id: str,
    kind: str,
    url: str | None = None,
    reason: str | None = None,
    until: str | None = None,
    site: str | None = None,
    evidence: dict | None = None,
    ad_id: str | None = None,
) -> dict:
    e = {
        "schema": "event.v1",
        "at": utcnow(),
        "run_id": run_id,
        "kind": kind,
        "url": url,
        "reason": reason,
        "until": until,
        "site": site,
    }
    if evidence is not None:
        e["evidence"] = evidence
    if ad_id:  # only when known: events before the field (and those about no ad) keep their exact shape
        e["ad_id"] = ad_id
    store.append("events", [e])
    return e


def _since_last_clear(site: str) -> list[dict]:
    """The site's events after its latest operator `cooldown_cleared`. The log is append-only, so its
    ORDER (not a second-resolution timestamp) decides what a clear covers."""
    tail: list[dict] = []
    for e in store.read("events"):
        if e["kind"] == "cooldown_cleared" and e.get("site") == site:
            tail = []
        else:
            tail.append(e)
    return tail


def active_cooldowns(site: str, now: datetime | None = None) -> list[dict]:
    """Cooldown events still blocking `site`. One recorded without a site (older events) applies to
    every site, fail closed, until that site's next clear."""
    now = now or datetime.now(UTC)
    return [
        e
        for e in _since_last_clear(site)
        if e["kind"] == "cooldown"
        and e.get("until")
        and e.get("site") in (site, None)
        and _t(e["until"]) > now
    ]


def cooldown_until(site: str, now: datetime | None = None) -> str | None:
    """The site's active cooldown end, or None. The event log IS the marker: no second state file."""
    active = active_cooldowns(site, now)
    return max(e["until"] for e in active) if active else None


def recent_challenges(site: str, now: datetime | None = None) -> int:
    """Challenges for `site` inside CHALLENGE_WINDOW and after its latest clear, whatever run or variant."""
    now = now or datetime.now(UTC)
    return sum(
        1
        for e in _since_last_clear(site)
        if e["kind"] == "challenge" and e.get("site") == site and now - _t(e["at"]) <= CHALLENGE_WINDOW
    )


def clear_cooldown(site: str, reason: str) -> dict | None:
    """Append the operator's `cooldown_cleared` for the site's latest active cooldown. Deletes nothing."""
    active = active_cooldowns(site)
    if not active:
        return None
    last = active[-1]
    e = {
        "schema": "event.v1",
        "at": utcnow(),
        "run_id": f"operator-{utcnow()}",
        "kind": "cooldown_cleared",
        "url": None,
        "reason": reason,
        "until": None,
        "site": site,
        "clears": {"run_id": last["run_id"], "at": last["at"], "until": last["until"]},
    }
    store.append("events", [e])
    return e


def run_docs() -> list[dict]:
    """Every run doc in DATA/runs (read only: nothing is created)."""
    d = store.data_dir() / "runs"
    return [store.read_json(p, "run.v1") for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def latch_stop(site: str) -> Stop | None:
    """The breaker latch's refusal for `site` (exit 14), or None. Derived from DATA: a run doc with
    breaker.tripped and no operator `breaker clear` since (breaker.latched)."""
    runs = breaker.latched(site, run_docs(), store.read("events"))
    if not runs:
        return None
    return Stop(
        EXIT_PARSE,
        f"refusing: {site} is latched by the gone-storm breaker of run {', '.join(runs)}; nothing is sent until "
        f"the operator decides (`{pipeline.consumer().cli} breaker clear --site {site} --resolve keep|void "
        "--note ... --approve-operator`)",
    )


def queue_name(run_id: str) -> str:
    """crawlee storage names allow only a-z, 0-9 and inner hyphens."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", run_id.lower())).strip("-")


@dataclass
class Run:
    """The run record (run.v1). crawlee's persisted request queue holds the requests themselves."""

    doc: dict

    @classmethod
    def new(cls, command: str, variant: str, source: str) -> Run:
        # A random suffix: two runs in the same second (e.g. back-to-back A/B variants) must never
        # share a run_id, because the run_id names crawlee's persisted queue (measured collision).
        run_id = f"{utcnow()}-{command}-{variant}-{secrets.token_hex(3)}"
        run = cls(
            {
                "schema": "run.v1",
                "run_id": run_id,
                "command": command,
                "variant": variant,
                "source": source,
                "created_at": utcnow(),
                "status": "running",
                "challenges": 0,
                "queue": queue_name(run_id),
            }
        )
        run.save()
        return run

    @classmethod
    def load(cls, run_id: str | None = None) -> Run:
        docs = [store.read_json(p, "run.v1") for p in sorted(store.runs_dir().glob("*.json"))]
        if run_id:
            docs = [d for d in docs if d["run_id"] == run_id]
        else:
            fixed = corrected_statuses()
            docs = [
                d
                for d in docs
                if fixed.get(d["run_id"], d["status"]) in ("running", "stopped")
                and d["command"] not in ("ab", "panel", "probe")
            ]
        if not docs:
            raise SystemExit("no resumable run found" + (f" with id {run_id}" if run_id else ""))
        return cls(docs[-1])

    def save(self) -> None:
        store.write_json(store.runs_dir() / f"{store.safe_name(self.doc['run_id'])}.json", "run.v1", self.doc)
        status.write(self.doc)  # the live mirror for the statusline / monitor (STATE, never data)


def pending(job: Run) -> int | None:
    """Requests still pending in a run's persisted queue (crawlee's own metadata), None if unknown."""
    import json as _json

    meta = store.state_dir() / "crawlee" / "request_queues" / job.doc["queue"].lower() / "__metadata__.json"
    try:
        return int(_json.loads(meta.read_text())["pending_request_count"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _queued(job: Run, keep) -> list[dict]:
    """Requests in a run's persisted queue that `keep(file_record)` selects, as {url, kind, meta}, read-only
    from crawlee's file storage (crawlee 1.11.1: one JSON per request). Opening the queue through crawlee's
    API would mark requests in progress, so the files are read instead; a test runs a real crawl into the
    queue, so a storage-format change on a crawlee bump turns it red."""
    import json as _json

    qdir = store.state_dir() / "crawlee" / "request_queues" / job.doc["queue"].lower()
    out = []
    for f in sorted(qdir.glob("*.json")):
        if f.name.startswith("__"):
            continue
        r = _json.loads(f.read_text())
        if keep(r):
            ud = dict(r.get("user_data") or {})
            ud = {k: v for k, v in ud.items() if not k.startswith("__")}  # crawlee's own bookkeeping
            out.append({"url": r["url"], "kind": ud.pop("kind", "list"), "meta": ud})
    return out


def pending_requests(job: Run) -> list[dict]:
    """The requests still PENDING in a run's persisted queue (`handled_at` null)."""
    return _queued(job, lambda r: r.get("handled_at") is None)


def failed_requests(job: Run) -> list[dict]:
    """The requests that FAILED in a run's persisted queue: handled with retries exhausted, crawlee's own
    RequestState.ERROR (kept in its `__crawlee` user data). Pending and failed never overlap."""
    from crawlee import RequestState

    def failed(r: dict) -> bool:
        state = ((r.get("user_data") or {}).get("__crawlee") or {}).get("state")
        return r.get("handled_at") is not None and state == RequestState.ERROR

    return _queued(job, failed)


def _storage():
    from crawlee.configuration import Configuration
    from crawlee.storage_clients import FileSystemStorageClient

    cfg = Configuration(storage_dir=str(store.state_dir() / "crawlee"), purge_on_start=False)
    return cfg, FileSystemStorageClient()


def stop_snapshot(run_id: str, html: str) -> str:
    """The whole page that stopped a run, gzipped into STATE (local, never synced: raw page content),
    so a stop can be diagnosed afterwards without a new request. Returns its STATE-relative path."""
    import gzip

    rel = f"stops/{store.safe_name(run_id)}/{utcnow().replace(':', '-')}.html.gz"
    p = store.state_dir() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(html.encode("utf-8", "replace"), mtime=0))
    return rel


def _served_at(res: FetchResult, final: bool = True) -> tuple:
    """The URL a page was served at, as a list snapshot compares it: scheme and host case-folded, the default
    port and the fragment dropped (a browser's page.url may differ from the request in exactly these), path and
    query kept exactly. A different path or query is a redirect: the cards are not the page asked for."""
    u = urlsplit((res.final_url or res.url) if final else res.url)
    port = u.port if u.port not in (None, 80, 443) else None
    return (u.scheme.lower(), (u.hostname or "").lower(), port, u.path or "/", u.query)


def redirected(res: FetchResult) -> bool:
    """A list page served at another URL than the one asked for (`_served_at`): its cards are not that page's.
    The one check for list snapshots and for a positive END (ADR-0017 A.1)."""
    return _served_at(res) != _served_at(res, final=False)


def _challenge(job: Run, url: str, reason: str, res: FetchResult | None = None, hops=None) -> Stop:
    rid, site = job.doc["run_id"], job.doc["source"]
    job.doc["challenges"] += 1
    ev = challenge.evidence(res) if res else None
    if ev is not None and job.doc.get("egress"):
        ev["egress"] = job.doc["egress"]  # where the blocked request left from
    if ev is not None and res is not None:
        ev["final_path"] = urlsplit(res.final_url or url).path  # always: "same as requested" is evidence too
        if hops is not None:
            ev["hops"] = hops
        if res.html:
            ev["snapshot"] = "STATE:" + stop_snapshot(rid, res.html)
    event(rid, "challenge", url, reason, site=site, evidence=ev)
    runlog.LOG.warning(
        "challenge %s at %s",
        reason,
        urlsplit(url).path,
        extra={"event": "challenge", "reason": reason, "path": urlsplit(url).path, "evidence": ev},
    )
    if (n := recent_challenges(site)) >= CHALLENGES_BEFORE_COOLDOWN:
        until = (datetime.now(UTC) + COOLDOWN).strftime("%Y-%m-%dT%H:%M:%SZ")
        event(rid, "cooldown", url, reason, until, site=site)
        job.doc["status"] = "aborted"
        job.save()
        return Stop(
            EXIT_COOLDOWN,
            f"{n} challenges for {site} within {CHALLENGE_WINDOW} (last: {reason} at {url}): aborted; "
            f"{site} is cooling down until {until}",
        )
    job.doc["status"] = "stopped"
    job.save()
    return Stop(
        EXIT_CHALLENGE,
        f"STOPPED: {reason} at {url}"
        + (f" (page saved: {ev['snapshot']})" if ev and ev.get("snapshot") else "")
        + ". To continue, run "
        f"`{pipeline.consumer().cli} browser open`, clear it once, close the window, then `{pipeline.consumer().cli} crawl resume`.",
    )


ZERO = {
    "list_pages": 0,
    "ad_pages": 0,
    "robots": 0,
    "other": 0,
    "egress_trace": 0,
    # A browser page load is more than its page GET (audit 2026-11-08: a median of ~90 resource refs
    # per page on the site's hosts). These count what the browser sent besides the navigations.
    "site_subresources": 0,
    "third_party": 0,
    "redirects": 0,
    "failed_before_request": 0,  # an attempt that errored before any GET (e.g. the browser did not launch)
}


def _same_site(host: str, site: str) -> bool:
    return host == site or host.endswith("." + site)


def request_listener(counts: dict, site: str, main_frame: object):
    """A Playwright page's "request" handler: counts every request the browser sends for one page.
    The main-frame navigation itself is the page GET (counted by the pre-navigation hook); a
    main-frame navigation with `redirected_from` is a redirect hop; anything else (sub-resources,
    iframes, XHR) is a site or third-party request. Non-network schemes (data:, blob:) are skipped."""

    def on_request(req) -> None:
        url = str(req.url)
        if not url.startswith(("http://", "https://")):
            return
        try:
            nav = req.is_navigation_request() and req.frame == main_frame
        except Exception:  # noqa: BLE001 - a service-worker request has no frame: not a navigation
            nav = False
        if nav:
            if req.redirected_from is not None:
                counts["redirects"] += 1
            return
        counts["site_subresources" if _same_site(urlsplit(url).hostname or "", site) else "third_party"] += 1

    return on_request


BLOCK_COUNTERS = (
    "trackers_blocked",
    "site_subresources_blocked",
    "third_party_blocked",
    "site_subresources_answered",
    "third_party_answered",
)


def response_listener(responded: set, key: str, main_frame: object):
    """A Playwright page's "response" handler: the page's main-frame navigation got an answer (any status),
    so a later load timeout is not a silence (the circuit breaker counts only attempts with no answer)."""

    def on_response(resp) -> None:
        req = resp.request
        if req.is_navigation_request() and req.frame == main_frame:
            responded.add(key)

    return on_response


def hop_listener(hops: dict, key: str, main_frame: object):
    """Counts THIS navigation's main-frame redirect hops (the run total is requests.redirects)."""

    def on_request(req) -> None:
        try:
            if req.is_navigation_request() and req.frame == main_frame and req.redirected_from is not None:
                hops[key] = hops.get(key, 0) + 1
        except Exception:  # noqa: BLE001 - a service-worker request has no frame
            pass

    return on_request


def answered_counter(counts: dict, site: str):
    """Counts an image request the browser ANSWERED itself (browser.block, image_mode answer). It fired
    "request" like any attempt (request_listener counted it) but never left: sent() subtracts it."""

    def on_answer(url: str) -> None:
        key = (
            "site_subresources_answered"
            if _same_site(urlsplit(url).hostname or "", site)
            else "third_party_answered"
        )
        counts[key] = counts.get(key, 0) + 1

    return on_answer


def tracker_counter(counts: dict):
    """Counts a third-party request the tracker lists blocked (browser.block, Fetch.failRequest). It fired
    "request" (request_listener counted it as third_party) but never left: sent() subtracts
    third_party_blocked. Its failure text is BlockedByClient, which blocked_listener does not count, so a
    tracker is counted once. trackers_blocked keeps the tracker share visible on its own."""

    def on_tracker(_url: str) -> None:
        counts["third_party_blocked"] = counts.get("third_party_blocked", 0) + 1
        counts["trackers_blocked"] = counts.get("trackers_blocked", 0) + 1

    return on_tracker


def blocked_listener(counts: dict, site: str, main_frame: object):
    """A Playwright page's "requestfailed" handler. A request the browser BLOCKED (browser.block)
    still fires "request" first, so request_listener has already counted it as an attempt; this
    counts the block, and sent() subtracts it. Navigations are never blocked, so are not counted."""
    from .browser import BLOCKED_FAILURE

    def on_failed(req) -> None:
        url = str(req.url)
        if not url.startswith(("http://", "https://")) or BLOCKED_FAILURE not in (req.failure or "").lower():
            return
        try:
            if req.is_navigation_request() and req.frame == main_frame:
                return
        except Exception:  # noqa: BLE001 - a service-worker request has no frame: not a navigation
            pass
        site_req = _same_site(urlsplit(url).hostname or "", site)
        key = "site_subresources_blocked" if site_req else "third_party_blocked"
        counts[key] = counts.get(key, 0) + 1

    return on_failed


def per_page(requests: dict) -> dict | None:
    """Site sub-resources per page GET: blocked, answered and sent (None when no browser blocking ran)."""
    if "site_subresources_blocked" not in requests:
        return None
    pages = requests.get("list_pages", 0) + requests.get("ad_pages", 0)
    if not pages:
        return None
    return {
        "pages": pages,
        "blocked": round(requests["site_subresources_blocked"] / pages, 1),
        "answered": round(requests.get("site_subresources_answered", 0) / pages, 1),
        "sent": round(sent(requests)["site_subresources"] / pages, 1),
    }


def sent(requests: dict) -> dict:
    """What actually LEFT the machine, by destination: site_subresources and third_party count every
    attempt the browser made, blocked and answered ones included; this subtracts both (neither left the
    browser). Any bound or display of browser requests uses this, never the raw attempt counts."""

    def left(kind: str) -> int:
        return (
            requests.get(kind, 0) - requests.get(f"{kind}_blocked", 0) - requests.get(f"{kind}_answered", 0)
        )

    return {"site_subresources": left("site_subresources"), "third_party": left("third_party")}


class Attempts:
    """Which page attempts reached their GET. crawlee opens the page (launches the browser) BEFORE
    the pre-navigation hook, so an error with no navigation for that attempt sent nothing."""

    def __init__(self, counts: dict) -> None:
        self.counts = counts
        self.open: set[str] = set()  # attempts whose GET was sent and that have not ended yet
        self.finished: set[str] = set()  # handled; a later error (e.g. a challenge stop) sent nothing new

    def navigated(self, key: str) -> None:
        self.finished.discard(key)
        self.open.add(key)

    def done(self, key: str) -> None:
        self.open.discard(key)
        self.finished.add(key)

    def errored(self, key: str) -> None:
        if key in self.open:
            self.open.discard(key)  # this attempt sent its GET; a retry starts a new attempt
        elif key not in self.finished:
            self.counts["failed_before_request"] += 1


def correction_event(
    run_id: str,
    *,
    field: str,
    recorded: dict,
    reconciled: dict,
    note: str,
    evidence: list[str],
    site: str | None = None,
    incident: str | None = None,
) -> dict:
    """A correction of a run's record, as an event.v1 line (built, not written: see append_correction)."""
    return {
        "schema": "event.v1",
        "at": utcnow(),
        "run_id": run_id,
        "kind": "run_correction",
        "url": None,
        "reason": None,
        "until": None,
        "site": site,
        "correction": {
            "field": field,
            "recorded": recorded,
            "reconciled": reconciled,
            "note": note,
            "evidence": evidence,
            **({"incident": incident} if incident else {}),
        },
    }


def append_correction(**kw) -> dict:
    """Append ONE correction line to events.jsonl (append-only); the run record is never rewritten.
    The run must exist."""
    run_id = kw["run_id"]
    Run.load(run_id)  # refuses an unknown run
    e = correction_event(**kw)
    store.append("events", [e])
    return e


def corrected_statuses() -> dict[str, str]:
    """run_id -> the status its latest `status` run_correction reconciled. The run record is never
    rewritten, so every reader of a run's status asks `status_of` (the rule itself: `corrections.latest`)."""
    return {rid: r["status"] for rid, r in corrections.latest(store.read("events"), "status").items()}


def status_of(doc: dict) -> str:
    """A run's status, its latest correction applied."""
    return corrected_statuses().get(doc["run_id"], doc["status"])


def correct_status(run_id: str, status: str, reason: str, note: str, evidence: list[str] = ()) -> dict:
    """Correct a run that recorded the wrong END, e.g. "completed" for a crawl interrupted with requests
    still pending. One run_correction (field "status"); what is reconciled is MEASURED, not typed: the
    pending requests in its persisted queue and its failed requests (retries exhausted) from the events."""
    job = Run.load(run_id)
    now = status_of(job.doc)
    if status == now:
        raise SystemExit(f"{run_id}: already {status}; nothing to correct")
    if status not in ("stopped", "aborted", "completed"):
        raise SystemExit(f"{status}: a run can be corrected to stopped, aborted or completed")
    if not note.strip():
        raise SystemExit("a status correction needs a --note")
    # the per-request files, not crawlee's cached pending counter (an interrupt may leave it unflushed)
    left = len(pending_requests(job))
    if status == "completed" and left:
        raise SystemExit(f"{run_id}: {left} requests are still pending; it did not complete")
    failed = sum(
        1 for e in store.read("events") if e.get("run_id") == run_id and e["kind"] == "failed_request"
    )
    return append_correction(
        run_id=run_id,
        field="status",
        recorded={"status": now},
        reconciled={"status": status, "reason": reason, "pending": left, "failed_requests": failed},
        note=note,
        evidence=[f"STATE:crawlee/request_queues/{job.doc['queue']}", "DATA:events.jsonl", *evidence],
        site=job.doc["source"],
    )


def _count(job: Run, counts: dict) -> None:
    """Add this attempt's request counts to the run record (a resume adds to the earlier ones)."""
    total = dict(job.doc.get("requests") or ZERO)
    for k, v in counts.items():
        total[k] = total.get(k, 0) + v
    job.doc["requests"] = total


def _counting(client, counts: dict) -> None:
    """Count every send_request on the backend's HTTP client (robots.txt, and anything else)."""
    original = client.send_request

    async def counted(url, *a, **kw):
        kind = "robots" if urlsplit(str(url)).path == "/robots.txt" else "other"
        counts[kind] += 1
        t0, status = time.monotonic(), None
        try:
            resp = await original(url, *a, **kw)
            status = getattr(resp, "status_code", None)
            return resp
        finally:
            runlog.request(kind, url, status, (time.monotonic() - t0) * 1000, counts)

    client.send_request = counted


def _requests(rs: list[dict]):
    return [_request(r) for r in rs]


def _request(r: dict, unique_key: str | None = None):
    """One crawlee request; its key is its URL (the queue's dedup), except a lost list page queued again."""
    from crawlee import Request

    return Request.from_url(
        r["url"], unique_key=unique_key or r["url"], user_data={"kind": r["kind"], **r["meta"]}
    )


def _unreachable(job: Run, url: str, error: Exception, n: int) -> Stop:
    """The circuit breaker's stop: the site (or the way to it) stopped answering. Not a challenge: no
    challenge event, no cooldown; the run stays resumable."""
    rid = job.doc["run_id"]
    reason = f"{n} consecutive attempts without a response (last: {type(error).__name__})"
    event(rid, "unreachable", url, reason, site=job.doc["source"])
    runlog.LOG.warning("unreachable: %s", reason, extra={"event": "unreachable", "reason": reason})
    job.doc["status"] = "stopped"
    job.save()
    return Stop(
        EXIT_UNREACHABLE,
        f"STOPPED: unreachable: {reason}, last at {url}. Nothing is wrong with the requests themselves; check "
        f"the egress or the site, then `{pipeline.consumer().cli} crawl resume`.",
    )


def _no_response(error: Exception) -> bool:
    """An attempt that got NO response: not an HTTP status (crawlee's HttpStatusCodeError carries one) and
    not our own handler's stop signal."""
    from crawlee.errors import HttpStatusCodeError

    return not isinstance(error, (HttpStatusCodeError, ChallengeDetected))


async def _settled_total(cfg, sc, key: str) -> int:
    """Requests a run's queue already settled in earlier attempts (crawlee's persisted statistics, which
    its max_requests_per_crawl counts against): a resume's ceiling starts from there."""
    from crawlee.statistics import StatisticsState
    from crawlee.storages import KeyValueStore

    kvs = await KeyValueStore.open(configuration=cfg, storage_client=sc)
    saved = await kvs.get_value(key)
    return StatisticsState.model_validate(saved).requests_total if saved else 0


async def _crawl(
    job: Run,
    requests: list[dict] | None,
    source,
    handle,
    polite: Politeness,
    opts: dict,
    counts: dict,
    ceiling: int | None = None,
    pol: policy.SyncPolicy | None = None,
) -> Stop | None:
    from crawlee import ConcurrencySettings, service_locator
    from crawlee._utils.robots import RobotsTxtFile
    from crawlee.storages import RequestQueue

    opts = dict(opts)
    name = (
        opts.pop("backend", None) or variants.get(job.doc["variant"]).backend
    )  # an override only for fixtures
    backend = load("drivers", name, variant=job.doc["variant"], **opts)
    _counting(backend.http_client, counts)
    # the run's one proxy ([crawl] proxy): crawlee hands it to the browser context and to every HTTP
    # request of the crawler; robots.txt below is fetched through it too. Offline backends never use it.
    px = proxy.config() if getattr(backend, "network", False) else None
    proxy_conf = None
    if px:
        from crawlee.proxy_configuration import ProxyConfiguration

        proxy_conf = ProxyConfiguration(proxy_urls=[proxy.url_with_credentials(px)])
    # crawlee caches opened storages process-wide; a cached one holds locks bound to the previous
    # run's event loop (measured: "Lock ... bound to a different event loop" on the 2nd run).
    service_locator.storage_instance_manager.clear_cache()
    cfg, sc = _storage()
    queue = await RequestQueue.open(name=job.doc["queue"], configuration=cfg, storage_client=sc)
    if requests:
        await queue.add_requests(_requests(requests))

    # robots.txt Crawl-delay can only LOWER the rate (crawlee parses it; we apply it to the limiter).
    per_minute = polite.per_minute
    robots = await RobotsTxtFile.load(
        source.robots_url,
        backend.http_client,
        proxy_info=await proxy_conf.new_proxy_info(None, None, None) if proxy_conf else None,
    )
    if (delay := robots.get_crawl_delay()) is not None and delay > 0:
        per_minute = min(per_minute, 60 / delay)

    rid = job.doc["run_id"]
    stopped: list[Stop] = []
    started: dict[str, float] = {}
    attempts = Attempts(counts)
    silent = [0]  # consecutive page attempts with no response (the circuit breaker)
    responded: set[str] = (
        set()
    )  # browser attempts whose main-frame response arrived (a load may still time out)
    hops: dict[str, int] = {}  # main-frame redirects per page navigation (browser only)
    ignore = tuple(getattr(source, "ignore_selectors", ()))
    gone_selectors = tuple(getattr(source, "gone_selectors", ()))  # the source's "ad no longer active" page
    end_selectors = tuple(getattr(source, "end_selectors", ()))  # its "no results" list page (an END)
    # the statuses its END answers with (unset: every status end_reason admits)
    end_statuses = tuple(getattr(source, "end_statuses", ()))
    blocks = variants.get(job.doc["variant"]).blocks
    block = browser.block_extensions() if blocks else ()
    site_host = (urlsplit(source.robots_url).hostname or "").removeprefix("www.")
    from crawlee.statistics import Statistics
    from crawlee.storages import KeyValueStore

    stats_key = f"__CRAWLER_STATISTICS_{job.doc['queue']}"
    # crawlee's own ceiling: never more requests than the plan holds. Its count is the queue's (persisted
    # across a resume), so the ceiling is what earlier attempts settled plus what this one may send.
    cap = None if ceiling is None else await _settled_total(cfg, sc, stats_key) + ceiling
    crawler = backend.crawler(
        **({"proxy_configuration": proxy_conf} if proxy_conf else {}),
        # crawlee persists its statistics under a per-PROCESS key (__CRAWLER_STATISTICS_0) in the shared
        # storage, which is never purged: its "Crawled N/M" then sums every earlier run (measured: 676/600).
        statistics=Statistics.with_default_state(
            persistence_enabled=True,
            persist_state_key=stats_key,
            persist_state_kvs_factory=lambda: KeyValueStore.open(configuration=cfg, storage_client=sc),
            log_message="Current request statistics:",
        ),
        request_manager=queue,
        configuration=cfg,
        storage_client=sc,
        respect_robots_txt_file=True,
        retry_on_blocked=False,
        ignore_http_error_status_codes=BLOCK_STATUSES,
        max_request_retries=MAX_RETRIES,
        max_requests_per_crawl=cap,
        concurrency_settings=ConcurrencySettings(
            min_concurrency=1,
            desired_concurrency=1,
            max_concurrency=polite.concurrency,
            max_tasks_per_minute=per_minute,
        ),
    )

    # Reuse the robots.txt we just loaded instead of letting crawlee GET it again (measured: 2 robots
    # GETs per run without this). The cache is crawlee 1.11.1's private LRU keyed by origin; the
    # request-accounting test (robots == 1) is the tripwire if a crawlee bump changes it.
    from yarl import URL

    cache = getattr(crawler, "_robots_txt_file_cache", None)
    if cache is not None:
        cache[str(URL(source.robots_url).origin())] = robots

    @crawler.pre_navigation_hook
    async def count_page(ctx) -> None:  # every page GET attempt, HTTP or browser, retries included
        counts["ad_pages" if ctx.request.user_data.get("kind") == "ad" else "list_pages"] += 1
        lst = job.doc.get("list") or {}
        if (
            walk
            and ctx.request.user_data.get("kind", "list") == "list"
            and lst.get("mode") in ("walk", "frontier")
        ):
            # a walk's signed ceiling counts every list GET it SENDS (a retry, a challenged GET, a page a crash
            # or a stop hands back): counted here, before the GET, and saved, so a resume's cap (ceiling -
            # settled) never lets the walk send more than it signed. The GET that reaches the ceiling is never
            # retried: its retry would be one GET past it.
            lst["settled"] = lst.get("settled", 0) + 1
            if lst["settled"] >= lst.get("ceiling", 0):
                ctx.request.no_retry = True
            job.save()
        started[ctx.request.unique_key] = time.monotonic()
        attempts.navigated(ctx.request.unique_key)
        page = getattr(ctx, "page", None)
        if page is not None:  # a browser backend: count what the page load sends besides this GET
            page.on("request", request_listener(counts, site_host, page.main_frame))
            hops[ctx.request.unique_key] = 0
            page.on("request", hop_listener(hops, ctx.request.unique_key, page.main_frame))
            page.on("response", response_listener(responded, ctx.request.unique_key, page.main_frame))
            if await browser.block(  # before the GET
                page,
                block,
                answered_counter(counts, site_host),
                trackers_site=site_host if blocks else None,
                on_tracker=tracker_counter(counts),
            ):
                for k in BLOCK_COUNTERS:
                    counts.setdefault(k, 0)  # a browser run with blocking records explicit zeros
                page.on("requestfailed", blocked_listener(counts, site_host, page.main_frame))

    def log_page(req, code, final_url=None) -> None:
        """One line per page attempt, written once: on its result, or on its error."""
        t0 = started.pop(req.unique_key, None)
        if t0 is not None:
            kind = "ad" if req.user_data.get("kind") == "ad" else "list"
            runlog.request(
                kind,
                req.url,
                code,
                (time.monotonic() - t0) * 1000,
                counts,
                req.user_data.get("page"),
                final_url=final_url,
                hops=hops.pop(req.unique_key, None),
            )
            status.write(job.doc, counts)  # progress after every page

    def circuit(ctx, error: Exception) -> None:
        """Count an attempt that sent its GET and got nothing back; at UNREACHABLE_AFTER in a row, stop."""
        key = ctx.request.unique_key
        if key not in attempts.open:
            return  # never sent: nothing to say about the site
        if key in responded or not _no_response(error):
            silent[0] = 0  # it answered (an error status, or a load that timed out after the response)
            return
        silent[0] += 1
        if silent[0] >= UNREACHABLE_AFTER and not stopped:
            stopped.append(_unreachable(job, ctx.request.url, error, silent[0]))
            crawler.stop("unreachable")

    walk = job.doc["command"] == "crawl-lists"  # a list walk: a lost list page leaves a hole in it

    def halt(stop: Stop, why: str) -> None:
        """Stop the run from inside a handler: record it (the first stop wins), save status `stopped`, ask
        crawlee to stop. Bookkeeping never raises out of here: an exception escaping a handler is a retry,
        and a retry is a second GET."""
        if not stopped:
            stopped.append(stop)
        try:
            job.doc["status"] = "stopped"
            job.save()
        except Exception as e:  # noqa: BLE001 - the stop itself must still happen
            runlog.LOG.warning("could not save the stopped run: %s", e, extra={"event": "stop_unsaved"})
        crawler.stop(why)

    def list_status(value: str) -> None:
        job.doc.setdefault("list", {}).setdefault("status", value)  # the first cause is the one recorded

    def parse_failed(req, error: Exception) -> None:
        """An ad page that answered but could not be parsed or validated: ONE event, no retry (its view is
        spent; a second GET would be another). Its snapshot, when saved, stays for a reparse."""
        m = req.user_data
        try:
            event(rid, "parse_failed", req.url, type(error).__name__, site=m.get("site") or job.doc["source"],
                  ad_id=m.get("ad_id") or None)  # fmt: skip
        except Exception as e:  # noqa: BLE001 - never a retry, even when the event cannot be written
            runlog.LOG.warning(
                "could not record parse_failed: %s", e, extra={"event": "parse_failed_unsaved"}
            )
        runlog.LOG.warning("parse_failed %s (%s: %s)", urlsplit(req.url).path, type(error).__name__, error,
                           extra={"event": "parse_failed", "reason": type(error).__name__})  # fmt: skip

    brk = breaker.Breaker(pol or policy.DEFAULT)

    def settle(req, outcome: str, reason: str | None = None) -> None:
        """One answered ad page into the breaker; on a trip: SAVE the trip on the run doc first (the latch
        reads it), then quarantine the run's weak gones, then stop (exit 14). Never raises: a raise here is a
        retry, and a retry is a second view."""
        if req.user_data.get("kind") != "ad":
            return
        trip = brk.settle(outcome, reason)
        if trip is None or stopped:
            return
        what = "gone-storm" if trip["kind"] == "gone" else "ad-failure"
        why = (f"STOPPED: the {what} breaker tripped: {trip['g']} of {trip['n']} answered ad pages "
               f"{'look gone by absence or a redirect' if trip['kind'] == 'gone' else 'failed to parse'} (p0 "
               f"{trip['p0']}, alpha {trip['alpha']}). The run's weak gones are quarantined (no tombstone) and "
               f"{job.doc['source']} is latched until `{pipeline.consumer().cli} breaker clear --approve-operator`")  # fmt: skip
        try:
            job.doc["breaker"] = {**trip, "at": utcnow()}
            job.save()
        except Exception as e:  # noqa: BLE001 - the stop itself must still happen
            runlog.LOG.warning("could not save the breaker trip: %s", e, extra={"event": "breaker_unsaved"})
        try:
            append_correction(
                run_id=rid, field="gone", recorded={"gone_weak": brk.g["gone"], "ad_answered": brk.n},
                reconciled={"quarantine": True, "scope": "weak"}, site=job.doc["source"],
                note=f"{what} breaker trip: the run's weak gones are doubtful", evidence=["DATA:events.jsonl"],
            )  # fmt: skip
        except Exception as e:  # noqa: BLE001
            runlog.LOG.warning("could not quarantine: %s", e, extra={"event": "quarantine_unsaved"})
        runlog.LOG.warning(why, extra={"event": "breaker", "breaker": trip})
        halt(Stop(EXIT_PARSE, why, kind="gone_storm" if trip["kind"] == "gone" else None), "breaker")

    @crawler.error_handler
    async def on_error(ctx, error: Exception) -> None:
        circuit(ctx, error)
        responded.discard(ctx.request.unique_key)
        attempts.errored(ctx.request.unique_key)
        log_page(ctx.request, None)

    def refused(req, e: Exception) -> None:
        """A write the DATA root refused after a page answered: the store's DataDirError or the OS's own OSError
        (a full disk, a permission error, a cloud-sync mount gone away). Resumable, exit 4, and never crawlee's
        retry (a second GET). A LIST page is handed back (reclaimed: a list GET adds no ad view, and the resume
        reads it). An AD page is left DONE in the queue: its view is spent, and the queue lives in STATE (local),
        the one record that does not depend on the DATA root that just refused, so no resume re-GETs it. What
        it would have written is lost; the warning names it."""
        halt(Stop(EXIT_DATA, f"STOPPED: the data folder refused a write ({e}); fix it, then "
                             f"`{pipeline.consumer().cli} crawl resume`"), "data folder")  # fmt: skip
        if req.user_data.get("kind", "list") != "ad":
            raise ChallengeDetected("data folder") from None  # crawlee reclaims the request: still pending
        runlog.LOG.warning("ad page %s answered but not saved (%s: %s): its view is spent, it is not re-sent",
                           urlsplit(req.url).path, type(e).__name__, e,
                           extra={"event": "ad_unsaved", "reason": type(e).__name__})  # fmt: skip

    @crawler.router.default_handler
    async def default(ctx) -> None:
        if stopped:  # a stop was requested: hand the request back untouched
            raise ChallengeDetected("run stopping")
        silent[0] = 0  # a response: the site answers
        responded.discard(ctx.request.unique_key)
        res = await backend.result(ctx)
        # The handler's own failures never reach crawlee: it would retry them (a second GET, for an ad page
        # a second view) and then record the run `completed`. A refused write, wherever it happens below (a
        # gone event, a snapshot, a record), takes the ONE path `refused`.
        try:
            await answered(ctx, res)
        except (store.DataDirError, OSError) as e:
            refused(ctx.request, e)

    def snapshot(m: dict, res, reason: str | None, end: bool = False) -> str:
        """crawl-lists --snapshot: save the answered list page and its sidecar BEFORE any parse or stop decision
        (ADR-0017 A.4), and return its outcome. A page with a challenge sign keeps only its sidecar: its body
        is already saved in STATE by the challenge stop (never synced); a page that merely lacks the list
        expectation (EXPECTED_MISSING: detect() found no challenge sign on it) is the site's own page, kept.
        Only a refused write (DataDirError/OSError) leaves here; it takes the one path `refused`. An END nominee
        (`end`) is never `stopped`: it goes to the list parser, which accepts or refuses the END."""
        outcome = (
            "past_end" if reason in challenge.GONE
            else "stopped" if reason and not end
            else "redirected" if redirected(res)
            else "listed"
        )  # fmt: skip
        page, keep = int(m["page"]), reason is None or reason in challenge.GONE + challenge.EXPECTED_MISSING
        try:
            body = store.write_list_snapshot(job.doc["source"], rid, page, res.html) if keep else None
            store.write_list_sidecar({
                "schema": "list_snapshot.v1", "site": job.doc["source"], "crawl_id": rid, "page": page,
                "requested_url": res.url, "final_url": res.final_url or res.url, "status": res.status,
                "fetched_at": utcnow(), "reason": reason, "outcome": outcome, "parsed": None, "skipped": None,
                "body": body,
            })  # fmt: skip
        except (store.DataDirError, OSError):
            raise
        except Exception as e:  # noqa: BLE001 - never crawlee's retry (a second GET): treat it as refused
            raise OSError(f"list snapshot of page {page} not saved ({type(e).__name__}: {e})") from e
        lst = job.doc.setdefault("list", {})
        snap = lst.setdefault("snapshot", {"dir": store.list_snapshot_dir(job.doc["source"], rid), "saved": 0,
                                           "past_end": [], "redirected": []})  # fmt: skip
        snap["saved"] += 1
        if outcome in ("past_end", "redirected") and page not in snap[outcome]:
            snap[outcome].append(page)
        return outcome

    def counted(m: dict, res) -> None:
        """A past-end or redirected page is saved, not kept: count its cards for the sidecar, best effort (the
        page may not parse at all), and append no row (a redirected page's cards are not the page asked for)."""
        try:
            lp = source.parse_list(res.html, url=res.url, subcategory=m.get("subcategory"), page=m["page"],
                                   seen_at=utcnow())  # fmt: skip
        except Exception:  # noqa: BLE001 - evidence only: an unparsable past-end page is still recorded
            return
        try:
            store.update_list_sidecar(job.doc["source"], rid, m["page"], parsed=len(lp.observations),
                                      skipped=getattr(lp, "skipped", None))  # fmt: skip
        except (store.DataDirError, OSError):
            raise
        except Exception as e:  # noqa: BLE001 - never crawlee's retry (a second GET): treat it as refused
            raise OSError(
                f"list snapshot counts of page {m['page']} not saved ({type(e).__name__}: {e})"
            ) from e

    async def answered(ctx, res) -> None:
        n_hops = hops.get(ctx.request.unique_key)
        log_page(ctx.request, res.status, res.final_url)
        attempts.done(ctx.request.unique_key)
        kind = ctx.request.user_data.get("kind", "list")
        reason = challenge.detect(res, source.expect.get(kind), ignore)
        # A positive END nominee (ADR-0017 A.1): the source's "no results" marker on a list page with no
        # challenge sign. The list handler, which has the card counts, accepts or refuses it; the challenge
        # branch below is skipped for it, so a past-end 404 is not a stop. Both paths (with and without
        # --snapshot) hand it to the handler alike. Only crawl-lists' handler decides an END, so only its runs
        # (`walk`) nominate one: any other list reader (ab) stops on such a page as before.
        nominee = (
            challenge.end_reason(res, reason, end_selectors, ignore, end_statuses)
            if kind == "list" and walk
            else None
        )
        end = nominee == "end_marker"
        if kind == "list" and ctx.request.user_data.get("snapshot"):
            outcome = snapshot(ctx.request.user_data, res, reason, end)
            if outcome == "redirected" or (outcome == "past_end" and not end):  # recorded: no stop, no rows
                counted(ctx.request.user_data, res)
                job.save()
                runlog.LOG.info("list page %s %s (%s), saved", ctx.request.user_data.get("page"), outcome,
                                reason or urlsplit(res.final_url).path, extra={"event": "list_" + outcome})  # fmt: skip
                return
        why = (
            challenge.gone_reason(res, reason, source.expect.get("list"), gone_selectors, ignore)
            if kind == "ad"
            else None
        )
        ad = ctx.request.user_data.get("ad_id") or None
        # The ad was removed: the site shows its expired notice or the category list instead (why), or the
        # response itself says so (a 404 or 410). Nothing to parse. The gone event is the ad's tombstone in
        # `liveness`, which every planner reads (plan.eligible, plan.admit), so a later plan does not list it.
        if gone := why or (
            reason if challenge.gone(reason, bool(ctx.request.user_data.get("revisit"))) else None
        ):
            try:
                event(rid, "gone", ctx.request.url, gone, site=job.doc["source"], ad_id=ad)
            finally:  # the page answered, so the breaker counts it even when its event was refused
                settle(ctx.request, "gone", gone)
            at = f", at {urlsplit(res.final_url).path}" if why else ""
            runlog.LOG.info("gone %s (%s%s)", urlsplit(ctx.request.url).path, gone, at,
                            extra={"event": "gone", "reason": gone})  # fmt: skip
            return
        if reason and not end:
            stopped.append(_challenge(job, ctx.request.url, reason, res, n_hops))
            crawler.stop(f"challenge: {reason}")
            raise ChallengeDetected(reason)  # crawlee reclaims the request: it stays pending for resume
        meta = dict(ctx.request.user_data) | ({"end_marker": True, "reason": reason} if end else {})
        # Order matters: a refused write (re-raised to `refused`) or a Stop is never a parse failure.
        try:
            new = handle(meta, res)
        except (store.DataDirError, OSError):
            raise
        except Stop as s:  # terminal: this request is DONE, so a resume could never read it again
            if kind == "list":
                list_status("failed")
            msg = str(s) if s.code == EXIT_PARSE else f"{s} (stopped by the handler, exit {s.code})"
            halt(Stop(EXIT_PARSE, msg or "stopped by the handler"), "handler stop")
            return
        except Exception as e:  # noqa: BLE001 - a parser or contract error: what came back cannot be used
            if kind == "ad":
                parse_failed(ctx.request, e)
                settle(ctx.request, "failed")
                return  # the run goes on: one ad that cannot be parsed is no reason to stop the others
            list_status("failed")
            what = (
                "violates its contract" if isinstance(e, contracts.ContractError) else "could not be parsed"
            )
            halt(Stop(EXIT_PARSE, f"STOPPED: list page {ctx.request.url} {what} ({type(e).__name__}: {e})"),
                 "list page unusable")  # fmt: skip
            return
        settle(ctx.request, "ok")
        if new:  # dynamic enqueue (e.g. the sample planned from page 1's page count)
            await queue.add_requests(_requests(new))

    @crawler.on_skipped_request
    async def skipped(url: str, reason) -> None:
        robots = "robots" in str(reason)
        event(rid, "robots_disallowed" if robots else "failed_request", url, str(reason))
        if walk and robots:  # a list page the walk needs is out of bounds: the walk can no longer complete
            list_status("blocked")
            halt(Stop(EXIT_INCOMPLETE, f"STOPPED: robots.txt disallows list page {url}; the walk has a hole "
                                       "a resume cannot fill"), "robots.txt")  # fmt: skip

    @crawler.failed_request_handler
    async def failed(ctx, error: Exception) -> None:
        circuit(ctx, error)
        responded.discard(ctx.request.unique_key)
        attempts.errored(ctx.request.unique_key)
        log_page(ctx.request, None)
        event(rid, "failed_request", ctx.request.url, type(error).__name__)
        if walk and ctx.request.user_data.get("kind", "list") == "list":
            # crawlee has marked it handled (ERROR), so the queue alone would drain to `completed` with a
            # hole. Queue the same page again under a fresh key, for the resume, and stop now. Also when a
            # stop is already under way: a page handed back once (a reclaim spends its one retry) that is
            # handed back again lands here, and must still be read by the resume. That stop and the status
            # it saved (e.g. `aborted` by a cooldown) are left as they are.
            if not stopped:
                list_status("failed")
                halt(Stop(EXIT_INCOMPLETE, f"STOPPED: list page {ctx.request.url} failed ({type(error).__name__})"
                                           f" after its retry; `{pipeline.consumer().cli} crawl resume` reads it "
                                           "again"), "list page failed")  # fmt: skip
            again = {"url": ctx.request.url, "kind": "list", "meta": {k: v for k, v in
                     ctx.request.user_data.items() if k != "kind" and not k.startswith("__")}}  # fmt: skip
            await queue.add_requests([_request(again, f"{ctx.request.url}#again-{secrets.token_hex(3)}")])

    await crawler.run()
    await backend.close()
    if stopped:
        return stopped[0]
    # "completed" means the queue is DRAINED. crawlee's run() also returns normally after its
    # max_requests_per_crawl ceiling, a SIGINT or any crawler.stop(): then the run is stopped, resumable.
    if not await queue.is_finished():
        left = pending(job)
        capped = cap is not None and crawler.statistics.state.requests_total >= cap
        why = f"the plan's ceiling of {ceiling} requests" if capped else "an interrupt"
        reason = f"ended by {why} with {left if left is not None else 'some'} requests pending"
        event(rid, "incomplete", None, reason, site=job.doc["source"])
        job.doc["status"] = "stopped"
        job.save()
        return Stop(
            EXIT_INCOMPLETE, f"STOPPED: {reason}; `{pipeline.consumer().cli} crawl resume` continues it."
        )
    if (job.doc.get("list") or {}).get("status") == "blocked":  # an earlier attempt's hole stays a hole
        job.doc["status"] = "stopped"
        job.save()
        return Stop(EXIT_INCOMPLETE, "STOPPED: robots.txt disallowed a list page of this walk; it cannot "
                                     "complete. Plan a new crawl.")  # fmt: skip
    return None


def _preflight(job: Run, polite: Politeness, name: str, counts: dict, card_info: dict | None) -> None:
    """Everything before the first site request, shared by a crawl and a probe: the politeness ceiling,
    the site cooldown, the run's one proxy, the egress preflight (a refusal stops, nothing sent to the
    site), the run card as line 1 of the log, status running."""
    polite.checked()
    if stop := latch_stop(job.doc["source"]):  # the CLI refuses before its gate; this is the backstop
        raise stop
    if until := cooldown_until(job.doc["source"]):
        raise Stop(
            EXIT_COOLDOWN,
            f"{job.doc['source']} is cooling down until {until} after repeated challenges; not starting",
        )
    if plugin_class("drivers", name).network:  # offline backends (fixtures) send nothing, so need no check
        _run_proxy(job)
        counts["egress_trace"] += 1
        try:
            job.doc["egress"] = egress.preflight()
        except egress.EgressRefused as e:
            _emit_card(job, card_info, polite, name, "REFUSED (not the home country)")  # line 1 of the log
            runlog.LOG.warning(
                "egress preflight refused: %s", e, extra={"event": "preflight", "reason": "egress_refused"}
            )
            job.doc["status"] = "stopped"  # resumable once the network is fixed
            _count(job, counts)  # one trace request, zero to the site
            job.save()
            raise Stop(EXIT_EGRESS, f"refusing to start: {e}") from e
    _emit_card(
        job, card_info, polite, name, job.doc.get("egress") or "n/a (offline backend)"
    )  # line 1 of the log
    if job.doc.get("egress"):
        runlog.LOG.info(
            "egress preflight passed: %s",
            job.doc["egress"].get("loc"),
            extra={"event": "preflight", "egress": job.doc["egress"]},
        )
    job.doc["status"] = "running"
    job.save()


def _run(
    job: Run,
    source,
    handle: Callable[[dict, FetchResult], list | None],
    polite: Politeness,
    requests: list[dict] | None = None,
    backend_opts: dict | None = None,
    card_info: dict | None = None,
    postcheck: Callable[[Run, Stop | None], Stop | None] | None = None,
    pol: policy.SyncPolicy | None = None,
) -> int:
    """The run itself; `run` wraps it in the live log."""
    name = (backend_opts or {}).get("backend") or variants.get(job.doc["variant"]).backend
    counts = {k: 0 for k in ZERO}
    _preflight(job, polite, name, counts, card_info)
    event(job.doc["run_id"], "started" if requests is not None else "resumed")
    if requests is None and (job.doc.get("list") or {}).get("status") == "failed":
        del job.doc["list"]["status"]  # its lost page was queued again: this attempt decides afresh
    stop, ended = None, False
    try:
        stop = asyncio.run(
            _crawl(
                job, requests, source, handle, polite, backend_opts or {}, counts, _ceiling(card_info), pol
            )
        )
        ended = True
    finally:
        _count(job, counts)  # recorded however the attempt ended
        job.save()  # before the postcheck, so a postcheck that raises cannot lose the counts
        if postcheck is not None and ended:  # the command's verdict on what the attempt wrote (zero yield)
            stop = postcheck(job, stop) or stop
            job.save()
        if pp := per_page(job.doc["requests"]):
            runlog.LOG.info(
                "site sub-resources per page: blocked %s / answered %s / sent %s (%s pages)",
                pp["blocked"], pp["answered"], pp["sent"], pp["pages"],
                extra={"event": "subresources", "per_page": pp},
            )  # fmt: skip
    if stop:
        raise stop
    job.doc["status"] = "completed"
    job.doc.pop("stop", None)  # an earlier attempt's stop, now resumed to the end
    job.save()
    event(job.doc["run_id"], "completed")
    return 0


def _ceiling(card_info: dict | None) -> int | None:
    """The most page requests this attempt's plan allows (robots.txt is not queued), set by each command;
    None when the command fixes no count."""
    return (card_info or {}).get("ceiling")


def _run_proxy(job: Run) -> None:
    """Fix this run's ONE proxy before anything is sent, and record it. A resumed run keeps the proxy it
    started with (its record), whatever a new plan would choose; a URL no longer configured refuses."""
    ps = proxy.proxy_set()
    if not ps:
        return
    try:
        if rec := job.doc.get("proxy"):
            proxy.activate(rec["url"])
        px = proxy.config()
    except proxy.ProxyError as e:
        raise Stop(EXIT_EGRESS, f"refusing to start: {e}") from None
    job.doc["proxy"] = {"url": px.url, "choice": ps.choice if len(ps.urls) > 1 else "only"}
    job.save()


def _emit_card(job: Run, info: dict | None, polite: Politeness, backend: str, egress_value) -> None:
    """Render the run card to stderr, log it as the run's `run_card` line, keep it as run.v1.card."""
    info = info or {}
    ref = job.doc.get("approval_ref") or {}
    planned = info.get("planned") or {}
    card = runcard.make(
        info.get("command", job.doc["command"]),
        info.get("purpose", job.doc["command"]),
        job.doc["run_id"],
        ctx={
            "queue": job.doc["queue"],
            "site": job.doc["source"],
            "ads": planned.get("ad"),
            "ab_at": info.get("ab_at"),
            "sample_file": info.get("sample_file"),
        },
        roots={"DATA": store.data_dir(), "STATE": store.state_dir()},
        planned=planned,
        per_minute=polite.per_minute,
        variant=job.doc["variant"],
        backend=backend,
        blocking=browser.blocking(variants.get(job.doc["variant"])) if backend == "browser" else None,
        # an offline backend never asks (with several proxies, config() refuses unless one is chosen)
        proxy=(proxy.config().public if backend not in (None, "fixture") and proxy.proxy_set() else None),
        plan_hash=ref.get("plan_hash"),
        approval_id=ref.get("approval_id"),
        egress=egress_value,
        log_abs=str(runlog.log_path(job.doc["run_id"])[0]),
        selection=info.get("selection"),
    )
    runcard.render(card)
    runlog.LOG.info("run card", extra={"event": "run_card", "card": card})
    job.doc["card"] = card


def run(
    job: Run,
    source,
    handle: Callable[[dict, FetchResult], list | None],
    polite: Politeness,
    requests: list[dict] | None = None,
    backend_opts: dict | None = None,
    card_info: dict | None = None,
    postcheck: Callable[[Run, Stop | None], Stop | None] | None = None,
    pol: policy.SyncPolicy | None = None,
) -> int:
    """Run (or resume, when `requests` is None) `job`. Returns 0, or raises Stop (recorded as run.v1
    `stop`). The whole attempt is logged live to runs/<run_id>.log.jsonl (ADR-0007), referenced from
    run.v1 `log_ref`. `postcheck(job, stop)` may turn an attempt that ended cleanly into a stop."""
    with runlog.attached(job.doc["run_id"]) as rel:
        job.doc["log_ref"] = rel
        # the first line of the log is the run card (written once the egress is known)
        try:
            code = _run(job, source, handle, polite, requests, backend_opts, card_info, postcheck, pol)
        except Stop as s:
            runlog.LOG.warning(
                "stopped (exit %s): %s",
                s.code,
                s,
                extra={"event": "stop", "code": s.code, "counts": job.doc.get("requests")},
            )
            job.doc["stop"] = {"code": s.code, "reason": str(s) or f"exit {s.code}"}
            job.save()
            incidents.open_incident(s.code, str(s), job.doc["run_id"], job.doc["command"], kind=s.kind)
            raise
        runlog.LOG.info("completed", extra={"event": "stop", "code": 0, "counts": job.doc.get("requests")})
        return code


# ── probe: ONE robots.txt GET, to ask "does the site answer normally from this egress?" ─────────────

PROBE_TIMEOUT = 20.0  # seconds for the one GET; no answer by then is "unreachable", never a retry
# A robots.txt body: at least one `User-agent:` record line (RFC 9309 §2.2), and not an HTML document.
ROBOTS_RECORD = re.compile(r"^\s*user-agent\s*:", re.I | re.M)
HTML_DOC = re.compile(r"<\s*(!doctype\s+html|html|head|body)\b", re.I)


def probe_markers(res: FetchResult) -> dict:
    """What the body IS, as markers only (never the body itself): robots records, an HTML document, the
    content type, the size."""
    body = res.html or ""
    return {
        "robots_records": len(ROBOTS_RECORD.findall(body)),
        "html": bool(HTML_DOC.search(body)),
        "content_type": {k.lower(): v for k, v in (res.headers or {}).items()}.get("content-type"),
        "bytes": len(body.encode()),
    }


def probe_verdict(res: FetchResult) -> str | None:
    """None = CLEAN: 200, a robots.txt body, no challenge sign. Else the stop reason: challenge.detect()'s
    code first (403/429/503, cf-mitigated, a challenge title, ...), then any other status, then a 200 whose
    body is not robots.txt (an HTML page, an empty body): failing closed, like detect()'s unexpected_page."""
    if reason := challenge.detect(res, None):
        return reason
    if res.status != 200:
        return f"http_{res.status}"
    m = probe_markers(res)
    return None if m["robots_records"] and not m["html"] else "unexpected_page"


async def _probe_get(url: str, backend_opts: dict, counts: dict, timeout: float):
    """The ONE GET, through the run's proxy, via the backend's HTTP client (send_request: no crawlee
    queue, so no retry). Returns (FetchResult | None, latency_ms, error | None)."""
    opts = dict(backend_opts)
    backend = load(
        "drivers", opts.pop("backend", None) or variants.get("http").backend, variant="http", **opts
    )
    _counting(backend.http_client, counts)
    proxy_info = None
    if getattr(backend, "network", False) and (px := proxy.config()):
        from crawlee.proxy_configuration import ProxyConfiguration

        proxy_info = await ProxyConfiguration(proxy_urls=[proxy.url_with_credentials(px)]).new_proxy_info(
            None, None, None
        )
    t0 = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            backend.http_client.send_request(url, proxy_info=proxy_info, timeout=timedelta(seconds=timeout)),
            timeout + 5,  # a client that ignores its own timeout still cannot hang the probe
        )
        body = (await resp.read()).decode("utf-8", errors="replace")
        res = FetchResult(
            url, resp.status_code, url, body, {k.lower(): v for k, v in dict(resp.headers).items()}
        )
        return res, round((time.monotonic() - t0) * 1000), None
    except Exception as e:  # noqa: BLE001 - no answer of any kind: recorded as unreachable
        return None, round((time.monotonic() - t0) * 1000), e
    finally:
        await backend.close()


def probe(
    job: Run, source, polite: Politeness, backend_opts: dict | None = None, card_info: dict | None = None
) -> int:
    """`probe robots`: the run's preflight (cooldown, proxy, egress), then ONE GET of the source's
    robots.txt, never retried. CLEAN (200 + a robots.txt body + no challenge sign) completes; a timeout or
    refused connection stops as unreachable (no cooldown); anything else is a challenge stop (it counts
    toward the site cooldown, like any challenge). The result is run.v1 `probe`: status, latency, verdict,
    markers, egress country."""
    with runlog.attached(job.doc["run_id"]) as rel:
        job.doc["log_ref"] = rel
        try:
            return _probe(job, source, polite, backend_opts or {}, card_info)
        except Stop as s:
            runlog.LOG.warning(
                "probe stopped (exit %s): %s",
                s.code,
                s,
                extra={"event": "stop", "code": s.code, "counts": job.doc.get("requests")},
            )
            job.save()
            incidents.open_incident(s.code, str(s), job.doc["run_id"], job.doc["command"])
            raise


def _probe(job: Run, source, polite: Politeness, backend_opts: dict, card_info: dict | None) -> int:
    name = backend_opts.get("backend") or variants.get("http").backend
    counts = {k: 0 for k in ZERO}
    _preflight(job, polite, name, counts, card_info)
    event(job.doc["run_id"], "started")
    url = source.robots_url
    try:
        res, ms, err = asyncio.run(_probe_get(url, backend_opts, counts, PROBE_TIMEOUT))
    finally:
        _count(job, counts)
        job.save()
    rec = {
        "url": url,
        "status": res.status if res else None,
        "latency_ms": ms,
        "egress_country": (job.doc.get("egress") or {}).get("loc"),
    }
    if res is None:
        job.doc["probe"] = rec | {"verdict": "unreachable", "reason": type(err).__name__}
        reason = f"no response to the probe within {PROBE_TIMEOUT:g} s ({type(err).__name__})"
        event(job.doc["run_id"], "unreachable", url, reason, site=job.doc["source"])
        job.doc["status"] = "stopped"
        job.save()
        raise Stop(EXIT_UNREACHABLE, f"NOT CLEAN: unreachable: {reason}.")
    rec |= {"markers": probe_markers(res)}
    if reason := probe_verdict(res):
        job.doc["probe"] = rec | {"verdict": "not_clean", "reason": reason}
        stop = _challenge(job, url, reason, res)
        msg = f"NOT CLEAN: {reason} at {url} (HTTP {res.status}, {ms} ms); nothing more was sent"
        raise Stop(stop.code, msg + (f". {stop}" if stop.code == EXIT_COOLDOWN else ""))
    job.doc["probe"] = rec | {"verdict": "clean", "reason": None}
    job.doc["status"] = "completed"
    job.save()
    event(job.doc["run_id"], "completed")
    runlog.LOG.info("probe clean", extra={"event": "probe", "probe": job.doc["probe"]})
    return 0
