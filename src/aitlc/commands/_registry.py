"""Command discovery and mounting for the aitlc Typer app.

Why this exists
---------------
cli.py used to import every command module and hand-wire ~30
`app.command(...)` / `app.add_typer(...)` calls. Adding a command meant editing
that central list, and the list silently encoded intent that belonged next to
each command: three modules (init, start, propose-fix) expose a Typer ``app``
yet are mounted as a single plain command, not a group. Nothing at the module
said so -- only cli.py knew.

Now each command module declares, at module scope, how it mounts:

    COMMAND = {"name": "run", "attr": "run", "order": 10}                      # plain command
    COMMAND = {"name": "xray", "attr": "app", "kind": "group", "order": 100}   # sub-command group

``register_all(app)`` discovers every module in this package, reads those
declarations, and mounts them in ``order``. Adding a command is dropping one
file with one ``COMMAND`` line -- cli.py never changes again. A module opts out
by simply not defining ``COMMAND`` (helpers, this registry, and ``__init__``
are ignored for free, and every name starting with ``_`` is skipped outright).

The declaration is a plain dict on purpose: a command module needs no import
from this registry, so there is no import cycle and nothing to keep in sync.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any

import typer

_VALID_KINDS = ("command", "group")


@dataclass(frozen=True)
class CommandSpec:
    """How one command module attaches to the top-level Typer app."""

    name: str  # subcommand name; "" mounts a group at the root (the escape hatch)
    attr: str  # attribute on the module: the function (command) or the Typer app (group)
    kind: str = "command"  # "command" -> app.command; "group" -> app.add_typer
    order: int = 100  # ascending mount / help order
    hidden: bool = False  # only meaningful for a group

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"CommandSpec({self.name!r}): kind must be one of {_VALID_KINDS}, got {self.kind!r}"
            )
        if self.kind == "command" and not self.name:
            raise ValueError("CommandSpec: a plain command needs a non-empty name")


def _coerce(raw: Any, module_name: str) -> CommandSpec:
    """Turn a module's COMMAND value into a CommandSpec, with a clear error if it cannot."""
    if isinstance(raw, CommandSpec):
        return raw
    if isinstance(raw, dict):
        try:
            return CommandSpec(**raw)
        except TypeError as exc:
            raise ValueError(f"{module_name}.COMMAND has invalid keys: {exc}") from exc
    raise TypeError(
        f"{module_name}.COMMAND must be a dict or CommandSpec (or a list of them), "
        f"got {type(raw).__name__}"
    )


def discover(package: str | None = None) -> list[tuple[CommandSpec, Any]]:
    """Import every sibling module that declares COMMAND; return (spec, module) pairs, ordered."""
    package = package or __package__ or "aitlc.commands"
    pkg = importlib.import_module(package)
    found: list[tuple[CommandSpec, Any]] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue  # this registry, and any private helper module
        module = importlib.import_module(f"{package}.{info.name}")
        raw = getattr(module, "COMMAND", None)
        if raw is None:
            continue  # not a mountable command module
        specs = raw if isinstance(raw, list) else [raw]
        for spec in specs:
            found.append((_coerce(spec, module.__name__), module))
    found.sort(key=lambda pair: (pair[0].order, pair[0].name))
    return found


def register_all(app: typer.Typer, package: str | None = None) -> list[str]:
    """Mount every discovered command onto ``app``; return the mounted names, in order."""
    mounted: list[str] = []
    for spec, module in discover(package):
        target = getattr(module, spec.attr, None)
        if target is None:
            raise AttributeError(
                f"{module.__name__}.COMMAND points at attr {spec.attr!r}, which does not exist"
            )
        if spec.kind == "group":
            app.add_typer(target, name=spec.name, hidden=spec.hidden)
        else:
            app.command(spec.name)(target)
        mounted.append(spec.name)
    return mounted
