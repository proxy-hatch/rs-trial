"""The exceptions step: each suppression has exactly one listed, current exception.

A suppression is a lint, type, coverage, test or secret-scan opt-out. It carries
`# rs-exception: EX-<n>` on its line, and .rapid-ship/exceptions.toml has one entry for that
ID. This module and the Python rows in stacks/ are the one definition of suppressions and
their rule names; rapid_ship/kit/adopt.py copies the Python rows.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from devctl.check.kit import KitLockError, managed_paths
from devctl.check.stacks.agnostic import text_rules
from devctl.check.stacks.python import autouse_fixtures, comment_rules, comments, marker_rules
from devctl.check.steps import CheckContext, StepResult, Tier, blocked, failed, passed
from devctl.repo import RepoError
from devctl.settings import ExceptionEntry, SettingsError, load_exceptions

MARKER = re.compile(r"rs-exception:\s*(?P<id>EX-\d+)")

_PYTHON_SOURCE = (".py", ".pyi")


@dataclass(frozen=True)
class SuppressedLine:
    path: str
    number: int
    rules: tuple[str, ...]  # suppressions that need an exception
    optional: tuple[str, ...]  # autouse fixtures: validated when marked, required only by diff
    markers: tuple[str, ...]


class ExceptionsStep:
    name = "exceptions"
    tier: Tier = "fast"

    def run(self, context: CheckContext) -> StepResult:
        root = context.repo.root
        try:
            entries = load_exceptions(root)
            managed = managed_paths(root, context.project.excluded)
            lines = scan(root, context.repo.files(), managed, context.project.stack)
        except (SettingsError, KitLockError, RepoError, OSError) as error:
            return blocked(self.name, str(error))
        errors = exception_errors(lines, entries)
        return failed(self.name, *errors) if errors else passed(self.name)


def scan(
    root: Path, files: Sequence[str], managed: frozenset[str], stack: str
) -> list[SuppressedLine]:
    """The lines that carry a suppression or, in Python source, an exception marker.

    Paths are relative to `root`. A file is skipped when it is a Kit file the project does not
    own (`managed`), is not a regular file, is a symlink, or is not UTF-8. Lines come in path
    then line order.
    """
    found: list[SuppressedLine] = []
    for path in sorted(files):
        if path in managed:
            continue
        target = root / path
        if not target.is_file() or target.is_symlink():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if stack == "python" and path.endswith(_PYTHON_SOURCE):
            found += _python_lines(path, text)
        else:
            found += _text_lines(path, text)
    return found


def _markers(text: str) -> tuple[str, ...]:
    return tuple(match.group("id") for match in MARKER.finditer(text))


def _python_lines(path: str, source: str) -> list[SuppressedLine]:
    """Suppressions come from comment tokens and the syntax tree, never from strings."""
    comment_by_line = comments(source)
    marks: dict[int, list[str]] = {}
    for number, rule in marker_rules(source):
        marks.setdefault(number, []).append(rule)
    autouse: dict[int, list[str]] = {}
    if PurePosixPath(path).name == "conftest.py":
        for _name, number in autouse_fixtures(source):
            autouse.setdefault(number, []).append("autouse")
    lines: list[SuppressedLine] = []
    for number in sorted(comment_by_line.keys() | marks.keys() | autouse.keys()):
        comment = comment_by_line.get(number, "")
        rules = (*comment_rules(comment), *text_rules(path, comment), *marks.get(number, ()))
        markers = _markers(comment)
        if rules or markers:
            lines.append(
                SuppressedLine(path, number, rules, tuple(autouse.get(number, ())), markers)
            )
    return lines


def _text_lines(path: str, text: str) -> list[SuppressedLine]:
    """Every line of a non-Python file. A marker counts only on a line with a suppression."""
    lines: list[SuppressedLine] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if rules := text_rules(path, line):
            lines.append(SuppressedLine(path, number, tuple(rules), (), _markers(line)))
    return lines


def normal_rule(rule: str) -> str:
    """'S607, S603' -> 'S603,S607'; 'zizmor:b, a' -> 'zizmor:a,b'."""
    prefix, colon, names = rule.strip().rpartition(":")
    parts = sorted(part for part in re.split(r"[\s,]+", names) if part)
    return f"{prefix.strip()}{colon}{','.join(parts)}"


def exception_errors(
    lines: Sequence[SuppressedLine], entries: Sequence[ExceptionEntry]
) -> list[str]:
    # load_exceptions has already checked the fields, the ID format and uniqueness.
    by_id = {entry.id: entry for entry in entries}
    errors: list[str] = []
    uses = Counter(marker for line in lines for marker in line.markers)
    for line in lines:
        errors += _line_errors(line, by_id)
    errors += [f"{i} is used on {n} lines; each ID is used once" for i, n in uses.items() if n > 1]
    errors += [
        f"{entry.id} is stale: no suppression carries it; delete the entry"
        for entry in by_id.values()
        if entry.rule != "file-size" and uses[entry.id] == 0
    ]
    return errors


def _line_errors(line: SuppressedLine, by_id: Mapping[str, ExceptionEntry]) -> list[str]:
    where = f"{line.path}:{line.number}"
    required, optional = list(line.rules), list(line.optional)
    errors: list[str] = []
    for marker in line.markers:
        entry = by_id.get(marker)
        if entry is None:
            errors.append(f"{where}: {marker} is not in .rapid-ship/exceptions.toml")
        elif entry.path != line.path:
            errors.append(f"{where}: {marker} names {entry.path}, not this file")
        elif (rule := normal_rule(entry.rule)) in required:
            required.remove(rule)
        elif rule in optional:
            optional.remove(rule)
        else:
            errors.append(
                f"{where}: {marker} is for rule {entry.rule}, which is not suppressed on this line"
            )
    errors += [
        f"{where}: suppression {rule} needs '# rs-exception: EX-<n>' and an entry in "
        ".rapid-ship/exceptions.toml"
        for rule in required
    ]
    return errors
