"""The JSON-Schema contracts, loaded once. Every record crosses a boundary through `validate`.

The schemas directory is the single source of truth: no field list is repeated in Python.
Format assertions (date-time, uri) are ON, matching check-jsonschema, so the CLI and the
`nix flake check` gate cannot disagree about what is valid.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


class ContractError(ValueError):
    """A record did not satisfy its schema. The message lists every violation."""


def schema_dir() -> Path:
    shipped = Path(__file__).with_name("schemas")  # installed wheel (force-include)
    if shipped.is_dir():
        return shipped
    for parent in Path(__file__).resolve().parents:  # source checkout
        if (parent / "schemas" / "run.v1.schema.json").is_file():
            return parent / "schemas"
    raise FileNotFoundError("schemas/ not found next to the package or in any parent directory")


_EXTRA_DIRS: list[Path] = []


def add_schema_dir(path: Path | str) -> None:
    """Add a consumer's schema directory (its record contracts) next to the harness's own."""
    p = Path(path)
    if p not in _EXTRA_DIRS:
        _EXTRA_DIRS.append(p)
        _registry.cache_clear()
        _validator.cache_clear()


@cache
def _registry() -> tuple[Registry, dict[str, dict]]:
    by_title: dict[str, dict] = {}
    resources = []
    for d in (schema_dir(), *_EXTRA_DIRS):
        for path in sorted(d.rglob("*.schema.json")):
            doc = json.loads(path.read_text())
            resources.append((doc["$id"], Resource.from_contents(doc)))
            by_title[doc["title"]] = doc
    return Registry().with_resources(resources), by_title


def names() -> list[str]:
    return sorted(_registry()[1])


def _load_consumer_schemas() -> bool:
    """Load the pipeline (which registers its [plugins].schema_dirs). True if that added any dir."""
    before = len(_EXTRA_DIRS)
    try:
        from . import pipeline

        pipeline.current()
    except Exception:  # noqa: BLE001 - no pipeline: the name is simply unknown
        return False
    return len(_EXTRA_DIRS) > before


@cache
def _validator(name: str) -> Draft202012Validator:
    registry, by_title = _registry()
    if name in by_title:
        schema = by_title[name]
    elif "#" in name:  # a sub-schema reference, e.g. listing.v1.schema.json#/$defs/text_hint
        if name.split("#", 1)[0] not in registry and _load_consumer_schemas():
            return _validator(name)  # the document is a consumer's: register its dirs first
        schema = {"$ref": name}
    elif _load_consumer_schemas():
        return _validator(name)
    else:
        raise KeyError(f"unknown contract {name!r}; known: {', '.join(names())}")
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def errors(name: str, obj: Any) -> list[str]:
    found = sorted(_validator(name).iter_errors(obj), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in found]


def validate[T](name: str, obj: T, where: str = "") -> T:
    errs = errors(name, obj)
    if errs:
        head = f"{name} violation{' at ' + where if where else ''}"
        raise ContractError(head + ":\n  " + "\n  ".join(errs))
    return obj
