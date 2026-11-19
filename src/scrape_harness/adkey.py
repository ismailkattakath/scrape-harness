"""Which ad an event is about: the ONE join rule that the liveness ledger and the independent postcondition
(plan.assert_admissible) share, so the gate and its check can never disagree on it. Pure, no I/O.

An event names its ad by (site, ad_id). An event without both (written before the field) joins by its url: an
observation's `ad_url` or a listing's `source.url`, narrowed by whichever of site and ad_id it does carry. A url
that matches no ad, or more than one, joins none: the rule never guesses."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def listing_key(r: Mapping) -> tuple:
    src = r.get("source") or {}
    return (src.get("site", r.get("site")), r["ad_id"])


def url_index(observations: Iterable[Mapping], listings: Iterable[Mapping]) -> dict[str, set]:
    """url -> the (site, ad_id) keys the records list under it."""
    idx: dict[str, set] = {}
    for o in observations:
        if o.get("ad_url"):
            idx.setdefault(o["ad_url"], set()).add((o["site"], o["ad_id"]))
    for r in listings:
        url = (r.get("source") or {}).get("url")
        if url:
            idx.setdefault(url, set()).add(listing_key(r))
    return idx


def event_key(e: Mapping, idx: dict[str, set]) -> tuple | None:
    """The (site, ad_id) event `e` is about, or None when it cannot be told."""
    site, aid = e.get("site"), e.get("ad_id")
    if site and aid:
        return (site, aid)
    keys = idx.get(e.get("url") or "", set())
    keys = {k for k in keys if (not site or k[0] == site) and (not aid or k[1] == aid)}
    return next(iter(keys)) if len(keys) == 1 else None
