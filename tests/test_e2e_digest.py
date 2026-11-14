"""Before/after guard for refactors: one offline e2e run of the generic pipeline, hashed.

The golden digest in tests/golden/e2e.digest.json records one run of the EXAMPLE consumer. Any later
commit must reproduce it, so a refactor that changes what the pipeline writes fails here. It is only ever
re-recorded deliberately (conftest.check_golden). History: the first recording (03d9c3f, the reference
consumer) held unchanged through the pipeline.toml refactor (9113947) and the naming change (f54eb48);
it was re-recorded once, for the switch to the example consumer.

NOT literally byte-identical, because some values are volatile by design. Exactly these are normalised,
nothing else:
  1. ISO-8601 timestamps (seen_at, fetched_at, at, created_at, until, checked_at, ...) and the
     timestamp part of file names                                   -> "TIME"
  2. run ids and crawlee queue names (they embed a time and a random suffix) -> "RUN1", "RUN2", ...
     in order of first appearance
  3. per-request timings (`ms`) and the card's ETA seconds           -> 0
  4. who/where on the run card (user, host, parent process, li_rev) -> fixed strings
  5. the temporary DATA/STATE parent, raw and symlink-resolved       -> "ROOT"
  6. crawlee's own log lines (their stats tables carry timings); our structured lines (those with an
     `event` field) are all kept
STATE (crawlee's queue store, its own metadata) is not hashed; DATA is, file by file.
"""

import hashlib
import json
import re
from pathlib import Path

from conftest import FIXTURES, check_golden

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
TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}[:_-]\d{2}[:_-]\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?", re.I)
RUN = re.compile(r"TIME-(crawl-lists|fetch-ads|ab)-[a-z0-9-]+?-[0-9a-f]{6}\b", re.I)


def run_pipeline() -> None:
    assert main(["crawl-lists", "--subcategory", "widgets", "--stride", "2", "--start", "1", *FIX]) == 0
    assert main(["fetch-ads", *FIX]) == 0
    assert main(["fetch-ads", "--sample", "2", "--seed", "3", "--dry-run"]) == 0
    assert main(["reparse"]) == 0
    assert main(["ab", "--subcategory", "widgets", "--pages", "1", "--variants", "http", *FIX]) == 0


def normalise(text: str, root: Path, runs: dict) -> str:
    base = root.parent  # holds both the DATA and the STATE temp roots
    for form in sorted(
        {str(base.resolve()), str(base)}, key=len, reverse=True
    ):  # macOS: /var -> /private/var
        text = text.replace(form, "ROOT")
    text = TIME.sub("TIME", text)

    def run_name(m: re.Match) -> str:
        key = m.group(0).lower()
        return runs.setdefault(key, f"RUN{len(runs) + 1}")

    return RUN.sub(run_name, text)


def scrub(obj, key=None):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("ms", "eta_s"):
                out[k] = 0
            elif k in ("user", "host", "parent", "li_rev"):
                out[k] = k.upper()
            else:
                out[k] = scrub(v, k)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def digest(data: Path) -> dict:
    runs: dict = {}
    files = {}
    for p in sorted(data.rglob("*")):
        if not p.is_file():
            continue
        rel = normalise(str(p.relative_to(data)), data, runs)
        if p.suffix == ".gz":
            files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()  # gzip mtime=0: deterministic
            continue
        text = p.read_text()
        if p.suffix in (".json", ".jsonl"):
            docs = (
                [json.loads(x) for x in text.splitlines() if x.strip()]
                if p.suffix == ".jsonl"
                else [json.loads(text)]
            )
            if p.name.endswith(".log.jsonl"):
                docs = [d for d in docs if "event" in d]  # rule 6
            text = "\n".join(json.dumps(scrub(d), sort_keys=True) for d in docs)
        files[rel] = hashlib.sha256(normalise(text, data, runs).encode()).hexdigest()
    total = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"digest": total, "files": files}


def test_the_e2e_run_reproduces_the_recorded_digest(data_dir):
    run_pipeline()
    check_golden("e2e.digest", digest(data_dir))
