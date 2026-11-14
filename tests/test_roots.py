"""DATA (synced) vs STATE (local) roots, the cloud-folder refusal, and `li migrate` (copy-only)."""

import hashlib
import json

import pytest
from conftest import FIXTURES

from scrape_harness import store
from scrape_harness.cli import EXIT_CONFIG, main

FIX = [
    "--driver",
    "fixture",
    "--fixtures",
    str(FIXTURES),
    "--max-tasks-per-minute",
    "6000",
    "--approve-politeness-override",
]


def test_state_lives_in_its_own_root(data_dir, tmp_path):
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    state = tmp_path / "state"
    assert (state / "crawlee").is_dir() and not (data_dir / "crawlee").exists()  # queues: state
    assert (data_dir / "observations.jsonl").exists() and (data_dir / "runs").is_dir()  # data, incl. run logs
    assert not (state / "observations.jsonl").exists()


@pytest.mark.parametrize(
    "bad",
    [
        "Library/CloudStorage/GoogleDrive-x/My Drive/li",
        "Google Drive/li",
        "Dropbox/li",
        "OneDrive - Org/li",
        "Library/Mobile Documents/com~apple~CloudDocs/li",
    ],
)
def test_state_in_a_cloud_folder_is_refused(data_dir, tmp_path, monkeypatch, bad):
    monkeypatch.setenv("EXAMPLE_STATE", str(tmp_path / "home" / bad))
    with pytest.raises(store.DataDirError, match="cloud-sync"):
        store.state_dir()


def test_data_in_a_cloud_folder_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_DATA", str(tmp_path / "Library/CloudStorage/GoogleDrive-x/My Drive/li"))
    monkeypatch.setenv("EXAMPLE_STATE", str(tmp_path / "local-state"))
    assert store.data_dir().is_dir() and store.state_dir().is_dir()


@pytest.mark.parametrize("state", ["same", "inside"])
def test_state_equal_to_or_inside_data_is_refused(data_dir, monkeypatch, state):
    monkeypatch.setenv("EXAMPLE_STATE", str(data_dir if state == "same" else data_dir / "state"))
    with pytest.raises(store.DataDirError):
        store.state_dir()


def test_default_state_root_is_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("EXAMPLE_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert store.default_state_dir() == tmp_path / "xdg" / "scrape-harness-example"


def old_root(tmp_path):
    """A synthetic pre-split single root: data files and state dirs side by side."""
    old = tmp_path / "old"
    (old / "approvals").mkdir(parents=True)
    (old / "approvals" / "ledger.jsonl").write_text('{"synthetic": 1}\n')
    (old / "approvals" / "ledger.jsonl").chmod(0o600)
    (old / "browser-profile" / "Default").mkdir(parents=True)
    (old / "browser-profile" / "Default" / "Preferences").write_text("{}")
    (old / "browser-profile" / "SingletonLock").symlink_to("synthetic-host-123")
    (old / "raw" / "example.invalid" / "1").mkdir(parents=True)
    (old / "raw" / "example.invalid" / "1" / "x.html.gz").write_bytes(b"\x1f\x8bsynthetic")
    (old / "observations.jsonl").write_text('{"o": 1}\n')
    (old / "import" / "phone").mkdir(parents=True)
    (old / "import" / "phone" / "README").write_text("synthetic import\n")
    for junk in (".DS_Store", "raw/._x.html.gz", "import/Icon\r"):
        (old / junk).write_bytes(b"finder litter")
    return old


def tree(root):
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() and not p.is_symlink() else "link")
        for p in sorted(root.rglob("*"))
        if not p.is_dir() or p.is_symlink()
    }


def test_migrate_dry_run_changes_nothing(tmp_path, capsys):
    old = old_root(tmp_path)
    before = tree(old)
    assert (
        main(
            [
                "migrate",
                "--from",
                str(old),
                "--to-data",
                str(tmp_path / "D"),
                "--to-state",
                str(tmp_path / "S"),
                "--dry-run",
            ]
        )
        == 0
    )
    assert tree(old) == before and not (tmp_path / "D").exists() and not (tmp_path / "S").exists()
    lines = capsys.readouterr().out.splitlines()
    rows = [json.loads(x) for x in lines]  # every stdout line is JSON
    summary = rows.pop()
    assert summary["summary"] is True and summary["actions"]["skip:os_junk"] == 3
    roots = {r["from"]: r.get("root") for r in rows}
    junk = {r["from"] for r in rows if r.get("reason") == "os_junk"}
    assert junk == {".DS_Store", "raw/._x.html.gz", "import/Icon\r"}
    assert (
        roots["approvals/ledger.jsonl"] == "state" and roots["browser-profile/Default/Preferences"] == "state"
    )
    assert roots["observations.jsonl"] == "data" and roots["import/phone/README"] == "data"
    assert roots["browser-profile/SingletonLock"] is None  # a live lock is skipped, not copied


def test_migrate_copies_with_matching_hashes_and_never_moves(tmp_path, capsys):
    old = old_root(tmp_path)
    before = tree(old)
    D, S = tmp_path / "D", tmp_path / "S"
    assert main(["migrate", "--from", str(old), "--to-data", str(D), "--to-state", str(S)]) == 0
    assert tree(old) == before  # the source is untouched: copied, never moved
    rows = [json.loads(x) for x in capsys.readouterr().out.splitlines()][:-1]
    for r in rows:
        if r["action"] == "copy":
            assert (
                r["verified"] and hashlib.sha256((tmp_path / r["to"]).read_bytes()).hexdigest() == r["sha256"]
            )
    assert (S / "approvals" / "ledger.jsonl").stat().st_mode & 0o777 == 0o600  # modes kept
    assert (D / "raw" / "example.invalid" / "1" / "x.html.gz").exists() and not (D / "approvals").exists()
    # a second run finds everything identical and copies nothing
    assert main(["migrate", "--from", str(old), "--to-data", str(D), "--to-state", str(S)]) == 0
    second = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert second[-1]["summary"] is True
    assert all(r["action"] in ("exists-identical", "skip") for r in second[:-1])
    names = {p.name for root in (D, S) for p in root.rglob("*")}
    assert not names & {".DS_Store", "Icon\r"} and not any(
        n.startswith("._") for n in names
    )  # junk never copied


def test_migrate_refuses_a_cloud_state_and_conflicts(tmp_path, capsys):
    old = old_root(tmp_path)
    assert (
        main(
            [
                "migrate",
                "--from",
                str(old),
                "--to-data",
                str(tmp_path / "D"),
                "--to-state",
                str(tmp_path / "Dropbox" / "S"),
            ]
        )
        == EXIT_CONFIG
    )
    D = tmp_path / "D"
    (D).mkdir()
    (D / "observations.jsonl").write_text("different\n")
    assert main(["migrate", "--from", str(old), "--to-data", str(D), "--to-state", str(tmp_path / "S")]) == 2
    assert (D / "observations.jsonl").read_text() == "different\n"  # never overwritten
    assert not (tmp_path / "S").exists()  # a conflict copies nothing at all


def test_old_single_root_prints_a_hint_and_moves_nothing(tmp_path, monkeypatch, capsys):
    old = old_root(tmp_path)
    monkeypatch.setenv("EXAMPLE_DATA", str(old))
    monkeypatch.delenv("EXAMPLE_STATE", raising=False)
    monkeypatch.setattr(store, "_hinted", False)
    before = tree(old)
    store.data_dir()
    assert "examplesite migrate" in capsys.readouterr().err and tree(old) == before


def test_migrate_stdout_is_pure_jsonl_even_when_piped(tmp_path):
    """The summary used to go to stderr and could land mid-line with 2>&1; now one stream, one line each."""
    import os
    import subprocess
    import sys

    old = old_root(tmp_path)
    for i in range(300):  # enough manifest to exceed a pipe buffer
        (old / "raw" / f"f{i:03}.txt").write_text("x" * 50)
    env = {**os.environ, "EXAMPLE_STATE": str(tmp_path / "unused-state")}
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "scrape_harness.cli",
            "migrate",
            "--from",
            str(old),
            "--to-data",
            str(tmp_path / "D"),
            "--to-state",
            str(tmp_path / "S"),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    lines = r.stdout.splitlines()
    parsed = [json.loads(x) for x in lines]  # every stdout line parses
    assert parsed[-1]["summary"] is True and sum(1 for p in parsed if "summary" in p) == 1
    assert r.returncode == 0 and len(parsed) > 300
