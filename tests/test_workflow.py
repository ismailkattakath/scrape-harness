"""The plan-pipeline workflow (plugin/workflows/plan-pipeline.js), driven under node with MOCKED agents:
its deterministic rule checks and its TOML rendering are real; the agents are fixed plans. The rendered
draft plus a [consumer] table must load through the real pipeline.v1 loader."""

import json
import re
import shutil
import subprocess

from conftest import PLUGIN, ROOT

from scrape_harness import pipeline

WF = PLUGIN / "workflows" / "plan-pipeline.js"
NODE = shutil.which("node") or "node"  # a check input of the flake: never silently skipped

GOOD = {
    "angle": "minimal",
    "stages": [
        {"name": "crawl-lists", "network": "site", "purpose": "list pages", "args": ["--subcategory", "w"],
         "inputs": [], "outputs": ["DATA:observations.jsonl", "@run"]},
        {"name": "fetch-ads", "network": "site", "purpose": "detail pages",
         "inputs": ["DATA:observations.jsonl"], "outputs": ["DATA:listings.jsonl", "DATA:raw/{site}/", "@run"]},
        {"name": "reparse", "network": "none", "purpose": "offline re-extraction",
         "inputs": ["DATA:listings.jsonl"], "outputs": ["DATA:listings.jsonl"]},
    ],
    "files": [{"kind": "observations", "path": "observations.jsonl", "schema": "observation.v1"},
              {"kind": "listings", "path": "listings.jsonl", "schema": "listing.v1"}],
    "requests_estimate": "~60", "risks": [],
}  # fmt: skip
BAD = {
    **GOOD,
    "angle": "broken",
    "stages": [
        {"name": "Fetch Everything", "network": "site", "purpose": "x", "inputs": ["DATA:nowhere.jsonl"],
         "outputs": ["DATA:raw/{site}/"]},
        {"name": "summary", "network": "none", "purpose": "analysis", "inputs": ["DATA:raw/{site}/"],
         "outputs": ["stdout:report"]},
        {"name": "crawl-more", "network": "site", "purpose": "y", "inputs": [], "outputs": ["DATA:more.jsonl"]},
    ],
}  # fmt: skip


def run(judge_final):
    body = re.sub(r"^export const meta", "const meta", WF.read_text(), count=1)
    harness = f"""
const plans = [{json.dumps(GOOD)}, {json.dumps(BAD)}, null];
let n = 0; const calls = [];
async function agent(prompt, opts) {{
  calls.push(opts.label);
  if (opts.label.startsWith('design:')) return plans[n++];
  if (opts.label.startsWith('review:')) return {{fatal: [], fixable: []}};
  return {{winner: 0, why: 'fewest requests', final: {json.dumps(judge_final)}, operator_questions: ['sample size?']}};
}}
const parallel = async ts => Promise.all(ts.map(t => t().catch(() => null)));
async function pipeline(items, ...stages) {{
  return Promise.all(items.map(async (it, i) => {{ let r = it; for (const s of stages) {{ r = await s(r, it, i); }} return r; }}));
}}
const phase = () => {{}}; const logs = []; const log = m => logs.push(m);
const args = {{objective: 'synthetic', site: 'example.invalid', name: 'draft'}};
(async () => {{ const out = await (async () => {{ {body} }})(); console.log(JSON.stringify({{out, calls, logs}})); }})()
  .catch(e => {{ console.error(e.stack); process.exit(1); }});
"""
    r = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_meta_is_a_pure_literal_with_matching_phases():
    src = WF.read_text()
    meta = re.search(r"export const meta = (\{.*?\n\})", src, re.S).group(1)
    assert "${" not in meta and not re.search(r"\b(Date\.now|Math\.random|new Date\(\))", src)
    titles = re.findall(r"title: '([^']+)'", meta)
    assert titles == re.findall(r"^phase\('([^']+)'\)", src, re.M) == ["Design", "Check", "Judge"]


def test_the_rule_checks_catch_a_broken_plan_and_pass_a_good_one():
    res = run(GOOD)
    logs = " ".join(res["logs"])
    assert "plan 1 (minimal): 0 rule errors" in logs and "plan 3" not in logs  # a dead draft is dropped
    bad = next(m for m in res["logs"] if m.startswith("plan 2"))
    assert int(re.search(r"(\d+) rule errors", bad).group(1)) >= 4
    assert res["out"]["rule_errors_remaining"] == [] and res["out"]["operator_questions"] == ["sample size?"]
    assert sum(c.startswith("review:") for c in res["calls"]) == 4  # 2 lenses x 2 viable plans


def test_the_judge_is_rechecked_not_trusted():
    res = run(BAD)
    errs = " ".join(res["out"]["rule_errors_remaining"])
    for needle in (
        "is not pipeline.v1",
        "produced by no earlier stage",
        "no later offline reparse",
        "after an analysis",
    ):
        assert needle in errs, needle


def test_the_rendered_draft_loads_with_a_consumer_table(tmp_path):
    toml = run(GOOD)["out"]["toml"]
    consumer = (ROOT / "pipeline.toml").read_text()
    table = consumer[consumer.index("[consumer]") : consumer.index("[plugins]")]
    p = tmp_path / "pipeline.toml"
    p.write_text(toml.replace("[groups]", table + "\n[groups]", 1))
    pl = pipeline.load(p)
    assert pl.stage_names == ["crawl-lists", "fetch-ads", "reparse"]
    assert pl.command("crawl-lists").args == ("--subcategory", "w") and pl.command("approve").operator_only
    assert set(pl.files) == {"observations", "listings"}
