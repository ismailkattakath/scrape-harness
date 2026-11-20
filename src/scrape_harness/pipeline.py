"""pipeline.toml: the ONE declaration of stages, I/O, network class and typed data files.

The run card, the store and the Bash hook derive from it; nothing else lists them. The file is
validated against pipeline.v1 on load, and group references ('@run') are expanded, so callers see
plain "ROOT:template" entries.

Located by, in order: $SCRAPE_HARNESS_PIPELINE, then pipeline.toml in $PRJ_ROOT, then in the
current directory. A missing or invalid file is an error, never an empty pipeline.

A consumer may keep its SITE specifics out of its repo: with [consumer].site_overlay = true, the keys in
OVERLAY_ALLOWED come from a site file's [pipeline.*] tables ($<env_prefix>_SITE_CONFIG, else
$<data_env>/config/site.toml; the plugin's guard reads the same file by the same rule). An unreadable site
file is an error (never the repo's example values); a key outside the allowlist is an error.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

from . import contracts

ENV = "SCRAPE_HARNESS_PIPELINE"
# The paths a site overlay may set (table -> keys); a table value updates the repo's table, anything else
# replaces it. The plugin's guard holds the same allowlist (stdlib only); a test asserts the two are equal.
OVERLAY_ALLOWED = {
    "consumer": ("default_site", "default_region", "site_hosts"),
    "plugins": ("sources", "python_path", "schema_dirs"),
    "crawl": ("proxy", "refresh"),
}
SITE_CONFIG_DEFAULT = ("config", "site.toml")  # under the data root
# Overlay list keys that ADD to the repo's list (a site's own code and schemas beside the consumer's), each
# entry resolved against the SITE FILE's folder, not the pipeline.toml's.
OVERLAY_DIRS = {("plugins", "python_path"), ("plugins", "schema_dirs")}


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class Consumer:
    """The consumer's world-visible names; the harness hard-codes none of them."""

    cli: str
    env_prefix: str
    data_env: str
    state_env: str
    state_dirname: str
    default_site: str
    default_region: str
    approval_key_fingerprint: str
    secret_envs: tuple[str, ...]
    site_hosts: tuple[str, ...]
    site_overlay: bool = False

    def knob(self, name: str) -> str:
        """The env name of a harness knob, e.g. knob("VARIANT") -> "LI_VARIANT"."""
        return f"{self.env_prefix}_{name}"


@dataclass(frozen=True)
class Command:
    name: str
    network: str  # none | site | paid
    operator_only: bool
    inputs: tuple[tuple[str, str], ...]  # (root, template)
    outputs: tuple[tuple[str, str], ...]
    args: tuple[str, ...] = ()
    secrets_env: tuple[str, ...] = ()  # env var NAMES the command needs (preflight checks presence only)


@dataclass(frozen=True)
class RefreshStep:
    """One step of `<cli> refresh` ([crawl.refresh].steps), in declared order."""

    name: str
    args: tuple[str, ...] = ()
    count_field: str | None = None  # paid_gate.count_field: the step's --dry-run JSON names its paid count
    paid_exempt: str | None = None  # why a paid step needs no count gate

    @property
    def count_flag(self) -> str:
        """`<cli> refresh --<step>-count N`: the operator's confirmation of this step's paid count."""
        return f"--{self.name.replace(' ', '-')}-count"


REFRESH_SITE_STEPS = ("crawl-lists", "fetch-ads")  # the site commands that take --refresh-run
REFRESH_REPEATABLE = ("crawl-lists",)  # one step per list set (e.g. two subcategories); each set signed
REFRESH_RUNNER_FLAGS = ("--approve-network", "--refresh-run", "--plan", "--dry-run")  # the runner's own


@dataclass(frozen=True)
class Pipeline:
    name: str
    path: Path
    consumer: Consumer
    stages: tuple[Command, ...]
    utilities: tuple[Command, ...]
    plan_outputs: tuple[tuple[str, str], ...]
    files: dict[str, tuple[str, str]]  # kind -> (path, schema)
    offline_flags: tuple[str, ...]
    python_path: tuple[Path, ...] = ()
    schema_dirs: tuple[Path, ...] = ()
    plugins: dict[str, dict[str, str]] | None = None  # group -> name -> "module:Class"
    commands: tuple[str, ...] = ()  # "module:function", each adds subcommands
    claude_allow: tuple[str, ...] = ()  # the consumer's own permission allow rules
    block_extensions: tuple[str, ...] | None = None  # [crawl]; None = browser.BLOCK_DEFAULT
    proxy: dict | None = None  # [crawl] proxy {url, user_secret, password_secret}
    image_mode: str | None = None  # [crawl] image_mode: answer (default) | block
    image_placeholder: tuple[int, int] | None = None  # [crawl] image_placeholder = [w, h]
    refresh: tuple[RefreshStep, ...] = ()  # [crawl.refresh].steps; () = no `<cli> refresh` chain
    overlay: Path | None = None  # the site file merged in (site_overlay), else None

    @property
    def stage_names(self) -> list[str]:
        return [c.name for c in self.stages]

    def command(self, name: str) -> Command | None:
        return next((c for c in (*self.stages, *self.utilities) if c.name == name), None)

    @property
    def network_commands(self) -> list[str]:
        return [c.name for c in (*self.stages, *self.utilities) if c.network != "none"]


def locate() -> Path:
    if os.environ.get(ENV):
        return Path(os.environ[ENV]).expanduser()
    for base in (os.environ.get("PRJ_ROOT"), os.getcwd()):
        if base and (Path(base) / "pipeline.toml").is_file():
            return Path(base) / "pipeline.toml"
    raise PipelineError(f"no pipeline.toml: set {ENV}, or run from a project whose root holds pipeline.toml")


def _entries(raw: list[str], groups: dict[str, list[str]], where: str) -> tuple[tuple[str, str], ...]:
    out = []
    for e in raw:
        if e.startswith("@"):
            if e[1:] not in groups:
                raise PipelineError(f"{where}: unknown group {e}")
            out += [tuple(g.split(":", 1)) for g in groups[e[1:]]]
        else:
            out.append(tuple(e.split(":", 1)))
    return tuple(out)


FILL = "FILL-ME"  # the starter template's placeholder (templates/default): never a valid value


def _unfilled(obj, at: str = "") -> list[str]:
    if isinstance(obj, str):
        return [at] if FILL in obj else []
    if isinstance(obj, list):
        return [x for i, v in enumerate(obj) for x in _unfilled(v, f"{at}[{i}]")]
    if isinstance(obj, dict):
        return [x for k, v in obj.items() for x in _unfilled(v, f"{at}.{k}" if at else k)]
    return []


def parse(doc: dict, path: Path) -> Pipeline:
    if holes := _unfilled(doc):
        raise PipelineError(
            f"{path} is still the starter template: fill in {', '.join(holes)} (every {FILL} value), then run "
            "`<cli> claude-settings --write`"
        )
    errs = contracts.errors("pipeline.v1", doc)
    if errs:
        raise PipelineError(f"{path}: not a valid pipeline.v1:\n  " + "\n  ".join(errs))
    groups = doc["groups"]
    names = [c["name"] for c in (*doc["stages"], *doc["utilities"])]
    if dup := sorted({n for n in names if names.count(n) > 1}):
        raise PipelineError(f"{path}: command declared twice: {', '.join(dup)}")

    def cmd(c: dict) -> Command:
        return Command(
            c["name"],
            c["network"],
            c.get("operator_only", False),
            _entries(c["inputs"], groups, f"{path}: {c['name']}.inputs"),
            _entries(c["outputs"], groups, f"{path}: {c['name']}.outputs"),
            tuple(c.get("args", ())),
            secrets_env=tuple(c.get("secrets_env", ())),
        )

    stages, utilities = tuple(cmd(c) for c in doc["stages"]), tuple(cmd(c) for c in doc["utilities"])
    refresh = _refresh(doc.get("crawl", {}).get("refresh"), {c.name: c for c in (*stages, *utilities)}, path)
    return Pipeline(
        name=doc["name"],
        path=path,
        consumer=Consumer(**{k: tuple(v) if isinstance(v, list) else v for k, v in doc["consumer"].items()}),
        stages=stages,
        utilities=utilities,
        plan_outputs=_entries(doc["plan_outputs"], groups, f"{path}: plan_outputs"),
        files={k: (v["path"], v["schema"]) for k, v in doc["files"].items()},
        offline_flags=tuple(doc["offline_flags"]),
        python_path=tuple(path.parent / d for d in doc.get("plugins", {}).get("python_path", [])),
        schema_dirs=tuple(path.parent / d for d in doc.get("plugins", {}).get("schema_dirs", [])),
        plugins={
            g: dict(v)
            for g, v in doc.get("plugins", {}).items()
            if g not in ("python_path", "schema_dirs", "commands")
        },
        commands=tuple(doc.get("plugins", {}).get("commands", [])),
        claude_allow=tuple(doc.get("claude", {}).get("allow", [])),
        block_extensions=(
            tuple(doc["crawl"]["block_extensions"]) if "block_extensions" in doc.get("crawl", {}) else None
        ),
        proxy=dict(doc["crawl"]["proxy"]) if "proxy" in doc.get("crawl", {}) else None,
        image_mode=doc.get("crawl", {}).get("image_mode"),
        image_placeholder=(
            tuple(doc["crawl"]["image_placeholder"]) if "image_placeholder" in doc.get("crawl", {}) else None
        ),
        refresh=refresh,
    )


def _refresh(raw: dict | None, commands: dict[str, Command], path: Path) -> tuple[RefreshStep, ...]:
    """[crawl.refresh].steps, checked against the declared commands (the schema checks the shape)."""
    if not raw:
        return ()
    out, seen = [], set()
    for s in raw["steps"]:
        name, where = s["name"], f"{path}: [crawl.refresh] step {s['name']}"
        c = commands.get(name)
        if c is None:
            raise PipelineError(f"{where}: not a declared stage or utility")
        if name in seen and name not in REFRESH_REPEATABLE:
            raise PipelineError(f"{where}: declared twice (only crawl-lists may repeat, with its own args)")
        seen.add(name)
        if c.network == "site" and name not in REFRESH_SITE_STEPS:
            raise PipelineError(
                f"{where}: a site step must be one of {', '.join(REFRESH_SITE_STEPS)} (the steps that take "
                "--refresh-run and so stay inside the refresh's bounded approval)"
            )
        if c.operator_only:
            raise PipelineError(f"{where}: an operator-only command is never a refresh step")
        gate, exempt = s.get("paid_gate"), s.get("paid_exempt")
        if c.network == "paid" and not (gate or exempt):
            raise PipelineError(f"{where}: a paid step must declare paid_gate or paid_exempt")
        if c.network != "paid" and (gate or exempt):
            raise PipelineError(f"{where}: paid_gate/paid_exempt are for paid steps only")
        args = tuple(s.get("args", ()))
        if bad := [a for a in args if a.split("=", 1)[0] in REFRESH_RUNNER_FLAGS]:
            raise PipelineError(f"{where}: {', '.join(bad)} is the runner's to add, never a step argument")
        out.append(RefreshStep(name, args, (gate or {}).get("count_field"), exempt))
    return tuple(out)


def _read(p: Path) -> dict:
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PipelineError(f"{p}: no such pipeline file") from None
    except tomllib.TOMLDecodeError as e:
        raise PipelineError(f"{p}: not valid TOML: {e}") from None


def overlay_file(doc: dict) -> Path | None:
    """The site file of a pipeline that declares site_overlay (None when it declares none)."""
    c = doc.get("consumer") or {}
    if c.get("site_overlay") is not True:
        return None
    if raw := os.environ.get(f"{c.get('env_prefix')}_SITE_CONFIG"):
        return Path(raw).expanduser()
    if data := os.environ.get(str(c.get("data_env"))):
        return Path(data).expanduser().joinpath(*SITE_CONFIG_DEFAULT)
    raise PipelineError(
        f"site_overlay: neither {c.get('env_prefix')}_SITE_CONFIG nor {c.get('data_env')} is set, so the site "
        "file cannot be found; refusing (the repo's example site values are never used)"
    )


def merge_overlay(doc: dict, overlay: Path) -> dict:
    """doc with the site file's allowlisted [pipeline.*] keys merged in (a copy; doc is not changed)."""
    try:
        ov = tomllib.loads(overlay.read_text(encoding="utf-8")).get("pipeline", {})
    except OSError as e:
        raise PipelineError(
            f"site_overlay: the site file {overlay} is unreadable ({type(e).__name__}); refusing"
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise PipelineError(f"site_overlay: the site file {overlay} is not valid TOML: {e}") from None
    if not isinstance(ov, dict):
        raise PipelineError(f"site_overlay: {overlay}: [pipeline] must be a table")
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in doc.items()}
    for table, keys in ov.items():
        allowed = OVERLAY_ALLOWED.get(table, ())
        if not isinstance(keys, dict) or (bad := sorted(set(keys) - set(allowed))) or not allowed:
            raise PipelineError(
                f"site_overlay: {overlay}: [pipeline.{table}] may hold only "
                f"{', '.join(allowed) or 'nothing (not an overlay table)'}"
                + (f"; found {', '.join(bad)}" if isinstance(keys, dict) and allowed else "")
            )
        base = out.setdefault(table, {})
        for k, v in keys.items():
            if (table, k) in OVERLAY_DIRS:
                if not isinstance(v, list) or not all(isinstance(d, str) for d in v):
                    raise PipelineError(
                        f"site_overlay: {overlay}: [pipeline.{table}] {k} must be a list of paths"
                    )
                # absolute, so parse()'s `pipeline.toml folder / d` leaves them as they are
                base[k] = [*base.get(k, []), *(str((overlay.parent / d).resolve()) for d in v)]
                continue
            base[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load(path: Path | str | None = None) -> Pipeline:
    p = Path(path) if path else locate()
    doc = _read(p)
    ov = overlay_file(doc)
    pl = parse(merge_overlay(doc, ov) if ov else doc, p.resolve())
    return replace(pl, overlay=ov.resolve()) if ov else pl


def load_repo(path: Path | str | None = None) -> Pipeline:
    """The repo's own pipeline, without any site overlay (what a committed file may be rendered from)."""
    p = Path(path) if path else locate()
    return parse(_read(p), p.resolve())


@cache
def _doc(key: str) -> dict:
    return _read(Path(key))


@cache
def _cached(key: str, _overlay: str | None = None) -> Pipeline:
    pl = load(key)
    for d in pl.schema_dirs:  # the consumer's record schemas join the contract registry
        contracts.add_schema_dir(d)
    return pl


def resolve(spec: str):
    """'module:attr' from the consumer's code, importable via [plugins].python_path."""
    import importlib
    import sys

    for d in current().python_path:
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    module, attr = spec.split(":")
    return getattr(importlib.import_module(module), attr)


def plugin_spec(group: str, name: str) -> str | None:
    return ((current().plugins or {}).get(group) or {}).get(name)


def declared(group: str) -> list[str]:
    return sorted((current().plugins or {}).get(group) or {})


def source_class(name: str):
    """A source declared in [plugins.sources] ('module:Class')."""
    spec = plugin_spec("sources", name)
    return resolve(spec) if spec else None


def current() -> Pipeline:
    """The located pipeline, loaded once per path and site file (a changed $SCRAPE_HARNESS_PIPELINE, or a
    site file named differently by the environment, reloads)."""
    key = str(locate().resolve())
    ov = overlay_file(_doc(key))
    return _cached(key, str(ov) if ov else None)


def consumer() -> Consumer:
    return current().consumer


def knob_name(name: str) -> str:
    return consumer().knob(name)


def knob(name: str, default: str | None = None) -> str | None:
    """The value of a harness knob from the environment (<env_prefix>_<name>)."""
    return os.environ.get(knob_name(name), default)
