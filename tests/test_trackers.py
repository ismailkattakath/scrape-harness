"""Tracker blocking (trackers.py): adblock-rust over the pinned EasyList + EasyPrivacy, checked inside
browser.block()'s one Fetch.requestPaused handler, third-party requests only.

The real-browser tests drive the pinned Chromium against a LOCAL server: `--host-resolver-rules` maps
every hostname to 127.0.0.1, so a tracker host's request reaches THIS server (nothing leaves the machine)
and the server's (host, path) log is the independent instrument for what was sent."""

import asyncio
import collections
import http.server
import re
import threading
from pathlib import Path

import pytest

from scrape_harness import browser, trackers
from scrape_harness.crawl import request_listener, sent, tracker_counter

SITE = "example.invalid"
PAGE_SRC = f"https://www.{SITE}/listings/widgets"

TRACKERS = {  # (url, CDP resource type): one or more per tracker family named in the requirement
    "gtm": ("https://www.googletagmanager.com/gtm.js?id=GTM-ABC123", "Script"),
    "ga": ("https://www.google-analytics.com/analytics.js", "Script"),
    "ga4-collect": ("https://www.google-analytics.com/g/collect?v=2&tid=G-X", "Ping"),
    "doubleclick-gpt": ("https://securepubads.g.doubleclick.net/tag/js/gpt.js", "Script"),
    "doubleclick-stats": ("https://stats.g.doubleclick.net/g/collect?v=2", "XHR"),
    "metrica-tag": ("https://mc.yandex.ru/metrika/tag.js", "Script"),
    "metrica-pixel": ("https://mc.yandex.com/watch/12345", "Image"),
}
# Paths EasyPrivacy blocks GENERICALLY (on any host): on the crawled site they must still load.
FIRST_PARTY = [
    (f"https://www.{SITE}/_lib/ga.js", "Script"),
    (f"https://www.{SITE}/__utm.js", "Script"),
    (f"https://cdn.{SITE}/acctag.js", "Script"),  # a subdomain is first-party too
    (f"https://{SITE}/listings/widgets?page=2", "Document"),
]


# ── the engine ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", TRACKERS)
def test_gtm_ga_doubleclick_and_metrica_are_blocked(name):
    url, kind = TRACKERS[name]
    assert trackers.blocked(url, PAGE_SRC, kind, SITE), name


@pytest.mark.parametrize("url, kind", FIRST_PARTY)
def test_first_party_is_never_blocked_even_where_a_list_would(url, kind):
    assert not trackers.blocked(url, PAGE_SRC, kind, SITE)
    # the guard is what saves these: the engine alone WOULD block the tracker-shaped paths
    if "page=2" not in url:
        assert trackers.engine().check_network_urls(url, PAGE_SRC, "script").matched


@pytest.mark.parametrize(
    "url, kind",
    [
        ("https://player.vimeo.com/video/76979871", "Document"),  # the embed iframe
        ("https://player.vimeo.com/api/player.js", "Script"),
        ("https://f.vimeocdn.com/p/4.30.0/js/player.js", "Script"),
    ],
)
def test_vimeo_embeds_are_allowed_with_no_custom_rule(url, kind):
    assert not trackers.blocked(url, PAGE_SRC, kind, SITE)


def test_missing_lists_refuse_loudly(monkeypatch, tmp_path):
    trackers.engine.cache_clear()
    try:
        monkeypatch.setenv(trackers.LISTS_ENV, str(tmp_path))  # an empty directory
        with pytest.raises(trackers.TrackerListsMissing):
            trackers.engine()
        monkeypatch.delenv(trackers.LISTS_ENV)
        with pytest.raises(trackers.TrackerListsMissing):
            trackers.engine()
    finally:
        trackers.engine.cache_clear()


# ── the pinned engine still understands the pinned lists ───────────────────────────────────────────

COSMETIC = re.compile(r"#[@?$%]?#|#@[$?]#")
# Rules the adblock 0.6.0 parser IGNORES in the lists at the pinned rev (measured 2026-11-08), by their
# option set (popup rules, ~2,970, are not pinned: a new window is never a request of a page load).
# The rest are pinned; the harness reads only `.matched`, and each degrades toward allowing:
#   rewrite            - "answer with a blank mp4" (abp-resource): ignored = the request is not rewritten;
#   method             - one rule, a POST-only block: ignored = that request is allowed;
#   csp / redirect-rule exceptions - one each; ignored = the CSP / redirect they cancel was never applied;
#   ~document,~subdocument - one rule's negated document types.
KNOWN_IGNORED = {
    ("domain", "rewrite"): 8,
    ("csp", "subdocument"): 1,
    ("document", "domain", "subdocument"): 1,
    ("method",): 1,
    ("domain", "redirect-rule", "script"): 1,
}


def ignored_rules() -> dict[tuple, int]:
    """Network rules of the pinned lists that the pinned parser does not keep (filter_exists is False),
    keyed by their sorted option names. $badfilter lines and the rules they cancel are dropped by design."""
    import adblock

    text = "".join((trackers.lists_dir() / n).read_text(encoding="utf-8") for n in trackers.LISTS)
    rules = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith(("!", "[")) and not COSMETIC.search(line)
    ]
    fs = adblock.FilterSet(debug=True)
    fs.add_filter_list(text)
    eng = adblock.Engine(filter_set=fs, optimize=False)
    cancelled = {r.replace("$badfilter", "").replace(",badfilter", "") for r in rules if "badfilter" in r}
    out: collections.Counter = collections.Counter()
    for r in rules:
        if "badfilter" in r or r in cancelled or eng.filter_exists(r):
            continue
        opts = r.rsplit("$", 1)[1].split(",") if "$" in r else []
        out[tuple(sorted({o.split("=")[0].lstrip("~") for o in opts}))] += 1
    return dict(out)


def test_a_list_bump_fails_while_the_lists_use_rules_the_pinned_parser_ignores():
    """GOES RED on a list bump that adds rules adblock 0.6.0 silently drops (a modifier it does not know),
    or changes the known ones: re-run the census, read each new rule, and either accept it here with the
    reason or bump the engine. A green bump with new unknown modifiers would block less than it claims."""
    got = ignored_rules()
    popup = {k: v for k, v in got.items() if "popup" in k}  # counts move with every bump: not pinned
    rest = {k: v for k, v in got.items() if "popup" not in k}
    assert popup, "no ignored $popup rules at all: the census found nothing, so it is measuring nothing"
    new = {k: v for k, v in rest.items() if k not in KNOWN_IGNORED}
    assert not new, f"rules the pinned parser ignores, new at this list rev: {new}"
    assert rest == KNOWN_IGNORED


def test_perturbation_the_census_sees_an_unknown_modifier(monkeypatch, tmp_path):
    """The census is an instrument that can disagree: one rule with a modifier 0.6.0 does not parse, added
    to a copy of the lists, shows up as new."""
    for n in trackers.LISTS:
        (tmp_path / n).write_text((trackers.lists_dir() / n).read_text(encoding="utf-8"))
    with (tmp_path / "easyprivacy.txt").open("a") as f:
        f.write("\n||tracker.example^$removeparam=utm_source\n||tracker.example^$urlskip=?u\n")
    monkeypatch.setenv(trackers.LISTS_ENV, str(tmp_path))
    got = ignored_rules()
    assert got.get(("removeparam",)) == 1 and got.get(("urlskip",)) == 1


# ── in the browser: the one Fetch handler ──────────────────────────────────────────────────────────


@pytest.fixture
def server():
    hits: collections.Counter = collections.Counter()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            hits[(host, self.path)] += 1
            if self.path.startswith("/t/page.html"):
                body, ctype = PAGE.replace("{P}", str(self.server.server_port)).encode(), "text/html"
            elif self.path.endswith(".css") or "/css" in self.path:
                body, ctype = b"body{}", "text/css"
            elif "/video/" in self.path:
                body, ctype = b"<!doctype html><title>player</title>", "text/html"
            else:
                body, ctype = b"/* ok */", "application/javascript"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "page.html" not in self.path:
                self.send_header("Cache-Control", "max-age=3600")  # cacheable: a second load may reuse it
            self.end_headers()
            self.wfile.write(body)

    class Server(http.server.ThreadingHTTPServer):
        # every host maps to this one server, so a page load opens many connections at once; the default
        # listen backlog (5) drops some (measured: one tracker request lost in the nix build's run)
        request_queue_size = 128

    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port, hits
    srv.shutdown()


PAGE = """<!doctype html><html><head><title>Widgets | Example</title>
<script src="/t/app.js"></script>
<script src="/_lib/ga.js"></script>
<link rel="stylesheet" href="http://fonts.googleapis.com:{P}/css?family=X">
<script src="http://www.googletagmanager.com:{P}/gtm.js?id=GTM-ABC123"></script>
<script src="http://www.google-analytics.com:{P}/analytics.js"></script>
<script src="http://securepubads.g.doubleclick.net:{P}/tag/js/gpt.js"></script>
<script src="http://mc.yandex.ru:{P}/metrika/tag.js"></script>
</head><body><h1>Widgets</h1>
<img src="http://mc.yandex.com:{P}/watch/12345" width="1" height="1">
<iframe src="http://player.vimeo.com:{P}/video/76979871"></iframe>
<script>fetch("http://stats.g.doubleclick.net:{P}/g/collect?v=2").catch(() => 0)</script>
</body></html>"""
TRACKER_HITS = {
    ("www.googletagmanager.com", "/gtm.js?id=GTM-ABC123"),
    ("www.google-analytics.com", "/analytics.js"),
    ("securepubads.g.doubleclick.net", "/tag/js/gpt.js"),
    ("mc.yandex.ru", "/metrika/tag.js"),
    ("mc.yandex.com", "/watch/12345"),
    ("stats.g.doubleclick.net", "/g/collect?v=2"),
}
ALLOWED_HITS = {
    (f"www.{SITE}", "/t/page.html"),
    (f"www.{SITE}", "/t/app.js"),
    (f"www.{SITE}", "/_lib/ga.js"),  # first-party, though EasyPrivacy lists the path
    ("fonts.googleapis.com", "/css?family=X"),
    ("player.vimeo.com", "/video/76979871"),
}


async def load(port: int, tmp_path: Path, with_trackers: bool, loads: int = 1):
    from playwright.async_api import async_playwright

    counts: collections.Counter = collections.Counter()
    opts = browser.launch_options()
    opts["args"] = [*opts["args"], "--host-resolver-rules=MAP * 127.0.0.1"]  # every host is THIS server
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(tmp_path / "profile"), headless=True, **opts)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("request", request_listener(counts, SITE, page.main_frame))
        await browser.block(
            page, (), trackers_site=SITE if with_trackers else None, on_tracker=tracker_counter(counts)
        )
        for _ in range(loads):
            await page.goto(f"http://www.{SITE}:{port}/t/page.html", wait_until="load")
            await page.wait_for_timeout(800)
        await ctx.close()
    return counts


def test_the_browser_blocks_trackers_in_the_fetch_handler_and_loads_everything_else(server, tmp_path):
    port, hits = server
    counts = asyncio.run(load(port, tmp_path, with_trackers=True))
    seen = {k for k in hits if k[1] != "/favicon.ico"}
    assert not (seen & TRACKER_HITS), seen & TRACKER_HITS  # no tracker request reached the server
    assert seen >= ALLOWED_HITS  # first-party (incl. the list-matching path), fonts and Vimeo all loaded
    assert counts["trackers_blocked"] == len(TRACKER_HITS)
    # `sent` matches the server: third-party requests that arrived = attempted - blocked
    third_sent = sum(v for (h, _), v in hits.items() if not h.endswith(SITE))
    assert sent(dict(counts))["third_party"] == third_sent


def test_pausing_every_request_keeps_the_http_cache(server, tmp_path):
    """Tracker blocking pauses EVERY request (not only images): two loads still fetch each cacheable
    first-party and allowed third-party resource once."""
    port, hits = server
    asyncio.run(load(port, tmp_path, with_trackers=True, loads=2))
    assert hits[(f"www.{SITE}", "/t/page.html")] == 2
    for k in [
        (f"www.{SITE}", "/t/app.js"),
        (f"www.{SITE}", "/_lib/ga.js"),
        ("fonts.googleapis.com", "/css?family=X"),
    ]:
        assert hits[k] == 1, (k, hits[k])


def test_perturbation_with_trackers_off_the_same_page_sends_them(server, tmp_path):
    """The test above holds only because the engine runs: off, the same page sends every tracker request."""
    port, hits = server
    counts = asyncio.run(load(port, tmp_path, with_trackers=False))
    assert {k for k in hits} >= TRACKER_HITS
    assert counts.get("trackers_blocked", 0) == 0


def test_perturbation_an_engine_that_matches_nothing_lets_trackers_through(server, tmp_path, monkeypatch):
    """The engine itself is the decider: with its verdict disabled the handler blocks nothing."""
    port, hits = server
    monkeypatch.setattr(trackers, "blocked", lambda *a, **k: False)
    asyncio.run(load(port, tmp_path, with_trackers=True))
    assert {k for k in hits} >= TRACKER_HITS
