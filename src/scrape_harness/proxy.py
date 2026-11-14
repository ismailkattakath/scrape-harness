"""The run's one proxy (operator decision 2026-11-08): pipeline.toml [crawl] proxy, used by EVERY
networked path of a run (the browser, the impit HTTP client incl. robots.txt, the egress preflight).
One fixed URL: no tiers, no rotation; a challenge still stops the run.

    [crawl]
    proxy = { url = "http://proxy.example:8080", user_secret = "<keychain item>", password_secret = "<item>" }

HTTP, not SOCKS5 (measured 2026-11-08, pinned Playwright Chromium): launch_persistent_context with an
authenticated socks5 proxy fails "Browser does not support socks5 proxy authentication". An HTTP proxy
also keeps every hostname remote: a CONNECT (https) or an absolute-URI request (http) carries the NAME,
so the proxy resolves it. The schema accepts http:// only.

Credentials: only Keychain item NAMES are in pipeline.toml. At launch they are read through the secret
CLI's exec path (`secret exec U=<item> P=<item> -- <a child that writes them to our pipe>`), so a value
is never in argv, the environment of this process, a log, a run record or the card. The card and the
plan show host:port only; the URL (not the credentials) is part of what an approval signs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from . import pipeline, runlog

USER_ENV, PASS_ENV = "SCRAPE_HARNESS_PROXY_USER", "SCRAPE_HARNESS_PROXY_PASSWORD"  # the child's only


class ProxyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Proxy:
    url: str  # http://host:port, no credentials
    user_secret: str
    password_secret: str

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def port(self) -> int:
        return urlsplit(self.url).port or 0

    @property
    def public(self) -> str:
        """What a card, a plan or a log may show."""
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class ProxySet:
    """[crawl.proxy]: one or more URLs sharing ONE login; a run uses exactly ONE of them, never the list
    (crawlee given a list freezes the persistent browser on one proxy while labelling proxy_info and
    rotating robots/HTTP over the others: mixed egress, per proxy-rotation research 2026-11-08)."""

    urls: tuple[str, ...]
    choice: str  # random | round_robin: how a NEW run (its --plan) picks one
    user_secret: str
    password_secret: str

    def proxy(self, url: str) -> Proxy:
        return Proxy(url, self.user_secret, self.password_secret)


def proxy_set() -> ProxySet | None:
    p = pipeline.current().proxy
    if not p:
        return None
    urls = tuple(p.get("urls") or [p["url"]])
    return ProxySet(urls, p.get("choice", "random"), p["user_secret"], p["password_secret"])


_ACTIVE: list[str] = []  # the run's one chosen URL (activate), for every networked path of this process


def activate(url: str) -> None:
    ps = proxy_set()
    if not ps or url not in ps.urls:
        raise ProxyError(f"{url} is not one of the configured [crawl.proxy] urls")
    _ACTIVE[:] = [url]


def deactivate() -> None:
    _ACTIVE.clear()


def choose(ps: ProxySet) -> str:
    """Pick one URL for a new run: `random`, or `round_robin` (a cursor in STATE, so consecutive runs
    rotate). Only a plan (or a single-URL set) calls this; a run reuses its plan's or record's URL."""
    if len(ps.urls) == 1:
        return ps.urls[0]
    if ps.choice == "round_robin":
        from . import store

        cur = store.state_dir() / "proxy" / "round-robin.json"
        cur.parent.mkdir(parents=True, exist_ok=True)
        try:
            i = json.loads(cur.read_text()).get("next", 0)
        except (OSError, ValueError):
            i = 0
        url = ps.urls[i % len(ps.urls)]
        tmp = cur.with_suffix(".tmp")
        tmp.write_text(json.dumps({"next": (i + 1) % len(ps.urls)}))
        tmp.replace(cur)
        return url
    import secrets

    return secrets.choice(ps.urls)


def current() -> Proxy | None:
    """config(), but None (not an error) when several are configured and none is chosen: for code that may
    be built outside a run (a browser pool in a test). A run activates its proxy before building either."""
    try:
        return config()
    except ProxyError:
        return None


def config() -> Proxy | None:
    """The run's ONE proxy: the activated URL, or the only one configured. Several configured and none
    activated raises: no networked path may pick its own (that is how egress gets mixed)."""
    ps = proxy_set()
    if not ps:
        return None
    if _ACTIVE and _ACTIVE[0] in ps.urls:
        return ps.proxy(_ACTIVE[0])
    if len(ps.urls) == 1:
        return ps.proxy(ps.urls[0])
    raise ProxyError(f"[crawl.proxy] lists {len(ps.urls)} proxies and none is chosen for this run")


def _secret_cli() -> str:
    return pipeline.knob("SECRET_CLI") or "secret"


_CACHE: dict[str, tuple[str, str]] = {}


def credentials(p: Proxy) -> tuple[str, str]:
    """(user, password), read once per process through `secret exec`, and registered for redaction."""
    if p.url in _CACHE:
        return _CACHE[p.url]
    child = "import json,os,sys;sys.stdout.write(json.dumps([os.environ[a] for a in sys.argv[1:]]))"
    cmd = [
        _secret_cli(), "exec", f"{USER_ENV}={p.user_secret}", f"{PASS_ENV}={p.password_secret}", "--",
        sys.executable, "-I", "-c", child, USER_ENV, PASS_ENV,
    ]  # fmt: skip
    try:
        out = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout  # noqa: S603
        user, password = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        # never echo the child's output: it may hold a value
        raise ProxyError(
            f"could not read the proxy credentials ({p.user_secret}, {p.password_secret}) through "
            f"`{_secret_cli()} exec` ({type(e).__name__}); is the Keychain unlocked?"
        ) from None
    if not user or not password:
        raise ProxyError(f"the proxy credentials ({p.user_secret}, {p.password_secret}) are empty")
    runlog.add_secrets(user, password)
    _CACHE[p.url] = (user, password)
    return user, password


def url_with_credentials(p: Proxy) -> str:
    """For crawlee's ProxyConfiguration, impit and urllib only: never logged (redacted if it ever is)."""
    user, password = credentials(p)
    return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{p.host}:{p.port}"


def playwright(p: Proxy) -> dict:
    user, password = credentials(p)
    return {"server": f"http://{p.host}:{p.port}", "username": user, "password": password}


def no_local_dns_flag(p: Proxy) -> str:
    """Chromium resolves NOTHING itself while proxied, except the proxy's own name (measured
    2026-11-08: with an HTTP proxy set, Chromium's resolver still ran a local lookup task for the
    site's hostname; with this rule every such request becomes ~notfound and the page loads through
    the proxy). Documented host-resolver-rules syntax: "MAP * ~NOTFOUND , EXCLUDE <host>"."""
    return f"--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE {p.host}"


def environ_cleared() -> None:
    """The secret CLI's exec must not leak into later children: nothing here sets these, but a parent
    might have; drop them so no subprocess inherits a credential."""
    for k in (USER_ENV, PASS_ENV):
        os.environ.pop(k, None)
