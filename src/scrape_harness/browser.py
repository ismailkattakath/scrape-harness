"""The DEDICATED browser: the hash-pinned Playwright Chromium with its own persistent profile at
the STATE root ($<state_env>)/browser-profile (local, never synced) and no extensions. crawlee's PlaywrightCrawler launches it
headless for crawls (drivers/browser.py); `<cli> browser open` launches the SAME executable on the
SAME profile HEADED for a human handoff. It never touches the operator's daily browser.

PIPE ONLY (operator decision 2026-11-07): every launch goes through Playwright's own launch /
launch_persistent_context, which talks to Chromium over --remote-debugging-pipe. No path passes
a debugging-port flag and nothing attaches over a CDP PORT, so the browser opens no TCP port. (Request
blocking sends one CDP command through Playwright's own session, over that same pipe.)

Configuration: LI_CHROMIUM overrides the executable; PLAYWRIGHT_BROWSERS_PATH (set by the flake's
wrapper) points Playwright at the Nix-pinned build.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from . import pipeline, store

FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-sync",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
]


# Images, fonts and media (operator decision 2026-11-08): the browser never fetches these. NOT CSS (not
# approved), and not crawlee's default list (it adds .css, .pdf, .zip).
BLOCK_DEFAULT = (
    *("jpg", "jpeg", "png", "webp", "gif", "svg", "avif", "ico"),
    *("woff", "woff2", "ttf", "otf"),
    *("mp4", "webm", "mp3", "m3u8"),
)
# Playwright's request.failure for a request CDP blocked (measured, Chromium 147: "inspector", i.e.
# net::ERR_BLOCKED_BY_INSPECTOR). Matched loosely so a fuller error text counts the same.
BLOCKED_FAILURE = "inspector"
IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "svg", "avif", "ico", "bmp", "apng"})
IMAGE_MODES = ("answer", "block")
PLACEHOLDER_DEFAULT = (300, 200)  # a typical card image; its layout stands in for the real one


class BrowserError(RuntimeError):
    pass


def block_extensions() -> tuple[str, ...]:
    """pipeline.toml [crawl] block_extensions, or BLOCK_DEFAULT when the consumer sets none."""
    exts = pipeline.current().block_extensions
    return BLOCK_DEFAULT if exts is None else exts


def image_mode() -> str:
    """pipeline.toml [crawl] image_mode: "answer" (default) or "block".

    Why answer is the default (measured 2026-11-08, offline fixture, 80 lazy images, 2 loads): a BLOCKED
    image fires onerror and collapses to height 0 (page 15,874 -> 1,338 px), so every lazy image enters the
    viewport and a site's retry-on-error loader re-requests it: 400 attempts vs 160 when answered, and the
    A/B showed 913 attempted per page with block. An ANSWERED image (a local SVG placeholder) loads: no
    error, no retry, the real layout (16,058 px), 0 bytes sent, the HTTP cache kept. Fonts and media stay
    blocked: one attempt each per load in the same fixture (no cascade there)."""
    return pipeline.current().image_mode or "answer"


def placeholder_size() -> tuple[int, int]:
    size = pipeline.current().image_placeholder
    return tuple(size) if size else PLACEHOLDER_DEFAULT  # type: ignore[return-value]


def placeholder_svg(size: tuple[int, int]) -> str:
    """The answer body, base64: an empty SVG of the placeholder's size (66 bytes at 300x200)."""
    import base64

    w, h = size
    return base64.b64encode(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"/>'.encode()
    ).decode()


def blocking(variant) -> str:
    """The run card's Blocking row for a browser variant."""
    if not variant.blocks:
        return f"off (variant {variant.name})"
    from . import trackers

    exts = block_extensions()
    imgs = [e for e in exts if e in IMAGE_EXTS]
    rest = [e for e in exts if e not in IMAGE_EXTS]
    if not exts:
        head = "no extensions ([crawl] block_extensions = [])"
    elif image_mode() == "answer" and imgs:
        w, h = placeholder_size()
        head = f"images ({' '.join(imgs)}) answered locally with a {w}x{h} placeholder"
        head += f"; {' '.join(rest)} blocked" if rest else ""
    else:
        head = f"{len(exts)} extensions never fetched: {' '.join(exts)}"
    return f"{head}; third-party trackers blocked ({trackers.describe()}); per page blocked / answered / sent after the run"


def block_patterns(exts) -> list[dict]:
    """One anchored URLPattern per extension: the PATH ends in .<ext>, any scheme, host, port and query.

    Why not crawlee's context.block_requests (measured 2026-11-08 on an offline fixture, Chromium 147):
      - it sends CDP `urls`, which match as a SUBSTRING (with or without *): `.png` blocked theme.png.css
        and `.gif` blocked lib.gif.js;
      - on our persistent context page.context.browser is None, so it takes its non-Chromium branch,
        page.route, which disables the HTTP cache (CSS/JS were fetched twice over two loads, vs once).
    `urlPatterns` is the same Network.setBlockedURLs command (the cache stays on), with URLPattern
    syntax. `*:*` keeps any port; one grouped pattern (`*.:ext(a|b)`) does not parse in Chromium 147.
    Navigations (the page, an iframe document) are never blocked, whatever their URL ends in."""
    return [{"urlPattern": f"*://*:*/*.{e}", "block": True} for e in exts]


def _path_ext(url: str) -> str:
    from urllib.parse import urlsplit

    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


async def block(
    page, exts, on_answer=None, mode: str | None = None, trackers_site: str | None = None, on_tracker=None
) -> bool:
    """Keep `exts` off the network for this page (its own CDP session, over Playwright's pipe), and, when
    `trackers_site` is given, every THIRD-PARTY request EasyList/EasyPrivacy block (trackers.py). False, and
    nothing sent, when there is nothing to do: an empty `exts` means block NO extension (crawlee's means its
    default).

    image_mode "answer": an image request whose PATH ends in one of the image extensions is answered by
    the browser itself (Fetch.fulfillRequest, a placeholder SVG; `on_answer(url)` counts it), any other
    image continues untouched. Everything else in `exts` (fonts, media), and every image in "block" mode,
    goes to Network.setBlockedURLs as before. A tracker is failed in the SAME Fetch.requestPaused handler
    (Fetch.failRequest BlockedByClient; `on_tracker(url)` counts it), checked before the image answer."""
    mode = mode or image_mode()
    answer = {e for e in exts if e in IMAGE_EXTS} if mode == "answer" else set()
    blocked = [e for e in exts if e not in answer]
    if not (blocked or answer or trackers_site):
        return False
    if trackers_site:
        from . import trackers

        trackers.engine()  # fail loudly BEFORE the page loads when the pinned lists are missing
    cdp = await page.context.new_cdp_session(page)  # Chromium only: any other engine raises, loudly
    if blocked:
        await cdp.send("Network.enable")
        await cdp.send("Network.setBlockedURLs", {"urlPatterns": block_patterns(blocked)})
    if answer or trackers_site:
        import asyncio

        from . import trackers

        body = placeholder_svg(placeholder_size())
        headers = [
            {"name": "Content-Type", "value": "image/svg+xml"},
            {"name": "Cache-Control", "value": "no-store"},
        ]
        top = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]

        async def paused(ev: dict) -> None:
            rid, url, kind = ev["requestId"], ev["request"]["url"], ev.get("resourceType")
            try:
                top_document = kind == "Document" and ev.get("frameId") == top  # the page itself: never
                if trackers_site and not top_document:
                    # the requesting document (an iframe's own, via its Referer), else the page, else the site
                    source = ev["request"].get("headers", {}).get("Referer") or (
                        page.url if page.url.startswith("http") else f"https://{trackers_site}/"
                    )
                    if trackers.blocked(url, source, kind or "Other", trackers_site):
                        await cdp.send(
                            "Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"}
                        )
                        if on_tracker:
                            on_tracker(url)
                        return
                if kind == "Image" and _path_ext(url) in answer:
                    await cdp.send(
                        "Fetch.fulfillRequest",
                        {"requestId": rid, "responseCode": 200, "responseHeaders": headers, "body": body},
                    )
                    if on_answer:
                        on_answer(url)
                    return
            except Exception:  # noqa: BLE001 - never leave a request paused: let it continue instead
                pass
            with contextlib.suppress(Exception):  # the page closed meanwhile
                await cdp.send("Fetch.continueRequest", {"requestId": rid})

        async def auth(ev: dict) -> None:
            # With Fetch enabled here, a proxy's 407 comes to THIS session, not to Playwright's (measured:
            # net::ERR_INVALID_AUTH_CREDENTIALS without this). Answer it with the run's proxy credentials,
            # already in memory (proxy.credentials, read once through `secret exec`); any other challenge
            # gets the browser's default.
            from . import proxy

            px = proxy.config()
            resp = {"response": "Default"}
            if ev.get("authChallenge", {}).get("source") == "Proxy" and px:
                user, password = proxy.credentials(px)
                resp = {"response": "ProvideCredentials", "username": user, "password": password}
            with contextlib.suppress(Exception):  # the page closed meanwhile
                await cdp.send(
                    "Fetch.continueWithAuth", {"requestId": ev["requestId"], "authChallengeResponse": resp}
                )

        from . import proxy as _proxy

        # Images only (answer mode, no tracker check, unproxied): pause images only. Tracker blocking, or a
        # proxy, pauses EVERY request: trackers can be any resource type, and with a proxy ANY Fetch.enable on
        # this session takes the 407 away from Playwright, the challenge reaching us only for requests we
        # pause. The non-matching requests continue at once (measured, proxied: the page loads, 1 proxy
        # challenge; CSS/JS caching equal to a run without Fetch).
        proxied = _proxy.current() is not None
        cdp.on("Fetch.requestPaused", lambda ev: asyncio.ensure_future(paused(ev)))
        cdp.on("Fetch.authRequired", lambda ev: asyncio.ensure_future(auth(ev)))
        pattern = {"urlPattern": "*", "requestStage": "Request"}
        every = proxied or bool(trackers_site)
        await cdp.send(
            "Fetch.enable",
            {
                "patterns": [pattern if every else pattern | {"resourceType": "Image"}],
                "handleAuthRequests": proxied,
            },
        )
    return True


def profile_dir() -> Path:
    p = store.state_dir() / "browser-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def claim_profile() -> None:
    """Refuse to share the profile: Chromium's SingletonLock means another process holds it."""
    lock = profile_dir() / "SingletonLock"
    if lock.exists() or lock.is_symlink():
        raise BrowserError(
            f"the dedicated profile is in use (is `{pipeline.consumer().cli} browser open` still running?). Close it first."
        )


def launch_options() -> dict:
    """For crawlee's PlaywrightCrawler. No executable_path unless overridden: Playwright then launches
    the build under PLAYWRIGHT_BROWSERS_PATH, i.e. the SAME pinned binary `executable()` returns."""
    # channel "chromium" = the FULL pinned Chromium in new-headless mode. Without it Playwright looks
    # for the separate chrome-headless-shell build, which the pin deliberately leaves out.
    opts: dict = {"args": FLAGS, "channel": "chromium"}
    if pipeline.knob("CHROMIUM"):
        opts["executable_path"] = pipeline.knob("CHROMIUM")
    return opts


def executable() -> str:
    if pipeline.knob("CHROMIUM"):
        return pipeline.knob("CHROMIUM")
    from playwright.sync_api import sync_playwright  # the pinned build's own path, not a guess

    with sync_playwright() as p:
        return p.chromium.executable_path


def open_headed(url: str) -> int:
    """Same pinned Chromium, same persistent user data dir, HEADED, through Playwright's own
    launch (--remote-debugging-pipe: no TCP port). Blocks until the operator closes the window."""
    from playwright.sync_api import sync_playwright

    claim_profile()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir()), headless=False, **launch_options())
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        ctx.wait_for_event("close", timeout=0)  # the operator closing the window ends the handoff
    return 0
