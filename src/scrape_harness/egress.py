"""Egress preflight (operator decision 2026-11-07, ADR-0004): before any networked crawl, ask
Cloudflare's own diagnostic endpoint where this machine's traffic leaves from, and REFUSE to start
unless it leaves from the operator's home country. It exists because the first real runs went out
through a VPN (loc=AT) and met a Cloudflare managed challenge. There is no override flag: the
operator fixes the network (disconnects the VPN) and re-runs.

Configuration, not code:
  LI_EGRESS_TRACE_URL  default https://www.cloudflare.com/cdn-cgi/trace (never the target site)
  LI_HOME_COUNTRY      default CA (ISO 3166-1 alpha-2, as the trace's `loc` reports it)

Only loc / colo / warp / gateway are kept. The trace also reports the public IP; it is NOT stored.
"""

from __future__ import annotations

import urllib.request
from datetime import UTC, datetime

from . import pipeline

DEFAULT_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
DEFAULT_HOME = "CA"
KEPT = ("loc", "colo", "warp", "gateway")
EXIT_EGRESS = 8


class EgressRefused(Exception):  # noqa: N818 - a refusal, carried to the CLI as exit 8
    pass


def trace_url() -> str:
    return pipeline.knob("EGRESS_TRACE_URL") or DEFAULT_TRACE_URL


def home_country() -> str:
    return (pipeline.knob("HOME_COUNTRY") or DEFAULT_HOME).upper()


def parse(body: str) -> dict:
    """The trace is `key=value` lines. Keep only the allowlisted keys (never `ip`)."""
    kv = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
    return {k: kv[k].strip() for k in KEPT if kv.get(k)}


def fetch(url: str, timeout: float = 10) -> str:
    """The trace, through the run's proxy when one is configured (a run's egress IS the proxy's), so the
    country checked is the one the site will see. An http(s) trace via an HTTP proxy carries the
    hostname (CONNECT / absolute URI): nothing is resolved here."""
    from . import proxy

    px = proxy.config() if url.startswith(("http://", "https://")) else None
    if px:
        u = proxy.url_with_credentials(px)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": u, "https": u}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never ambient *_proxy env
    with opener.open(url, timeout=timeout) as r:  # noqa: S310 - a configured diagnostic URL
        return r.read(4096).decode("utf-8", errors="replace")


def preflight(get=fetch) -> dict:
    """Return the egress record, or raise EgressRefused. Fails CLOSED: an unreachable or unparsable
    trace refuses exactly like a foreign country does."""
    url, home = trace_url(), home_country()
    try:
        egress = parse(get(url))
    except Exception as e:  # noqa: BLE001 - any failure to learn the egress is a refusal
        raise EgressRefused(
            f"egress preflight failed ({type(e).__name__}) at {url}; refusing to crawl"
        ) from e
    egress |= {"checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "trace_url": url}
    loc = egress.get("loc", "").upper()
    if loc != home:
        raise EgressRefused(
            f"egress is {loc or 'unknown'} (colo {egress.get('colo', '?')}, warp {egress.get('warp', '?')}), "
            f"not {home}. A VPN or proxy is probably on. Disconnect it and re-run; there is no override."
        )
    return egress
