"""awake.hold(): an approved networked run holds the Mac awake until it exits (caffeinate -i -w PID)."""

from __future__ import annotations

import argparse
import os
import sys

import pytest

from scrape_harness import awake, cli


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    fake = tmp_path / "caffeinate"
    fake.write_text("")
    monkeypatch.setattr(awake, "CAFFEINATE", fake)
    monkeypatch.setattr(awake, "_spawn", calls.append)
    monkeypatch.setattr(awake, "_held", set())
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("EX_NO_CAFFEINATE", raising=False)
    return calls


def test_hold_waits_on_this_process_and_keeps_only_the_system_awake(spawned):
    assert awake.hold() is True
    assert spawned == [[str(awake.CAFFEINATE), "-i", "-w", str(os.getpid())]]


def test_hold_is_once_per_process(spawned):
    awake.hold()
    assert awake.hold() is False
    assert len(spawned) == 1


@pytest.mark.parametrize("why", ["linux", "no-binary", "opt-out"])
def test_hold_is_a_no_op_off_macos_without_caffeinate_or_when_opted_out(spawned, monkeypatch, tmp_path, why):
    if why == "linux":
        monkeypatch.setattr(sys, "platform", "linux")
    elif why == "no-binary":
        monkeypatch.setattr(awake, "CAFFEINATE", tmp_path / "absent")
    else:
        monkeypatch.setenv("EX_NO_CAFFEINATE", "1")
    assert awake.hold() is False
    assert spawned == []


class _Net:
    network = True


def test_need_network_holds_only_once_approved(spawned):
    with pytest.raises(SystemExit):
        cli.need_network(argparse.Namespace(approve_network=False), _Net(), "crawl-lists")
    assert spawned == []  # a refusal holds nothing
    cli.need_network(argparse.Namespace(approve_network=True), _Net(), "crawl-lists")
    assert len(spawned) == 1


def test_offline_plugins_never_hold(spawned):
    class Offline:
        network = False

    cli.need_network(argparse.Namespace(approve_network=True), Offline(), "fixtures")
    assert spawned == []
