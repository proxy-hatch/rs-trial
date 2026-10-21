"""The diff step: what a change does to the tests, compared with its base.

The base is the PR base in CI, or the merge base with the default branch locally. The head is
the work tree, with untracked files that Git does not ignore.

A test is removed when it is present at the base and absent everywhere at the head. A test's
identity is its name within its file (`test_x` or `TestA::test_x`), so a test moved to another
file is not removed. Each removal must be declared under `Removed tests:` in the PR's Evidence
section, and is classed unit when its path matches a `[tests] unit` glob, else behavioural.

A change also fails when it adds an assertion that cannot fail, or a new autouse fixture in a
conftest.py without an exception marker. It lists the Protected paths it touches and never fails
on them: CODEOWNERS enforces their approval.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from devctl.check.evidence import removed_test_declarations
from devctl.check.exceptions import MARKER
from devctl.check.stacks.python import (
    autouse_fixtures,
    comments,
    is_test_file,
    tautologies,
    test_identities,
)
from devctl.check.steps import (
    CheckContext,
    StepResult,
    Tier,
    blocked,
    failed,
    not_applicable,
    passed,
)
from devctl.repo import Repo, RepoError

# What reading and parsing a test file raises for text that is not valid Python: bad syntax,
# a null byte, or bytes that are not UTF-8.
_UNPARSABLE = (SyntaxError, ValueError)

_UNDECLARED = "declare it under 'Removed tests:' in the PR's Evidence section"
_NO_BODY = " (pass --pr-body <file> to check a body locally)"
_ALWAYS_TRUE = "is always true; assert on the behaviour instead"
_AUTOUSE = (
    "needs an exception: add '# rs-exception: EX-<n>' on its decorator line and an entry with "
    "rule autouse"
)

# GitHub reads the first of these that exists.
_CODEOWNERS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
# A pattern, then one or more owners: @user, @org/team or an email address. Walk comments out the
# Protected lines, so a leading '#' is allowed; prose comments name no owner and do not match.
_OWNED_LINE = re.compile(r"^#?\s*(?P<pattern>[^\s#]+)(?:\s+(?:@[\w./-]+|\S+@\S+))+\s*$")
_NO_CODEOWNERS = "no CODEOWNERS file; Protected paths are not listed"


@dataclass(frozen=True)
class Change:
    path: str
    status: Literal["A", "M", "D"]


def changed_files(repo: Repo, base: str) -> list[Change]:
    """Changes from base to the work tree, plus untracked files that Git does not ignore.

    They come in path order.
    """
    fields = repo.git("diff", "--name-status", "-z", "--no-renames", base).split("\0")
    changes = [
        Change(path, "A" if status == "A" else "D" if status == "D" else "M")
        for status, path in zip(fields[0::2], fields[1::2], strict=False)
        if path
    ]
    known = {change.path for change in changes}
    untracked = repo.git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    changes += [Change(path, "A") for path in untracked if path and path not in known]
    return sorted(changes, key=lambda change: change.path)


def _removed(identities: Mapping[str, tuple[list[str], list[str]]]) -> list[tuple[str, str]]:
    """The (path, identity) of each removed test, by identity, then path."""
    totals: Counter[str] = Counter()
    places: dict[str, list[str]] = defaultdict(list)
    for path, (before, after) in sorted(identities.items()):
        totals.update(before)
        totals.subtract(after)
        for name, count in sorted((Counter(before) - Counter(after)).items()):
            places[name].extend([path] * count)
    return [
        (path, name)
        for name, paths in sorted(places.items())
        for path in paths[: max(totals[name], 0)]
    ]


def removed_tests(identities: Mapping[str, tuple[list[str], list[str]]]) -> list[str]:
    """identities maps each changed test file to its test identities at (base, head).

    Files that did not change hold the same tests at both ends, so they are left out.
    """
    return [f"{path}::{name}" for path, name in _removed(identities)]


def _glob(pattern: str) -> str:
    parts, index = [], 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        else:
            char = pattern[index]
            parts.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
            index += 1
    return "".join(parts)


def glob_match(pattern: str, path: str) -> bool:
    """Whether path matches pattern; "**" spans directories, "*" and "?" do not."""
    return re.fullmatch(_glob(pattern), path) is not None


def codeowners_match(pattern: str, path: str) -> bool:
    """Whether a CODEOWNERS pattern covers path, by gitignore's rules.

    A pattern with a '/' before its end is anchored at the root; any other matches at any depth.
    A trailing '/' matches only below that directory. Otherwise the pattern matches the path or
    anything below it.
    """
    below_only = pattern.endswith("/")
    name = pattern.rstrip("/")
    anywhere = "" if "/" in name else "(?:.*/)?"
    below = "/.*" if below_only else "(?:/.*)?"
    return re.fullmatch(anywhere + _glob(name.lstrip("/")) + below, path) is not None


def protected_patterns(root: Path) -> list[str] | None:
    """The Protected-path patterns in the CODEOWNERS file GitHub reads; None without one.

    Commented-out lines count, so the set is listed in Walk too. The catch-all '*' is Walk's
    line for every path, not a Protected path. Raises OSError when the file cannot be read.
    """
    for name in _CODEOWNERS:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise OSError(f"{name} is not UTF-8: {error}") from error
        matches = (_OWNED_LINE.match(line) for line in text.splitlines())
        return [match["pattern"] for match in matches if match and match["pattern"] != "*"]
    return None


def _protected(root: Path, changes: Sequence[Change]) -> list[str]:
    patterns = protected_patterns(root)
    if patterns is None:
        return [_NO_CODEOWNERS]
    return sorted(
        f"protected: {change.path}"
        for change in changes
        if any(codeowners_match(pattern, change.path) for pattern in patterns)
    )


def _base_tests(repo: Repo, base: str, path: str) -> list[str] | None:
    """The tests in path at base; None when that text does not parse."""
    try:
        return test_identities(repo.git("show", f"{base}:{path}"))
    except _UNPARSABLE:
        return None


def _changed_tests(
    repo: Repo, base: str, changes: Sequence[Change]
) -> tuple[dict[str, tuple[list[str], list[str]]], list[str]]:
    """Each changed test file's tests at (base, head), and a problem per file that stops parsing.

    A head that does not parse counts as holding its base's tests, so its problem is reported
    once rather than as a removal of each test.
    """
    identities: dict[str, tuple[list[str], list[str]]] = {}
    problems: list[str] = []
    for change in changes:
        if not is_test_file(change.path):
            continue
        before = None if change.status == "A" else _base_tests(repo, base, change.path)
        head = repo.root / change.path
        try:
            after = (
                [] if change.status == "D" else test_identities(head.read_text(encoding="utf-8"))
            )
        except _UNPARSABLE as error:
            if before is not None:
                problems.append(f"{change.path}: cannot parse the tests at head ({error})")
            after = before or []
        identities[change.path] = (before or [], after)
    return identities, problems


def _removals(
    context: CheckContext, removed: Sequence[tuple[str, str]]
) -> tuple[list[str], list[str]]:
    """A problem for each undeclared removal and a note for each declared one, in removed order.

    Whether a declared removal goes to the owner is the escalation list's business, not this
    step's (Spec 1 section 5).
    """
    body = context.pr_body
    declared = {} if body is None else removed_test_declarations(body)[0]
    problems: list[str] = []
    notes: list[str] = []
    for path, name in removed:
        test = f"{path}::{name}"
        unit = any(glob_match(pattern, path) for pattern in context.project.unit_tests)
        removal = f"removed {'unit' if unit else 'behavioural'} test {test}"
        if test not in declared:
            problems.append(f"{removal}: {_UNDECLARED}{_NO_BODY if body is None else ''}")
        else:
            notes.append(f"{removal}: declared ({declared[test]})")
    return problems, notes


def _found[T](find: Callable[[str], list[T]], source: str) -> list[T]:
    """What find returns for source; nothing when source is not valid Python."""
    try:
        return find(source)
    except _UNPARSABLE:
        return []


def _texts(repo: Repo, base: str, change: Change) -> tuple[str, str]:
    """A changed file's text at base and at head; the change must not delete it.

    An added file is empty at base. A head that is not UTF-8 reads as empty, so, like text that
    does not parse, it holds no tautology or fixture.
    """
    before = "" if change.status == "A" else repo.git("show", f"{base}:{change.path}")
    try:
        return before, (repo.root / change.path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return before, ""


def _added_tautologies(path: str, before: str, after: str) -> list[str]:
    """A problem for each tautology at head beyond the copies of it at base.

    Copies are matched by their text. The first copies in the file count as the existing ones,
    so an added copy is reported at its later line.
    """
    existing = Counter(text for _line, text in _found(tautologies, before))
    problems: list[str] = []
    for line, text in sorted(_found(tautologies, after)):
        if existing[text] > 0:
            existing[text] -= 1
        else:
            problems.append(f"{path}:{line}: {text} {_ALWAYS_TRUE}")
    return problems


def _new_autouse(path: str, before: str, after: str) -> list[str]:
    """A problem for each autouse fixture new at head without a marker on its decorator line.

    The exceptions step checks that the marked ID exists and is for rule autouse.
    """
    existing = {name for name, _line in _found(autouse_fixtures, before)}
    comment_by_line = comments(after)
    return [
        f"{path}:{line}: new autouse fixture {name} {_AUTOUSE}"
        for name, line in sorted(_found(autouse_fixtures, after), key=lambda fixture: fixture[1])
        if name not in existing and not MARKER.search(comment_by_line.get(line, ""))
    ]


def _python_rules(
    context: CheckContext, base: str, changes: Sequence[Change]
) -> tuple[list[str], list[str]]:
    """Problems, then notes, from the removed-test, tautology and autouse rules."""
    repo = context.repo
    identities, unparsable = _changed_tests(repo, base, changes)
    problems, notes = _removals(context, _removed(identities))
    problems += unparsable
    kept = [change for change in changes if change.status != "D"]
    for change in kept:
        if is_test_file(change.path):
            problems += _added_tautologies(change.path, *_texts(repo, base, change))
    for change in kept:
        if PurePosixPath(change.path).name == "conftest.py":
            problems += _new_autouse(change.path, *_texts(repo, base, change))
    return problems, notes


class DiffStep:
    name = "diff"
    tier: Tier = "full"

    def run(self, context: CheckContext) -> StepResult:
        base = context.base
        if base is None:
            return not_applicable(
                self.name, "no base commit: not a change (pass --base to check one)"
            )
        problems: list[str] = []
        notes: list[str] = []
        try:
            changes = changed_files(context.repo, base)
            if context.project.stack == "python":
                problems, notes = _python_rules(context, base, changes)
            notes += _protected(context.repo.root, changes)
        except (RepoError, OSError) as error:
            return blocked(self.name, str(error))
        return failed(self.name, *problems, *notes) if problems else passed(self.name, *notes)
