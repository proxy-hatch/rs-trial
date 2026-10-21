"""The kit step: the lock is readable and every managed file that is not excluded is present.

devctl cannot import rapid_ship, so this module reads .rapid-ship/kit.lock itself and ignores
its [variables] table. [kit] exclude in project.toml is the only record of an exclusion. A Kit
file the project edited or deleted has a seeded entry after rapid-ship sync, so it is the
project's own and never required.
"""

from __future__ import annotations

import hashlib
import stat
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from devctl.check.steps import CheckContext, StepResult, Tier, failed, passed

LOCK_PATH = ".rapid-ship/kit.lock"
# rapid-ship is fetched from a git tag, so a message that tells the owner to run it names one.
RAPID_SHIP_SOURCE = "git+https://github.com/proxy-hatch/rapid-ship"

_STACKS = ("python", "agnostic")
_KINDS = ("managed", "seeded")


class KitLockError(Exception):
    """The Kit lock is missing or invalid."""


@dataclass(frozen=True)
class _LockFile:
    path: str
    kind: str
    sha256: str | None
    link: str | None


@dataclass(frozen=True)
class _Lock:
    kit_version: str
    files: tuple[_LockFile, ...]  # the [[file]] entries in lock order


def is_excluded(path: str, excluded: Sequence[str]) -> bool:
    """A copy of rapid_ship.kit.templates.is_excluded; test_is_excluded_matches_the_cli pins it.

    True when path is an entry of [kit] exclude or lies below one. Excluding a skill
    directory also excludes its .claude/skills link.
    """
    parts = PurePosixPath(path).parts
    link = len(parts) == 3 and parts[:2] == (".claude", "skills")
    return (link and _covered(f".agents/skills/{parts[2]}", excluded)) or _covered(path, excluded)


def _covered(path: str, excluded: Sequence[str]) -> bool:
    return any(path == entry or path.startswith(f"{entry}/") for entry in excluded)


def _lock_error(reason: object) -> KitLockError:
    return KitLockError(f"{LOCK_PATH}: {reason}")


def _read_lock(root: Path) -> dict[str, object]:
    try:
        return tomllib.loads((root / LOCK_PATH).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _lock_error("missing; run rapid-ship init") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise _lock_error(error) from error


def _lock_file(item: object, number: int) -> _LockFile:
    where = f"[[file]] #{number}"
    if not isinstance(item, dict):
        raise _lock_error(f"{where}: expected a table")
    path, kind = item.get("path"), item.get("kind")
    if not isinstance(path, str):
        raise _lock_error(f"{where}: path must be a string")
    if kind not in _KINDS:
        raise _lock_error(f"{where} ({path}): kind must be managed or seeded")
    sha256, link = item.get("sha256"), item.get("link")
    sha256 = sha256 if isinstance(sha256, str) else None
    link = link if isinstance(link, str) else None
    if kind == "managed" and sha256 is None and link is None:
        raise _lock_error(f"{where} ({path}): a managed entry needs sha256 or link")
    return _LockFile(path, kind, sha256, link)


def _lock(root: Path) -> _Lock:
    """The lock's kit_version and [[file]] entries. Raises KitLockError if it is unreadable."""
    data = _read_lock(root)
    kit_version = data.get("kit_version")
    if not isinstance(kit_version, str):
        raise _lock_error("kit_version must be a string")
    if data.get("stack") not in _STACKS:
        raise _lock_error("stack must be python or agnostic")
    items = data.get("file", [])
    if not isinstance(items, list):
        raise _lock_error("file must be a list of [[file]] entries")
    files = tuple(_lock_file(item, number) for number, item in enumerate(items, start=1))
    return _Lock(kit_version, files)


def _entries(root: Path) -> tuple[_LockFile, ...]:
    """The lock's [[file]] entries in lock order. Raises KitLockError if the lock is unreadable."""
    return _lock(root).files


def _managed(entries: Sequence[_LockFile], excluded: Sequence[str]) -> list[_LockFile]:
    return [
        entry
        for entry in entries
        if entry.kind == "managed" and not is_excluded(entry.path, excluded)
    ]


def managed_paths(root: Path, excluded: Sequence[str]) -> frozenset[str]:
    """The managed paths in the lock, less those the project owns under [kit] exclude."""
    return frozenset(entry.path for entry in _managed(_entries(root), excluded))


def _command(kit_version: str, command: str) -> str:
    return f"uvx --from {RAPID_SHIP_SOURCE}@v{kit_version} rapid-ship {command}"


def rapid_ship_command(root: Path, command: str) -> str:
    """The full command that runs rapid-ship at the lock's kit_version, for devctl's messages.

    Raises KitLockError when the lock is unreadable.
    """
    return _command(_lock(root).kit_version, command)


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _problem(root: Path, entry: _LockFile, kit_version: str) -> str | None:
    """What is wrong with a managed entry's path on disk, or None when it is as the lock says.

    Both of spec section 5's choices are named, because sync alone keeps the deletion.
    """
    target = root / entry.path
    if entry.link is not None:
        present, noun = target.is_symlink(), "link"
    else:
        present, noun = _is_regular_file(target), "file"
    if present:
        return None
    revert = _command(kit_version, f"sync --revert {entry.path}")
    keep = _command(kit_version, "sync")
    return (
        f"{entry.path}: managed {noun} is missing; {revert} restores it, "
        f"and {keep} keeps the deletion as the project's own"
    )


@dataclass(frozen=True)
class KitSummary:
    """Where the lock's managed files stand on disk, for doctor. Paths are in lock order."""

    kit_version: str
    unchanged: tuple[str, ...]
    modified: tuple[str, ...]
    missing: tuple[str, ...]
    # Covered by [kit] exclude in project.toml: the project's own, so never compared.
    excluded: tuple[str, ...]


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _state(root: Path, entry: _LockFile) -> Literal["unchanged", "modified", "missing"]:
    """A managed entry's path against the lock. Presence is read as the kit step reads it.

    A file is present when it is a regular file, so a symlink in its place is missing; a link is
    present when it is a symlink. A file that cannot be read differs from the lock.
    """
    target = root / entry.path
    if entry.link is not None:
        if not target.is_symlink():
            return "missing"
        return "unchanged" if target.readlink() == Path(entry.link) else "modified"
    if not _is_regular_file(target):
        return "missing"
    return "unchanged" if _sha256(target) == entry.sha256 else "modified"


def kit_summary(root: Path, excluded: Sequence[str]) -> KitSummary:
    """Compare the lock's managed entries with the disk. Raises KitLockError if the lock is bad.

    The lock records no exclusion, so the caller passes [kit] exclude. A path it covers is the
    project's own and goes to `excluded` without a look at the disk. The lock's own entry and the
    seeded entries are skipped.
    """
    lock = _lock(root)
    found: dict[str, list[str]] = {"unchanged": [], "modified": [], "missing": [], "excluded": []}
    for entry in lock.files:
        if entry.kind != "managed" or entry.path == LOCK_PATH:
            continue
        state = "excluded" if is_excluded(entry.path, excluded) else _state(root, entry)
        found[state].append(entry.path)
    return KitSummary(
        lock.kit_version,
        tuple(found["unchanged"]),
        tuple(found["modified"]),
        tuple(found["missing"]),
        tuple(found["excluded"]),
    )


class KitStep:
    name = "kit"
    tier: Tier = "fast"

    def run(self, context: CheckContext) -> StepResult:
        root = context.repo.root
        try:
            lock = _lock(root)
        except KitLockError as error:
            return failed(self.name, str(error))
        managed = _managed(lock.files, context.project.excluded)
        problems = [
            problem for entry in managed if (problem := _problem(root, entry, lock.kit_version))
        ]
        return failed(self.name, *problems) if problems else passed(self.name)
