"""`<cli> claude-md`: the route table in a consumer's CLAUDE.md, GENERATED between two marker lines.

"When the user says / asks ... -> use ..." rows, from two sources and nothing else:
  - the plugin's skills and agents, at the ONE pinned plugin path ($SCRAPE_HARNESS_PLUGIN, which the dev
    shell, the test builds and the CLI wrapper all set from the flake's packages.plugin): the name, and the
    trigger clause of the description ("Use when / whenever / before ...");
  - `cli spec` rows for each command pipeline.toml declares (stage or utility): network class and help.
The render is repo-only (pipeline.repo_only()), like `cli spec`: a site-overlay consumer's block never
holds a site file's value and needs no site file. Rows are sorted, cells pipe-escaped, so the block is
byte-identical on every machine and under every PYTHONHASHSEED.

Only the lines strictly between START and END are rewritten; everything else in the file is the
consumer's. Each marker must be a whole line, present once, START before END, or the command refuses
(exit 4) and writes nothing.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from pathlib import Path

from . import pipeline

START = "<!-- scrape-harness:routes:start -->"
END = "<!-- scrape-harness:routes:end -->"
PLUGIN_ENV = "SCRAPE_HARNESS_PLUGIN"
EXIT_REFUSED = 4  # the CLI's config refusal (EXIT_CONFIG)
EXIT_DRIFT = 1  # as `cli spec --check`
# The trigger clause: "Use when|whenever|before <clause>." up to the first sentence end.
TRIGGER = re.compile(r"\bUse (when(?:ever)?|before)\b(.*?)(?:\.(?=\s|$)|$)", re.S)


class Refused(RuntimeError):
    pass


def plugin_dir(explicit: str | None = None) -> Path:
    """The pinned plugin: --plugin, else $SCRAPE_HARNESS_PLUGIN. No fallback: another copy is never used."""
    raw = explicit or os.environ.get(PLUGIN_ENV)
    if not raw:
        raise Refused(
            f"no plugin: {PLUGIN_ENV} is unset (the dev shell and the CLI wrapper set it); or pass --plugin"
        )
    p = Path(raw)
    if not (p / ".claude-plugin" / "plugin.json").is_file():
        raise Refused(f"no plugin at {p} (no .claude-plugin/plugin.json); check {PLUGIN_ENV} or --plugin")
    return p


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def frontmatter(path: Path) -> dict[str, str]:
    """The top-level `key: value` pairs of a Markdown file's YAML frontmatter (stdlib: no YAML library).
    A quoted value is unquoted; a block scalar (`>`, `|`, with `-`/`+`) joins its indented lines."""
    m = re.match(r"^---\n(.*?)\n---\n", path.read_text(), re.S)
    if not m:
        raise Refused(f"{path}: no frontmatter")
    out: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        if key and (line.startswith((" ", "\t")) or not line.strip()):
            out[key] = (out[key] + " " + line.strip()).strip()
            continue
        key = None
        k, sep, v = line.partition(":")
        if not sep or not k.strip() or k[0] in " \t#":
            continue
        k, v = k.strip(), v.strip()
        if v in (">", "|", ">-", "|-", ">+", "|+"):
            out[k], key = "", k
        else:
            out[k] = _unquote(v)
    return out


def trigger(path: Path, description: str) -> str:
    m = TRIGGER.search(" ".join(description.split()))
    if not m or not m.group(2).strip():
        raise Refused(f"{path}: the description has no 'Use when/whenever/before ...' clause to route by")
    return f"{m.group(1)} {m.group(2).strip()}"


def _cell(text: str | None) -> str:
    from .clispec import _cell as cell  # the ONE pipe-escape, shared with docs/cli.md

    return cell(text)


def plugin_routes(plugin: Path) -> list[tuple[str, str]]:
    """(when, use) for every agent, then every skill, each sorted by name."""
    name = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())["name"]
    rows = []
    for kind, files in (
        ("agent", sorted((plugin / "agents").glob("*.md"))),
        ("skill", sorted((plugin / "skills").glob("*/SKILL.md"))),
    ):
        found = []
        for f in files:
            fm = frontmatter(f)
            n = fm.get("name") or (f.parent.name if kind == "skill" else f.stem)
            found.append((n, trigger(f, fm.get("description", ""))))
        rows += [(when, f"{kind} `{name}:{n}`") for n, when in sorted(found)]
    return rows


def command_routes(pl: pipeline.Pipeline) -> list[dict]:
    """One route per declared stage (in order), then per declared utility (by name): `when`, `use`,
    `network`, `cmd`, and `planned` (it takes --plan: the plan -> approve -> --approve-network rule)."""
    from . import cli, clispec

    rs = clispec.rows(cli.parser(), pl)
    plans = {r["cmd"] for r in rs if r["kind"] == "option" and r["opt"] == "--plan"}
    rows = [r for r in rs if r["kind"] == "command" and r["declared"]]
    rows.sort(key=lambda r: (r["declared"]["role"] != "stage", r["declared"]["position"] or 0, r["cmd"]))
    out = []
    for r in rows:
        d = r["declared"]
        where = f" (stage {d['position']}/{len(pl.stages)})" if d["position"] else ""
        who = " (operator only: the hook asks)" if d["operator_only"] else ""
        out.append(
            {
                "when": r["help"] or f"run {r['cmd']}",
                "use": f"`{pl.consumer.cli} {r['cmd']}`{where}{who}",
                "network": d["network"],
                "cmd": r["cmd"],
                "planned": d["network"] != "none" and r["cmd"] in plans,
            }
        )
    return out


def render(plugin: str | None = None) -> str:
    """The block's lines (between the markers), each ending in a newline."""
    with pipeline.repo_only():
        pl = pipeline.current()
        cmds = command_routes(pl)
    p = plugin_dir(plugin)
    c = pl.consumer.cli
    lines = [
        f"<!-- GENERATED by `{c} claude-md --write` from the scrape-harness plugin's skills and agents and "
        f"`{c} cli spec` (this repo's pipeline.toml only). Do not edit between the markers; "
        f"`{c} claude-md --check` fails on drift. -->",
        "",
        "| When the user says / asks ... | Use | Network |",
        "|---|---|---|",
    ]
    lines += [f"| {_cell(w)} | {_cell(u)} |  |" for w, u in plugin_routes(p)]
    lines += [f"| {_cell(r['when'])} | {_cell(r['use'])} | {_cell(r['network'])} |" for r in cmds]
    net = [r["cmd"] for r in cmds if r["planned"]]
    if net:
        offline = ", ".join(f"`{f}`" for f in pl.offline_flags)
        lines += [
            "",
            "Networked commands ("
            + ", ".join(f"`{n}`" for n in net)
            + f"): `{c} <command> ... --plan`, then `{c} approve <plan_hash>` (the operator's Touch ID), "
            f"then the same command with `--approve-network`. Offline ({offline}) sends nothing.",
        ]
    return "\n".join(lines) + "\n"


def _split(text: str, path: Path) -> tuple[str, str, str]:
    """(head through the START line, the block, the END line onward), or Refused."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ln.rstrip("\r\n") == START]
    ends = [i for i, ln in enumerate(lines) if ln.rstrip("\r\n") == END]
    for marker, at in ((START, starts), (END, ends)):
        if text.count(marker) != len(at):
            raise Refused(f"{path}: the marker {marker} must be a line of its own")
        if len(at) != 1:
            raise Refused(f"{path}: the marker {marker} must appear exactly once (found {len(at)})")
    s, e = starts[0], ends[0]
    if e < s:
        raise Refused(f"{path}: the end marker comes before the start marker")
    return "".join(lines[: s + 1]), "".join(lines[s + 1 : e]), "".join(lines[e:])


def default_file() -> Path:
    with pipeline.repo_only():
        return pipeline.current().path.parent / "CLAUDE.md"


def cmd(args: argparse.Namespace) -> int:
    try:
        f = Path(args.file) if args.file else default_file()
        if not (args.write or args.check):
            sys.stdout.write(render(args.plugin))
            return 0
        if not f.is_file():
            raise Refused(
                f"{f}: no such file; add the two marker lines ({START} / {END}) where the table goes"
            )
        old = f.read_text()
        head, _, tail = _split(old, f)
        new = head + render(args.plugin) + tail
    except Refused as e:
        print(f"refusing: {e}", file=sys.stderr)
        return EXIT_REFUSED
    if args.write:
        changed = new != old
        if changed:
            f.write_text(new)
        print(json.dumps({"path": str(f), "written": changed}))
        return 0
    if new == old:
        return 0
    sys.stdout.writelines(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), str(f), "generated")
    )
    return EXIT_DRIFT
