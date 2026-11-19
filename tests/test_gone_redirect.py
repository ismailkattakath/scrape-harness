"""A removed ad that redirects to its category list is GONE, not a wall; a password field is not a wall by
itself; every stop keeps its page; and a past stop can be reclassified.

Measured case (2026-11-08): a removed ad answered 200 after a redirect with its category list's title,
and stopped as signin_wall because the site carries a hidden login modal (a password field, "Sign in",
"Something went wrong") on every page: 32/32 and 100/100 normal ad pages had it.
"""

import gzip
import json

import pytest
from conftest import FIXTURES, LIST, ORIGIN
from example_site.source import ExampleSite

from scrape_harness import challenge, crawl, incidents, store
from scrape_harness.cli import main
from scrape_harness.interfaces import FetchResult

FAST = crawl.Politeness(per_minute=6000, override=True)
MODAL = (
    '<div id="login-modal" style="display:none"><form><input type="email"><input type="password">'
    "<button>Sign in</button></form><p>Something went wrong. You can try again.</p></div>"
)
AD_A, AD_B = f"{ORIGIN}/ads/1000001", f"{ORIGIN}/ads/1000002"


def page(body: str, title: str = "Category | Example") -> str:
    return f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"


LIST_WITH_MODAL = (FIXTURES / "list-p1.html").read_text().replace("</body>", MODAL + "</body>")
WALL = page('<h1>Sign in</h1><form><input type="email"><input type="password"></form>', "Sign in")


def res(html: str, url: str = AD_A, final: str | None = None, status: int = 200) -> FetchResult:
    return FetchResult(url, status, final or url, html, {})


# ── the detector ────────────────────────────────────────────────────────────────────────────────────
def test_a_hidden_login_modal_is_chrome_once_the_source_names_it():
    r = res(page("<main>no ad here</main>" + MODAL))
    assert challenge.detect(r, "#description") == "signin_wall"  # the modal's own form says "Sign in"
    assert challenge.detect(r, "#description", ("#login-modal",)) == "unexpected_page"  # stripped: honest


def test_a_password_field_without_any_sign_in_signal_is_unexpected_not_a_wall():
    r = res(page('<form><input type="password"><button>Go</button></form>', "Profile"))
    assert challenge.detect(r, "#description") == "unexpected_page"


def test_a_real_wall_still_is_one():
    assert challenge.detect(res(WALL), "#description") == "signin_wall"
    assert challenge.detect(res(WALL, final=f"{ORIGIN}/login"), "#description") == "login_redirect"


@pytest.mark.parametrize(
    ("final", "html", "gone"),
    [
        (LIST, LIST_WITH_MODAL, True),  # redirected to a clean list page: removed ad
        (None, LIST_WITH_MODAL, False),  # no redirect: a list page at the ad's own URL is unexpected
        (f"{ORIGIN}/signin-required", WALL, False),  # redirected to a wall: no list markup, still a stop
        (LIST, LIST_WITH_MODAL.replace("<title>", "<title>Just a moment...</title><x>"), False),  # challenge
    ],
)
def test_redirected_to_list_is_tight(final, html, gone):
    r = res(html, final=final)
    reason = challenge.detect(r, "#description")
    assert challenge.redirected_to_list(r, reason, "#results") is gone


# ── end to end, through crawl.run with the fixture backend ──────────────────────────────────────────
def fixtures(tmp_path, overrides, extra_files=()):
    d = tmp_path / "fx"
    d.mkdir(parents=True, exist_ok=True)
    for f in FIXTURES.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    for name, text in extra_files:
        (d / name).write_text(text)
    (d / "urls.json").write_text(json.dumps(json.loads((FIXTURES / "urls.json").read_text()) | overrides))
    return {"backend": "fixture", "root": str(d)}


def ads(*urls):
    return [{"url": u, "kind": "ad", "meta": {"ad_id": u.rsplit("/", 1)[1]}} for u in urls]


def run_log(job) -> list[dict]:
    path = runlog_path(job)
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def runlog_path(job):
    from scrape_harness import runlog

    return runlog.log_path(job.doc["run_id"])[0]


def test_an_ad_redirected_to_its_category_list_is_gone_and_the_run_goes_on(data_dir, tmp_path):
    opts = fixtures(
        tmp_path,
        {AD_A: {"file": "list-modal.html", "final_url": LIST}, AD_B: "ad-1000002.html"},
        [("list-modal.html", LIST_WITH_MODAL)],
    )
    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    done = []
    assert crawl.run(job, ExampleSite(), lambda m, r: done.append(r.url), FAST, ads(AD_A, AD_B), opts) == 0
    assert done == [AD_B] and job.doc["challenges"] == 0 and job.doc["status"] == "completed"
    gone = [e for e in store.read("events") if e["kind"] == "gone"]
    assert [(e["url"], e["reason"]) for e in gone] == [(AD_A, "redirected_to_list")]
    lines = [x for x in run_log(job) if x.get("event") == "request" and x.get("kind") == "ad"]
    assert {"path": "/ads/1000001", "final_path": "/listings/widgets/sample-region"}.items() <= lines[
        0
    ].items()
    assert lines[1]["final_path"] == lines[1]["path"]  # always logged: "no redirect" is evidence too


def test_a_stop_saves_the_whole_page_to_state_and_names_it(data_dir, tmp_path):
    opts = fixtures(
        tmp_path, {AD_A: {"file": "wall.html", "final_url": f"{ORIGIN}/members"}}, [("wall.html", WALL)]
    )
    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    with pytest.raises(crawl.Stop) as e:
        crawl.run(job, ExampleSite(), lambda m, r: None, FAST, ads(AD_A), opts)
    (ev,) = [x["evidence"] for x in store.read("events") if x["kind"] == "challenge"]
    assert ev["final_path"] == "/members" and ev["snapshot"].startswith("STATE:stops/")
    saved = store.state_dir() / ev["snapshot"].removeprefix("STATE:")
    assert gzip.decompress(saved.read_bytes()).decode() == WALL  # the whole page, not an excerpt
    assert ev["snapshot"] in str(e.value)  # the stop names it
    (inc,) = incidents.open_ones()
    assert ev["snapshot"] in inc["reason"]  # and so does its incident
    assert not list(store.data_dir().rglob("stops"))  # raw page content stays out of DATA


def test_resume_applies_the_same_rules(data_dir, tmp_path):
    """A run stopped by a real wall, then resumed: the remaining ad, now redirected to the list, is gone."""
    files = [("wall.html", WALL), ("list-modal.html", LIST_WITH_MODAL)]
    opts = fixtures(
        tmp_path, {AD_A: "wall.html", AD_B: {"file": "list-modal.html", "final_url": LIST}}, files
    )
    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    with pytest.raises(crawl.Stop):
        crawl.run(job, ExampleSite(), lambda m, r: None, FAST, ads(AD_A, AD_B), opts)
    # the operator cleared the wall: AD_A is a real ad now; resume (requests=None) picks the queue up
    opts = fixtures(
        tmp_path, {AD_A: "ad-1000001.html", AD_B: {"file": "list-modal.html", "final_url": LIST}}, files
    )
    done = []
    resumed = crawl.Run.load(job.doc["run_id"])
    assert crawl.run(resumed, ExampleSite(), lambda m, r: done.append(r.url), FAST, None, opts) == 0
    assert done == [AD_A]
    assert [e["url"] for e in store.read("events") if e["kind"] == "gone"] == [AD_B]


def test_a_past_stop_can_be_reclassified_as_gone(data_dir, tmp_path, capsys):
    opts = fixtures(tmp_path, {AD_A: {"file": "wall.html", "final_url": LIST}}, [("wall.html", WALL)])
    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    with pytest.raises(crawl.Stop):
        crawl.run(job, ExampleSite(), lambda m, r: None, FAST, ads(AD_A), opts)
    (inc,) = incidents.open_ones()  # the stop opened it, as in a real run
    capsys.readouterr()
    assert (
        main(["incidents", "reclassify", inc["id"], "--as", "gone", "--note", "the list title, a redirect"])
        == 0
    )
    (fix,) = [e for e in store.read("events") if e["kind"] == "run_correction"]
    c = fix["correction"]
    assert c["field"] == "stop" and c["incident"] == inc["id"] and c["reconciled"]["reason"] == "gone"
    assert c["recorded"]["reason"] == "signin_wall" and c["recorded"]["url"] == AD_A
    assert inc["id"] not in {r["id"] for r in incidents.open_ones()}  # acked, with the note
    assert crawl.Run.load(job.doc["run_id"]).doc["challenges"] == 1  # the run record is never rewritten
    with pytest.raises(SystemExit, match="already reclassified"):
        main(["incidents", "reclassify", inc["id"], "--as", "gone", "--note", "again"])


def test_an_acked_stop_and_a_cooldown_incident_can_be_reclassified(data_dir, tmp_path, capsys):
    """Stop 1 acked earlier; stop 2 tripped the cooldown (its incident is kind cooldown): both reclassify,
    each against the challenge that opened it."""
    opts = fixtures(
        tmp_path, {AD_A: {"file": "wall.html", "final_url": LIST}, AD_B: "wall.html"}, [("wall.html", WALL)]
    )
    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    with pytest.raises(crawl.Stop):
        crawl.run(job, ExampleSite(), lambda m, r: None, FAST, ads(AD_A, AD_B), opts)
    (first,) = incidents.open_ones()
    incidents.ack(first["id"], "looked at it", "agent")  # acked before any reclassify existed
    with pytest.raises(crawl.Stop):  # the resume hits the second wall: 2 challenges in the hour, cooldown
        crawl.run(crawl.Run.load(job.doc["run_id"]), ExampleSite(), lambda m, r: None, FAST, None, opts)
    cool = [r for r in incidents.open_ones() if r["kind"] == "cooldown"]
    assert len(cool) == 1
    capsys.readouterr()
    for inc in (first, cool[0]):
        assert main(["incidents", "reclassify", inc["id"], "--as", "gone", "--note", "evidence"]) == 0
    fixes = [e["correction"] for e in store.read("events") if e["kind"] == "run_correction"]
    assert [f["incident"] for f in fixes] == [first["id"], cool[0]["id"]]
    assert [f["recorded"]["url"] for f in fixes] == [AD_A, AD_B]  # each against ITS challenge
    assert not incidents.open_ones()  # the cooldown incident is acked now; the first kept its own ack


class SiteWithChrome(ExampleSite):
    ignore_selectors = ("#login-modal",)  # what a source declares for a hidden login modal


def test_the_sources_ignore_selectors_reach_the_detector_in_a_run(data_dir, tmp_path):
    lost = page("<main>this page is not an ad</main>" + MODAL)  # no redirect, not a list: a real stop
    opts = fixtures(tmp_path, {AD_A: "lost.html"}, [("lost.html", lost)])
    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    with pytest.raises(crawl.Stop):
        crawl.run(job, SiteWithChrome(), lambda m, r: None, FAST, ads(AD_A), opts)
    (ch,) = [e for e in store.read("events") if e["kind"] == "challenge"]
    assert ch["reason"] == "unexpected_page"  # not signin_wall: the modal is the site's chrome


def test_hops_count_only_this_navigations_main_frame_redirects():
    main = object()

    class Req:
        def __init__(self, nav, frame, redirected):
            self._nav, self.frame, self.redirected_from = nav, frame, redirected

        def is_navigation_request(self):
            return self._nav

    hops: dict = {}
    on = crawl.hop_listener(hops, "k", main)
    for r in (
        Req(True, main, None),
        Req(True, main, object()),
        Req(True, main, object()),
        Req(True, object(), object()),
        Req(False, main, object()),
    ):
        on(r)
    assert hops == {"k": 2}  # two main-frame redirects; an iframe's and a sub-resource's do not count


# ── a soft 404: the site serves its list (or an "ad no longer active" page) AT the ad URL, no redirect ──
EXPIRED = page(
    '<main class="ad-expired-page"><section class="ad-expired"><h2>Sorry, this ad is no longer active</h2>'
    "</section></main>" + MODAL
)


class SiteWithGonePage(ExampleSite):
    ignore_selectors = ("#login-modal",)
    gone_selectors = ("main.ad-expired-page",)  # what a source declares for its expired-ad page


def test_a_list_page_served_at_the_ad_url_is_gone_regardless_of_hops(data_dir, tmp_path):
    """The measured shape: 200, no redirect (final == requested, 0 hops), the category list at the ad URL."""
    opts = fixtures(
        tmp_path, {AD_A: "list-modal.html", AD_B: "ad-1000002.html"}, [("list-modal.html", LIST_WITH_MODAL)]
    )
    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    done = []
    assert crawl.run(job, ExampleSite(), lambda m, r: done.append(r.url), FAST, ads(AD_A, AD_B), opts) == 0
    assert done == [AD_B] and job.doc["challenges"] == 0
    assert [(e["url"], e["reason"]) for e in store.read("events") if e["kind"] == "gone"] == [
        (AD_A, "served_list")
    ]


def test_the_sources_expired_ad_page_is_gone(data_dir, tmp_path):
    opts = fixtures(tmp_path, {AD_A: "expired.html"}, [("expired.html", EXPIRED)])
    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    assert crawl.run(job, SiteWithGonePage(), lambda m, r: None, FAST, ads(AD_A), opts) == 0
    assert [e["reason"] for e in store.read("events") if e["kind"] == "gone"] == ["expired_notice"]


@pytest.mark.parametrize(
    "html", [LIST_WITH_MODAL, EXPIRED]
)  # both gone paths: a served list, an expired notice
@pytest.mark.parametrize(
    ("status", "headers", "title"),
    [(200, {"cf-mitigated": "challenge"}, None), (200, {}, "Just a moment..."), (403, {}, None)],
)
def test_a_challenge_sign_keeps_a_soft_404_shape_a_stop(html, status, headers, title):
    if title:
        html = html.replace("<title>", f"<title>{title}</title><x>", 1)
    r = FetchResult(AD_A, status, AD_A, html, headers)
    reason = challenge.detect(r, "#description") or "unexpected_page"
    assert challenge.gone_reason(r, reason, "#results", ("main.ad-expired-page",)) is None


def test_the_resume_plan_counts_what_is_still_pending(data_dir):
    from scrape_harness.cli import _planned_from

    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    meta = store.state_dir() / "crawlee" / "request_queues" / job.doc["queue"].lower() / "__metadata__.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"pending_request_count": 719, "handled_request_count": 55}))
    assert crawl.pending(job) == 719
    assert _planned_from({"command": "crawl resume", "resumes": "fetch-ads", "pending": 719}) == {
        "ad": 719,
        "robots": 1,
    }
    assert _planned_from({"command": "crawl resume", "resumes": "crawl-lists", "pending": 3}) == {
        "list": 3,
        "robots": 1,
    }


# ── fetch-ads --supersede: a NEW run of exactly a stopped run's pending ads ────────────────────────────
def sighted_now(*urls):
    """Fresh list sightings of these ads: a superseded queue is admitted through the gate again (plan.admit),
    so only an ad with a fresh good sighting, alive and due, is taken over."""
    from datetime import UTC, datetime

    from test_fresh_gate import card_obs

    at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.append("observations", [card_obs(i, at) | {"ad_id": u.rsplit("-", 1)[1], "ad_url": u}
                                  for i, u in enumerate(urls)])  # fmt: skip


def test_supersede_takes_exactly_the_pending_ads_and_closes_the_old_run(data_dir, tmp_path, capsys):
    base = f"{ORIGIN}/listings/widgets/sample-region"  # the example's real ad URLs (its parser reads the id)
    a, b, c = f"{base}/blue-widget-1000001", f"{base}/red-widget-1000002", f"{base}/green-widget-1000003"
    sighted_now(a, b, c)
    files = [("wall.html", WALL)]
    opts = fixtures(tmp_path, {a: "ad-1000001.html", b: "wall.html", c: "ad-1000003.html"}, files)
    old = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    with pytest.raises(crawl.Stop):  # a real crawl: its queue is crawlee's own file storage
        reqs = [
            {"url": u, "kind": "ad", "meta": {"reason": "recruit", "card_city_label": None}}
            for u in (a, b, c)
        ]
        crawl.run(old, ExampleSite(), lambda m, r: None, FAST, reqs, opts)  # what cmd_fetch_ads queues
    pending = {r["url"] for r in crawl.pending_requests(crawl.Run.load(old.doc["run_id"]))}
    assert {r["meta"]["reason"] for r in crawl.pending_requests(crawl.Run.load(old.doc["run_id"]))} == {
        "recruit"
    }
    assert pending == {b, c}  # a was handled; the wall page stays pending, so does c
    # the wall is cleared; a NEW run over exactly those two
    opts = fixtures(tmp_path, {a: "ad-1000001.html", b: "ad-1000002.html", c: "ad-1000003.html"}, files)
    fix = ["--driver", "fixture", "--fixtures", opts["root"], "--max-tasks-per-minute", "6000",
           "--approve-politeness-override"]  # fmt: skip
    capsys.readouterr()
    assert main(["fetch-ads", "--supersede", old.doc["run_id"], *fix]) == 0
    docs = [json.loads(p.read_text()) for p in store.data_dir().joinpath("runs").glob("*.json")]
    (doc,) = [d for d in docs if d.get("supersedes")]
    assert (
        doc["supersedes"] == old.doc["run_id"]
        and doc["requests"]["ad_pages"] == 2
        and doc["status"] == "completed"
    )
    (sup,) = [e for e in store.read("events") if e["kind"] == "superseded"]
    assert (
        sup["run_id"] == old.doc["run_id"] and sup["reason"] == doc["run_id"]
    )  # the old run names its successor
    assert {r["fetch_reason"] for r in store.read("listings")} == {"recruit"}  # the queue's reason survives
    with pytest.raises(SystemExit, match="stopped or aborted"):
        main(["fetch-ads", "--supersede", doc["run_id"], *fix])  # a completed run cannot be superseded
    # the old run stays "stopped" and its own queue still lists b and c as pending: a second supersede
    # would GET them again, so each scope is taken over once
    with pytest.raises(SystemExit, match="already taken over"):
        main(["fetch-ads", "--supersede", old.doc["run_id"], *fix])
    assert len([e for e in store.read("events") if e["kind"] == "superseded"]) == 1


def test_supersede_failed_takes_exactly_the_failed_ads_once(data_dir, tmp_path, capsys):
    base = f"{ORIGIN}/listings/widgets/sample-region"
    a, b = f"{base}/blue-widget-1000001", f"{base}/red-widget-1000002"
    sighted_now(a, b)
    bad = [("bad.html", "<main>x</main>")]
    opts = fixtures(tmp_path, {a: {"file": "bad.html", "status": 502}, b: "ad-1000002.html"}, bad)
    old = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    reqs = [{"url": u, "kind": "ad", "meta": {"reason": "recruit", "card_city_label": None}} for u in (a, b)]
    crawl.run(old, ExampleSite(), lambda m, r: None, FAST, reqs, opts)  # a 502 is retried, then FAILS
    old = crawl.Run.load(old.doc["run_id"])
    assert [r["url"] for r in crawl.failed_requests(old)] == [a] and crawl.pending_requests(old) == []
    # a run that recorded "completed" with a failed request is corrected to stopped (as …1a71c3 was)
    crawl.correct_status(old.doc["run_id"], "stopped", "interrupted", "test: one ad failed on retries")
    opts = fixtures(tmp_path, {a: "ad-1000001.html", b: "ad-1000002.html"}, bad)
    fix = ["--driver", "fixture", "--fixtures", opts["root"], "--max-tasks-per-minute", "6000",
           "--approve-politeness-override"]  # fmt: skip
    with pytest.raises(SystemExit, match="nothing pending"):
        main(["fetch-ads", "--supersede", old.doc["run_id"], *fix])  # without --failed: none pending
    capsys.readouterr()
    assert main(["fetch-ads", "--supersede", old.doc["run_id"], "--failed", "--dry-run", *fix]) == 0
    dry = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith('{"reason"')]
    assert [d["url"] for d in dry] == [a]
    assert main(["fetch-ads", "--supersede", old.doc["run_id"], "--failed", *fix]) == 0
    docs = [json.loads(p.read_text()) for p in store.data_dir().joinpath("runs").glob("*.json")]
    (doc,) = [d for d in docs if d.get("supersedes")]
    assert doc["supersede_scope"] == "failed" and doc["requests"]["ad_pages"] == 1
    with pytest.raises(SystemExit, match="already taken over"):
        main(["fetch-ads", "--supersede", old.doc["run_id"], "--failed", *fix])


def test_plan_proxy_exclude_never_picks_an_excluded_url(monkeypatch):
    import dataclasses as dc
    from types import SimpleNamespace

    from scrape_harness import pipeline, proxy
    from scrape_harness.cli import _proxy_plan

    real = pipeline.current()
    urls = [
        "http://ca-a.proxy.example:8080",
        "http://ca-b.proxy.example:8080",
        "http://ca-c.proxy.example:8080",
    ]
    cfg = {"urls": urls, "choice": "random", "user_secret": "a:b", "password_secret": "a:c"}
    monkeypatch.setattr(pipeline, "current", lambda: dc.replace(real, proxy=cfg))
    monkeypatch.setattr("scrape_harness.cli._networked", lambda a, v: True)
    try:
        for _ in range(20):
            got = _proxy_plan(SimpleNamespace(plan=True, proxy_exclude=["ca-a."]), {})["proxy"]
            assert got in urls[1:]
        with pytest.raises(SystemExit, match="leaves no proxy"):
            _proxy_plan(SimpleNamespace(plan=True, proxy_exclude=["proxy.example"]), {})
    finally:
        proxy.deactivate()


def test_a_404_that_is_the_expired_ad_page_is_gone_on_a_first_visit(data_dir, tmp_path):
    """Measured: the same expired-ad page, once with 200 (stop 2), once with 404 (the next run's stop)."""
    opts = fixtures(tmp_path, {AD_A: {"file": "expired.html", "status": 404}, AD_B: "ad-1000002.html"},
                    [("expired.html", EXPIRED)])  # fmt: skip
    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    assert crawl.run(job, SiteWithGonePage(), lambda m, r: None, FAST, ads(AD_A, AD_B), opts) == 0
    assert [(e["url"], e["reason"]) for e in store.read("events") if e["kind"] == "gone"] == [
        (AD_A, "expired_notice")
    ]
    assert job.doc["challenges"] == 0


# ── the standard outcome classes for an AD request (RFC 9110 + crawlee's own handling) ───────────────
def one_ad(data_dir, tmp_path, entry, files=()):
    opts = fixtures(tmp_path, {AD_A: entry, AD_B: "ad-1000002.html"}, files)
    job = crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    try:
        code = crawl.run(job, SiteWithGonePage(), lambda m, r: None, FAST, ads(AD_A, AD_B), opts)
    except crawl.Stop as e:
        code = e
    events = [e for e in store.read("events") if e.get("run_id") == job.doc["run_id"]]
    return code, job, events


@pytest.mark.parametrize(
    ("status", "body", "label"),
    [
        (404, "", "http_404"),  # a bare 404 with an EMPTY body: gone
        (410, "", "http_410"),
        (404, EXPIRED, "expired_notice"),  # the measured stop-3 shape: the expired-ad page as a 404
        (410, LIST_WITH_MODAL, "served_list"),  # a category list served as a 410
    ],
)
def test_404_and_410_are_gone_and_the_run_goes_on(data_dir, tmp_path, status, body, label):
    code, job, events = one_ad(data_dir, tmp_path, {"file": "p.html", "status": status}, [("p.html", body)])
    assert code == 0 and job.doc["challenges"] == 0
    assert [(e["url"], e["reason"]) for e in events if e["kind"] == "gone"] == [(AD_A, label)]


@pytest.mark.parametrize(
    ("status", "headers", "title", "stop"),
    [
        (404, {"cf-mitigated": "challenge"}, None, "cf_mitigated"),  # a 404 with a challenge sign: STOP
        (404, {}, "Just a moment...", "challenge_title"),
        (403, {}, None, "http_403"),
        (429, {}, None, "http_429"),
        (503, {}, None, "http_503"),
    ],
)
def test_a_challenge_sign_or_a_block_status_stops(data_dir, tmp_path, status, headers, title, stop):
    body = page("<main>x</main>", title or "x")
    code, job, events = one_ad(
        data_dir, tmp_path, {"file": "p.html", "status": status, "headers": headers}, [("p.html", body)]
    )
    assert isinstance(code, crawl.Stop) and stop in str(code)
    assert [e["reason"] for e in events if e["kind"] == "challenge"] == [stop]


def test_another_5xx_is_crawlees_retry_then_a_failed_request_never_a_stop(data_dir, tmp_path):
    code, job, events = one_ad(
        data_dir, tmp_path, {"file": "p.html", "status": 502}, [("p.html", page("<main>x</main>"))]
    )
    assert code == 0 and job.doc["challenges"] == 0  # not a stop
    assert job.doc["requests"]["ad_pages"] > 2  # crawlee retried the 502 (AD_A more than once, plus AD_B)
    assert [e["url"] for e in events if e["kind"] == "failed_request"] == [AD_A]  # then it failed


def test_a_404_page_carrying_only_the_passive_beacon_is_gone_not_a_challenge():
    """Regression (2026-11-13): a removed ad answered 404 with its category list, and the page had drawn
    Cloudflare's passive jsd beacon (on ~3% of normal pages). The beacon alone is no challenge on a 404."""
    beacon = "<script>var u='/cdn-cgi/challenge-platform/scripts/jsd/main.js?ray=abc'</script>"
    r = FetchResult(AD_A, 404, AD_A, LIST_WITH_MODAL.replace("</body>", beacon + "</body>"), {})
    reason = challenge.detect(r, "#description")
    assert reason == "http_404"
    assert challenge.gone_reason(r, reason, "#results", ("main.ad-expired-page",)) == "served_list"
    # nor on any other status: a 502 is a 502, and a 200 expired-ad page with no ad body is gone (…470b75)
    assert challenge.detect(FetchResult(AD_A, 502, AD_A, r.html, {}), "#description") == "http_502"
    ok = FetchResult(AD_A, 200, AD_A, r.html, {})
    reason = challenge.detect(ok, "#description")
    assert reason != "challenge_marker"
    assert challenge.gone_reason(ok, reason, "#results", ("main.ad-expired-page",)) is not None
    # a REAL challenge sign beside the beacon still stops the run
    real = r.html.replace(
        "</body>", '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/x"></script></body>'
    )
    assert challenge.detect(FetchResult(AD_A, 404, AD_A, real, {}), "#description") == "challenge_marker"


def test_a_404_list_page_with_a_challenge_marker_still_stops():
    marked = LIST_WITH_MODAL.replace(
        "</body>", '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/x"></script></body>'
    )
    r = FetchResult(AD_A, 404, AD_A, marked, {})
    reason = challenge.detect(r, "#description")
    assert reason != "http_404"  # a marker, corroborated by the 404, wins first
    assert challenge.gone_reason(r, reason, "#results", ("main.ad-expired-page",)) is None
