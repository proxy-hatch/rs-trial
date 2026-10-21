"""Resolve references: the names of the checks that enforce a Rule or a Pattern.

A reference is `<kind>:<value>`. `check` and `test` work for every stack. `ruff`, `banned`,
`contract` and `type` read pyproject.toml and exist only for the Python stack.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from devctl.check.stacks.python import test_identities
from devctl.check.steps import CheckContext

# Ruff selects these when `select` is not set.
_RUFF_DEFAULT_SELECT = ["E4", "E7", "E9", "F"]
_PYTHON_KINDS = frozenset({"ruff", "banned", "contract", "type"})


class _Unreadable(Exception):
    """pyproject.toml is missing or invalid. The message names what is wrong."""


def path_exists(root: Path, path: str) -> bool:
    """Whether `path` names a file or directory inside the repository at `root`."""
    parts = PurePosixPath(path).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts or "\\" in path:
        return False
    return (root / path).exists()


def resolve(reference: str, context: CheckContext) -> str | None:
    """None when the reference resolves, else why it does not."""
    kind, _, value = reference.partition(":")
    if not kind or not value:
        return f"{reference}: write <kind>:<value>"
    if kind == "check":
        return _check(reference, value, context)
    if kind == "test":
        return _test(reference, value, context)
    if kind not in _PYTHON_KINDS:
        return f"{reference}: unknown reference kind {kind}"
    if context.project.stack != "python":
        return f"{reference}: unknown reference kind {kind} for the {context.project.stack} stack"
    try:
        data = _pyproject(context.repo.root)
        return _PYTHON_RESOLVERS[kind](reference, value, context, data)
    except _Unreadable as error:
        return f"{reference}: {error}"


def _check(reference: str, step: str, context: CheckContext) -> str | None:
    from devctl.check import all_steps  # Imported late: devctl.check imports this module.

    if any(candidate.name == step for candidate in all_steps(context.project)):
        return None
    return f"{reference}: no devctl check step is named {step}"


def _test(reference: str, value: str, context: CheckContext) -> str | None:
    path, separator, name = value.partition("::")
    if not path or not separator or not name:
        return f"{reference}: write test:<path>::<name>"
    if not path_exists(context.repo.root, path) or not (context.repo.root / path).is_file():
        return f"{reference}: {path} is not a file"
    try:
        text = (context.repo.root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return f"{reference}: cannot read {path}: {error}"
    if context.project.stack == "python":
        try:
            found = name in test_identities(text)
        except SyntaxError as error:
            return f"{reference}: {path} does not parse: {error.msg} (line {error.lineno})"
    else:
        found = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) is not None
    return None if found else f"{reference}: {path} has no test named {name}"


def _pyproject(root: Path) -> dict[str, object]:
    try:
        return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _Unreadable("pyproject.toml is missing") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise _Unreadable(f"pyproject.toml: {error}") from error


def _table(data: dict[str, object], *path: str) -> dict[str, object]:
    """The table at `path` in pyproject.toml; empty when absent."""
    node: object = data
    for key in path:
        if not isinstance(node, dict):
            break
        node = node.get(key, {})
    if not isinstance(node, dict):
        raise _Unreadable(f"[{'.'.join(path)}] must be a table")
    return node


def _strings(
    table: dict[str, object], key: str, where: str, *, split_commas: bool = False
) -> list[str] | None:
    """A list of strings, or a lone string; None when absent. A string is one item, or with
    split_commas a comma-separated list, as mypy reads `files`."""
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, str):
        items = value.split(",") if split_commas else [value]
        return [item.strip() for item in items if item.strip()]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise _Unreadable(f"{where} {key} must be a list of strings")


def _longest(prefixes: Iterable[str], code: str) -> int:
    """The length of the longest prefix that begins `code`, or -1. ALL matches with length 0."""
    return max(
        (
            0 if prefix == "ALL" else len(prefix)
            for prefix in prefixes
            if prefix == "ALL" or code.startswith(prefix)
        ),
        default=-1,
    )


def _ruff_setting(data: dict[str, object], key: str) -> list[str] | None:
    # `[tool.ruff.lint]` wins; the deprecated top-level `[tool.ruff]` setting fills the gaps.
    lint = _strings(_table(data, "tool", "ruff", "lint"), key, "[tool.ruff.lint]")
    return lint if lint is not None else _strings(_table(data, "tool", "ruff"), key, "[tool.ruff]")


def _ruff(reference: str, code: str, context: CheckContext, data: dict[str, object]) -> str | None:
    select = _ruff_setting(data, "select")
    selected = _RUFF_DEFAULT_SELECT if select is None else select
    selected = [*selected, *(_ruff_setting(data, "extend-select") or [])]
    ignored = [
        *(_ruff_setting(data, "ignore") or []),
        *(_ruff_setting(data, "extend-ignore") or []),
    ]
    if _longest(selected, code) > _longest(ignored, code):
        return None
    return f"{reference}: Ruff does not select {code} in pyproject.toml"


def _banned(
    reference: str, member: str, context: CheckContext, data: dict[str, object]
) -> str | None:
    banned = _table(data, "tool", "ruff", "lint", "flake8-tidy-imports", "banned-api")
    if member in banned:
        return None
    return f"{reference}: {member} is not in [tool.ruff.lint.flake8-tidy-imports.banned-api]"


def _contract(
    reference: str, name: str, context: CheckContext, data: dict[str, object]
) -> str | None:
    contracts = _table(data, "tool", "importlinter").get("contracts", [])
    if isinstance(contracts, list) and any(
        isinstance(entry, dict) and entry.get("name") == name for entry in contracts
    ):
        return None
    return f"{reference}: no [[tool.importlinter.contracts]] entry is named {name}"


def _type(reference: str, path: str, context: CheckContext, data: dict[str, object]) -> str | None:
    if not path_exists(context.repo.root, path) or not (context.repo.root / path).is_file():
        return f"{reference}: {path} is not a file"
    mypy = _table(data, "tool", "mypy")
    covered = _strings(mypy, "files", "[tool.mypy]", split_commas=True) or []
    if not any(_is_under(path, entry) for entry in covered):
        return f"{reference}: [tool.mypy] files does not cover {path}"
    for pattern in _strings(mypy, "exclude", "[tool.mypy]") or []:
        try:
            if re.search(pattern, path):
                return f"{reference}: [tool.mypy] exclude matches {path}"
        except re.error as error:
            raise _Unreadable(
                f"[tool.mypy] exclude {pattern!r} is not a regular expression"
            ) from error
    return None


def _is_under(path: str, entry: str) -> bool:
    entry = entry.removeprefix("./").rstrip("/")
    return entry in ("", ".") or path == entry or path.startswith(f"{entry}/")


_PYTHON_RESOLVERS: dict[str, Callable[[str, str, CheckContext, dict[str, object]], str | None]] = {
    "ruff": _ruff,
    "banned": _banned,
    "contract": _contract,
    "type": _type,
}
