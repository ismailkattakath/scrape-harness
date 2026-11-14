"""Browser backend: crawlee's PlaywrightCrawler launching the hash-pinned Chromium into the
DEDICATED persistent profile (operator decision, ADR-0003).

  - executable: the Nix-pinned Playwright Chromium (Playwright resolves it from
    PLAYWRIGHT_BROWSERS_PATH, which the flake points at the pin; LI_CHROMIUM overrides);
  - user_data_dir: the STATE root ($<state_env>)/browser-profile, persistent (use_incognito_pages=False), so
    the cookies of a human handoff in `<cli> browser open` carry over;
  - headless unless the operator opens it; no extensions;
  - fingerprint_generator: crawlee's default (ON) for the `desktop` variant, OFF (None) otherwise.
    crawlee applies the fingerprint to the persistent context (AsyncNewContext -> the persistent
    browser's new_context -> launch_persistent_context), so persistence and fingerprints combine.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import timedelta
from pathlib import Path

from .. import browser, proxy, variants
from ..interfaces import FetchResult

# crawlee's pool RETIRES a browser after 100 pages and launches the next one at once, but closes the
# retired one only on its 30 s idle sweep. On ONE persistent profile the new launch then dies with
# ProcessSingleton "profile already in use" until the sweep runs (measured: run 2026-11-08 ...1b25bd, 8
# launch failures in pairs every ~14.5 min = 100 pages; an offline fixture with a forced retire after 2
# pages: 12 failures, 4 of 6 pages lost). Retiring also regenerates the fingerprint (the context, so the
# fingerprint, is per browser) on the same cookies. So: ONE browser per run, never retired, and any other
# relaunch (a crash) waits for the old process to release the profile.
NEVER_RETIRE = sys.maxsize
RELEASE_WAIT_S = 45.0  # below crawlee's 60 s launch timeout (BrowserPool operation_timeout), above any sweep


async def wait_for_profile_release(profile: str | Path, timeout: float = RELEASE_WAIT_S) -> None:
    """Return once no Chromium holds `profile` (its SingletonLock is gone); raise BrowserError after
    `timeout`. A pre-launch hook: a relaunch never races the process it replaces."""
    lock = Path(profile) / "SingletonLock"
    deadline = time.monotonic() + timeout
    while lock.exists() or lock.is_symlink():
        if time.monotonic() >= deadline:
            raise browser.BrowserError(f"the browser profile is still locked after {timeout:.0f} s: {lock}")
        await asyncio.sleep(0.25)


def fingerprints(on: bool):
    """The generator PlaywrightCrawler builds for fingerprint_generator="default" and browser_type
    "chromium" (crawlee 1.11.1, _playwright_crawler.py), built here because we pass our own pool, and a
    pool and that string cannot be combined. tests/test_browser_pool.py asserts the two are equal."""
    if not on:
        return None
    from crawlee.fingerprint_suite import DefaultFingerprintGenerator, HeaderGeneratorOptions

    return DefaultFingerprintGenerator(header_options=HeaderGeneratorOptions(browsers=["chrome"]))


class BrowserBackend:
    network = True

    def __init__(self, variant: str | None = None, **_: object) -> None:
        from crawlee.http_clients import ImpitHttpClient

        self.variant = variants.get(variant)
        self.http_client = ImpitHttpClient()  # robots.txt only; pages go through the browser

    def crawler(self, **common):
        from crawlee.crawlers import PlaywrightCrawler

        v = self.variant
        # Fingerprints OFF must mean OFF: crawlee's controller otherwise injects generated headers
        # (a Windows-Chrome UA, measured 2026-11-07) even with fingerprint_generator=None. An explicit
        # extra_http_headers wins over the generator (`filtered_options.get('extra_http_headers', ...)`).
        context: dict = {} if v.fingerprints else {"extra_http_headers": {}}
        if v.mobile:
            context |= {
                "viewport": {"width": v.viewport[0], "height": v.viewport[1]},
                "is_mobile": True,
                "has_touch": True,
            }
            if v.user_agent:
                context["user_agent"] = v.user_agent
        browser.claim_profile()
        return PlaywrightCrawler(browser_pool=self.pool(context), http_client=self.http_client, **common)

    def pool(self, context: dict, retire_after: int = NEVER_RETIRE):
        from crawlee.browsers import BrowserPool

        profile = browser.profile_dir()
        launch = browser.launch_options()
        if px := proxy.current():  # proxied: Chromium resolves no hostname itself (proxy.no_local_dns_flag)
            launch = launch | {"args": [*launch["args"], proxy.no_local_dns_flag(px)]}
        pool = BrowserPool.with_default_plugin(
            browser_type="chromium",
            user_data_dir=str(profile),
            use_incognito_pages=False,
            headless=True,
            browser_launch_options=launch,
            browser_new_context_options=context or None,
            fingerprint_generator=fingerprints(self.variant.fingerprints),
            retire_browser_after_page_count=retire_after,
            # the sweep that closes a retired or idle browser (crawlee: every 30 s); a relaunch waits for it
            close_inactive_browsers_interval=timedelta(seconds=5),
        )

        @pool.pre_launch_hook
        async def _wait(_page_id, _plugin) -> None:
            await wait_for_profile_release(profile)

        return pool

    async def result(self, ctx) -> FetchResult:
        url = ctx.request.url
        status = ctx.response.status if ctx.response else 0
        headers = dict(ctx.response.headers) if ctx.response else {}
        return FetchResult(url, status, ctx.page.url, await ctx.page.content(), headers)

    async def close(self) -> None:
        pass
