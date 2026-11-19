"""The plug-in seams. A site (Source) or a fetch backend (Driver) implements one of these and registers
it under the matching entry-point group (scrape_harness.sources / scrape_harness.drivers) or in code
with register(); the core loads it by name.

Records crossing these seams are the schema'd dicts (observation.v1 / listing.v1, shipped by the
consumer), validated by the caller, so an implementation cannot leak an untyped shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Protocol


@dataclass(frozen=True)
class Region:
    """Geocoding context for one listing region. Public geography only, never a personal point."""

    query_suffix: str  # appended to every phrase sent to a geocoder
    bounds: str | None = None  # "south,west|north,east" bias box; biases, never excludes
    city_queries: dict[str, str] = field(
        default_factory=dict
    )  # label -> query, where the label alone misleads


@dataclass
class ListPage:
    observations: list[dict]  # observation.v1, minus nothing: the source fills every field it knows
    page_count: int | None
    skipped: int | None = None  # optional: cards the parser saw but could not read (list snapshots record it)


@dataclass(frozen=True)
class FetchResult:
    """What one GET returned. The scheduler, not the driver, judges it (challenge detectors)."""

    url: str
    status: int
    final_url: str
    html: str
    headers: dict[str, str] = field(
        default_factory=dict
    )  # lower-cased names; evidence only, never stored whole


class Driver(Protocol):
    """A fetch BACKEND: builds a crawlee crawler and turns its crawling context into a FetchResult.
    It never judges a page: the scheduler (crawl.py) owns politeness, robots.txt and challenge
    detection, so impit HTTP, the dedicated browser and the offline fixture are interchangeable."""

    network: bool
    http_client: Any  # a crawlee HttpClient; also used for robots.txt

    def crawler(self, **common: Any) -> Any: ...

    async def result(self, ctx: Any) -> FetchResult: ...

    async def close(self) -> None: ...


class Source(Protocol):
    """A site. Its records must carry the core floor the harness reads:
    observation.v1: schema, ad_id, site, seen_at, ad_url, list{url, subcategory, page, page_count,
      position, sponsored, sample_weight}, card (the source's own shape), card_hash, bumped_at;
    listing.v1: schema, ad_id, fetched_at, fetch_reason, source{site, url}; the harness adds raw_ref and
      derived{method}. Everything else (content, location, time, site) is the source's own.
    The consumer ships both schemas (examples/example_site/schemas/ is a complete pair)."""

    site: str
    parser: str  # parse_ad version tag, recorded by `<cli> reparse` in derived.method
    home_url: str  # opened once per session (a tab on the origin), never an ad page
    robots_url: str
    expect: dict[str, str]  # page kind -> CSS selector that a real page of that kind must contain
    # optional: selectors of chrome on EVERY page (a hidden login modal...) the detector ignores
    # ignore_selectors: tuple[str, ...]
    # optional: selectors of the site's own "this ad is no longer active" page (a soft 404): gone, not a stop
    # gone_selectors: tuple[str, ...]
    # optional: selectors of the site's own "no results" template on a list page past the last one (a positive
    # END, ADR-0017 A.1); a 404/410 or 200 list page carrying it, with 0 cards at the URL asked for, ends the
    # list. Without it a past-end page is never an END (fail closed). Prove it on a saved past-end page.
    # end_selectors: tuple[str, ...]
    # optional: the list's order. "bump_desc" (newest bump first) lets a frontier walk stop early at a confirmed
    # anchor (ADR-0017 A.2); any other value, and the default "unordered", walks to a positive END or the signed
    # ceiling (it costs list GETs, never views). Declare bump_desc only behind an offline evidence test.
    # order: str
    default_variant: dict[str, str]  # page kind -> variant name (variants.py), i.e. the fetch backend
    coverage_fields: tuple[str, ...]  # dotted paths into observation.v1 the A/B report measures

    def region(self, name: str) -> Region: ...

    def list_url(self, subcategory: str, region: str, page: int) -> str: ...

    def parse_list(self, html: str, *, url: str, subcategory: str, page: int, seen_at: str) -> ListPage: ...

    def parse_ad(self, html: str, *, url: str, fetched_at: str, fetch_reason: str) -> dict: ...


GROUPS = ("sources", "drivers")  # the harness's own seams; a consumer may declare more ([plugins.<group>])


_REGISTERED: dict[str, dict[str, Any]] = {}


def register(group: str, name: str, cls: Any) -> None:
    """Register a plug-in in code (a consumer that is not an installed package, or a test). Entry points
    stay the standard way; a registration with the same name wins."""
    _REGISTERED.setdefault(group, {})[name] = cls


def available(group: str) -> list[str]:
    eps = {ep.name for ep in entry_points(group=f"scrape_harness.{group}")}
    declared: set[str] = set()
    try:
        from . import pipeline

        declared = set(pipeline.declared(group))
    except Exception:  # noqa: BLE001 - no pipeline: only installed/registered plug-ins
        pass
    return sorted(eps | declared | set(_REGISTERED.get(group, {})))


def plugin_class(group: str, name: str) -> Any:
    if name in _REGISTERED.get(group, {}):
        return _REGISTERED[group][name]
    try:
        from . import pipeline

        spec = pipeline.plugin_spec(group, name)
    except Exception:  # noqa: BLE001 - no pipeline: only installed/registered plug-ins
        spec = None
    if spec:
        return pipeline.resolve(spec)
    found = [ep for ep in entry_points(group=f"scrape_harness.{group}") if ep.name == name]
    if not found:
        raise SystemExit(
            f"no {group[:-1]} named {name!r}; available: {', '.join(available(group)) or 'none'}"
        )
    return found[0].load()


def load(group: str, name: str, **kwargs: Any) -> Any:
    return plugin_class(group, name)(**kwargs)
