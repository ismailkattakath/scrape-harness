"""`<cli> cli spec`: the whole argparse tree as cli.v1 JSONL, joined with pipeline.toml.

The parser (the core's commands plus the consumer's [plugins].commands) and pipeline.toml are the ONE
source; this module only reads them. One row per command, positional and option, sorted, so two runs are
byte-identical and a generated file (docs, a route table) can be drift-checked with `--check FILE`.

Rows read raw argparse attributes (`help`, `default`, `nargs`), never `format_help()`, whose output
depends on the terminal width and the Python version.

Not in the tree: `plan <command> ...` (the same as `<command> ... --plan`) and `statusline`, which
`main` handles before parsing. `run` is in the tree as a help-only entry.

Some defaults and help texts come from `pipeline.consumer()` (e.g. `--source`'s default site). `main`
builds the parser for `cli spec` inside `pipeline.repo_only()`, so a site-overlay consumer renders from its
REPO pipeline.toml: the committed placeholders, never a site file's values, and no site file is needed (the
Nix sandbox of checks.cli-spec-drift has none). One render on every machine.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from . import pipeline

KIND_ORDER = {"command": 0, "positional": 1, "option": 2}
PRE_PARSE = (
    ("plan <command> ...", "the same as `<command> ... --plan`"),
    ("statusline", "Claude Code's status line: one fast line, never a failure"),
)


def _subparsers(p: argparse.ArgumentParser) -> argparse._SubParsersAction | None:  # noqa: SLF001
    return next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)  # noqa: SLF001


def _default(a: argparse.Action) -> str | None:
    if a.nargs == 0 or a.default is None or a.default is argparse.SUPPRESS:
        return None  # a flag's value is its presence
    return a.default if isinstance(a.default, str) else str(a.default)


def _declared(pl: pipeline.Pipeline, name: str) -> dict | None:
    c = pl.command(name)
    if c is None:
        return None
    stage = name in pl.stage_names
    return {
        "role": "stage" if stage else "utility",
        "position": pl.stage_names.index(name) + 1 if stage else None,
        "network": c.network,
        "operator_only": c.operator_only,
        "inputs": [f"{root}:{tpl}" for root, tpl in c.inputs],
        "outputs": [f"{root}:{tpl}" for root, tpl in c.outputs],
    }


def _row(kind: str, cmd: str, opt: str | None, a: argparse.Action | None, help_: str | None, declared):
    return {
        "kind": kind,
        "cmd": cmd,
        "opt": opt,
        "takes_value": bool(a is not None and a.nargs != 0),
        "required": bool(a is not None and a.required),
        "default": _default(a) if a is not None else None,
        "choices": [str(c) for c in a.choices] if a is not None and a.choices is not None else None,
        "help": help_,
        "declared": declared,
    }


def _walk(p: argparse.ArgumentParser, path: list[str], pl: pipeline.Pipeline, out: list[tuple]) -> None:
    sub = _subparsers(p)
    if sub is None:
        return
    helps = {ca.dest: ca.help for ca in sub._choices_actions}  # noqa: SLF001 (only parsers given help=)
    seen: set[int] = set()
    for name, child in sub.choices.items():
        if id(child) in seen:  # an alias points at a parser already walked under its first name
            continue
        seen.add(id(child))
        cmd = " ".join([*path, name])
        out.append(((cmd, 0, 0, ""), _row("command", cmd, None, None, helps.get(name), _declared(pl, cmd))))
        for i, a in enumerate(child._actions):  # noqa: SLF001
            if isinstance(a, (argparse._HelpAction, argparse._SubParsersAction)):  # noqa: SLF001
                continue
            if a.option_strings:
                opt = sorted(a.option_strings, key=lambda s: (-len(s), s))[0]  # the long form
                out.append(((cmd, 2, 0, opt), _row("option", cmd, opt, a, a.help, None)))
            else:  # positionals keep their argument order
                opt = a.metavar if isinstance(a.metavar, str) else a.dest
                out.append(((cmd, 1, i, ""), _row("positional", cmd, opt, a, a.help, None)))
        _walk(child, [*path, name], pl, out)


def rows(p: argparse.ArgumentParser, pl: pipeline.Pipeline | None = None) -> list[dict]:
    out: list[tuple] = []
    _walk(p, [], pl or pipeline.current(), out)
    return [r for _, r in sorted(out, key=lambda t: t[0])]


def jsonl(rs: list[dict]) -> str:
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rs)


def as_json(rs: list[dict]) -> str:
    return json.dumps(rs, sort_keys=True, ensure_ascii=False) + "\n"


def _cell(text: str | None) -> str:
    return (text or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _value(r: dict) -> str:
    if r["choices"] is not None:
        return "`" + _cell("|".join(r["choices"])) + "`"
    return "value" if r["takes_value"] else "flag"


def markdown(rs: list[dict], prog: str) -> str:
    """A stable command table, then one option table per command that has arguments."""
    lines = [
        f"# `{prog}` command reference",
        "",
        f"GENERATED by `{prog} cli spec --format md` from the argparse tree and pipeline.toml.",
        f"Do not edit. `{prog} cli spec --format md --check <this file>` fails on drift.",
        "",
        "Handled before parsing, so not in the tables: "
        + "; ".join(f"`{prog} {c}` ({why})" for c, why in PRE_PARSE)
        + ".",
        "",
        "## Commands",
        "",
        "| Command | Declared | Network | Does |",
        "|---|---|---|---|",
    ]
    for r in rs:
        if r["kind"] == "command":
            d = r["declared"]
            role = f"{d['role']} {d['position']}" if d and d["position"] else (d["role"] if d else "")
            lines.append(f"| `{r['cmd']}` | {role} | {d['network'] if d else ''} | {_cell(r['help'])} |")
    for cmd in sorted({r["cmd"] for r in rs if r["kind"] != "command"}):
        lines += ["", f"### `{cmd}`", "", "| Argument | Value | Default | Help |", "|---|---|---|---|"]
        for r in rs:
            if r["cmd"] == cmd and r["kind"] != "command":
                name = f"`{_cell(r['opt'])}`" + (
                    " (required)" if r["required"] and r["kind"] == "option" else ""
                )
                default = f"`{_cell(r['default'])}`" if r["default"] is not None else ""
                lines.append(f"| {name} | {_value(r)} | {default} | {_cell(r['help'])} |")
    return "\n".join(lines) + "\n"


def render(fmt: str) -> str:
    from . import cli

    rs = rows(cli.parser())
    if fmt == "md":
        return markdown(rs, pipeline.consumer().cli)
    return as_json(rs) if fmt == "json" else jsonl(rs)


def cmd(args: argparse.Namespace) -> int:
    """Print the spec; with --check FILE, exit 0 when FILE equals it, else 1 and a unified diff."""
    text = render(args.format)
    if not args.check:
        sys.stdout.write(text)
        return 0
    f = Path(args.check)
    old = f.read_text() if f.is_file() else ""
    if old == text:
        return 0
    sys.stdout.writelines(
        difflib.unified_diff(old.splitlines(True), text.splitlines(True), str(f), "generated")
    )
    return 1
