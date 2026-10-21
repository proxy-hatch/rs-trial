"""The file-size step: a source file stays within the line cap unless it has an exception.

A file-size exception is an entry in .rapid-ship/exceptions.toml with `rule = "file-size"` and
the file's path. It has no inline marker, and it fails as stale once the file is within the cap
or gone.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from devctl.check.kit import KitLockError, managed_paths
from devctl.check.steps import (
    CheckContext,
    StepResult,
    Tier,
    blocked,
    failed,
    not_applicable,
    passed,
)
from devctl.repo import RepoError
from devctl.settings import ExceptionEntry, SettingsError, load_exceptions

LINE_CAP = 500

_RULE = "file-size"
_PYTHON_SOURCE = (".py", ".pyi")


class FileSizeStep:
    name = "file-size"
    tier: Tier = "fast"

    def run(self, context: CheckContext) -> StepResult:
        project = context.project
        if not project.source_roots:
            return not_applicable(self.name, "no [map] source_roots")
        root = context.repo.root
        try:
            entries = load_exceptions(root)
            managed = managed_paths(root, project.excluded)
            files = context.repo.files()
            sizes = line_counts(root, files, project.source_roots, managed, project.stack)
        except (SettingsError, KitLockError, RepoError, OSError) as error:
            return blocked(self.name, str(error))
        problems = size_errors(sizes, entries)
        return failed(self.name, *problems) if problems else passed(self.name)


def line_counts(
    root: Path,
    files: Sequence[str],
    source_roots: Sequence[str],
    managed: frozenset[str],
    stack: str,
) -> dict[str, int]:
    """Line count of each source file under a source root.

    Paths are relative to `root`. A file is skipped when it is a Kit file the project does not
    own (`managed`), is not a regular file, is a symlink, or is not UTF-8. On the Python stack
    only `.py` and `.pyi` files count.
    """
    counts: dict[str, int] = {}
    for path in files:
        if path in managed or not any(path.startswith(f"{source}/") for source in source_roots):
            continue
        if stack == "python" and not path.endswith(_PYTHON_SOURCE):
            continue
        target = root / path
        if not target.is_file() or target.is_symlink():
            continue
        try:
            counts[path] = len(target.read_text(encoding="utf-8").splitlines())
        except UnicodeDecodeError:
            continue
    return counts


def size_errors(sizes: dict[str, int], entries: Sequence[ExceptionEntry]) -> list[str]:
    """Over-cap files with no exception and exceptions for files not over the cap, by path."""
    over = {path for path, lines in sizes.items() if lines > LINE_CAP}
    excepted = {entry.path for entry in entries if entry.rule == _RULE}
    errors = [
        (
            path,
            f"{path}: {sizes[path]} lines exceeds the {LINE_CAP}-line cap; "
            f"split the file or add a {_RULE} exception",
        )
        for path in over - excepted
    ]
    errors += [
        (
            entry.path,
            f"{entry.id}: {entry.path} is within the {LINE_CAP}-line cap or gone; "
            f"delete this {_RULE} exception",
        )
        for entry in entries
        if entry.rule == _RULE and entry.path not in over
    ]
    return [message for _path, message in sorted(errors)]
