"""The site's Source: a STUB to replace. It parses nothing yet, so every run finds no records.

Write parse_list/parse_ad against SYNTHETIC fixtures in tests/fixtures/ (invented values, .invalid URLs);
never paste a real page. The records must carry the harness's core floor (see scrape_harness.interfaces
.Source); this project's own fields go under `card` and `site` in schemas/.
"""

from __future__ import annotations

from scrape_harness.interfaces import ListPage, Region


class Site:
    site = "FILL-ME"  # = pipeline.toml [consumer].default_site
    parser = "site-ad@0"  # bump on every parse_ad change; `reparse` records it
    home_url = "https://www.template.invalid/"
    robots_url = "https://www.template.invalid/robots.txt"
    expect = {"list": "main", "ad": "main"}  # a selector a REAL page of each kind contains
    default_variant = {"list": "desktop", "ad": "desktop"}
    coverage_fields = ("card.title",)

    def region(self, name: str) -> Region:
        return Region(query_suffix=name)

    def list_url(self, subcategory: str, region: str, page: int) -> str:
        return f"https://www.template.invalid/{subcategory}/{region}?page={page}"

    def parse_list(self, html: str, *, url: str, subcategory: str, page: int, seen_at: str) -> ListPage:
        return ListPage(observations=[], page_count=None)

    def parse_ad(self, html: str, *, url: str, fetched_at: str, fetch_reason: str) -> dict:
        raise NotImplementedError("parse_ad: write it against a synthetic fixture first")
