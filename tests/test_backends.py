"""Backend wiring per variant, checked without launching a browser or opening a socket."""

import pytest

from scrape_harness import variants
from scrape_harness.drivers.browser import BrowserBackend


@pytest.fixture
def captured(monkeypatch, data_dir):
    seen = {}

    class FakeCrawler:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr("crawlee.crawlers.PlaywrightCrawler", FakeCrawler)
    return seen


@pytest.mark.parametrize(
    ("name", "fingerprint", "headers_pinned", "mobile"),
    [("desktop", "default", False, False), ("desktop-nofp", None, True, False), ("mobile", None, True, True)],
)
def test_browser_variants(captured, data_dir, name, fingerprint, headers_pinned, mobile):
    from crawlee.fingerprint_suite import DefaultFingerprintGenerator

    BrowserBackend(variant=name).crawler(max_request_retries=1)
    (plugin,) = captured["browser_pool"].plugins  # the variant lives in our pool's one plugin
    gen = plugin._fingerprint_generator
    assert isinstance(gen, DefaultFingerprintGenerator) if fingerprint else gen is None
    assert plugin._use_incognito_pages is False and plugin.browser_launch_options["headless"] is True
    assert plugin._user_data_dir == str(
        data_dir.parent / "state" / "browser-profile"
    )  # the dedicated profile
    assert plugin.browser_launch_options["channel"] == "chromium"
    assert "--disable-extensions" in plugin.browser_launch_options["args"]
    ctx = plugin.browser_new_context_options or {}
    assert (
        "extra_http_headers" in ctx
    ) is headers_pinned  # fingerprints OFF really means no generated headers
    assert ctx.get("is_mobile", False) is mobile


def test_every_variant_names_a_registered_backend():
    from scrape_harness.interfaces import available

    assert {v.backend for v in variants.VARIANTS.values()} <= set(available("drivers"))
    assert variants.get().name == "desktop"


def test_production_variant_is_fingerprinted_desktop_for_lists_and_ads(monkeypatch):
    """Operator decision 2026-11-07 (ADR-0003): desktop, fingerprints ON, for crawl-lists and fetch-ads;
    resume reuses the run's recorded variant."""
    from example_site.source import ExampleSite

    monkeypatch.delenv("EX_VARIANT", raising=False)
    assert ExampleSite.default_variant == {"list": "desktop", "ad": "desktop"}
    assert variants.get().name == "desktop" and variants.get().fingerprints is True
