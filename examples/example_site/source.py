"""ExampleSite: the Source for the synthetic `example.invalid` classifieds site (widgets for sale).

The markup is invented for this example:
  list page  <main id="results"> <article class="result" data-id="N" [data-featured]> with
             <a class="result__link"> <h2 class="result__title"> <span class="result__city"> and
             <span class="result__price">; a <nav class="pager"> of ?page=N links.
  ad page    <h1>, <div id="description">, <span id="city">, <time class="posted" datetime=...>,
             <dl class="facts"> of <dt>/<dd> rows, <ul class="photos"> of <img src>, and an optional
             <a id="seller-site">; schema.org JSON-LD / microdata are read by the generic schemaorg module.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from scrape_harness import schemaorg
from scrape_harness.interfaces import ListPage, Region

SITE = "example.invalid"
ORIGIN = f"https://www.{SITE}"
IMG = f"https://img.{SITE}/"
AD_ID = re.compile(r"-(\d+)/?$")

# One invented region; its bounding box is open ocean, so it can never bias a real place.
REGIONS = {"sample-region": Region(query_suffix="Sample Region", bounds="10.00,-30.00|11.00,-29.00")}


def squash(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text()).strip() if node else ""


def ad_id(url: str) -> str | None:
    m = AD_ID.search(urlsplit(url).path)
    return m.group(1) if m else None


def price(text: str) -> int | None:
    return int(text) if re.fullmatch(r"\d+", text) else None


def card_hash(title: str | None, card: dict) -> str:
    canon = json.dumps({"title": title, **card}, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()[:16]


def page_count(doc: BeautifulSoup) -> int | None:
    pages = [
        int(m.group(1))
        for a in doc.select('nav.pager a[href*="page="]')
        if (m := re.search(r"[?&]page=(\d+)", a.get("href", "")))
    ]
    return max(pages) if pages else None


def facts(doc: BeautifulSoup) -> dict[str, str]:
    """<dl class="facts">: each <dt> label with the <dd> after it, lower-cased labels, empty rows skipped."""
    out = {}
    for dt in doc.select("dl.facts dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None and squash(dt) and squash(dd):
            out[squash(dt).lower()] = squash(dd)
    return out


class ExampleSite:
    site = SITE
    # Bump when parse_ad extracts anything new or differently; `reparse` tags versions with it.
    parser = "example-ad@1"
    home_url = f"{ORIGIN}/"
    robots_url = f"{ORIGIN}/robots.txt"
    # What a REAL page of each kind contains; its absence makes the scheduler stop (challenge.py).
    expect = {"list": "#results", "ad": "#description"}
    default_variant = {"list": "desktop", "ad": "desktop"}
    # Card fields the A/B report measures coverage of (dotted paths into observation.v1).
    coverage_fields = ("card.title", "card.city_label", "card.example.price", "card.example.featured")

    def region(self, name: str) -> Region:
        if name not in REGIONS:
            raise SystemExit(f"{SITE}: no region config for {name!r}; known: {', '.join(REGIONS)}")
        return REGIONS[name]

    def list_url(self, subcategory: str, region: str, page: int) -> str:
        return f"{ORIGIN}/listings/{subcategory}/{region}?page={page}"

    def parse_list(self, html: str, *, url: str, subcategory: str, page: int, seen_at: str) -> ListPage:
        doc = BeautifulSoup(html, "html.parser")
        out = []
        for pos, card in enumerate(doc.select("#results article.result"), 1):
            link = card.select_one("a.result__link[href]")
            href = urljoin(ORIGIN, link.get("href", "")) if link else ""
            aid = ad_id(href)
            if not aid:
                continue
            title = squash(card.select_one(".result__title")) or None
            fields = {
                "city_label": squash(card.select_one(".result__city")) or None,
                "example": {
                    "price": price(squash(card.select_one(".result__price"))),
                    "featured": card.has_attr("data-featured"),
                },
            }
            out.append(
                {
                    "schema": "observation.v1",
                    "ad_id": aid,
                    "site": self.site,
                    "seen_at": seen_at,
                    "ad_url": href,
                    "list": {
                        "url": url,
                        "subcategory": subcategory,
                        "page": page,
                        "page_count": None,
                        "position": pos,
                        "sponsored": card.select_one(".result__badge--sponsored") is not None,
                        "sample_weight": None,
                    },
                    "card": {"title": title, **fields},
                    "bumped_at": None,  # list pages carry no absolute time
                    "card_hash": card_hash(title, fields),
                }
            )
        return ListPage(observations=out, page_count=page_count(doc))

    def parse_ad(self, html: str, *, url: str, fetched_at: str, fetch_reason: str) -> dict:
        doc = BeautifulSoup(html, "html.parser")
        parts = [p for p in urlsplit(url).path.split("/") if p]
        sdo = schemaorg.extract(html, url)
        pins = schemaorg.pins(sdo)
        photos = list(
            dict.fromkeys(
                img.get("src", "")
                for img in doc.select("ul.photos img[src]")
                if img.get("src", "").startswith(IMG)
            )
        )
        posted = doc.select_one("time.posted[datetime]")
        seller = doc.select_one("a#seller-site[href]")
        return {
            "schema": "listing.v1",
            "ad_id": ad_id(url) or "",
            "fetched_at": fetched_at,
            "fetch_reason": fetch_reason,
            "source": {
                "site": self.site,
                "url": url,
                "category": parts[0] if len(parts) > 0 else None,
                "subcategory": parts[1] if len(parts) > 1 else None,
                "region": parts[2] if len(parts) > 2 else None,
            },
            "content": {
                "title": squash(doc.select_one("h1")),
                "description": squash(doc.select_one("#description")),
                "contact": {"website": seller.get("href") if seller else None},
                "media_urls": photos,
            },
            "time": {
                "first_published": schemaorg.date_published(sdo),
                "bumped_at": posted.get("datetime") if posted else None,
            },
            "location": {
                "city_label": squash(doc.select_one("#city")) or None,
                "pin": {**pins[0], "approximate": True} if pins else None,
            },
            "site": {"example": {"facts": facts(doc), "page_schema_type": schemaorg.main_type(sdo)}},
        }
