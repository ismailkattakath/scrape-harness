"""Fixed challenge detectors. No model judgement: a hit stops the run.

Order matters only for the reason code reported; any hit stops. Codes are stable strings that
land in event.v1 `reason`.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .interfaces import FetchResult

BLOCK_STATUS = {403: "http_403", 429: "http_429", 503: "http_503"}
# Script/asset markers that only challenge interstitials carry.
CHALLENGE_MARKERS = re.compile(r"cf-chl|/cdn-cgi/challenge-platform|challenges\.cloudflare\.com", re.I)
# Cloudflare's passive bot-detection beacon: injected at random into NORMAL pages (42 of 1,364 saved ad
# pages, 3.1%, measured 2026-11-13), so it is never a challenge marker (see detect()).
JSD_BEACON = re.compile(r"/cdn-cgi/challenge-platform/scripts/jsd/main\.js", re.I)
CAPTCHA_IFRAME = re.compile(r"(recaptcha|hcaptcha\.com|challenges\.cloudflare\.com)", re.I)
CHALLENGE_TITLES = re.compile(
    r"^\s*(just a moment\.*|attention required!?.*|access denied|security check|verify you are human|"
    r"checking your browser.*|ddos protection.*)\s*$",
    re.I,
)
LOGIN_PATH = re.compile(r"/(login|log-in|signin|sign-in|sign_in|auth)(/|$)", re.I)
GATE_TEXT = re.compile(
    r"are you (over )?18|i am (over )?18|over 18 years|age verification|verify your age|"
    r"(accept|agree to) (the )?(terms|conditions)|you must be 18",
    re.I,
)


SIGNIN_TEXT = re.compile(r"\b(sign|log)[ -]?in\b|\blogin\b", re.I)
EXPECTED_MISSING = ("signin_wall", "consent_gate", "unexpected_page")


def _doc(r: FetchResult, ignore=()):
    doc = BeautifulSoup(r.html or "", "html.parser")
    for sel in ignore:  # the source's always-present chrome (e.g. a hidden login modal on every page)
        for el in doc.select(sel):
            el.decompose()
    return doc


def detect(r: FetchResult, expect: str | None, ignore=()) -> str | None:
    """Return a reason code when the page is not the content it should be, else None.

    `expect` is the source's CSS selector for a real page of this kind (None: status/markers only).
    A missing expected element with no recognised marker is still a stop (`unexpected_page`):
    failing closed beats parsing a wall as an empty listing."""
    if r.status in BLOCK_STATUS:
        return BLOCK_STATUS[r.status]
    if LOGIN_PATH.search(urlsplit(r.final_url).path) and not LOGIN_PATH.search(urlsplit(r.url).path):
        return "login_redirect"
    doc = _doc(r, ignore)
    title = doc.title.get_text() if doc.title else ""
    mitigated = {k.lower(): v for k, v in (r.headers or {}).items()}.get("cf-mitigated")
    if mitigated:  # Cloudflare's own statement that it served a mitigation instead of the page
        return "cf_mitigated"
    if CHALLENGE_TITLES.match(title):
        return "challenge_title"
    # A marker ALONE is not a challenge: Cloudflare injects its background bot-detection beacon
    # (/cdn-cgi/challenge-platform/scripts/jsd/main.js) into NORMAL 200 pages (false positive,
    # 2026-11-07, ADR-0004). It counts only when corroborated: an error status, or the page is not
    # the expected page. (A cf-mitigated header or a challenge title already returned above.)
    # The same rule for captcha iframes: a reCAPTCHA/hCaptcha/Turnstile widget on a REAL page (a
    # contact or report form on an ad) is not a challenge (ADR-0004).
    corroborated = r.status >= 400 or (expect is not None and doc.select_one(expect) is None)
    if corroborated and any(CAPTCHA_IFRAME.search(f.get("src") or "") for f in doc.find_all("iframe")):
        return "captcha_iframe"
    # The passive beacon is never a marker, whatever corroborates it: it rides on ordinary pages, and it
    # stopped two runs on 2026-11-13 as their ONLY sign, both on expired-ad pages (a 404, …3838e4; a 200 with
    # no ad body, …470b75). Every real challenge sign (cf-mitigated, a challenge title, cf-chl,
    # challenge-platform/h/…, challenges.cloudflare.com, a captcha iframe, 403/429/503) still counts.
    html = JSD_BEACON.sub("", r.html or "")
    if corroborated and CHALLENGE_MARKERS.search(html):
        return "challenge_marker"
    if r.status >= 400:
        return f"http_{r.status}"
    if expect and doc.select_one(expect) is None:
        # a password field alone is not a wall: a site can carry a hidden login modal on EVERY page (32/32
        # and 100/100 normal ad pages, 2026-11-08), which its source strips via ignore_selectors. What is
        # left counts with a sign-in signal: a login path, or "sign in" in the title, a heading, or the
        # password field's own form. Otherwise the page is honestly "unexpected".
        pw = doc.select_one('input[type="password"]')
        form = pw.find_parent("form") if pw else None
        texts = [title, *(h.get_text(" ") for h in doc.select("h1, h2")), form.get_text(" ") if form else ""]
        texts += [b.get("value") or "" for b in (form.select("input[type=submit]") if form else [])]
        signal = LOGIN_PATH.search(urlsplit(r.final_url or "").path) or any(
            SIGNIN_TEXT.search(t) for t in texts
        )
        if pw and signal:
            return "signin_wall"
        if GATE_TEXT.search(doc.get_text(" ")):
            return "consent_gate"
        return "unexpected_page"
    return None


def redirected_to_list(r: FetchResult, reason: str | None, list_expect: str | None, ignore=()) -> bool:
    """An AD request that ended, by a redirect, on a clean LIST page: the ad is gone (removed or expired,
    and the site sent its visitors to the category), not a wall. Clean means detect() finds nothing on it
    with the list expectation: the list selector is present and there is no challenge sign (cf-mitigated,
    a challenge title; markers/captcha count only when corroborated, and a page matching the list is not).
    Measured case (2026-11-08): an ad answered 200 after a redirect with its category list's title, and
    stopped as signin_wall on the site's always-present login modal.
    """
    return (
        reason in EXPECTED_MISSING
        and bool(list_expect)
        and urlsplit(r.final_url or r.url).path != urlsplit(r.url).path
        and detect(r, list_expect, ignore) is None
    )


def gone_reason(
    r: FetchResult, reason: str | None, list_expect: str | None, gone_selectors=(), ignore=()
) -> str | None:
    """Why an AD request's page means the ad is GONE, or None. Only when the expected ad element is missing
    (reason in EXPECTED_MISSING, which detect() only returns when no challenge sign is on the page):
      - expired_notice: the source's own "this ad is no longer active" page (gone_selectors);
      - redirected_to_list / served_list: a clean LIST page (the source's list expectation passes), reached
        by a redirect, or served at the ad's own URL. Measured 2026-11-08: a soft-404 served the category
        list AT the ad URL (200, no hop, canonical still the ad), so a redirect alone cannot be required."""
    # reason in EXPECTED_MISSING already means NO challenge sign: detect() returns every challenge code
    # (error status, login redirect, cf-mitigated, challenge title, corroborated markers/captcha) first.
    # 404/410 (RFC 9110: the resource is not found / gone) on an AD request is gone, first visit or revisit,
    # with or without proof: detect() returns http_404/410 only after every challenge check (login redirect,
    # cf-mitigated, a challenge title, captcha/markers, which an error status corroborates), so the reason
    # itself means no challenge sign. Proof only refines the label. Operator decision 2026-11-08 (the standard
    # rule, after three gone-ad stops: 200 + list, 200 + expired page, 404 + expired page).
    if reason not in EXPECTED_MISSING and reason not in GONE:
        return None
    doc = _doc(r, ignore)
    if any(doc.select_one(s) for s in gone_selectors):
        return "expired_notice"
    listed = bool(list_expect) and (
        doc.select_one(list_expect) is not None if reason in GONE else detect(r, list_expect, ignore) is None
    )
    if listed:
        moved = urlsplit(r.final_url or r.url).path != urlsplit(r.url).path
        return "redirected_to_list" if moved else "served_list"
    return reason if reason in GONE else None  # a bare 404/410: gone, labelled by its status


GONE = ("http_404", "http_410")


def end_reason(
    r: FetchResult, reason: str | None, end_selectors=(), ignore=(), end_statuses=()
) -> str | None:
    """`end_marker` when a LIST page carries the source's own "no results" template (end_selectors) on a page
    with no challenge sign and no wall, else None. It admits a clean page (None), one that is merely not the list
    (`unexpected_page`) and a 404/410 (GONE), because detect() returns http_4xx before the expectation check, so
    a past-end 404 with the marker would otherwise be a challenge stop. Every challenge sign (a block status, a
    login redirect, cf-mitigated, a challenge title, corroborated markers or captcha) returns from detect()
    before GONE, so it is never an end; nor is a sign-in wall or a consent gate, which stay stops (exit 5) even
    when their template carries a generic "no results" component. A source that declares `end_statuses` (its
    measured past-end answer, e.g. (404, 410)) ends a list only on those statuses: a 200 "no results" page (a
    filter's empty result, a soft block) is then never its END. The marker only nominates the page: the list
    handler accepts END (0 cards, no redirect) or refuses it (ADR-0017 A.1)."""
    if not end_selectors or not (reason is None or reason == "unexpected_page" or reason in GONE):
        return None
    if end_statuses and r.status not in end_statuses:
        return None
    doc = _doc(r, ignore)
    return "end_marker" if any(doc.select_one(s) for s in end_selectors) else None


def gone(reason: str | None, revisit: bool) -> bool:
    """A REVISIT of a known ad that answers 404/410 with no challenge sign is a removed page ("gone"),
    not a challenge. detect() reaches a bare http_404/http_410 only after the captcha-iframe and
    challenge-marker checks, which an error status corroborates, so a challenge served AS a 404 still
    stops the run. First visits are unchanged: an unknown URL answering 404 stays a stop."""
    return revisit and reason in GONE


EVIDENCE_HEADERS = {"server": "server", "cf-ray": "cf_ray", "cf-mitigated": "cf_mitigated"}  # an allowlist
TOKENISH = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")  # long opaque strings (tokens, keys) never leave the page


def evidence(r: FetchResult) -> dict:
    """What the blocked response itself says, captured with NO extra request: status, three allowlisted
    headers, the page title and a <=500-char visible-text excerpt. Cookies and tokens are never kept:
    only allowlisted headers are read, and long opaque strings in the excerpt are redacted."""
    headers = {k.lower(): v for k, v in (r.headers or {}).items()}
    doc = BeautifulSoup(r.html or "", "html.parser")
    for tag in doc(["script", "style", "noscript"]):
        tag.decompose()
    text = TOKENISH.sub("[redacted]", re.sub(r"\s+", " ", doc.get_text(" ")).strip())
    title = TOKENISH.sub("[redacted]", doc.title.get_text().strip()) if doc.title else None
    ev: dict = {"status": r.status, "title": (title or None) and title[:200], "excerpt": text[:500]}
    for name, key in EVIDENCE_HEADERS.items():
        if headers.get(name):
            ev[key] = str(headers[name])[:200]
    return ev
