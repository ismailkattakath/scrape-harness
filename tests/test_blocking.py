"""Request blocking (browser.block, [crawl] block_extensions) and what the counters say about it.

The real-browser tests drive the pinned Chromium against a LOCAL server (127.0.0.1, synthetic fixture,
throwaway profile): nothing leaves the machine. The server's own hit log is the independent instrument:
what the counters call `sent` must be exactly what the server received.

Fixture page tests/blocking_site/ad/page2.html: theme.png.css + lib.gif.js (a blocked extension as a
SUBSTRING of a CSS/JS name), and an iframe document gif-lover.gif.html that loads s.css, app.js (whose
fetch sends data.json) and 6 assets that must be blocked (jpg, webp?query, gif, mp4, woff2 from the CSS,
png from the CSS), plus photo?fmt=.jpg (the extension only in the QUERY: not blocked).
"""

import asyncio
import collections
import functools
import http.server
import threading

import pytest
from conftest import ROOT

from scrape_harness import browser, contracts, pipeline
from scrape_harness.crawl import answered_counter, blocked_listener, request_listener, sent

SITE = ROOT / "tests" / "blocking_site"  # not under fixtures/: the fixture driver reads every file there
BLOCKED = {"/a/p.jpg", "/a/q.webp?v=1", "/a/x.gif", "/a/v.mp4", "/a/f.woff2", "/a/bg.png"}
NEVER = {"/a/theme.png.css", "/a/lib.gif.js", "/a/s.css", "/a/app.js", "/a/data.json", "/a/photo?fmt=.jpg"}
FRAMES = {"/ad/page2.html", "/ad/gif-lover.gif.html"}


@pytest.fixture
def server():
    hits = collections.Counter()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "max-age=3600")  # cacheable: a second load may reuse it
            super().end_headers()

        def do_GET(self):
            hits[self.path] += 1
            super().do_GET()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(SITE)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", hits
    srv.shutdown()


async def load(base, exts, loads, tmp_path, mode="block", path="/ad/page2.html"):
    from playwright.async_api import async_playwright

    counts = collections.Counter()
    failed: list[str] = []
    probes: list = []
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, **browser.launch_options()
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        # the same wiring as crawl's pre-navigation hook
        page.on("request", request_listener(counts, "127.0.0.1", page.main_frame))
        if await browser.block(page, exts, answered_counter(counts, "127.0.0.1"), mode=mode):
            page.on("requestfailed", blocked_listener(counts, "127.0.0.1", page.main_frame))
        page.on("requestfailed", lambda r: failed.append(r.url.removeprefix(base)))
        for _ in range(loads):
            await page.goto(base + path, wait_until="load")
            await page.wait_for_timeout(800)
            probes.append(await page.evaluate("window.__probe ? window.__probe() : null"))
        await ctx.close()
    if path == "/ad/page2.html":
        return counts, set(failed)
    return counts, set(failed), probes


def test_the_default_list_blocks_exactly_images_fonts_media_and_sent_matches_the_server(server, tmp_path):
    base, hits = server
    counts, failed = asyncio.run(load(base, browser.BLOCK_DEFAULT, 1, tmp_path))
    got = {k for k in hits if k != "/favicon.ico"}  # the browser's own favicon fetch is not a page request
    assert failed == BLOCKED  # exactly these, nothing more
    assert not (got & BLOCKED)  # and the server never saw one of them
    assert got >= NEVER | FRAMES  # the page, the iframe, CSS, JS, XHR and a query-only ".jpg" all loaded
    assert counts["site_subresources_blocked"] == len(BLOCKED)
    # the counters' `sent` is exactly what reached the server, besides the page GET itself
    assert sent(dict(counts))["site_subresources"] == sum(hits[k] for k in got) - 1
    assert sent(dict(counts))["third_party"] == 0


def test_blocking_keeps_the_http_cache(server, tmp_path):
    """CDP blocking leaves the cache on: two loads fetch each CSS/JS once (page.route fetched them twice)."""
    base, hits = server
    asyncio.run(load(base, browser.BLOCK_DEFAULT, 2, tmp_path, mode="block"))
    assert {k: hits[k] for k in ("/a/s.css", "/a/app.js", "/a/theme.png.css")} == dict.fromkeys(
        ("/a/s.css", "/a/app.js", "/a/theme.png.css"), 1
    )


def test_an_empty_list_blocks_nothing(server, tmp_path):
    """crawlee treats [] as ITS default list (which blocks .css); ours sends no block at all."""
    base, hits = server
    counts, failed = asyncio.run(load(base, (), 1, tmp_path))
    assert not failed and set(hits) >= BLOCKED and "site_subresources_blocked" not in counts


def test_patterns_are_anchored_one_per_extension():
    assert browser.block_patterns(("png", "woff2")) == [
        {"urlPattern": "*://*:*/*.png", "block": True},
        {"urlPattern": "*://*:*/*.woff2", "block": True},
    ]


def test_sent_subtracts_blocked_and_reads_old_records():
    assert sent({"site_subresources": 90, "site_subresources_blocked": 70, "third_party": 5}) == {
        "site_subresources": 20,
        "third_party": 5,
    }
    assert sent({"list_pages": 1}) == {"site_subresources": 0, "third_party": 0}  # a record from before


def test_the_knob_defaults_and_refuses_css(monkeypatch):
    assert browser.block_extensions() == browser.BLOCK_DEFAULT  # the example pipeline sets none
    base = {"crawl": {"block_extensions": ["jpg"]}}
    assert not [e for e in contracts.errors("pipeline.v1", base) if e.startswith("crawl")]
    for bad in ("css", "js", "html", ".jpg", "JPG"):
        errs = contracts.errors("pipeline.v1", {"crawl": {"block_extensions": [bad]}})
        assert any(e.startswith("crawl/block_extensions/0") for e in errs), bad
    monkeypatch.setattr(
        pipeline, "current", lambda: type("P", (), {"block_extensions": ("png",)})()
    )  # a consumer that set its own list
    assert browser.block_extensions() == ("png",)


def test_desktop_unblocked_is_desktop_without_blocking_and_an_explicit_ab_arm(data_dir):
    from dataclasses import replace

    from scrape_harness import variants

    d, u = variants.get("desktop"), variants.get("desktop-unblocked")
    assert replace(u, name="desktop", blocks=True, in_default_ab=True) == d  # otherwise identical
    assert browser.blocking(u) == "off (variant desktop-unblocked)"
    assert browser.blocking(d).startswith(
        "images (jpg jpeg png webp gif svg avif ico) answered locally with a 300x200"
    )


def test_the_run_record_says_per_page_blocked_and_sent():
    from scrape_harness.crawl import per_page

    assert per_page(
        {
            "ad_pages": 10,
            "site_subresources": 900,
            "site_subresources_blocked": 300,
            "site_subresources_answered": 400,
        }
    ) == {"pages": 10, "blocked": 30.0, "answered": 40.0, "sent": 20.0}
    assert per_page({"ad_pages": 10, "site_subresources": 900}) is None  # no blocking ran: no claim


# ── image_mode "answer" (the default): the cascade a BLOCKED lazy page starts, and its absence ─────────
LAZY = 20  # tests/blocking_site/lazy: 20 native loading="lazy" images + 20 IntersectionObserver ones
IMAGES = {f"/lazy/img/{k}{i}.png" for k in "nd" for i in range(LAZY)}


def lazy(base, hits, tmp_path, mode, loads=1):
    counts, failed, probes = asyncio.run(
        load(base, browser.BLOCK_DEFAULT, loads, tmp_path, mode=mode, path="/lazy/index.html")
    )
    return counts, failed, probes, {k for k in hits if k.split("?")[0] in IMAGES}


def test_answered_images_load_without_retries_keep_the_layout_and_never_reach_the_server(server, tmp_path):
    base, hits = server
    counts, failed, (probe,), image_hits = lazy(base, hits, tmp_path / "a", "answer")
    seen = {k: v for k, v in hits.items() if k != "/favicon.ico"}  # this load only, before the next one
    assert probe["errored"] == 0 and not failed  # no onerror, so no retry
    assert (
        counts["site_subresources_answered"] == 2 * LAZY and not image_hits
    )  # each answered once, none sent
    # the counters reconcile with the server's own log: sent == what it received, besides the page GET
    assert sent(dict(counts))["site_subresources"] == sum(seen.values()) - 1
    # the placeholder keeps the cards' layout: 40 images x the 300x200 placeholder's 200 px, plus the header
    # (measured against an unblocked load once: 16,058 vs 15,874 px; that reference is timing-dependent,
    # since native-lazy images far below the viewport may not have loaded yet, so the analytic value is used)
    assert 2 * LAZY * 200 <= probe["height"] <= 2 * LAZY * 200 * 1.05


def test_blocked_images_cascade_on_the_same_page(server, tmp_path):
    """The reason answer is the default: blocked, every IntersectionObserver image errors and is retried
    twice by the page's own loader, so attempts exceed the 40 images; still nothing reaches the server."""
    base, hits = server
    counts, failed, (probe,), image_hits = lazy(base, hits, tmp_path, "block")
    assert probe["errored"] == 2 * LAZY and not image_hits
    # measured (20+20 and 40+40 fixtures alike): Chromium itself attempts a blocked native-lazy image
    # twice; the page's loader tries each observed one 1 + 2 retries
    assert counts["site_subresources_blocked"] == 2 * LAZY + 3 * LAZY
    assert probe["height"] < 0.2 * 200 * 2 * LAZY  # collapsed: every lazy image entered the viewport


def test_answering_keeps_the_http_cache(server, tmp_path):
    base, hits = server
    lazy(base, hits, tmp_path, "answer", loads=2)
    assert hits["/lazy/img/s.css"] == 1  # the CSS was fetched once over two loads


def test_image_mode_defaults_to_answer_and_the_schema_bounds_it(monkeypatch):
    import dataclasses

    assert browser.image_mode() == "answer" and browser.placeholder_size() == (300, 200)
    for ok in ({"image_mode": "block"}, {"image_mode": "answer", "image_placeholder": [320, 240]}):
        assert not [e for e in contracts.errors("pipeline.v1", {"crawl": ok}) if e.startswith("crawl")], ok
    for bad in ({"image_mode": "fake"}, {"image_placeholder": [0, 10]}, {"image_placeholder": [300]}):
        assert [e for e in contracts.errors("pipeline.v1", {"crawl": bad}) if e.startswith("crawl")], bad
    real = pipeline.current()
    monkeypatch.setattr(pipeline, "current", lambda: dataclasses.replace(real, image_mode="block"))
    assert browser.image_mode() == "block"
    assert browser.blocking(__import__("scrape_harness.variants").variants.get("desktop")).startswith(
        "16 extensions"
    )
