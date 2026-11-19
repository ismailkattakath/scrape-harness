"""The run card: one stage list, a SQUARE grid table, real values only, clipboard (mocked), clean stdout."""

import io
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
from conftest import FIXTURES
from wcwidth import wcswidth

from scrape_harness import crawl, pipeline, runcard
from scrape_harness.cli import main

FIX = [
    "--driver",
    "fixture",
    "--fixtures",
    str(FIXTURES),
    "--max-tasks-per-minute",
    "6000",
    "--approve-politeness-override",
]
PLACEHOLDER = re.compile(r"<[^<>\n]*>")
RID = "2026-01-15T12:00:00Z-fetch-ads-desktop-abc123"


class Tty(io.StringIO):
    def isatty(self):
        return True


def card(tmp_path, **kw):
    log = str(tmp_path / "DATA" / "runs" / "2026-01-15T12_00_00Z-fetch-ads-desktop-abc123.log.jsonl")
    return runcard.make(
        "fetch-ads",
        "fetch 600 ad pages (depth-stratified sample, seed 42, equal allocation)",
        RID,
        ctx={
            "queue": "2026-01-15t12-00-00z-fetch-ads-desktop-abc123",
            "site": "example.invalid",
            "ads": 600,
            "sample_file": "2026-01-15T12_00_00Z-fetch-ads-desktop-abc123",
        },
        roots={"DATA": tmp_path / "DATA", "STATE": tmp_path / "STATE"},
        planned={"ad": 600, "robots": 1},
        per_minute=7,
        variant="desktop",
        backend="browser",
        plan_hash="a" * 64,
        approval_id="0123456789abcdef",
        egress={"loc": "CA", "colo": "YYZ"},
        log_abs=log,
        **kw,
    )


def test_stage_numbers_come_from_the_pipeline():
    for i, name in enumerate(runcard.stages(), 1):
        assert runcard.stage(name) == f"{i}/{len(runcard.stages())}"
    assert runcard.stage("ab") == runcard.stage("migrate") == "utility"


def test_the_rows_the_operator_asked_for(tmp_path):
    names = [k for k, _ in runcard.rows(card(tmp_path))]
    assert names == [
        "Run",
        "What",
        "Stage",
        "Roots",
        "Inputs",
        "Outputs",
        "Scale",
        "ETA",
        "Triggered by",
        "Approval",
        "Egress",
        "Live log",
        "Follow it",
    ]


@pytest.mark.parametrize("width", [80, 120])
def test_every_line_of_the_grid_has_the_same_display_width(tmp_path, monkeypatch, width):
    monkeypatch.delenv("NO_COLOR", raising=False)
    text = re.sub(r"\x1b\[[0-9;]*m", "", runcard.render(card(tmp_path), Tty(), width=width))
    lines = [ln for ln in text.splitlines() if ln.strip() and "RUN CARD" not in ln]  # the title sits above
    widths = {wcswidth(ln) for ln in lines}
    assert len(widths) == 1 and widths.pop() <= width  # borders cannot misalign
    assert lines[0].startswith("┌") and lines[-1].startswith("└") and any(ln.startswith("├") for ln in lines)
    assert "RUN CARD" in text and "Field" in text and "Value" in text


def test_no_placeholders_in_the_card_or_its_json(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    c = card(tmp_path)
    for text in (runcard.render(c, Tty(), width=120), runcard.render(c, io.StringIO()), json.dumps(c)):
        assert not PLACEHOLDER.search(text), PLACEHOLDER.search(text)
    assert "DATA:raw/example.invalid/ (600 ad snapshots)" in c["outputs"]
    assert c["live_log"]["tail"] == f"tail -f {shlex.quote(c['live_log']['path'])} | jq -c ."
    assert Path(c["live_log"]["path"]).is_absolute()


def test_plan_and_dry_run_cards_have_no_placeholders_either(data_dir, capsys):
    capsys.readouterr()
    main(
        ["crawl-lists", "--subcategory", "x", "--stride", "2", "--seed", "1", "--plan", "--driver", "browser"]
    )
    main(["fetch-ads", "--sample", "2", "--seed", "1", "--dry-run"])
    err = capsys.readouterr().err
    assert "RUN CARD" in err and not PLACEHOLDER.search(err)


def test_plain_rendering_without_a_tty_or_with_no_color(tmp_path, monkeypatch):
    text = runcard.render(card(tmp_path), io.StringIO())
    assert text.startswith("== RUN CARD ==") and "┌" not in text and "Stage: 2/3" in text
    monkeypatch.setenv("NO_COLOR", "1")
    assert runcard.render(card(tmp_path), Tty()).startswith("== RUN CARD ==")


def test_clipboard_gets_exactly_the_tail_line(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("EX_BUILD_REV", "test")  # no git call to muddle the mocked subprocess
    monkeypatch.delenv("EX_NO_CLIPBOARD", raising=False)
    monkeypatch.setattr(runcard.shutil, "which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    monkeypatch.setattr(runcard.subprocess, "run", lambda argv, **kw: calls.append((argv, kw["input"])))
    c = card(tmp_path)
    assert calls == [(["/usr/bin/pbcopy"], c["live_log"]["tail"])]
    assert c["live_log"]["copied"] is True and "(copied to clipboard)" in runcard.render(c, io.StringIO())


def test_li_no_clipboard_suppresses_it(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("EX_BUILD_REV", "test")
    monkeypatch.setenv("EX_NO_CLIPBOARD", "1")
    monkeypatch.setattr(runcard.shutil, "which", lambda name: "/usr/bin/pbcopy")
    monkeypatch.setattr(runcard.subprocess, "run", lambda *a, **k: calls.append(a))
    assert card(tmp_path)["live_log"]["copied"] is False and calls == []


def test_parent_is_claude_under_claude_code(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert runcard.parent_process() == "claude"


def test_a_real_run_card_names_real_existing_paths(data_dir, tmp_path, capsys):
    capsys.readouterr()
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    out, err = capsys.readouterr()
    assert all(json.loads(x) for x in out.splitlines()) and "RUN CARD" in err and not PLACEHOLDER.search(err)
    run = json.loads(next((data_dir / "runs").glob("*.json")).read_text())
    c = run["card"]
    assert c["stage"] == "1/3" and not PLACEHOLDER.search(json.dumps(c))
    roots = {k: Path(v) for k, v in c["roots"].items()}
    for entry in c["outputs"]:
        root, rel = entry.split(":", 1)
        assert (roots[root] / rel).exists(), entry  # every output the card names is a real path
    assert Path(c["live_log"]["path"]) == data_dir / run["log_ref"]


def test_offline_stages_print_a_card_too(data_dir, capsys):
    assert main(["reparse"]) == 0
    assert "Stage: 3/3" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["dry-run", "plan"])
def test_dry_run_and_plan_cards_say_would_not_none(tmp_path, mode):
    c = runcard.make(
        "fetch-ads", "DRY RUN", None, mode=mode, roots={"DATA": tmp_path}, planned={"ad": 4}, per_minute=7
    )
    r = dict(runcard.rows(c))
    assert r["Scale"] == "would send 4 requests (ad 4) at 7/min, one at a time; none sent now"
    assert r["ETA"] == "would take ~0 min 34 s at 7/min"  # 4 requests at 7/min = 34.3 s
    assert "none (no planned requests)" not in json.dumps(r)


def test_a_real_dry_run_card_is_consistent(data_dir, capsys):
    from scrape_harness import store

    store.append(
        "observations",
        [
            {
                "schema": "observation.v1",
                "ad_id": "1000001",
                "site": "example.invalid",
                "seen_at": crawl.utcnow(),  # fresh: an old sighting is dormant, never planned
                "ad_url": "https://www.example.invalid/listings/widgets/sample-region/synthetic_ad-1000001",
                "list": {
                    "url": "https://www.example.invalid/listings/widgets/sample-region?page=1",
                    "subcategory": "widgets",
                    "page": 1,
                    "page_count": 3,
                    "position": 1,
                    "sponsored": False,
                    "sample_weight": None,
                },
                "card": {"title": "t", "city_label": None},
                "bumped_at": None,
                "card_hash": "sha256:0000000000000001",
            }
        ],
    )
    capsys.readouterr()
    assert main(["fetch-ads", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert (
        "would send 2 requests (ad 1, robots 1)" in err and "none sent now" in err and "would take ~" in err
    )
    assert "none (no planned requests)" not in err


def test_the_copied_tail_line_survives_spaces_apostrophes_and_dollars(tmp_path, monkeypatch):
    """The DATA root lives under "My Drive/05 Projects"; the line must resolve to the real file in a shell."""
    calls = []
    monkeypatch.setenv("EX_BUILD_REV", "test")
    monkeypatch.delenv("EX_NO_CLIPBOARD", raising=False)
    monkeypatch.setattr(runcard.shutil, "which", lambda name: "/usr/bin/pbcopy")
    real_run = subprocess.run
    monkeypatch.setattr(runcard.subprocess, "run", lambda argv, **kw: calls.append(kw["input"]))
    log = tmp_path / "My Drive" / "05 Projects" / "it's $HOME" / "runs" / "x.log.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("{}\n")
    runcard.make("fetch-ads", "t", "r", log_abs=str(log))
    (copied,) = calls
    assert copied.startswith("tail -f ") and copied.endswith(" | jq -c .")
    as_ls = "ls " + copied[len("tail -f ") : -len(" | jq -c .")]  # tail swapped for ls, the jq pipe dropped
    out = real_run(
        ["sh", "-c", as_ls],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "HOME": "/nonexistent",
            "PATH": os.environ["PATH"],
        },  # keep PATH: the Linux sandbox has no /bin/ls
    )
    assert out.returncode == 0 and out.stdout.strip() == str(log)  # $HOME was NOT expanded, ' did not break


def test_a_flag_approved_stage_and_an_unrated_plan(tmp_path):
    """Ported from listing-insights ca12297: a stage approved by flag (not by a signed plan) says so, and
    planned requests with no rate set say the ETA is unknown instead of 'none'."""
    c = runcard.make(
        "fetch-ads",
        "t",
        None,
        roots={"DATA": tmp_path},
        planned={"ad": 3},
        approval="--approve-network (operator-approved run)",
        backend="http",
    )
    r = dict(runcard.rows(c))
    assert r["Approval"] == "--approve-network (operator-approved run)"
    assert r["ETA"] == "unknown (requests planned, no rate set)"


def _overlay_consumer(tmp_path, monkeypatch):
    """The example pipeline with site_overlay, whose site file adds a code dir OUTSIDE the project."""
    from conftest import ROOT

    proj = tmp_path / "proj"
    proj.mkdir()
    base = (
        (ROOT / "pipeline.toml").read_text().replace("[consumer]\n", "[consumer]\nsite_overlay = true\n", 1)
    )
    (proj / "pipeline.toml").write_text(base)
    sd = tmp_path / "sitedir"
    (sd / "code" / "pkg" / "__pycache__").mkdir(parents=True)
    (sd / "code" / "pkg" / "a.py").write_text("A = 1\n")
    (sd / "code" / "pkg" / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"junk")
    (sd / "site.toml").write_text('[pipeline.plugins]\npython_path = ["code"]\n')
    monkeypatch.setenv("EX_SITE_CONFIG", str(sd / "site.toml"))
    monkeypatch.setenv(pipeline.ENV, str(proj / "pipeline.toml"))
    return sd / "code"


def test_the_card_carries_the_sha_of_overlay_code_outside_the_repo(tmp_path, monkeypatch):
    code = _overlay_consumer(tmp_path, monkeypatch)
    card = runcard.make("reparse", "x")
    sc = card["triggered_by"]["site_code"]
    assert list(sc) == [str(code.resolve())] and len(sc[str(code.resolve())]) == 64
    row = dict(runcard.rows(card))["Site code"]
    assert row == f"{code.resolve()}  sha256 {sc[str(code.resolve())][:16]}"
    (code / "pkg" / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"other junk")  # bytecode: no effect
    assert runcard.make("reparse", "x")["triggered_by"]["site_code"] == sc
    (code / "pkg" / "a.py").write_text("A = 2\n")  # one changed byte of code: a different sha
    assert runcard.make("reparse", "x")["triggered_by"]["site_code"] != sc


def test_without_overlay_code_the_card_is_unchanged():
    card = runcard.make("reparse", "x")
    assert "site_code" not in card["triggered_by"] and "Site code" not in dict(runcard.rows(card))
