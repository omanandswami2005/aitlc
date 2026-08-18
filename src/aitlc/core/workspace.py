"""One directory for everything a run produces.

Every command writes something — traces, cached artifacts, session state,
status files, browser profiles, logs — and by default they all landed under
`reports/`, mixed in with whatever else the project puts there. Finding "the
things from the investigation I am doing right now" then means knowing which
of a dozen subdirectories each command happens to use.

A workspace makes that one directory. Point it at the thing being worked on:

    aitlc --workspace PROJ-29019 debug start PROJ-29019

and every artifact from every command lands under `PROJ-29019/`, so a person
or an agent can look in one place instead of learning the layout. Switch the
name and the previous investigation stays intact beside it, which also makes
"what did we collect last time" answerable by looking rather than by
remembering.

Resolution order, most specific first:

1. `set_workspace()` — what `--workspace` on the command line calls.
2. `AITLC_WORKSPACE` in the environment, for a shell that is working on one
   thing for a while.
3. `[project].workspace` in `aitlc.toml`, for a project that always wants it.
4. `reports`, the historical default, so nothing moves for anyone who does
   not ask for this.

The path is deliberately *relative to the project root*, not absolute: an
absolute path would scatter artifacts outside the repo where `.gitignore`
does not reach them, and the point of this is to make them easy to find and
easy to throw away together.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_WORKSPACE = "reports"

_override: str | None = None
_config_default: str | None = None


class WorkspaceError(ValueError):
    """A workspace name that cannot be used as a directory."""


def _validate(name: str) -> str:
    """Reject names that would escape the project or confuse a filesystem.

    A workspace is a directory *inside* the project. `..` or an absolute path
    would put artifacts somewhere the repo does not cover, which defeats both
    halves of the point — finding them together and deleting them together.
    """
    raw = (name or "").strip()
    if not raw:
        raise WorkspaceError("workspace name is empty")
    # Absoluteness is judged on the raw text, before any trimming. Stripping
    # a leading slash first would quietly turn `-w /etc` into `<project>/etc`
    # -- a path the caller never asked for, and the opposite of refusing it.
    if Path(raw).is_absolute():
        raise WorkspaceError(
            f"workspace must be relative to the project root, not absolute: {name!r}"
        )
    cleaned = raw.rstrip("/")
    if not cleaned:
        raise WorkspaceError("workspace name is empty")
    if ".." in Path(cleaned).parts:
        raise WorkspaceError(
            f"workspace must stay inside the project: {name!r}"
        )
    return cleaned


def set_workspace(name: str | None) -> None:
    """Set the workspace for this process. None restores normal resolution."""
    global _override
    _override = _validate(name) if name else None


def set_config_default(name: str | None) -> None:
    """Register `[project].workspace`, the lowest-precedence source.

    Called when config loads so core helpers that only receive a `root_dir`
    still honour the file, without every one of them having to be handed a
    config object.
    """
    global _config_default
    _config_default = _validate(name) if name else None


def current_workspace(config_value: str | None = None) -> str:
    """The workspace in effect, by the documented precedence."""
    if _override:
        return _override
    from_env = os.environ.get("AITLC_WORKSPACE")
    if from_env:
        return _validate(from_env)
    if config_value:
        return _validate(config_value)
    if _config_default:
        return _config_default
    return DEFAULT_WORKSPACE


def output_root(root_dir: Path | str, config_value: str | None = None) -> Path:
    """The directory every artifact goes under, for this project."""
    return Path(root_dir) / current_workspace(config_value)


def output_path(root_dir: Path | str, *parts: str) -> Path:
    """A path inside the workspace. Does not create anything."""
    return output_root(root_dir).joinpath(*parts)


def ensure(root_dir: Path | str, *parts: str) -> Path:
    """A path inside the workspace, with its parent directory created.

    Callers used to `mkdir(parents=True)` at each of nineteen sites, and
    forgetting it produced a `FileNotFoundError` from whichever command was
    unlucky rather than from anything to do with the missing directory.
    """
    path = output_path(root_dir, *parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
