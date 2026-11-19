"""crawlee-backed runs, offline: the fixture backend is a crawlee HttpClient, so the queue,
robots.txt, concurrency limit, retries and our challenge stop all run for real."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIXTURES, LIST, ORIGIN
from example_site.source import ExampleSite

from scrape_harness import challenge, crawl, store
from scrape_harness.drivers.fixture import FixtureBackend
from scrape_harness.interfaces import FetchResult

LIST_EXPECT = ExampleSite.expect["list"]
FAST = crawl.Politeness(per_minute=6000, override=True)  # tests only: the override is explicit


def page(name):
    return (FIXTURES / name).read_text()


@pytest.mark.parametrize(
    ("status", "final", "html", "expect", "reason"),
    [
        (429, None, "", None, "http_429"),
        (403, None, "", None, "http_403"),
        (503, None, "", None, "http_503"),
        (200, f"{ORIGIN}/login?next=x", "<html></html>", None, "login_redirect"),
        (200, None, page("challenge-captcha.html"), LIST_EXPECT, "captcha_iframe"),
        (200, None, page("challenge-captcha.html"), None, None),  # a widget alone, nothing to corroborate
        (200, None, page("challenge-cf.html"), None, "challenge_title"),
        (200, None, page("wall-signin.html"), LIST_EXPECT, "signin_wall"),
        (200, None, page("gate-age.html"), LIST_EXPECT, "consent_gate"),
        (200, None, page("home.html"), LIST_EXPECT, "unexpected_page"),
        (200, None, page("list-p1.html"), LIST_EXPECT, None),
    ],
)
def test_detectors(status, final, html, expect, reason):
    url = f"{LIST}?page=1"
    assert challenge.detect(FetchResult(url, status, final or url, html), expect) == reason


def test_age_words_inside_a_real_page_are_not_a_gate():
    html = page("list-p1.html").replace("Synthetic example ad one", "Are you over 18? Synthetic")
    assert challenge.detect(FetchResult("u", 200, "u", html), LIST_EXPECT) is None


def test_politeness_ceiling_needs_the_operator():
    for p in (crawl.Politeness(concurrency=2), crawl.Politeness(per_minute=61)):
        with pytest.raises(crawl.Stop) as e:
            p.checked()
        assert e.value.code == crawl.EXIT_POLITENESS
    assert crawl.Politeness(concurrency=1, per_minute=60).checked()
    assert crawl.Politeness(concurrency=2, override=True).checked()


def fixtures(tmp_path, overrides):
    d = tmp_path / "fx"
    d.mkdir(parents=True)
    for f in FIXTURES.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    (d / "urls.json").write_text(json.dumps(json.loads((FIXTURES / "urls.json").read_text()) | overrides))
    return {"backend": "fixture", "root": str(d)}


def reqs(*urls):
    return [{"url": u, "kind": "list", "meta": {"page": 1}} for u in urls]


class Rec:
    def __init__(self):
        self.done = []

    def __call__(self, m, res):
        self.done.append(res.url)


def test_one_at_a_time_dedup_and_robots(data_dir, tmp_path):
    opts = fixtures(tmp_path, {f"{ORIGIN}/private/x": "list-p1.html"})
    job = crawl.Run.new("crawl-lists", "desktop", "example.invalid")
    rec = Rec()
    with pytest.raises(crawl.Stop) as e:
        crawl.run(
            job,
            ExampleSite(),
            rec,
            FAST,
            reqs(f"{LIST}?page=1", f"{LIST}?page=1", f"{ORIGIN}/private/x"),
            opts,
        )
    assert rec.done == [f"{LIST}?page=1"]  # deduplicated by unique_key; robots.txt Disallow skipped
    gets = FixtureBackend.last.gets
    assert gets.count(f"{LIST}?page=1") == 1 and f"{ORIGIN}/private/x" not in gets
    assert FixtureBackend.last.max_inflight == 1
    kinds = [e["kind"] for e in store.read("events")]
    assert kinds[0] == "started" and "robots_disallowed" in kinds
    # a list page the walk cannot read is a hole: the run stops (resumable), never `completed`
    assert e.value.code == crawl.EXIT_INCOMPLETE and "completed" not in kinds
    assert job.doc["status"] == "stopped" and job.doc["list"]["status"] == "blocked"


def test_challenge_stops_and_resume_finishes_without_refetch(data_dir, tmp_path):
    p1, p3 = f"{LIST}?page=1", f"{LIST}?page=3"
    job = crawl.Run.new("crawl-lists", "desktop", "example.invalid")
    rec = Rec()
    with pytest.raises(crawl.Stop) as e:
        crawl.run(
            job,
            ExampleSite(),
            rec,
            FAST,
            reqs(p1, p3),
            fixtures(tmp_path / "a", {p3: {"file": "", "status": 429}}),
        )
    assert e.value.code == crawl.EXIT_CHALLENGE  # crawlee did NOT back off and retry the 429: we stopped
    saved = crawl.Run.load()
    assert saved.doc["status"] == "stopped" and saved.doc["challenges"] == 1
    ev = [x for x in store.read("events") if x["kind"] == "challenge"]
    assert (ev[0]["url"], ev[0]["reason"]) == (p3, "http_429")

    crawl.run(saved, ExampleSite(), rec, FAST, None, fixtures(tmp_path / "b", {}))
    pages = [u for u in FixtureBackend.last.gets if "page=" in u]
    assert pages == [p3]  # page 1 was handled in the first run: never fetched again
    assert rec.done == [p1, p3]
    assert crawl.Run.load(saved.doc["run_id"]).doc["status"] == "completed"


def test_second_challenge_aborts_and_cooldown_blocks_starts(data_dir, tmp_path):
    p1 = f"{LIST}?page=1"
    job = crawl.Run.new("crawl-lists", "desktop", "example.invalid")
    first = True
    for want in (crawl.EXIT_CHALLENGE, crawl.EXIT_COOLDOWN):
        with pytest.raises(crawl.Stop) as e:
            crawl.run(
                job,
                ExampleSite(),
                Rec(),
                FAST,
                reqs(p1) if first else None,
                fixtures(tmp_path / str(want), {p1: "challenge-captcha.html"}),
            )
        first = False
        assert e.value.code == want
    assert job.doc["status"] == "aborted"
    until = crawl.cooldown_until("example.invalid")
    assert datetime.fromisoformat(until.replace("Z", "+00:00")) > datetime.now(UTC) + timedelta(hours=23)
    with pytest.raises(crawl.Stop) as e:
        crawl.run(
            crawl.Run.new("crawl-lists", "desktop", "example.invalid"),
            ExampleSite(),
            Rec(),
            FAST,
            reqs(p1),
            fixtures(tmp_path / "x", {}),
        )
    assert e.value.code == crawl.EXIT_COOLDOWN
    assert (
        crawl.cooldown_until("example.invalid", datetime.now(UTC) + timedelta(hours=25)) is None
    )  # it expires


def test_challenges_are_counted_per_site_across_runs(data_dir, tmp_path):
    p1 = f"{LIST}?page=1"
    blocked = {p1: {"file": "challenge-cf.html", "status": 403}}
    with pytest.raises(crawl.Stop) as e:
        crawl.run(
            crawl.Run.new("crawl-lists", "http", "example.invalid"),
            ExampleSite(),
            Rec(),
            FAST,
            reqs(p1),
            fixtures(tmp_path / "a", blocked),
        )
    assert e.value.code == crawl.EXIT_CHALLENGE
    with pytest.raises(crawl.Stop) as e:  # a DIFFERENT run (and variant): still the same site
        crawl.run(
            crawl.Run.new("crawl-lists", "desktop", "example.invalid"),
            ExampleSite(),
            Rec(),
            FAST,
            reqs(p1),
            fixtures(tmp_path / "b", blocked),
        )
    assert e.value.code == crawl.EXIT_COOLDOWN
    cool = [x for x in store.read("events") if x["kind"] == "cooldown"]
    assert len(cool) == 1 and cool[0]["site"] == "example.invalid"
    assert crawl.cooldown_until("other.example") is None  # another site is not cooled down


def test_a_challenge_outside_the_window_does_not_count(data_dir, tmp_path):
    old = (datetime.now(UTC) - crawl.CHALLENGE_WINDOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.append(
        "events",
        [
            {
                "schema": "event.v1",
                "at": old,
                "run_id": "old",
                "kind": "challenge",
                "url": None,
                "reason": "http_403",
                "until": None,
                "site": "example.invalid",
            }
        ],
    )
    p1 = f"{LIST}?page=1"
    with pytest.raises(crawl.Stop) as e:
        crawl.run(
            crawl.Run.new("crawl-lists", "http", "example.invalid"),
            ExampleSite(),
            Rec(),
            FAST,
            reqs(p1),
            fixtures(tmp_path, {p1: {"file": "", "status": 403}}),
        )
    assert e.value.code == crawl.EXIT_CHALLENGE


def test_challenge_evidence_is_captured_without_cookies_or_tokens(data_dir, tmp_path):
    p1 = f"{LIST}?page=1"
    token = "A" * 40
    blocked = {
        p1: {
            "file": "challenge-cf.html",
            "status": 403,
            "headers": {
                "Server": "cloudflare",
                "CF-RAY": "0123456789abcdef-YYZ",
                "cf-mitigated": "challenge",
                "set-cookie": f"__cf_bm={token}; path=/",
            },
        }
    }
    (tmp_path / "page").mkdir()
    with pytest.raises(crawl.Stop):
        crawl.run(
            crawl.Run.new("crawl-lists", "http", "example.invalid"),
            ExampleSite(),
            Rec(),
            FAST,
            reqs(p1),
            fixtures(tmp_path / "page", blocked),
        )
    (ev,) = [x for x in store.read("events") if x["kind"] == "challenge"]
    assert ev["evidence"]["status"] == 403 and ev["evidence"]["server"] == "cloudflare"
    assert (
        ev["evidence"]["cf_ray"] == "0123456789abcdef-YYZ" and ev["evidence"]["cf_mitigated"] == "challenge"
    )
    assert ev["evidence"]["title"] == "Just a moment..." and len(ev["evidence"]["excerpt"]) <= 500
    raw = (data_dir / "events.jsonl").read_text()
    assert token not in raw and "cookie" not in raw.lower()


def test_evidence_redacts_tokens_in_page_text():
    res = FetchResult("u", 403, "u", "<title>t</title><p>ray " + "x" * 64 + " end</p>" + "y " * 400)
    ev = challenge.evidence(res)
    assert "x" * 64 not in ev["excerpt"] and "[redacted]" in ev["excerpt"] and len(ev["excerpt"]) <= 500


def test_cloudflare_beacon_on_a_real_200_page_is_not_a_challenge():
    """Regression (2026-11-07): Cloudflare's background beacon script sits on NORMAL pages."""
    html = page("list-with-cf-beacon.html")
    assert "/cdn-cgi/challenge-platform/scripts/jsd/main.js" in html
    res = FetchResult("u", 200, "u", html, {"server": "cloudflare"})
    assert challenge.detect(res, LIST_EXPECT) is None
    assert challenge.detect(res, None) is None  # robots/home: status and markers only


def test_true_managed_challenge_is_still_a_challenge():
    res = FetchResult(
        "u", 403, "u", page("managed-challenge.html"), {"server": "cloudflare", "cf-mitigated": "challenge"}
    )
    assert challenge.detect(res, LIST_EXPECT) == "http_403"
    ev = challenge.evidence(res)
    assert (ev["cf_mitigated"], ev["title"]) == ("challenge", "Just a moment...")


@pytest.mark.parametrize(
    ("status", "headers", "html", "expect", "reason"),
    [
        (200, {"cf-mitigated": "challenge"}, "list-with-cf-beacon.html", LIST_EXPECT, "cf_mitigated"),
        (200, {}, "managed-challenge.html", LIST_EXPECT, "challenge_title"),
        (200, {}, "challenge-cf.html", None, "challenge_title"),
        (418, {}, "list-with-cf-beacon.html", LIST_EXPECT, "http_418"),  # the beacon alone: never a marker
    ],
)
def test_marker_counts_only_when_corroborated(status, headers, html, expect, reason):
    assert challenge.detect(FetchResult("u", status, "u", page(html), headers), expect) == reason


def test_marker_on_a_page_missing_its_content_is_a_challenge():
    html = "<html><head><title>x</title></head><body><script src='/cdn-cgi/challenge-platform/x.js'></script></body></html>"
    assert challenge.detect(FetchResult("u", 200, "u", html), LIST_EXPECT) == "challenge_marker"


def test_runs_created_in_the_same_second_never_share_a_queue(data_dir):
    a = crawl.Run.new("ab", "http", "example.invalid")
    b = crawl.Run.new("ab", "http", "example.invalid")
    assert a.doc["run_id"] != b.doc["run_id"] and a.doc["queue"] != b.doc["queue"]


def test_recaptcha_contact_form_on_a_real_200_ad_page_is_not_a_challenge():
    html = page("ad-with-recaptcha-form.html")
    assert "recaptcha/api2/anchor" in html and 'id="description"' in html
    assert challenge.detect(FetchResult("u", 200, "u", html), ExampleSite.expect["ad"]) is None


@pytest.mark.parametrize(
    ("status", "headers", "expect", "reason"),
    [
        (403, {}, ExampleSite.expect["ad"], "http_403"),
        (418, {}, ExampleSite.expect["ad"], "captcha_iframe"),
        (200, {"cf-mitigated": "challenge"}, ExampleSite.expect["ad"], "cf_mitigated"),
        (200, {}, "#something-the-page-lacks", "captcha_iframe"),
    ],
)
def test_captcha_iframe_counts_only_when_corroborated(status, headers, expect, reason):
    html = page("ad-with-recaptcha-form.html")
    assert challenge.detect(FetchResult("u", status, "u", html, headers), expect) == reason
