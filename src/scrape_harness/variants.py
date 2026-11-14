"""Page variants: which crawlee backend fetches a page, and how the browser presents itself.
Configuration, not code paths. Each variant names its BACKEND (an entry point in the
`scrape_harness.drivers` group), so "impit HTTP vs dedicated browser" is a per-variant value.

| variant      | backend    | fingerprints                    | presents as            |
|--------------|------------|---------------------------------|------------------------|
| http         | impit-http | n/a (impit's default browser)    | plain HTTP, no cookies |
| desktop      | browser    | ON (crawlee default)            | desktop Chromium       |
| desktop-nofp | browser    | OFF                             | desktop Chromium       |
| mobile       | browser    | OFF                             | mobile UA + viewport   |
| desktop-unblocked | browser | ON (crawlee default)         | desktop Chromium, fetches images/fonts/media |

`desktop` is the primary variant: crawlee's fingerprint generator is on. `LI_VARIANT`,
`--variant`, or a source's per-kind default selects another. The browser variants share the
dedicated persistent profile, so a gate the operator cleared in `<cli> browser open` stays cleared.
Every browser variant blocks [crawl] block_extensions except `desktop-unblocked`: the A/B arm that
measures what blocking changes (challenge rate, requests sent per page), otherwise identical to `desktop`.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import pipeline


@dataclass(frozen=True)
class Variant:
    name: str
    backend: str
    fingerprints: bool = False
    mobile: bool = False
    viewport: tuple[int, int] = (1366, 900)
    user_agent: str | None = None
    blocks: bool = True  # browser variants: never fetch [crawl] block_extensions
    in_default_ab: bool = True  # `ab` without --variants compares these


MOBILE_UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"

VARIANTS = {
    "http": Variant("http", backend="impit-http"),
    "desktop": Variant("desktop", backend="browser", fingerprints=True),
    "desktop-nofp": Variant("desktop-nofp", backend="browser"),
    "desktop-unblocked": Variant(
        "desktop-unblocked", backend="browser", fingerprints=True, blocks=False, in_default_ab=False
    ),
    "mobile": Variant("mobile", backend="browser", mobile=True, viewport=(412, 915), user_agent=MOBILE_UA),
}
DEFAULT = "desktop"


def get(name: str | None = None) -> Variant:
    name = name or pipeline.knob("VARIANT") or DEFAULT
    if name not in VARIANTS:
        raise SystemExit(f"unknown variant {name!r}; known: {', '.join(VARIANTS)}")
    return VARIANTS[name]
