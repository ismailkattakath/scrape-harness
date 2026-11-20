"""`<cli> refresh --proxy-only TEXT / --proxy-exclude TEXT`: which configured [crawl.proxy] url a NEW refresh
plan picks. The flags filter the pick at plan time; the signed bounds carry the chosen url exactly as before
(the flags themselves are never signed), so a plan without them hashes as it did. A resume keeps its proxy.

Synthetic proxies only (*.example.invalid); nothing here opens a socket: only plans and dry runs."""

import json
from datetime import UTC, datetime

import pytest
from test_approvals import networked, operator_key, runs  # noqa: F401
from test_refresh import chain, plan  # noqa: F401

from scrape_harness import approvals, pipeline, refresh, store
from scrape_harness.cli import main

P1, P2 = "http://p1.example.invalid:8080", "http://p2.example.invalid:8080"


def with_proxies(*urls, choice="round_robin"):
    """[crawl.proxy] in the test pipeline. round_robin with a fresh STATE cursor picks the FIRST url, so a
    no-flag plan's pick is known (and a flag that changes it shows)."""
    p = pipeline.locate()
    block = (f"[crawl.proxy]\nurls = {json.dumps(list(urls))}\nchoice = \"{choice}\"\n"
             'user_secret = "test-user"\npassword_secret = "test-pass"\n\n')  # fmt: skip
    p.write_text(p.read_text().replace("[crawl.refresh]", block + "[crawl.refresh]"))
    pipeline._cached.cache_clear()  # noqa: SLF001
    pipeline._doc.cache_clear()  # noqa: SLF001


class Frozen(datetime):
    """A fixed clock: a refresh plan signs not_before/expires_at, so a literal-hash pin needs one."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(refresh, "datetime", Frozen)


def dry(capsys, *opts):
    capsys.readouterr()
    code = main(["refresh", "--dry-run", *opts])
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def planned(capsys, *opts):
    capsys.readouterr()
    assert main(["refresh", *opts]) == 10
    doc = json.loads(capsys.readouterr().out)
    return doc["plan"], doc["plan_hash"]


# ── the frozen signed surface: a refresh plan without the flags hashes as recorded ───────────────

# Computed from origin/main 1be586e (before the flags existed), in a `git archive` copy, with this file's
# fixtures and clock. A change that moves one of these breaks every pending approval of such a plan (exit 10).
OLD_REFRESH_HASHES = {
    "no proxy": ((), "8a9e8d9cf2f98dda4010fa9f158bbcdf056f50d8419ef7ae4cea86a796896ff8"),
    "one proxy": ((P1,), "ad51becfcedadded88d7ad2771199615f0bcd954206d38f75aca664f67019111"),
    "two proxies": ((P2, P1), "345693ab2c562ae420cf4a75c90b2899ee78af0d53276f7640c19c7d223d032e"),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(OLD_REFRESH_HASHES))
def test_old_refresh_plans_hash_exactly_as_recorded(chain, frozen, capsys, name):  # noqa: F811
    urls, recorded = OLD_REFRESH_HASHES[name]
    if urls:
        with_proxies(*urls)
    _, h = planned(capsys)
    assert h == recorded


def test_a_flag_signs_only_the_url_it_picked(chain, frozen, capsys):  # noqa: F811
    """--proxy-only P2 picks P2 where the no-flag plan picks P1, and the plan is EXACTLY the no-flag plan of a
    pipeline whose pick is P2 (the two-proxy pin): the flag itself is never in the bounds."""
    with_proxies(P1, P2)
    b, h = planned(capsys, "--proxy-only", "p2.")
    assert b["proxy"] == P2 and h == OLD_REFRESH_HASHES["two proxies"][1]
    assert not any("proxy_only" in k or "proxy_exclude" in k for k in b)


# ── the pick ──────────────────────────────────────────────────────────────────────────────────


def test_proxy_only_picks_the_one_match(chain, capsys):  # noqa: F811
    with_proxies(P1, P2)
    for _ in range(3):  # round_robin would alternate; the flag pins it
        code, d, _ = dry(capsys, "--proxy-only", "p2.example")
        assert code == 0 and d["bounds"]["proxy"] == P2


def test_proxy_exclude_never_picks_a_match_and_repeats(chain, capsys):  # noqa: F811
    with_proxies(P1, P2, "http://p3.example.invalid:8080")
    seen = set()
    for _ in range(4):
        code, d, _ = dry(capsys, "--proxy-exclude", "p1.", "--proxy-exclude", "p3.")
        assert code == 0
        seen.add(d["bounds"]["proxy"])
    assert seen == {P2}


@pytest.mark.parametrize(
    ("urls", "opts", "msg"),
    [
        ((P1, P2), ("--proxy-only", "example.invalid"), "--proxy-only example.invalid matches 2 configured"),
        ((P1, P2), ("--proxy-only", "p9."), "--proxy-only p9. matches 0 configured"),
        ((P1, P2), ("--proxy-exclude", "p1.", "--proxy-exclude", "p2."), "leaves no proxy of 2"),
        ((P1,), ("--proxy-exclude", "p1."), "leaves no proxy of 1"),
        ((P1,), ("--proxy-only", "p2."), "matches 0 configured"),
        ((), ("--proxy-only", "p1."), "matches 0 configured"),
        ((P1, P2), ("--proxy-only", "p1.", "--proxy-exclude", "p1."), "leaves no proxy of 2"),
    ],
)
def test_a_flag_that_leaves_no_single_proxy_refuses_before_any_plan(chain, data_dir, capsys, urls, opts, msg):  # noqa: F811
    if urls:
        with_proxies(*urls)
    for extra in ((), ("--dry-run",)):
        capsys.readouterr()
        with pytest.raises(SystemExit) as e:
            main(["refresh", *extra, *opts])
        assert msg in str(e.value)
        assert capsys.readouterr().out == ""
    assert not list(store.state_dir().glob("plans/*")) and refresh.states() == []


def test_one_proxy_and_a_flag_that_keeps_it_plans_as_without(chain, frozen, capsys):  # noqa: F811
    with_proxies(P1)
    assert planned(capsys, "--proxy-only", "p1.")[1] == OLD_REFRESH_HASHES["one proxy"][1]
    assert planned(capsys, "--proxy-exclude", "p2.")[1] == OLD_REFRESH_HASHES["one proxy"][1]


# ── a live approval, and a resume ─────────────────────────────────────────────────────────────


def test_a_live_approval_for_an_excluded_proxy_is_not_used(chain, data_dir, capsys):  # noqa: F811
    """An approved P1 plan is live; --proxy-exclude p1 must not run under it: it plans P2 instead (exit 10).
    A flag that keeps P1 finds that approval as before."""
    with_proxies(P1, P2)
    b, h = planned(capsys)
    assert b["proxy"] == P1 and main(["approve", h]) == 0
    code, d, _ = dry(capsys, "--proxy-exclude", "p1.")
    assert code == 0 and "approval_id" not in d and d["bounds"]["proxy"] == P2
    code, d, _ = dry(capsys, "--proxy-only", "p1.")
    assert code == 0 and d["bounds"]["proxy"] == P1 and d["approval_id"]
    b2, _ = planned(capsys, "--proxy-only", "p2.")
    assert b2["proxy"] == P2


def test_a_resume_keeps_its_recorded_proxy_whatever_the_flags(chain, data_dir, capsys):  # noqa: F811
    with_proxies(P1, P2)
    b, h = planned(capsys)
    assert b["proxy"] == P1 and main(["approve", h]) == 0
    (a,) = [r for r in approvals.chain() if r["kind"] == "bounded"]
    st = refresh._new_state(a, list(b["steps"]))  # noqa: SLF001 - an unfinished run of that approval
    st["status"] = "failed"
    refresh.save_state(st)
    code, d, err = dry(capsys, "--proxy-exclude", "p1.")
    assert code == 0 and d["resumes"] == st["run_id"] and d["bounds"]["proxy"] == P1
    assert "keeps its signed proxy" in err


def test_the_flags_are_in_the_refresh_help(capsys):
    with pytest.raises(SystemExit):
        main(["refresh", "--help"])
    out = capsys.readouterr().out
    assert "--proxy-only" in out and "--proxy-exclude" in out
