"""Pipe-only browser (operator decision 2026-11-07): no TCP DevTools port on any path."""

import re
from pathlib import Path

from conftest import ROOT

from scrape_harness import browser
from scrape_harness.drivers.browser import BrowserBackend

PORT = re.compile(r"--remote-debugging-(port|address)")


def test_launch_options_never_open_a_port(monkeypatch):
    monkeypatch.delenv("EX_CHROMIUM", raising=False)
    opts = browser.launch_options()
    assert not any(PORT.search(a) for a in opts["args"])
    assert opts["channel"] == "chromium" and "executable_path" not in opts


def test_crawler_path_uses_the_same_pipe_launch(monkeypatch, data_dir):
    seen = {}
    monkeypatch.setattr("crawlee.crawlers.PlaywrightCrawler", lambda **kw: seen.update(kw))
    BrowserBackend(variant="desktop").crawler()
    (plugin,) = seen["browser_pool"].plugins
    assert not any(PORT.search(a) for a in plugin.browser_launch_options["args"])


def test_headed_handoff_is_playwright_persistent_launch(monkeypatch, data_dir):
    calls = {}

    class Ctx:
        pages = []

        def new_page(self):
            return type("P", (), {"goto": lambda self, u: calls.setdefault("url", u)})()

        def wait_for_event(self, name, timeout):
            calls["waited"] = (name, timeout)

    class Chromium:
        def launch_persistent_context(self, user_data_dir, **kw):
            calls.update(user_data_dir=user_data_dir, **kw)
            return Ctx()

    class PW:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: PW())
    assert browser.open_headed("https://www.example.invalid/") == 0
    assert (
        calls["user_data_dir"] == str(data_dir.parent / "state" / "browser-profile")
        and calls["headless"] is False
    )
    assert not any(PORT.search(a) for a in calls["args"]) and calls["waited"] == ("close", 0)


def test_no_source_file_opens_a_cdp_port_or_attaches():
    src = [p for p in (ROOT / "src").rglob("*.py")]
    hits = [
        p.name
        for p in src
        if re.search(r"remote-debugging-port|connect_over_cdp|DevToolsActivePort", p.read_text())
    ]
    assert hits == []
    assert Path(browser.__file__).exists()
