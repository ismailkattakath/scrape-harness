"""A run ends as "completed" only with its queue drained; otherwise it is "stopped" and resumable.

Three ways a run used to end "completed" with requests left: the site stopped answering (a storm of
timeouts), crawlee's own ceiling (max_requests_per_crawl), an interrupt. And a run that recorded the
wrong end gets an append-only correction, never a rewrite."""

from __future__ import annotations

import json

import pytest
from conftest import ORIGIN
from example_site.source import ExampleSite
from test_gone_redirect import FAST, ads, fixtures

from scrape_harness import crawl, incidents, store
from scrape_harness.cli import main
from scrape_harness.drivers.fixture import FixtureHttpClient


def url(i: int) -> str:
    return f"{ORIGIN}/ads/{2000000 + i}"


def run(tmp_path, mapping: dict, n: int, ceiling: int | None = None, job=None, resume=False, handle=None):
    opts = fixtures(tmp_path, mapping)
    job = job or crawl.Run.new("fetch-ads", "desktop", "example.invalid")
    card = {"command": "fetch-ads", "purpose": "test", "ceiling": ceiling}
    reqs = None if resume else ads(*map(url, range(n)))
    try:
        code = crawl.run(job, ExampleSite(), handle or (lambda m, r: None), FAST, reqs, opts, card)
    except crawl.Stop as e:
        code = e
    return code, job, [e for e in store.read("events") if e.get("run_id") == job.doc["run_id"]]


OK = "ad-1000002.html"
SILENT = {"error": "timeout"}


# ── (a) the circuit breaker ────────────────────────────────────────────────────────────────────────


def test_five_silent_attempts_in_a_row_stop_the_run_as_unreachable_not_a_challenge(data_dir, tmp_path):
    FixtureHttpClient.log.clear()
    code, job, events = run(tmp_path, {url(i): SILENT for i in range(8)}, 8)
    assert isinstance(code, crawl.Stop) and code.code == crawl.EXIT_UNREACHABLE
    assert "unreachable" in str(code)
    ad_gets = [u for u in FixtureHttpClient.log if "/ads/" in u]
    assert len(ad_gets) == crawl.UNREACHABLE_AFTER  # it stopped AT the fifth silence, not later
    assert job.doc["status"] == "stopped"  # resumable, never "completed"
    assert [e["kind"] for e in events].count("unreachable") == 1
    # not a challenge: no challenge event, no challenge counted, no cooldown
    assert not [e for e in events if e["kind"] in ("challenge", "cooldown")]
    assert job.doc["challenges"] == 0 and crawl.recent_challenges("example.invalid") == 0
    assert crawl.cooldown_until("example.invalid") is None
    assert crawl.pending(job) and crawl.pending(job) > 0  # the rest waits in the queue
    (inc,) = incidents.open_ones()
    assert inc["kind"] == "unreachable" and inc["code"] == crawl.EXIT_UNREACHABLE


def test_a_response_between_silences_resets_the_breaker(data_dir, tmp_path):
    # silent, answered, silent, answered ...: 8 silent attempts in all (each silent ad is retried once),
    # never 5 in a row, so the run goes on to drain its queue
    mapping = {url(i): (SILENT if i % 2 == 0 else OK) for i in range(8)}
    code, job, events = run(tmp_path, mapping, 8)
    assert code == 0 and job.doc["status"] == "completed"
    assert not [e for e in events if e["kind"] == "unreachable"]
    assert len([e for e in events if e["kind"] == "failed_request"]) == 4  # retried, then failed


def test_an_http_error_status_is_a_response_not_a_silence(data_dir, tmp_path):
    code, job, events = run(tmp_path, {url(i): {"file": OK, "status": 502} for i in range(6)}, 6)
    assert code == 0 and job.doc["status"] == "completed"  # 12 failed attempts, each one answered
    assert not [e for e in events if e["kind"] == "unreachable"]


class _Req:
    def __init__(self, nav: bool, frame: object) -> None:
        self._nav, self.frame = nav, frame

    def is_navigation_request(self) -> bool:
        return self._nav


class _Resp:
    def __init__(self, nav: bool, frame: object) -> None:
        self.request = _Req(nav, frame)


def test_only_the_main_frame_navigation_response_counts_as_an_answer():
    main_frame, iframe, seen = object(), object(), set()
    on = crawl.response_listener(seen, "k", main_frame)
    on(_Resp(False, main_frame))  # a sub-resource of the page
    on(_Resp(True, iframe))  # an iframe's navigation
    assert seen == set()
    on(_Resp(True, main_frame))
    assert seen == {"k"}


# ── (b) queue drained, else stopped; crawlee's ceiling ─────────────────────────────────────────────


def test_the_ceiling_stops_the_run_and_it_is_never_recorded_completed(data_dir, tmp_path):
    FixtureHttpClient.log.clear()
    code, job, events = run(tmp_path, {url(i): OK for i in range(3)}, 3, ceiling=1)
    assert isinstance(code, crawl.Stop) and code.code == crawl.EXIT_INCOMPLETE
    assert job.doc["status"] == "stopped"
    assert [u for u in FixtureHttpClient.log if "/ads/" in u] == [url(0)]  # crawlee sent no more
    assert crawl.pending(job) == 2
    (inc,) = [e for e in events if e["kind"] == "incomplete"]
    assert "ceiling of 1" in inc["reason"] and "2 requests pending" in inc["reason"]
    assert "completed" not in [e["kind"] for e in events]


def test_a_ceiling_the_plan_fits_in_completes(data_dir, tmp_path):
    code, job, _ = run(tmp_path, {url(i): OK for i in range(3)}, 3, ceiling=3)
    assert code == 0 and job.doc["status"] == "completed" and crawl.pending(job) == 0


def test_a_resumes_ceiling_counts_from_what_the_queue_already_settled(data_dir, tmp_path):
    mapping = {url(i): OK for i in range(3)}
    _, job, _ = run(tmp_path, mapping, 3, ceiling=1)
    FixtureHttpClient.log.clear()
    code, job, _ = run(tmp_path, mapping, 3, ceiling=crawl.pending(job), job=job, resume=True)
    assert code == 0 and job.doc["status"] == "completed"
    assert sorted(u for u in FixtureHttpClient.log if "/ads/" in u) == [url(1), url(2)]  # no duplicate GET


def test_an_interrupted_crawl_is_stopped_not_completed(data_dir, tmp_path):
    # a real SIGINT after the first page: crawlee 1.11.1 cancels its run task and run() RETURNS NORMALLY
    # with requests left (the measured way a run recorded "completed" with 81 pending)
    import os
    import signal

    def interrupt(m, r):
        os.kill(os.getpid(), signal.SIGINT)

    code, job, events = run(tmp_path, {url(i): OK for i in range(3)}, 3, handle=interrupt)
    assert isinstance(code, crawl.Stop) and code.code == crawl.EXIT_INCOMPLETE
    assert job.doc["status"] == "stopped"
    assert "an interrupt" in [e for e in events if e["kind"] == "incomplete"][0]["reason"]


# ── (c) a correction of a run's recorded end ───────────────────────────────────────────────────────


def recorded_completed_with_pending(tmp_path):
    """The measured bug's shape: a run record says "completed" while its queue still holds requests."""
    _, job, _ = run(tmp_path, {url(i): OK for i in range(3)}, 3, ceiling=1)
    job.doc["status"] = "completed"
    job.save()
    return job


def test_crawl_correct_records_the_measured_end_and_never_rewrites_the_run(data_dir, tmp_path, capsys):
    job = recorded_completed_with_pending(tmp_path)
    rid = job.doc["run_id"]
    path = store.runs_dir() / f"{store.safe_name(rid)}.json"
    before = path.read_bytes()
    assert main(["crawl", "correct", "--run-id", rid, "--status", "stopped", "--reason", "unreachable",
                 "--note", "timeout storm; the operator interrupted it"]) == 0  # fmt: skip
    ev = json.loads(capsys.readouterr().out)["correction"]
    assert path.read_bytes() == before  # the record is never rewritten
    c = ev["correction"]
    assert c["field"] == "status" and c["recorded"] == {"status": "completed"}
    assert c["reconciled"] == {
        "status": "stopped",
        "reason": "unreachable",
        "pending": 2,
        "failed_requests": 0,
    }
    assert crawl.status_of(job.doc) == "stopped"
    assert crawl.Run.load().doc["run_id"] == rid  # resumable again: the default resume finds it


def test_crawl_correct_refuses_what_the_evidence_contradicts(data_dir, tmp_path):
    job = recorded_completed_with_pending(tmp_path)
    rid = job.doc["run_id"]
    with pytest.raises(SystemExit, match="already completed"):
        crawl.correct_status(rid, "completed", "x", "a note")
    crawl.correct_status(rid, "stopped", "unreachable", "a note")
    with pytest.raises(SystemExit, match="still pending"):
        crawl.correct_status(rid, "completed", "x", "a note")
    with pytest.raises(SystemExit, match="needs a --note"):
        crawl.correct_status(rid, "aborted", "x", "  ")
    with pytest.raises(SystemExit, match="no resumable run"):
        crawl.correct_status("no-such-run", "stopped", "x", "a note")
