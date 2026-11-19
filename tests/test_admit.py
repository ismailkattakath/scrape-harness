"""Queued requests are admitted through the same gate as a fresh plan (ADR-0017 §B.5), and an independent
postcondition refuses (exit 14) any plan that names an ad the records say must not get a view (§B.6).
Exact GET counts from the fixture client; every record here is SYNTHETIC."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIXTURES
from example_site.source import ExampleSite
from hypothesis import given, settings
from hypothesis import strategies as st
from test_approvals import networked, operator_key  # noqa: F401 - fixtures
from test_fresh_gate import card_obs
from test_gone_redirect import FAST, WALL

from scrape_harness import cli, crawl, plan, policy, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureHttpClient

POL = policy.DEFAULT
SITE = "example.invalid"
OLD = "2026-01-15T11:58:00Z"


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> str:
    return iso(datetime.now(UTC))


def folder(tmp_path, mapping: dict):
    d = tmp_path / f"fx{len(list(tmp_path.glob('fx*')))}"
    d.mkdir()
    for f in FIXTURES.iterdir():
        (d / f.name).write_bytes(f.read_bytes())
    (d / "wall.html").write_text(WALL)
    (d / "urls.json").write_text(json.dumps(json.loads((FIXTURES / "urls.json").read_text()) | mapping))
    return d


def fix(d):
    return ["--driver", "fixture", "--fixtures", str(d), "--max-tasks-per-minute", "6000",
            "--approve-politeness-override"]  # fmt: skip


def ad_gets():
    return [u for u in FixtureHttpClient.log if "synthetic_ad-" in u]


def req(o, reason="new"):
    return {"url": o["ad_url"], "kind": "ad",
            "meta": {"reason": reason, "card_city_label": None, "ad_id": o["ad_id"], "site": SITE}}  # fmt: skip


@pytest.fixture
def world(data_dir, tmp_path):
    """Four queued ads of a stopped fetch-ads run (its first GET met a wall, so all four stay pending):
    a is gone since, b was fetched since, c is fresh and alive, d was only sighted long ago (dormant)."""
    a, b, c = (card_obs(i, now()) for i in range(3))
    d = card_obs(3, OLD)
    store.append("observations", [a, b, c, d])
    fx = folder(tmp_path, {a["ad_url"]: "wall.html"})
    old = crawl.Run.new("fetch-ads", "desktop", SITE)
    with pytest.raises(crawl.Stop):
        crawl.run(old, ExampleSite(), lambda m, r: None, FAST, [req(o) for o in (a, b, c, d)],
                  {"backend": "fixture", "root": str(fx)})  # fmt: skip
    assert len(crawl.pending_requests(crawl.Run.load(old.doc["run_id"]))) == 4
    crawl.event("ADS1", "gone", a["ad_url"], "expired_notice", site=SITE, ad_id=a["ad_id"])
    html = (FIXTURES / "ad-1000001.html").read_text()
    store.append(
        "listings", [ExampleSite().parse_ad(html, url=b["ad_url"], fetched_at=now(), fetch_reason="new")]
    )
    ok = folder(tmp_path, {o["ad_url"]: "ad-1000001.html" for o in (a, b, c, d)})
    FixtureHttpClient.log.clear()
    return {"old": old.doc["run_id"], "ads": (a, b, c, d), "fx": ok}


# ── plan.admit: pure ───────────────────────────────────────────────────────────────────────────────


def test_admit_keeps_only_eligible_ads_not_fetched_since_and_once():
    a, b, c = (card_obs(i, "2026-01-15T11:00:00Z") for i in range(3))
    cands = [{"reason": "new", "obs": a, "fetched_at": None},
             {"reason": "card_changed", "obs": b, "fetched_at": "2026-01-15T12:30:00Z"}]  # fmt: skip
    legacy = {"url": a["ad_url"], "kind": "ad", "meta": {"reason": "new"}}  # before meta carried ad_id
    kept, dropped = plan.admit([legacy, req(a), req(b), req(c)], cands, since="2026-01-15T12:00:00Z")
    assert [(r["url"], r["meta"]["reason"], r["meta"]["ad_id"]) for r in kept] == [
        (a["ad_url"], "new", a["ad_id"])
    ]
    assert dropped == {"ineligible": 1, "fetched_since": 1, "duplicate": 1}
    # a never-fetched ad keeps the queue's reason (a recruit stays a recruit); a due revisit takes the current one
    kept, _ = plan.admit([req(a, "recruit"), req(b, "new")], cands, since="2026-01-15T13:00:00Z")
    assert [r["meta"]["reason"] for r in kept] == ["recruit", "card_changed"]


# ── --supersede and crawl resume go through it ──────────────────────────────────────────────────────


def test_supersede_takes_only_the_admitted_ads(world, capsys):
    a, b, c, d = world["ads"]
    capsys.readouterr()
    assert main(["fetch-ads", "--supersede", world["old"], "--dry-run", *fix(world["fx"])]) == 0
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [x["url"] for x in out if "url" in x] == [c["ad_url"]]
    assert main(["fetch-ads", "--supersede", world["old"], *fix(world["fx"])]) == 0
    assert ad_gets() == [c["ad_url"]]


def test_resume_of_a_fetch_ads_run_is_a_filtered_new_run(world, data_dir):
    a, b, c, d = world["ads"]
    assert main(["crawl", "resume", "--run-id", world["old"], *fix(world["fx"])]) == 0
    assert ad_gets() == [c["ad_url"]]
    docs = [json.loads(p.read_text()) for p in (data_dir / "runs").glob("*fetch-ads*.json")]
    (new,) = [x for x in docs if x.get("supersedes") == world["old"]]
    assert new["status"] == "completed" and new["requests"]["ad_pages"] == 1
    assert crawl.Run.load(world["old"]).doc["status"] == "stopped"  # the old run's queue is never re-sent
    assert main(["crawl", "resume", "--run-id", world["old"], *fix(world["fx"])]) == crawl.EXIT_PARSE  # once


def test_resume_refuses_an_aborted_fetch_ads_run(world):
    crawl.correct_status(world["old"], "aborted", "interrupted", "synthetic")
    assert main(["crawl", "resume", "--run-id", world["old"], *fix(world["fx"])]) == crawl.EXIT_PARSE
    assert ad_gets() == []


def test_resume_refuses_a_fetch_ads_run_older_than_the_window(world, monkeypatch):
    later = datetime.now(UTC) + timedelta(hours=POL.window_h + 1)
    monkeypatch.setattr(cli, "_now", lambda: later)
    assert main(["crawl", "resume", "--run-id", world["old"], *fix(world["fx"])]) == crawl.EXIT_PARSE
    assert ad_gets() == []


def test_a_signed_supersede_reproduces_its_as_of_29_minutes_later(world, networked, capsys, monkeypatch):  # noqa: F811
    c = world["ads"][2]
    t0 = datetime.now(UTC)
    monkeypatch.setattr(cli, "_now", lambda: t0)
    capsys.readouterr()
    assert main(["fetch-ads", "--supersede", world["old"], "--plan", *fix(world["fx"])]) == 0
    p = json.loads(capsys.readouterr().out)
    assert p["plan"]["ads"] == [["new", c["ad_url"]]] and p["plan"]["as_of"] and p["plan"]["policy_digest"]
    assert main(["approve", p["plan_hash"]]) == 0
    monkeypatch.setattr(cli, "_now", lambda: t0 + timedelta(minutes=29))
    assert main(["fetch-ads", "--supersede", world["old"], "--approve-network", *fix(world["fx"])]) == 0
    assert ad_gets() == [c["ad_url"]]


# ── the postcondition: independent of the selector ──────────────────────────────────────────────────


def obs_at(i, seen_at):
    return card_obs(i, seen_at)


def listing(o, fetched_at, reason="new"):
    return {"ad_id": o["ad_id"], "fetched_at": fetched_at, "fetch_reason": reason,
            "source": {"site": SITE, "url": o["ad_url"]}}  # fmt: skip


def gone(o, at, reason="served_list", run_id="R1", with_id=True):
    e = {"schema": "event.v1", "at": at, "run_id": run_id, "kind": "gone", "url": o["ad_url"], "reason": reason,
         "until": None, "site": SITE}  # fmt: skip
    return e | ({"ad_id": o["ad_id"]} if with_id else {})


def planned(*os):
    return [{"site": o["site"], "ad_id": o["ad_id"], "url": o["ad_url"]} for o in os]


T = "2026-01-15T12:00:00Z"


def check(plans, obs, listings=(), events=(), as_of=T):
    return plan.assert_admissible(plans, obs, listings, events, as_of, POL)


def test_the_postcondition_passes_a_fresh_new_ad():
    o = obs_at(0, "2026-01-15T11:00:00Z")
    assert check(planned(o), [o]) == []


@pytest.mark.parametrize("with_id", [True, False])  # a gone joined by ad_id, or by url (older events)
def test_the_postcondition_refuses_a_gone_ad(with_id):
    o = obs_at(0, "2026-01-15T10:00:00Z")
    (v,) = check(planned(o), [o], events=[gone(o, "2026-01-15T11:00:00Z", with_id=with_id)])
    assert "gone" in v


def test_the_postcondition_refuses_a_stale_sighting_a_fresh_fetch_and_a_duplicate():
    stale = obs_at(0, "2026-01-01T00:00:00Z")
    fetched = obs_at(1, "2026-01-15T10:00:00Z")
    fresh = obs_at(2, "2026-01-15T10:00:00Z")
    out = check(planned(stale, fetched, fresh, fresh), [stale, fetched, fresh],
                listings=[listing(fetched, "2026-01-15T10:30:00Z")])  # fmt: skip
    assert len(out) == 3 and any("window" in v for v in out) and any("fetched" in v for v in out)
    assert any("twice" in v for v in out)


def test_the_postcondition_lets_a_re_admitted_ad_and_a_voided_gone_through():
    o, p = obs_at(0, "2026-01-14T00:00:00Z"), obs_at(1, "2026-01-15T10:00:00Z")
    re_seen = obs_at(0, "2026-01-15T11:00:00Z")  # 35 h after the gone: a re-admission
    void = {"schema": "event.v1", "at": T, "run_id": "R2", "kind": "run_correction", "url": None, "reason": None,
            "until": None, "site": SITE, "correction": {"field": "gone", "recorded": {}, "reconciled": {"void": True},
                                                        "note": "synthetic", "evidence": []}}  # fmt: skip
    events = [gone(o, "2026-01-14T00:00:00Z"), gone(p, "2026-01-15T10:30:00Z", run_id="R2"), void]
    assert check(planned(re_seen, p), [o, re_seen, p], events=events) == []


def test_a_selector_bug_is_refused_exit_14_before_any_get(data_dir, tmp_path, monkeypatch):
    a, b = card_obs(0, now()), card_obs(1, now())
    store.append("observations", [a, b])
    crawl.event("ADS1", "gone", a["ad_url"], "expired_notice", site=SITE, ad_id=a["ad_id"])
    real = plan.select_ads
    monkeypatch.setattr(plan, "select_ads", lambda cands, budget, pol: ([("new", a), *real(cands, budget, pol)[0]],
                                                                        {"new": 2, "revisit": 0}))  # fmt: skip
    fx = folder(tmp_path, {o["ad_url"]: "ad-1000001.html" for o in (a, b)})
    FixtureHttpClient.log.clear()
    assert main(["fetch-ads", *fix(fx)]) == crawl.EXIT_PARSE
    assert ad_gets() == [] and not list((data_dir / "runs").glob("*fetch-ads*"))


# ── property: everything the gate admits passes the postcondition ───────────────────────────────────

BASE = datetime(2026, 1, 15, 12, tzinfo=UTC)
KINDS = ["seen", "seen_aborted", "fetch", "reparse", "gone_weak", "gone_positive", "void", "quarantine"]
event_st = st.tuples(
    st.integers(0, 2),  # ad
    st.sampled_from(KINDS),
    st.integers(-24 * 5, 0),  # hours before as_of: mostly inside the 72 h window, some just outside
)


def correction_of(run_id, reconciled, at):
    return {"schema": "event.v1", "at": at, "run_id": run_id, "kind": "run_correction", "url": None,
            "reason": None, "until": None, "site": SITE,
            "correction": {"field": "gone", "recorded": {}, "reconciled": reconciled, "note": "synthetic",
                           "evidence": []}}  # fmt: skip


@settings(max_examples=300, deadline=None)
@given(st.lists(event_st, max_size=16), st.integers(0, 6))
def test_property_the_gate_and_the_postcondition_agree(evs, budget):
    """A false positive of the postcondition is terminal in a refresh, so: whatever the gate admits passes.
    The world mixes good and aborted crawls, OK fetches and reparses, weak and positive gones from two runs,
    and voids and weak-scoped quarantines of those runs."""
    observations, listings, events = [], [], []
    runs = [{"run_id": "BAD", "command": "crawl-lists", "status": "aborted"}]
    for ad, kind, h in sorted(evs, key=lambda x: x[2]):
        at = iso(BASE + timedelta(hours=h))
        o = card_obs(ad, at) | {"card_hash": f"sha256:{(h // 48) % 2:016x}"}
        run = "R1" if h % 2 else "R2"
        if kind == "seen":
            observations.append(o)
        elif kind == "seen_aborted":
            observations.append(o | {"crawl_id": "BAD"})
        elif kind in ("fetch", "reparse"):
            listings.append(listing(o, at, "new" if kind == "fetch" else "reparse"))
        elif kind.startswith("gone"):
            events.append(gone(o, at, "served_list" if kind == "gone_weak" else "expired_notice", run_id=run))
        elif kind == "void":
            events.append(
                correction_of(run, {"void": True, "scope": "weak"} if h % 3 else {"void": True}, at)
            )
        else:
            events.append(correction_of(run, {"quarantine": True, "scope": "weak"}, at))
    recs = (observations, listings, events, runs)
    pool, _, as_of = cli.fetch_pool(recs, POL, iso(BASE))
    todo, _ = plan.select_ads(pool, budget, POL)
    plans = [{"site": o["site"], "ad_id": o["ad_id"], "url": o["ad_url"]} for _, o in todo]
    assert plan.assert_admissible(plans, observations, listings, events, as_of, POL, budget) == []


# ── the revisit lane's HARD cap holds on every path, and the postcondition checks it ────────────────


def revisit_heavy(n_rev=30, n_new=3):
    """n_rev ads fetched 10 days ago and sighted again now (stale_7d), n_new never fetched."""
    seen = iso(BASE - timedelta(hours=1))
    observations, listings = [], []
    for i in range(n_rev + n_new):
        o = card_obs(i, seen)
        observations.append(o)
        if i < n_rev:
            listings.append(listing(o, iso(BASE - timedelta(days=10))))
    return observations, listings


def test_the_postcondition_refuses_more_revisits_than_ceil_rho_b():
    observations, listings = revisit_heavy(n_rev=5, n_new=0)
    plans = planned(*observations[:2])  # ceil(0.2 * 5) = 1 revisit allowed
    bad = plan.assert_admissible(plans, observations, listings, [], iso(BASE), POL, 5)
    assert bad == ["2 revisit(s) planned (ads with a listing), above the cap ceil(rho * B) = 1 for B = 5"]
    assert plan.assert_admissible(plans[:1], observations, listings, [], iso(BASE), POL, 5) == []
    # without B the rule is not checked
    assert plan.assert_admissible(plans, observations, listings, [], iso(BASE), POL) == []


@pytest.mark.parametrize("n", [10, 33, 100])
def test_sample_caps_the_revisit_lane_at_ceil_rho_n(data_dir, n):
    import argparse

    observations, listings = revisit_heavy()
    recs = (observations, listings, [], [])
    args = argparse.Namespace(select="refetch", sample=n, seed=1, refresh_run=None, subcategories=None,
                              allocation="equal", stratify="depth", max_fetches=300)  # fmt: skip
    sel = cli._select(args, POL, recs, iso(BASE))
    reasons = [r for r, _ in sel.todo]
    cap = POL.revisit_cap(n)
    assert reasons.count("stale_7d") == min(30, cap)  # never more, whatever the new lane leaves unused
    assert reasons.count("new") == min(3, n - reasons.count("stale_7d"))
    assert sel.ads["lanes"] == {"new": reasons.count("new"), "revisit": reasons.count("stale_7d")}
    assert sum(s["sampled"] for s in sel.design["strata"]) == len(sel.todo)
    assert {s.get("lane") for s in sel.design["strata"]} == {"new", "revisit"}
    plans = [{"site": o["site"], "ad_id": o["ad_id"], "url": o["ad_url"]} for _, o in sel.todo]
    assert plan.assert_admissible(plans, observations, listings, [], sel.ads["as_of"], POL, n) == []
    job = crawl.Run.new("fetch-ads", "desktop", SITE)  # the run doc carries both: run.v1 must accept them
    job.doc["ads"], job.doc["sample"] = dict(sel.ads), dict(sel.design)
    job.save()  # validates run.v1 (store.write_json)
