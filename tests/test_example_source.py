"""The example consumer's Source parses its own synthetic markup into records that validate."""

from conftest import FIXTURES, LIST
from example_site.source import ExampleSite

from scrape_harness import contracts


def parse_list(name, page):
    return ExampleSite().parse_list(
        (FIXTURES / name).read_text(),
        url=f"{LIST}?page={page}",
        subcategory="widgets",
        page=page,
        seen_at="2026-01-15T11:58:00Z",
    )


def test_list_page():
    lp = parse_list("list-p1.html", 1)
    assert lp.page_count == 3
    assert [o["ad_id"] for o in lp.observations] == ["1000001", "1000002", "1000009"]
    one, two, promo = lp.observations
    assert one["card"] == {
        "title": "Blue widget",
        "city_label": "Sampletown",
        "example": {"price": 12, "featured": True},
    }
    assert two["card"]["example"] == {"price": None, "featured": False}  # "n/a" is not a price
    assert promo["list"]["sponsored"] and not one["list"]["sponsored"]
    assert [o["list"]["position"] for o in lp.observations] == [1, 2, 3]
    assert one["card_hash"] != two["card_hash"]
    for o in lp.observations:
        contracts.validate("observation.v1", o)
    (three,) = parse_list("list-p3.html", 3).observations
    assert three["ad_id"] == "1000003" and parse_list("list-p3.html", 3).page_count is None


def test_ad_page():
    url = f"{LIST}/blue-widget-1000001"
    rec = ExampleSite().parse_ad(
        (FIXTURES / "ad-1000001.html").read_text(),
        url=url,
        fetched_at="2026-01-15T12:00:00Z",
        fetch_reason="new",
    )
    contracts.validate("listing.v1", rec)
    assert rec["ad_id"] == "1000001" and rec["source"]["subcategory"] == "widgets"
    assert (
        rec["content"]["description"]
        == "A synthetic blue widget. Pick-up near Sample St, details on request."
    )
    assert rec["content"]["media_urls"] == [
        "https://img.example.invalid/widget-1000001-1.jpg"
    ]  # deduped, own host only
    assert rec["site"]["example"]["facts"] == {
        "colour": "Blue",
        "condition": "New",
    }  # the empty row is skipped
    assert rec["time"] == {"first_published": "2026-01-01T09:00:00Z", "bumped_at": "2026-01-15T11:30:00Z"}
    assert rec["location"]["pin"] == {"lat": 10.5, "lon": -29.5, "approximate": True}


def test_an_ad_without_a_pin_has_none():
    rec = ExampleSite().parse_ad(
        (FIXTURES / "ad-1000002.html").read_text(),
        url=f"{LIST}/red-widget-1000002",
        fetched_at="2026-01-15T12:00:00Z",
        fetch_reason="new",
    )
    assert rec["location"]["pin"] is None and contracts.validate("listing.v1", rec)


def test_the_example_declares_every_source_attribute():
    s = ExampleSite()
    for attr in ("site", "parser", "home_url", "robots_url", "expect", "default_variant", "coverage_fields"):
        assert getattr(s, attr)
    assert s.list_url("widgets", "sample-region", 2) == f"{LIST}?page=2"
