"""Test-only consumer code for `<cli> refresh` (tests/test_refresh.py), declared in a test pipeline.toml the
way a consumer declares its own (python_path + [plugins.drivers] + [plugins].commands), so the refresh's
step SUBPROCESSES find it too. Nothing here opens a socket.

- NetFixture: the offline fixture backend, but declared networked, so every gate applies (the approval,
  --approve-network, the egress preflight) while it still sends nothing.
- geocode: a stand-in PAID command. --dry-run prints {"pending": $TEST_GEOCODE_PENDING}; a real run
  appends what it was allowed to DATA/geocode.log and prints {"geocoded": N}.
- EndSite: the example source with an end marker declared (`end_selectors`), so a walk step may enter a
  refresh (ADR-0017 Migration step 5); a monkeypatch would never reach the step subprocesses.
- export-db: a stand-in paid_exempt command that prints a digest; $TEST_EXPORT_FAIL=1 makes it fail once
  (exit 1) so a resume can be tested.
"""

from __future__ import annotations

import json
import os

from example_site.source import ExampleSite

from scrape_harness import cli, store
from scrape_harness.drivers.fixture import FixtureBackend


class NetFixture(FixtureBackend):
    network = True


class EndSite(ExampleSite):
    end_selectors = ("#no-results",)  # the synthetic past-end template's marker (tests/test_refresh_walks.py)


def _log(name: str, text: str) -> None:
    with (store.data_dir() / name).open("a") as f:
        f.write(text + "\n")


def geocode(args) -> int:
    pending = int(os.environ.get("TEST_GEOCODE_PENDING", "0"))
    if args.dry_run:
        cli.card("geocode", "DRY RUN: count the pending geocodes")
        print(json.dumps({"pending": pending}))
        return 0
    _log(
        "geocode.log",
        json.dumps({"max_requests": args.max_requests, "approve_network": args.approve_network}),
    )
    print(
        json.dumps(
            {"geocoded": min(pending, args.max_requests if args.max_requests is not None else pending)}
        )
    )
    return 0


def export_db(args) -> int:
    flag = store.data_dir() / "export-failed-once"
    if os.environ.get("TEST_EXPORT_FAIL") == "1" and not flag.exists():
        flag.write_text("x")
        print("export-db: simulated failure", flush=True)
        return 1
    _log("export.log", "exported")
    print(json.dumps({"rows": 3, "digest": "sha256:" + "d" * 64}))
    return 0


def register(sub) -> None:
    g = sub.add_parser("geocode", help="test: a paid stand-in")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--max-requests", type=int)
    g.add_argument("--approve-network", action="store_true")
    g.set_defaults(fn=geocode)
    e = sub.add_parser("export-db", help="test: a paid_exempt stand-in")
    e.add_argument("--approve-network", action="store_true")
    e.set_defaults(fn=export_db)
