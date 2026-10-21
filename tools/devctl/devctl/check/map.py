"""The map step: guide links resolve, every maintained directory has a guide and every guide
is reachable, feature files, Patterns and Rules name things that exist, and every skill has its
discovery alias and front matter."""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shlex
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import yaml

from devctl.check.markdown import ListItem, front_matter, navigation_links, rules_items
from devctl.check.references import path_exists, resolve
from devctl.check.steps import CheckContext, StepResult, Tier, blocked, failed, passed
from devctl.repo import RepoError
from devctl.settings import ProjectSettings

ROOT_GUIDE = "AGENTS.md"
FEATURES = "docs/features"
PATTERNS = "docs/patterns"
SKILLS = ".agents/skills"
# The reference may be wrapped in backticks, as in "enforced by: `check:map`".
_RULE_END = re.compile(r"(?:^|\s)(?:enforced by: `?(?P<reference>[^\s`]+)`?|advisory)$")


class MapStep:
    name = "map"
    tier: Tier = "fast"

    def run(self, context: CheckContext) -> StepResult:
        try:
            files = context.repo.files()
        except RepoError as error:
            return blocked(self.name, str(error))
        root = context.repo.root
        found = guides(root, files, context.project)
        errors = [
            *link_errors(root, files, found),
            *coverage_errors(files, context.project),
            *reachability_errors(root, found),
            *feature_errors(context, files),
            *pattern_errors(context, files),
            *rule_errors(context, found),
            *skill_alias_errors(root, files),
            *skill_errors(root, files),
        ]
        return failed(self.name, *errors) if errors else passed(self.name)


def guides(root: Path, files: Sequence[str], project: ProjectSettings) -> list[str]:
    """The AGENTS.md files among `files` that exist as files, less those under [map] exclude."""
    return [
        path
        for path in files
        if PurePosixPath(path).name == "AGENTS.md"
        and (root / path).is_file()
        and not _map_excluded(path, project)
    ]


def _map_excluded(path: str, project: ProjectSettings) -> bool:
    """Whether `path` is an entry of [map] exclude or lies under one."""
    return any(path == entry or path.startswith(f"{entry}/") for entry in project.map_exclude)


def link_errors(root: Path, files: Sequence[str], found: Sequence[str]) -> list[str]:
    """Map rule 1: each local link in each guide reaches a file the checkout maintains."""
    maintained = set(files)
    maintained.update(parent.as_posix() for path in files for parent in PurePosixPath(path).parents)
    aliases = [root / path for path in files if (root / path).is_symlink()]
    errors: list[str] = []
    for guide in found:
        try:
            links = _links(root, guide)
        except (OSError, ValueError) as error:
            errors.append(f"{guide}:{error}")
            continue
        for line, target in links:
            if problem := _link_error(root, guide, line, target, maintained, aliases):
                errors.append(problem)
    return errors


def _links(root: Path, guide: str) -> list[tuple[int, str]]:
    """The (line, target) of each link in `guide`. OSError or ValueError when it cannot be read."""
    return navigation_links((root / guide).read_text(encoding="utf-8"))


def _local_path(target: str) -> str | None:
    """The unescaped, unquoted path of a link to a local file or directory, else None."""
    target = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])", r"\1", target)
    url = urlsplit(target)
    if url.scheme or url.netloc or not url.path:
        return None
    return unquote(url.path)


def _link_error(
    root: Path,
    guide: str,
    line: int,
    target: str,
    maintained: set[str],
    aliases: Sequence[Path],
) -> str | None:
    path = _local_path(target)
    if path is None:
        return None
    dependency = Path(os.path.normpath((root / guide).parent / path))
    if not dependency.exists():
        return f"{guide}:{line}: missing target {target!r}; update the navigation link"
    relative = dependency.relative_to(root).as_posix() if dependency.is_relative_to(root) else None
    # Git records directory aliases, not their descendants. Both the navigation
    # path (or its alias) and the resolved destination must belong to the checkout.
    resolved, real_root = dependency.resolve(), root.resolve()
    if (
        (relative in maintained or any(dependency.is_relative_to(alias) for alias in aliases))
        and resolved.is_relative_to(real_root)
        and resolved.relative_to(real_root).as_posix() in maintained
    ):
        return None
    return (
        f"{guide}:{line}: target {target!r} is not maintained in this checkout; "
        "track its source or update the navigation link"
    )


def coverage_errors(files: Sequence[str], project: ProjectSettings) -> list[str]:
    """Map rule 2: each top-level directory and source-root child has a guide or is unguided."""
    present = set(files)
    errors = [
        f"{root}: source root has no files; fix [map] source_roots"
        for root in project.source_roots
        if not any(path.startswith(f"{root}/") for path in files)
    ]
    needing = {path.split("/", 1)[0] for path in files if "/" in path}
    for root in project.source_roots:
        needing.update(_children(files, root))
    errors += [
        f"{directory}/: add an AGENTS.md guide, or list the directory under [map] unguided"
        for directory in sorted(needing)
        if _needs_guide(directory, project) and f"{directory}/AGENTS.md" not in present
    ]
    return errors


def _children(files: Sequence[str], root: str) -> set[str]:
    """`<root>/<child>` for each child directory of `root` that holds a file."""
    prefix = f"{root}/"
    return {
        prefix + path[len(prefix) :].split("/", 1)[0]
        for path in files
        if path.startswith(prefix) and "/" in path[len(prefix) :]
    }


def _needs_guide(directory: str, project: ProjectSettings) -> bool:
    name = PurePosixPath(directory).name
    # Python writes bytecode into __pycache__ on import; hidden directories hold tool state.
    if name.startswith(".") or name == "__pycache__":
        return False
    return directory not in project.unguided and not _map_excluded(directory, project)


def reachability_errors(root: Path, found: Sequence[str]) -> list[str]:
    """Map rule 3: every guide is reached from the root guide by links, directly or by directory."""
    if ROOT_GUIDE not in found:
        return [f"{ROOT_GUIDE}: add a root guide that links every other guide"]
    reached = {ROOT_GUIDE}
    pending = deque([ROOT_GUIDE])
    while pending:
        guide = pending.popleft()
        targets = set(_guide_targets(root, guide))
        for other in found:
            holder = posixpath.dirname(other) or "."
            if other not in reached and (other in targets or holder in targets):
                reached.add(other)
                pending.append(other)
    return [
        f"{guide}: not reachable by links from {ROOT_GUIDE}; link it from its parent guide"
        for guide in sorted(set(found) - reached)
    ]


def _guide_targets(root: Path, guide: str) -> list[str]:
    """Where `guide`'s local links point, as repository-relative paths. An unreadable guide has
    none; `link_errors` reports it."""
    try:
        links = _links(root, guide)
    except (OSError, ValueError):
        return []
    directory = posixpath.dirname(guide)
    paths = (_local_path(target) for _, target in links)
    return [posixpath.normpath(posixpath.join(directory, path)) for path in paths if path]


def skill_alias_errors(root: Path, files: Sequence[str]) -> list[str]:
    """Map rule 7, alias half: each skill has a SKILL.md and a symlink in .claude/skills."""
    present = set(files)
    canonical = _skill_names(root, files, ".agents/skills")
    aliases = _skill_names(root, files, ".claude/skills")
    errors = [_canonical_error(root, name) for name in sorted(canonical)]
    errors += [_alias_error(root, present, name) for name in sorted(canonical)]
    errors += [
        f".claude/skills/{name}: no matching .agents/skills/{name} in Git; "
        "remove the alias or add the skill"
        for name in sorted(aliases - canonical)
    ]
    errors += [
        f"{path}: only skill links belong in .claude/skills; remove it"
        for path in _stray_files(root, files)
    ]
    return [error for error in errors if error]


def _skill_names(root: Path, files: Sequence[str], surface: str) -> set[str]:
    names: set[str] = set()
    for path in files:
        if path.startswith(f"{surface}/"):
            name = path[len(surface) + 1 :].split("/", 1)[0]
            entry = root / surface / name
            if entry.is_symlink() or entry.is_dir():  # files are not skills
                names.add(name)
    return names


def _stray_files(root: Path, files: Sequence[str]) -> list[str]:
    """Files directly under .claude/skills, such as a guide; rule 7 allows only skill links."""
    names = {path.split("/")[2] for path in files if path.startswith(".claude/skills/")}
    entries = {name: root / ".claude/skills" / name for name in names}
    return sorted(
        f".claude/skills/{name}"
        for name, entry in entries.items()
        if not entry.is_symlink() and not entry.is_dir()
    )


def _canonical_error(root: Path, name: str) -> str | None:
    entry = root / ".agents/skills" / name
    if entry.is_symlink() and (
        entry.readlink().is_absolute() or not entry.resolve().is_relative_to(root.resolve())
    ):
        return f".agents/skills/{name}: make it a relative symlink inside the repository"
    if not (entry / "SKILL.md").is_file():
        return f".agents/skills/{name}: missing SKILL.md"
    return None


def _alias_error(root: Path, present: set[str], name: str) -> str | None:
    relative = f".claude/skills/{name}"
    alias, canonical = root / relative, root / ".agents/skills" / name
    expected = f"../../.agents/skills/{name}"
    if relative in present and alias.is_symlink():
        resolved = alias.resolve()
        inside = resolved.is_relative_to(root.resolve())
        if not alias.readlink().is_absolute() and inside and resolved == canonical.resolve():
            return None
        return f"{relative}: make it a relative symlink to {expected}"
    if (
        alias.is_dir()
        and not alias.is_symlink()
        and any(p.startswith(f"{relative}/") for p in present)
    ):
        return (
            f"{relative}: a copy is not an alias; replace it with a relative symlink to {expected}"
        )
    return f"{relative}: add a relative symlink to {expected} and track it in Git"


def registered_verbs() -> frozenset[str]:
    """The devctl verbs, including the project's own from devctl_project.register."""
    from devctl.cli import build_parser  # Imported late: devctl.cli imports devctl.check.

    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return frozenset(action.choices)
    return frozenset()


def _front_matter(root: Path, path: str, keys: str) -> dict[str, object] | str:
    """The front matter of `path` as a mapping, or the message that says what is wrong."""
    try:
        data = front_matter((root / path).read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as error:
        return f"{path}: {' '.join(str(error).split())}"
    if data is None:
        return f"{path}: no front matter; start the file with --- and the keys {keys}"
    if not isinstance(data, dict):
        return f"{path}: front matter must be a mapping"
    return data


def _text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_list(data: dict[str, object], key: str, *, allow_empty: bool) -> list[str] | None:
    """The list under `key`, or None when it is missing, not a list of text or not allowed empty."""
    value = data.get(key)
    if not isinstance(value, list) or (not value and not allow_empty):
        return None
    if not all(isinstance(item, str) and item.strip() for item in value):
        return None
    return value


def _documents(root: Path, files: Sequence[str], directory: str) -> list[str]:
    """The Markdown files under `directory`, other than its README, that exist as files."""
    return [
        path
        for path in files
        if path.startswith(f"{directory}/")
        and path.endswith(".md")
        and path != f"{directory}/README.md"
        and (root / path).is_file()
    ]


def _document_errors(
    context: CheckContext,
    files: Sequence[str],
    directory: str,
    keys: str,
    problems: Callable[[CheckContext, str, dict[str, object]], list[str]],
) -> list[str]:
    errors: list[str] = []
    for path in _documents(context.repo.root, files, directory):
        data = _front_matter(context.repo.root, path, keys)
        errors.extend([data] if isinstance(data, str) else problems(context, path, data))
    return errors


def feature_errors(context: CheckContext, files: Sequence[str]) -> list[str]:
    """Map rule 4: each feature file has front matter, and what it names exists."""
    keys = "purpose, entry_points, data, observe and reproduce"
    return _document_errors(context, files, FEATURES, keys, _feature_problems)


def _feature_problems(context: CheckContext, path: str, data: dict[str, object]) -> list[str]:
    problems: list[str] = []
    if _text(data, "purpose") is None:
        problems.append(f"{path}: purpose is missing or empty")
    for key, allow_empty in (("entry_points", False), ("data", True)):
        items = _string_list(data, key, allow_empty=allow_empty)
        if items is None:
            problems.append(f"{path}: {key} is missing or empty")
            continue
        problems += [
            f"{path}: {key} names {item}, which does not exist"
            for item in items
            if not path_exists(context.repo.root, item)
        ]
    if _text(data, "observe") is None:
        problems.append(f"{path}: observe is missing or empty")
    if problem := _reproduce_problem(path, data):
        problems.append(problem)
    return problems


def _reproduce_problem(path: str, data: dict[str, object]) -> str | None:
    command = _text(data, "reproduce")
    if command is None:
        return f"{path}: reproduce is missing or empty"
    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    if words[:1] != ["./devctl"]:
        return f"{path}: reproduce must be a ./devctl command"
    if len(words) < 2:
        return f"{path}: reproduce names no verb after ./devctl"
    if words[1] not in registered_verbs():
        return f"{path}: reproduce runs {command}; {words[1]} is not a devctl verb"
    return None


def pattern_errors(context: CheckContext, files: Sequence[str]) -> list[str]:
    """Map rule 5: each Pattern file has a reference that exists and enforcement that resolves."""
    return _document_errors(
        context, files, PATTERNS, "reference and enforced_by", _pattern_problems
    )


def _pattern_problems(context: CheckContext, path: str, data: dict[str, object]) -> list[str]:
    problems: list[str] = []
    reference = _text(data, "reference")
    if reference is None:
        problems.append(f"{path}: reference is missing or empty")
    elif not path_exists(context.repo.root, reference):
        problems.append(f"{path}: reference names {reference}, which does not exist")
    enforced = _string_list(data, "enforced_by", allow_empty=False)
    if enforced is None:
        problems.append(f"{path}: enforced_by is missing or empty")
        return problems
    reasons = (resolve(item, context) for item in enforced)
    return problems + [f"{path}: {reason}" for reason in reasons if reason is not None]


def rule_errors(context: CheckContext, found: Sequence[str]) -> list[str]:
    """Map rule 6: each rule under a Rules heading ends with a marker whose reference resolves."""
    errors: list[str] = []
    for guide in found:
        try:
            items = rules_items((context.repo.root / guide).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # link_errors reports a guide that cannot be read
        errors += [problem for item in items if (problem := _rule_problem(context, guide, item))]
    return errors


def _rule_problem(context: CheckContext, guide: str, item: ListItem) -> str | None:
    ending = _RULE_END.search(item.text)
    if ending is None:
        return f"{guide}:{item.line}: end the rule with 'enforced by: <reference>' or 'advisory'"
    reference = ending.group("reference")
    reason = resolve(reference, context) if reference else None
    return f"{guide}:{item.line}: {reason}" if reason else None


def skill_errors(root: Path, files: Sequence[str]) -> list[str]:
    """Map rule 7, front-matter half: each SKILL.md names its directory and has a description."""
    errors: list[str] = []
    for name in sorted(_skill_names(root, files, SKILLS)):
        path = f"{SKILLS}/{name}/SKILL.md"
        if not (root / path).is_file():
            continue  # skill_alias_errors reports the missing SKILL.md
        data = _front_matter(root, path, "name and description")
        if isinstance(data, str):
            errors.append(data)
            continue
        if data.get("name") != name:
            errors.append(f"{path}: name must be {name}, the directory name")
        if _text(data, "description") is None:
            errors.append(f"{path}: description is missing or empty")
    return errors
