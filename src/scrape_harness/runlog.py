"""Live per-run log (operator decision 2026-11-07, ADR-0007): stdlib logging only.

A JSON-lines FileHandler at the DATA root ($<data_env>)/runs/<run_id>.log.jsonl is attached to the ROOT
logger for the run, so crawlee's own loggers (they propagate to root) land in it too, and is removed
at the end. StreamHandler flushes every record, so `tail -f <file> | jq -c .` follows it live.

Never logged: cookies, headers beyond the challenge-evidence allowlist, query strings. URLs are
reduced to their path; long opaque strings and cookie/authorization values in messages are redacted.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import store

LOG = logging.getLogger("scrape_harness.run")
URL_QUERY = re.compile(r"(https?://[^\s?#\"']+)\?[^\s#\"']*")
SECRETISH = re.compile(r"(?i)\b(set-cookie|cookie|authorization|api[_-]?key|token|key)\s*[=:]\s*[^\s,;&\"']+")
TOKENISH = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
FIELDS = (
    "event",
    "kind",
    "path",
    "page",
    "status",
    "ms",
    "counts",
    "reason",
    "code",
    "egress",
    "evidence",
    "run_id",
    "card",
    "final_path",
    "hops",
)


USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@\"']+@", re.I)  # scheme://user:pass@host
_SECRETS: list[str] = []  # values this process uses (proxy credentials): redacted wherever they appear


def add_secrets(*values: str) -> None:
    """Register values to redact from EVERY log record of this process (any logger, any handler: the run
    log, crawlee's console lines, a traceback), not just from this module's formatter."""
    for v in values:
        if v and v not in _SECRETS:
            _SECRETS.append(v)
    _SECRETS.sort(key=len, reverse=True)  # a longer value first, so a shorter one cannot split it
    _install_record_factory()


_FACTORY: list = []


def _install_record_factory() -> None:
    if _FACTORY:
        return
    import traceback

    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        rec = base(*args, **kwargs)
        msg = rec.getMessage()
        if rec.exc_info and rec.exc_info[1] is not None:
            tb = "".join(traceback.format_exception(*rec.exc_info))
            if any(v in tb for v in _SECRETS) or USERINFO.search(tb):  # never print it raw
                msg, rec.exc_info, rec.exc_text = f"{msg}\n{tb}", None, None
        clean = scrub(msg)  # credentials only: hashes and queries stay readable on the console
        if clean != msg or rec.args:
            rec.msg, rec.args = clean, ()
        return rec

    logging.setLogRecordFactory(factory)
    _FACTORY.append(factory)


def scrub(text: str) -> str:
    """Credentials only: the registered values and any URL userinfo."""
    for v in _SECRETS:
        text = text.replace(v, "[redacted]")
    return USERINFO.sub(r"\1[redacted]@", text)


def redact(text: str) -> str:
    text = scrub(text)
    text = URL_QUERY.sub(r"\1?[query]", text)
    text = SECRETISH.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    return TOKENISH.sub("[redacted]", text)


class JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "at": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for k in FIELDS:
            if hasattr(record, k):
                line[k] = getattr(record, k)
        if record.exc_info:
            line["error"] = redact(f"{record.exc_info[0].__name__}: {record.exc_info[1]}")
        return json.dumps(line, ensure_ascii=False, sort_keys=True, default=str)


def log_path(run_id: str) -> tuple[Path, str]:
    rel = f"runs/{store.safe_name(run_id)}.log.jsonl"
    return store.data_dir() / rel, rel


@contextmanager
def attached(run_id: str):
    """Attach the run's JSON-lines handler to the root logger; yield its path relative to the data dir."""
    path, rel = log_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not root.handlers:  # keep crawlee's console output: it installs it only on a handler-less root
        from crawlee._log_config import configure_logger

        configure_logger(root)
    handler = logging.FileHandler(path, encoding="utf-8")  # emit() flushes every record
    handler.setFormatter(JsonLines())
    handler.setLevel(logging.INFO)
    level = root.level
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    try:
        yield rel
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
        handler.close()


def request(
    kind: str, url: str, status: int | None, ms: float, counts: dict, page=None, final_url=None, hops=None
) -> None:
    """One line per page attempt. `final_path` where the page ended (always, when known), `hops`
    (main-frame redirects of THIS navigation) only where the backend can see them (the browser)."""
    from urllib.parse import urlsplit

    extra = {
        "event": "request",
        "kind": kind,
        "path": urlsplit(str(url)).path,
        "status": status,
        "ms": round(ms, 1),
        "counts": dict(counts),
    }
    if page is not None:
        extra["page"] = page
    if final_url:  # always where known: an unchanged path is evidence too (no redirect happened)
        extra["final_path"] = urlsplit(str(final_url)).path
    if hops is not None:
        extra["hops"] = hops
    LOG.info("%s %s -> %s", kind, extra["path"], status, extra=extra)
