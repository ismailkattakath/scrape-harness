"""Tracker blocking: Brave's adblock-rust engine (the `adblock` Python binding, nixpkgs) over EasyList +
EasyPrivacy, both pinned by a commit of easylist/easylist's gh-pages branch plus sha256 (flake.nix
`filterLists`). No hand-kept host list and no custom rule: what is blocked is what the maintained lists
say, and a refresh is a pin bump.

Only THIRD-PARTY requests are ever checked: a request to the crawled site (its host or a subdomain) is
never blocked, whatever a list says, and neither is the page's own navigation. The check runs inside
browser.block()'s one Fetch.requestPaused handler (one pausing client per page).

Measured 2026-11-08 offline (616 saved pages, these lists): 3.0 of 4.4 static third-party references per
page blocked (Google Tag Manager, Yandex Metrica script + pixel; the DoubleClick preconnect), fonts and
the Vimeo/YouTube embeds allowed, 0 of 56,640 first-party checks blocked, 11 us per check, 0.26 s build.

Configuration: SCRAPE_HARNESS_FILTER_LISTS names the directory holding easylist.txt and easyprivacy.txt
(the flake's wrapper, dev shell and test build set it to the pinned store path)."""

from __future__ import annotations

import functools
import os
from pathlib import Path
from urllib.parse import urlsplit

LISTS_ENV = "SCRAPE_HARNESS_FILTER_LISTS"
LISTS = ("easylist.txt", "easyprivacy.txt")

# CDP Network.ResourceType -> the request type adblock-rust understands (its names follow the WebExtensions
# webRequest ResourceType). An iframe document is a "subdocument"; the top document is never checked.
REQUEST_TYPE = {
    "Document": "subdocument",
    "Stylesheet": "stylesheet",
    "Image": "image",
    "Media": "media",
    "Font": "font",
    "Script": "script",
    "XHR": "xmlhttprequest",
    "Fetch": "xmlhttprequest",
    "Ping": "ping",
    "CSPViolationReport": "csp_report",
    "WebSocket": "websocket",
    "TextTrack": "media",
    "Manifest": "other",
    "EventSource": "other",
    "Prefetch": "other",
    "SignedExchange": "other",
    "Other": "other",
}


class TrackerListsMissing(RuntimeError):
    pass


def lists_dir() -> Path:
    d = os.environ.get(LISTS_ENV)
    if not d or not all((Path(d) / n).is_file() for n in LISTS):
        raise TrackerListsMissing(
            f"{LISTS_ENV} must name a directory holding {' and '.join(LISTS)} (the flake's wrapper sets it); "
            "refusing to crawl with tracker blocking silently off"
        )
    return Path(d)


@functools.cache
def engine():
    """The engine, built once per process from the pinned lists (0.26 s measured)."""
    import adblock

    fs = adblock.FilterSet()
    for name in LISTS:
        fs.add_filter_list((lists_dir() / name).read_text(encoding="utf-8"))
    return adblock.Engine(filter_set=fs, optimize=True)


def same_site(host: str, site: str) -> bool:
    host, site = host.lower().removeprefix("www."), site.lower().removeprefix("www.")
    return host == site or host.endswith("." + site)


def blocked(url: str, source_url: str, resource_type: str, site: str) -> bool:
    """Whether the lists block this request. A first-party request (the crawled site or a subdomain) is
    never blocked, nor a non-http(s) one; the engine sees only third-party requests."""
    if not url.startswith(("http://", "https://")):
        return False
    if same_site(urlsplit(url).hostname or "", site):
        return False
    return (
        engine()
        .check_network_urls(
            url=url, source_url=source_url, request_type=REQUEST_TYPE.get(resource_type, "other")
        )
        .matched
    )


def describe() -> str:
    """For the run card: which lists, at which pin."""
    pin = os.environ.get(f"{LISTS_ENV}_PIN", "")
    return "EasyList + EasyPrivacy" + (f" @ {pin[:8]}" if pin else "")
