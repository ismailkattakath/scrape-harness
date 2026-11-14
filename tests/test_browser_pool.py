"""One browser per run on the persistent profile, and a relaunch that waits for the profile.

crawlee's pool retires a browser after N pages and launches the next at once, while the retired one
still holds the profile: ProcessSingleton "profile already in use" (production run ...1b25bd: 8 launch
failures; offline with a forced retire after 2 pages: 12 failures and 4 of 6 pages lost). The real-browser
test forces that retire against a LOCAL server (127.0.0.1, throwaway STATE profile): nothing leaves the
machine."""

import asyncio
import functools
import http.server
import threading

import pytest
from conftest import ROOT

from scrape_harness import browser
from scrape_harness.drivers import browser as drv
from scrape_harness.drivers.browser import BrowserBackend

SITE = ROOT / "tests" / "blocking_site"


def test_a_run_never_retires_its_browser(data_dir):
    pool = BrowserBackend(variant="desktop").pool({})
    assert pool._retire_browser_after_page_count == drv.NEVER_RETIRE


def test_our_fingerprints_are_the_ones_playwright_crawler_builds(data_dir):
    """We build the generator ourselves (a pool and fingerprint_generator="default" cannot be combined):
    it must equal crawlee's own, so a crawlee upgrade that changes it fails here, not silently."""
    from crawlee.crawlers import PlaywrightCrawler

    theirs = PlaywrightCrawler(browser_type="chromium", fingerprint_generator="default", headless=True)
    (plugin,) = theirs._browser_pool.plugins
    ours = drv.fingerprints(True)
    assert type(ours) is type(plugin._fingerprint_generator)
    assert ours._options == plugin._fingerprint_generator._options
    assert drv.fingerprints(False) is None


def test_the_release_wait_returns_when_the_lock_goes_and_refuses_a_stuck_one(tmp_path):
    lock = tmp_path / "SingletonLock"
    lock.symlink_to("host-1234")  # Chromium's lock is a dangling symlink

    async def release_later():
        await asyncio.sleep(0.5)
        lock.unlink()

    async def both():
        await asyncio.gather(drv.wait_for_profile_release(tmp_path, timeout=5), release_later())

    asyncio.run(both())
    lock.symlink_to("host-1234")
    with pytest.raises(browser.BrowserError, match="still locked"):
        asyncio.run(drv.wait_for_profile_release(tmp_path, timeout=0.5))


@pytest.fixture
def site():
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    )
    srv.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_a_forced_retire_relaunches_on_the_same_profile_without_a_launch_failure(data_dir, site):
    from crawlee import ConcurrencySettings
    from crawlee.crawlers import PlaywrightCrawler
    from crawlee.storage_clients import MemoryStorageClient

    pool = BrowserBackend(variant="desktop").pool({}, retire_after=2)  # 5 pages: 3 browsers, 2 relaunches
    launches, errors, done = [], [], []
    pool.pre_launch_hook(lambda page_id, _plugin: asyncio.sleep(0, launches.append(page_id)))
    crawler = PlaywrightCrawler(
        browser_pool=pool,
        storage_client=MemoryStorageClient(),
        concurrency_settings=ConcurrencySettings(max_concurrency=1, desired_concurrency=1),
        max_request_retries=3,
    )

    @crawler.router.default_handler
    async def ok(ctx):
        done.append(ctx.request.url)

    @crawler.error_handler
    async def failed(ctx, err):
        errors.append(str(err)[:120])

    asyncio.run(crawler.run([f"{site}/ad/pic.jpg?{i}" for i in range(5)]))
    assert errors == [] and len(done) == 5 and len(launches) == 3
