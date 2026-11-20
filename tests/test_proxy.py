"""The run's one proxy ([crawl] proxy): every networked path goes through it, by HOSTNAME, with auth,
and its credentials never land anywhere but the connection.

Offline by construction: the site and the trace endpoint are *.invalid names that do NOT resolve on
this machine, so any local DNS lookup by a client fails the run. A local fake proxy (tests/fakeproxy.py)
requires Basic auth, records each target as the client named it, serves *.invalid from a local server
and refuses everything else. The credentials come from a fake `secret` CLI that records its own argv.
"""

import dataclasses
import http.server
import json
import logging
import sys
import threading
from pathlib import Path

import pytest
from conftest import FIXTURES
from example_site.source import ExampleSite
from fakeproxy import FakeProxy
from test_approvals import KeyFileSigner, make_key, operator_key  # noqa: F401 - fixtures by import

from scrape_harness import browser, contracts, crawl, egress, pipeline, proxy, runlog

USER, PASSWORD = "proxyuser-7f3a", "pw-Zq81-x9secret"  # distinctive, so a leak is findable
SITE = "http://www.example.invalid"
LIST = f"{SITE}/listings/widgets/sample-region?page=1"
FAST = crawl.Politeness(per_minute=6000, override=True)


class Upstream(http.server.SimpleHTTPRequestHandler):
    """What the proxy reaches for any *.invalid name: the site's fixtures and the trace endpoint."""

    ROUTES = {"/robots.txt": "robots.txt", "/cdn-cgi/trace": "trace-ca.txt", "/at/trace": "trace-at.txt"}

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802 - http.server's name
        path = self.path.split("?")[0]
        name = self.ROUTES.get(path) or ("list-p1.html" if path.startswith("/listings/") else None)
        body = (FIXTURES / name).read_bytes() if name else b""
        self.send_response(200 if name else 404)
        self.send_header("Content-Type", "text/html" if name and name.endswith("html") else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def proxied(data_dir, tmp_path_factory, monkeypatch, loopback_only):
    tmp_path = tmp_path_factory.mktemp("keychain")  # outside DATA/STATE: the fake Keychain holds values
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fp = FakeProxy(USER, PASSWORD, ("127.0.0.1", srv.server_port))
    port = fp.start()
    # a fake secret CLI: `secret exec K=<item> ... -- cmd...`, values by item name; it records its argv
    argv_log = tmp_path / "secret-argv.json"
    cli = tmp_path / "secret"
    cli.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "vals = {'test:proxy:user': " + repr(USER) + ", 'test:proxy:password': " + repr(PASSWORD) + "}\n"
        "args = sys.argv[2:]; i = args.index('--')\n"
        "env = dict(os.environ, **{k: vals[v] for k, v in (a.split('=', 1) for a in args[:i])})\n"
        "os.execvpe(args[i + 1], args[i + 1:], env)\n"
    )
    cli.chmod(0o755)
    monkeypatch.setenv("EX_SECRET_CLI", str(cli))
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", "http://trace.invalid/cdn-cgi/trace")
    real = pipeline.current()
    cfg = {
        "url": f"http://127.0.0.1:{port}",
        "user_secret": "test:proxy:user",
        "password_secret": "test:proxy:password",
    }
    monkeypatch.setattr(pipeline, "current", lambda: dataclasses.replace(real, proxy=cfg))
    netlog = tmp_path / "netlog.json"
    launch = browser.launch_options
    monkeypatch.setattr(
        browser,
        "launch_options",
        lambda: (
            (o := launch())
            | {"args": [*o["args"], f"--log-net-log={netlog}", "--net-log-capture-mode=Default"]}
        ),
    )
    yield type("P", (), {"proxy": fp, "port": port, "argv_log": argv_log, "netlog": netlog, "tmp": tmp_path})
    fp.stop()
    srv.shutdown()
    proxy._CACHE.clear()
    runlog._SECRETS.clear()


def site_source():
    s = ExampleSite()
    s.robots_url = f"{SITE}/robots.txt"  # the same source over http: TLS is not what is under test
    return s


def resolver_hosts(netlog: Path) -> set[str]:
    t = netlog.read_text().rstrip()
    d = json.loads(t if t.endswith("}") else t.rstrip(",") + "]}")
    types = {v: k for k, v in d["constants"]["logEventTypes"].items()}
    return {
        str(e["params"]["host"])
        for e in d["events"]
        if types.get(e["type"]) == "HOST_RESOLVER_MANAGER_REQUEST" and "host" in e.get("params", {})
    }


@pytest.mark.parametrize("variant", ["http", "desktop"])
def test_a_proxied_run_sends_everything_through_the_proxy_by_hostname(proxied, data_dir, variant):
    done = []
    job = crawl.Run.new("crawl-lists", variant, "example.invalid")
    crawl.run(job, site_source(), lambda m, res: done.append((res.url, res.status)), FAST, [
        {"url": LIST, "kind": "list", "meta": {"page": 1}}
    ])  # fmt: skip
    assert done == [(LIST, 200)]
    seen = set(proxied.proxy.seen)
    # the egress preflight, robots.txt and the page all went through the proxy, each BY NAME
    assert {("absolute", "trace.invalid"), ("absolute", "www.example.invalid")} <= seen
    paths = set(proxied.proxy.paths)  # robots.txt, the page and the trace, each through the proxy
    assert {
        ("www.example.invalid", "/robots.txt"),
        ("www.example.invalid", "/listings/widgets/sample-region"),
        ("trace.invalid", "/cdn-cgi/trace"),
    } <= paths
    assert all(host.endswith(".invalid") for _, host in seen)  # never an IP: no client resolved one
    assert job.doc["requests"]["robots"] == 1 and job.doc["egress"]["loc"] == "CA"
    assert job.doc["card"]["triggered_by"]["proxy"] == f"127.0.0.1:{proxied.port}"  # host:port only
    if variant == "desktop":  # Chromium's own resolver never saw the site's name (proxy.no_local_dns_flag)
        assert not {h for h in resolver_hosts(proxied.netlog) if "example.invalid" in h}
    # the credentials: only in the connection. Not in argv, a record, a log, the card or STATE.
    argv = proxied.argv_log.read_text()
    assert "test:proxy:user" in argv and USER not in argv and PASSWORD not in argv
    from scrape_harness import store

    for f in [*store.data_dir().rglob("*"), *store.state_dir().rglob("*")]:
        if f.is_file() and f.suffix != ".gz":
            text = f.read_bytes().decode("utf-8", "replace")
            assert USER not in text and PASSWORD not in text, f


def test_the_preflight_goes_through_the_proxy_and_refuses_a_foreign_egress(proxied, monkeypatch):
    assert egress.preflight()["loc"] == "CA"
    monkeypatch.setenv("EX_EGRESS_TRACE_URL", "http://trace.invalid/at/trace")
    with pytest.raises(egress.EgressRefused, match="egress is AT"):
        egress.preflight()  # the CLI turns this into exit 8, as for a VPN
    assert proxied.proxy.seen.count(("absolute", "trace.invalid")) == 2


def test_without_credentials_the_proxy_refuses_and_the_preflight_fails_closed(proxied, monkeypatch):
    proxy._CACHE[pipeline.current().proxy["url"]] = ("someone", "wrong")
    with pytest.raises(egress.EgressRefused, match="preflight failed"):
        egress.preflight()
    assert proxied.proxy.unauthenticated == 1 and not proxied.proxy.seen


def test_a_failed_credential_read_names_the_items_never_a_value(proxied, tmp_path, monkeypatch):
    bad = tmp_path / "bad-secret"
    bad.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write({PASSWORD!r}); sys.exit(1)\n")
    bad.chmod(0o755)
    monkeypatch.setenv("EX_SECRET_CLI", str(bad))
    with pytest.raises(proxy.ProxyError) as e:
        proxy.credentials(proxy.config())
    assert "test:proxy:user" in str(e.value) and PASSWORD not in str(e.value)


def test_registered_credentials_are_scrubbed_from_every_logger(proxied, capsys):
    proxy.credentials(proxy.config())
    log = logging.getLogger("crawlee.test")
    handler = logging.StreamHandler(sys.stderr)
    log.addHandler(handler)
    try:
        log.warning("retrying via http://%s:%s@proxy:8080/ (%s)", USER, PASSWORD, "x")
        try:
            raise ConnectionError(f"proxy said no to {PASSWORD}")
        except ConnectionError:
            log.exception("failed")
    finally:
        log.removeHandler(handler)
    err = capsys.readouterr().err
    assert PASSWORD not in err and USER not in err and "[redacted]" in err
    assert "plan 0123456789abcdef0123456789abcdef" in runlog.scrub("plan 0123456789abcdef0123456789abcdef")


def test_the_schema_takes_an_http_proxy_by_item_names_only():
    ok = {
        "url": "http://ca-tor.proxy.example:8080",
        "user_secret": "proxy:x:user",
        "password_secret": "proxy:x:pw",
    }
    assert not [e for e in contracts.errors("pipeline.v1", {"crawl": {"proxy": ok}}) if e.startswith("crawl")]
    for url in (
        "socks5://h.example:1080",
        "http://u:p@h.example:8080",
        "http://h.example",
        "https://h.example:8080",
    ):
        errs = contracts.errors("pipeline.v1", {"crawl": {"proxy": ok | {"url": url}}})
        assert any(e.startswith("crawl/proxy/url") for e in errs), url


def test_the_proxy_url_is_in_the_plan_only_when_configured(proxied, monkeypatch):
    from types import SimpleNamespace

    from scrape_harness.cli import _proxy_plan

    args = SimpleNamespace(plan=False)
    assert _proxy_plan(args, {}) == {"proxy": f"http://127.0.0.1:{proxied.port}"}
    real = pipeline.current()
    monkeypatch.setattr(pipeline, "current", lambda: dataclasses.replace(real, proxy=None))
    assert _proxy_plan(args, {}) == {}  # no key: every plan without a proxy hashes as before


def test_chromium_is_told_to_resolve_nothing_but_the_proxy(proxied):
    assert (
        proxy.no_local_dns_flag(proxy.config()) == "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"
    )


# ── several proxies, ONE per run ────────────────────────────────────────────────────────────────────
DEAD = ("http://127.0.0.1:19", "http://127.0.0.1:29")  # nothing listens: a run that used one fails
ONE_LIST = [{"url": LIST, "kind": "list", "meta": {"page": 1}}]


@pytest.fixture
def three(proxied, monkeypatch):
    """[crawl.proxy] with three URLs: the fake proxy and two dead ones, one shared login."""
    live = f"http://127.0.0.1:{proxied.port}"
    real = pipeline.current()
    cfg = {
        "urls": [DEAD[0], live, DEAD[1]],
        "choice": "random",
        "user_secret": "test:proxy:user",
        "password_secret": "test:proxy:password",
    }
    monkeypatch.setattr(pipeline, "current", lambda: dataclasses.replace(real, proxy=cfg))
    proxy.deactivate()
    yield type("T", (), {"live": live, "cfg": cfg})
    proxy.deactivate()


def test_plan_chooses_one_and_the_run_finds_the_one_that_was_signed(three, operator_key):  # noqa: F811
    from types import SimpleNamespace

    from scrape_harness import approvals
    from scrape_harness.cli import _proxy_plan

    doc = {"command": "fetch-ads", "ads": ["1"]}
    chosen = _proxy_plan(SimpleNamespace(plan=True), doc)["proxy"]
    assert chosen in three.cfg["urls"] and proxy.config().url == chosen  # chosen AND active
    approvals.approve(approvals.save_plan(doc | {"proxy": chosen}))  # what `--plan` + `approve` do
    for _ in range(5):  # whatever a new choice would be, the run takes the SIGNED one
        proxy.deactivate()
        assert _proxy_plan(SimpleNamespace(plan=False), doc) == {"proxy": chosen}
        assert proxy.config().url == chosen


def test_round_robin_rotates_through_a_cursor_in_state(three, data_dir):
    from scrape_harness import store

    ps = dataclasses.replace(proxy.proxy_set(), choice="round_robin")
    assert [proxy.choose(ps) for _ in range(4)] == [*ps.urls, ps.urls[0]]
    assert (store.state_dir() / "proxy" / "round-robin.json").exists()


def test_a_run_hands_crawlee_exactly_one_url_and_records_it(three, data_dir, monkeypatch):
    from crawlee import proxy_configuration

    seen = []
    real_init = proxy_configuration.ProxyConfiguration.__init__

    def spy(self, *a, **kw):
        seen.append(list(kw.get("proxy_urls") or []))
        real_init(self, *a, **kw)

    monkeypatch.setattr(proxy_configuration.ProxyConfiguration, "__init__", spy)
    proxy.activate(three.live)
    job = crawl.Run.new("crawl-lists", "http", "example.invalid")
    assert crawl.run(job, site_source(), lambda m, r: None, FAST, ONE_LIST) == 0
    assert len(seen) == 1 and len(seen[0]) == 1  # ONE url, never the list
    assert seen[0][0].endswith(three.live.removeprefix("http://"))
    assert job.doc["proxy"] == {"url": three.live, "choice": "random"}


def test_several_proxies_and_none_chosen_refuses_before_anything_is_sent(three, data_dir):
    job = crawl.Run.new("crawl-lists", "http", "example.invalid")
    with pytest.raises(crawl.Stop) as e:
        crawl.run(job, site_source(), lambda m, r: None, FAST, ONE_LIST)
    assert e.value.code == egress.EXIT_EGRESS and "none is chosen" in str(e.value)


def test_a_resumed_run_keeps_its_original_proxy(three, data_dir):
    job = crawl.Run.new("crawl-lists", "http", "example.invalid")
    job.doc["proxy"] = {"url": three.live, "choice": "random"}  # what the run started with
    job.save()
    proxy.activate(DEAD[0])  # meanwhile a new plan chose another (dead) one
    assert crawl.run(crawl.Run.load(job.doc["run_id"]), site_source(), lambda m, r: None, FAST, ONE_LIST) == 0
    assert proxy.config().url == three.live


def test_a_record_naming_a_proxy_no_longer_configured_refuses(three, data_dir):
    job = crawl.Run.new("crawl-lists", "http", "example.invalid")
    job.doc["proxy"] = {"url": "http://127.0.0.1:39", "choice": "random"}
    job.save()
    with pytest.raises(crawl.Stop, match="not one of the configured"):
        crawl.run(job, site_source(), lambda m, r: None, FAST, ONE_LIST)


def test_the_schema_takes_url_or_urls_never_both():
    base = {"user_secret": "a:b", "password_secret": "a:c"}
    ok = [
        base | {"url": "http://h.example:8080"},
        base | {"urls": ["http://a.example:8080", "http://b.example:8080"], "choice": "round_robin"},
    ]
    for p in ok:
        assert not [
            e for e in contracts.errors("pipeline.v1", {"crawl": {"proxy": p}}) if e.startswith("crawl")
        ], p
    bad = [
        base | {"url": "http://h.example:8080", "urls": ["http://a.example:8080"]},
        base,
        base | {"urls": []},
    ]
    for p in bad:
        assert [
            e for e in contracts.errors("pipeline.v1", {"crawl": {"proxy": p}}) if e.startswith("crawl")
        ], p


def test_an_offline_plan_names_no_proxy_and_reads_no_approval(three, monkeypatch):
    """Offline backends send nothing: picking a proxy (which reads the approval ledger) must not happen."""
    from types import SimpleNamespace

    from scrape_harness import approvals, cli

    monkeypatch.setattr(
        approvals, "has_live", lambda h: (_ for _ in ()).throw(AssertionError("read the ledger"))
    )
    args = SimpleNamespace(
        plan=False, concurrency=1, max_tasks_per_minute=6, approve_politeness_override=False, driver="fixture"
    )
    monkeypatch.setattr(cli, "_backend_name", lambda a, v: "fixture")
    doc = cli.run_plan(args, "crawl-lists", site_source(), ["desktop"], subcategory="widgets")
    assert "proxy" not in doc


def test_an_offline_run_with_several_proxies_configured_never_asks_for_one(three, data_dir, tmp_path):
    from test_crawl import fixtures

    job = crawl.Run.new("crawl-lists", "desktop", "example.invalid")
    opts = fixtures(tmp_path, {})
    assert crawl.run(job, ExampleSite(), lambda m, r: None, FAST, [
        {"url": "https://www.example.invalid/listings/widgets/sample-region?page=1", "kind": "list", "meta": {"page": 1}}
    ], opts) == 0  # fmt: skip
    assert "proxy" not in job.doc and "proxy" not in job.doc["card"]["triggered_by"]


def test_a_resume_plan_signs_the_runs_own_proxy_never_a_new_pick(three, data_dir, monkeypatch):
    from types import SimpleNamespace

    from scrape_harness.cli import _proxy_plan

    monkeypatch.setattr("scrape_harness.cli._networked", lambda a, v: True)
    for _ in range(10):  # a random pick would hit the others; the record's URL must always win
        proxy.deactivate()
        assert _proxy_plan(SimpleNamespace(plan=True, kept_proxy=DEAD[1]), {}) == {"proxy": DEAD[1]}
    with pytest.raises(SystemExit, match="no longer configured"):
        _proxy_plan(SimpleNamespace(plan=True, kept_proxy="http://127.0.0.1:49"), {})


class _Picked(Exception):
    pass


def test_one_substring_pick_serves_refresh_probe_robots_and_crawl_lists(monkeypatch):
    """`refresh --proxy-only/--proxy-exclude`, `probe robots --proxy-only` and a crawl plan's `--proxy-exclude`
    pick by ONE function (proxy.allowed), so the match rule and the refusal text cannot drift apart."""
    import dataclasses as dc
    from types import SimpleNamespace

    from scrape_harness import cli, refresh

    urls = ("http://ca-a.proxy.example.invalid:8080", "http://ca-b.proxy.example.invalid:8080")
    real = pipeline.current()
    cfg = {"urls": list(urls), "choice": "random", "user_secret": "a:b", "password_secret": "a:c"}
    monkeypatch.setattr(pipeline, "current", lambda: dc.replace(real, proxy=cfg))
    calls = []

    def spy(*a, **k):
        calls.append(a)
        raise _Picked

    monkeypatch.setattr(proxy, "allowed", spy)
    for run in (
        lambda: refresh.allowed_proxies(urls, "ca-a."),
        lambda: cli.cmd_probe_robots(SimpleNamespace(source=real.consumer.default_site, proxy_only="ca-a.")),
        lambda: cli._proxy_plan(SimpleNamespace(plan=True, proxy_exclude=["ca-a."]), {}),  # noqa: SLF001
    ):
        with pytest.raises(_Picked):
            run()
    assert len(calls) == 3
    monkeypatch.undo()
    for only, exclude, msg in [
        ("ca-", (), "--proxy-only ca- matches 2 configured proxies, not 1"),
        (None, ("ca-a.", "ca-b."), "--proxy-exclude ca-a. --proxy-exclude ca-b. leaves no proxy of 2"),
    ]:
        with pytest.raises(SystemExit, match=msg):
            proxy.allowed(urls, only, exclude)
    assert proxy.allowed(urls, None, ()) is None
    assert proxy.allowed(urls, "ca-b.", ()) == (urls[1],)
