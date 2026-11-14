from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "example_site"
# The plugin lives in the ONE marketplace repo (plugins/scrape-harness), pinned as this flake's
# marketplace input; the build and the dev shell export its store path. No fallback: a missing
# plugin must fail every hook test, never silently test some other copy.
PLUGIN = Path(os.environ.get("SCRAPE_HARNESS_PLUGIN") or "/nonexistent/SCRAPE_HARNESS_PLUGIN-unset")
# The EXAMPLE consumer drives the generic tests end to end: its pipeline (the repo root's), its source and
# its record schemas. A test that needs another pipeline passes it explicitly (loader arg / subprocess env).
os.environ.setdefault("SCRAPE_HARNESS_PIPELINE", str(ROOT / "pipeline.toml"))
sys.path.insert(0, str(Path(__file__).parent))  # conftest, for the test modules
sys.path.insert(0, str(EXAMPLE.parent))  # so test modules can import example_site directly

# The example's source and record schemas are NOT registered here: pipeline.toml [plugins] declares
# them, exactly as a consumer would, so the CLI finds them in-process and in a doit subprocess alike.
FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN_DIR = ROOT / "tests" / "golden"
RECORD = "SCRAPE_HARNESS_RECORD_GOLDEN"


def check_golden(name: str, got) -> None:
    """Compare `got` with tests/golden/<name>.json. A golden is (re)written ONLY when
    SCRAPE_HARNESS_RECORD_GOLDEN=1, and that run then FAILS on purpose, so a recording is never a pass:
    review the diff (git diff tests/golden), commit it with the reason, and re-run without the flag."""
    import json

    path = GOLDEN_DIR / f"{name}.json"
    text = json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get(RECORD) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        pytest.fail(f"RECORDED {path}; review `git diff tests/golden`, commit, re-run without {RECORD}")
    if not path.exists():
        pytest.fail(f"no golden {path}; record it deliberately with {RECORD}=1 and review it")
    assert json.loads(text) == json.loads(path.read_text()), f"differs from {path}"


EXAMPLES = ROOT / "schemas" / "examples"
ORIGIN = "https://www.example.invalid"
LIST = f"{ORIGIN}/listings/widgets/sample-region"


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    """Every test gets throwaway DATA/STATE roots, so nothing can write under $HOME (a monitor test once
    wrote a stall incident to the default ~/.local/state/scrape-harness-example). data_dir uses the same."""
    monkeypatch.setenv("EXAMPLE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


@pytest.fixture(autouse=True)
def no_clipboard(monkeypatch):
    """The darwin build sandbox is off and /usr/bin/pbcopy exists: never touch the real clipboard."""
    monkeypatch.setenv("EX_NO_CLIPBOARD", "1")
    monkeypatch.setenv("EX_NO_NOTIFY", "1")  # no macOS notification from approve in tests


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open an internet socket fails the test (stdio subprocesses are unaffected)."""

    def refuse(*_a, **_k):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


LOOPBACK = ("127.0.0.1", "::1")


@pytest.fixture
def loopback_only(monkeypatch):
    """Opt-in, for tests whose servers run on THIS machine: a connection to 127.0.0.1/::1 is allowed,
    anything else still fails the test. A client that skipped its proxy and dialled a site name itself
    therefore fails too."""

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] in LOOPBACK:
            return _REAL_CONNECT(sock, address)
        raise AssertionError(f"a test tried to open a network connection to {address!r}")

    def create(address, *a, **k):
        if address[0] in LOOPBACK:
            return _REAL_CREATE(address, *a, **k)
        raise AssertionError(f"a test tried to open a network connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create)


_REAL_CONNECT, _REAL_CREATE = socket.socket.connect, socket.create_connection  # before any monkeypatch


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("EXAMPLE_DATA", str(d))
    monkeypatch.setenv("EXAMPLE_STATE", str(tmp_path / "state"))  # never the real ~/.local/state
    monkeypatch.delenv("EX_NETWORK_APPROVED", raising=False)
    return d


@pytest.fixture(autouse=True)
def root_log_handlers_restored():
    """crawlee installs a console handler on a handler-less root logger, bound to THIS test's captured
    stderr; a later test then logs into a closed stream ("--- Logging error ---" on its stderr). Measured
    on main: test_cooldown_clear.py then test_runcard's real-run card test fails. Remove what a test added."""
    import logging

    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for h in [h for h in root.handlers if h not in before]:
        root.removeHandler(h)
