"""Site-agnostic schema.org reads via extruct (microdata + JSON-LD). A source asks for the pin and
the publication date here instead of hard-coding one site's markup."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import extruct


def extract(html: str, base_url: str | None = None) -> dict:
    return extruct.extract(
        html, base_url=base_url, syntaxes=["microdata", "json-ld"], uniform=True, errors="ignore"
    )


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _num(v: Any) -> float | None:
    v = v[0] if isinstance(v, list) and v else v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pins(data: dict) -> list[dict]:
    """Every GeoCoordinates-like node ({latitude, longitude}) anywhere in the extracted data."""
    out = []
    for n in _walk(data):
        lat, lon = _num(n.get("latitude")), _num(n.get("longitude"))
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            out.append({"lat": lat, "lon": lon})
    return out


def date_published(data: dict) -> str | None:
    for n in _walk(data):
        v = n.get("datePublished")
        if isinstance(v, str) and v:
            return v
    return None


def main_type(data: dict) -> str | None:
    """The first top-level microdata item's type, e.g. AdultEntertainment."""
    for item in data.get("microdata") or []:
        t = item.get("@type")
        t = t[0] if isinstance(t, list) and t else t
        if isinstance(t, str) and t:
            return t.rstrip("/").rsplit("/", 1)[-1]
    return None
