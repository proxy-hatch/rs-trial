"""The Python stack: its check steps, how pytest names tests and which suppressions it carries."""

from __future__ import annotations

import ast
import io
import json
import re
import shlex
import tempfile
import tokenize
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from devctl.check.steps import (
    CheckContext,
    CommandStep,
    StepResult,
    Tier,
    blocked,
    command_result,
    failed,
    output_tail,
    passed,
    run_tool,
)

# rapid_ship/kit/adopt.py copies these patterns, comment_rules, marker_rules, _codes and
# _dotted, because rapid-ship cannot import devctl. tests/devctl/test_devctl_adopt_rules.py
# fails when the copies differ.
_NOQA = re.compile(
    r"#\s*(?:(?:ruff|flake8)\s*:\s*)?noqa\b"
    r"(?::\s*(?P<codes>[A-Za-z]+[0-9]+(?:[\s,]+[A-Za-z]+[0-9]+)*))?",
    re.IGNORECASE,
)
_TYPE_IGNORE = re.compile(r"#\s*type:\s*ignore\b(?:\[(?P<codes>[^\]]*)\])?")
_MYPY = re.compile(r"#\s*mypy:\s*(?P<setting>ignore-errors|disable-error-code)")
_NO_COVER = re.compile(r"#\s*pragma:\s*no\s+cover", re.IGNORECASE)
_SKIP_MARKS = frozenset({"skip", "skipif", "xfail"})
_SKIP_CALLS = frozenset({"skip", "xfail", "importorskip"})

# `uv run` exits 2 when the project does not install the tool, so a missing tool is blocked, not
# failed. --locked stops a stale lock instead of rewriting it. lint-imports also exits 1 for its
# own errors. pytest exits 5 when it collects no tests.
FORMAT = CommandStep("format", "fast", ("uv", "run", "--locked", "ruff", "format", "--check", "."))
LINT = CommandStep(
    "lint", "fast", ("uv", "run", "--locked", "ruff", "check", "--output-format", "concise", ".")
)
TYPES = CommandStep("types", "full", ("uv", "run", "--locked", "mypy"))
CONTRACTS = CommandStep("contracts", "full", ("uv", "run", "--locked", "lint-imports"))
TESTS = CommandStep(
    "tests", "full", ("uv", "run", "--locked", "pytest"), failed_codes=frozenset({1, 5})
)

# Spec section 5 names pip-audit because `uv audit` is still experimental (uv 0.13.1 says so) and
# uv 0.11.0 exits 0 with known vulnerabilities. uv 0.13.1 exits 1 for user errors too, so no
# exit code tells findings from errors. The step reads pip-audit's JSON report instead.
PIP_AUDIT = "pip-audit==2.10.1"


class AuditStep:
    """Audit the locked dependencies, all groups and extras, for known vulnerabilities."""

    name = "audit"
    tier: Tier = "full"

    def run(self, context: CheckContext) -> StepResult:
        with tempfile.TemporaryDirectory(prefix="devctl-audit-") as scratch:
            requirements = Path(scratch, "requirements.txt")
            report = Path(scratch, "audit.json")
            export = (
                *("uv", "export", "--locked", "--format", "requirements-txt"),
                *("--all-groups", "--all-extras", "--no-emit-project", "--no-emit-local"),
                *("--no-header", "--quiet"),
                *("--output-file", str(requirements)),
            )
            exported = run_tool(context.repo.root, export, 300)
            if exported is None or exported.timed_out or exported.returncode != 0:
                return command_result(self.name, export, exported, frozenset())
            audit = (
                *("uv", "tool", "run", "--from", PIP_AUDIT, "pip-audit"),
                *("--requirement", str(requirements)),
                *("--require-hashes", "--disable-pip", "--strict", "--progress-spinner", "off"),
                *("--format", "json", "--output", str(report)),
            )
            audited = run_tool(context.repo.root, audit, 600)
            if audited is None or audited.timed_out:
                return command_result(self.name, audit, audited, frozenset())
            vulnerabilities = _vulnerabilities(report)
            if vulnerabilities is None:
                return blocked(
                    self.name, "pip-audit wrote no readable report", *output_tail(audited)
                )
            if vulnerabilities:
                return failed(self.name, *vulnerabilities)
            if audited.returncode != 0:
                return blocked(
                    self.name,
                    f"pip-audit exited {audited.returncode}, but its report lists no vulnerability",
                    *output_tail(audited),
                )
            return passed(self.name)


def _vulnerabilities(report: Path) -> list[str] | None:
    """One line per distinct vulnerability in pip-audit's JSON report; None when unreadable.

    pip-audit 2.10.1 can list the same vulnerability twice. A shape this does not understand is
    None, never an empty list, so it cannot pass.
    """
    try:
        document = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    dependencies = document.get("dependencies") if isinstance(document, dict) else None
    if not isinstance(dependencies, list):
        return None
    lines: list[str] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            return None
        found = dependency.get("vulns", [])  # A skipped dependency has no `vulns`.
        if not isinstance(found, list):
            return None
        for vulnerability in found:
            line = _vulnerability_line(dependency, vulnerability)
            if line is None:
                return None
            lines.append(line)
    return list(dict.fromkeys(lines))


def _vulnerability_line(dependency: dict[str, object], vulnerability: object) -> str | None:
    if not isinstance(vulnerability, dict):
        return None
    name, version = dependency.get("name"), dependency.get("version")
    identifier, fixes = vulnerability.get("id"), vulnerability.get("fix_versions", [])
    if not (isinstance(name, str) and isinstance(version, str) and isinstance(identifier, str)):
        return None
    if not isinstance(fixes, list) or not all(isinstance(fix, str) for fix in fixes):
        return None
    fixed = f"fixed in {', '.join(fixes)}" if fixes else "no fix yet"
    return f"{name} {version}: {identifier} ({fixed})"


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _parse(source: str) -> ast.Module:
    """Parse source. ast.parse rejects the UTF-8 byte-order mark that some editors write first."""
    return ast.parse(source.removeprefix("\ufeff"))


def test_identities(source: str) -> list[str]:
    """Pytest's default names: test* functions, and test* methods in Test* classes."""
    return list(_tests(_parse(source).body, ""))


def _tests(body: Sequence[ast.stmt], prefix: str) -> Iterator[str]:
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test"
        ):
            yield prefix + node.name
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            yield from _tests(node.body, f"{prefix}{node.name}::")


_TRUTHY_CALLS = frozenset({"assertTrue", "assert_"})
_EQUAL_CALLS = frozenset(
    {"assertEqual", "assertEquals", "assertIs", "assertGreaterEqual", "assertLessEqual"}
)
_REFLEXIVE = (ast.Eq, ast.Is, ast.GtE, ast.LtE)


def tautologies(source: str) -> list[tuple[int, str]]:
    """Assertions that cannot fail; SyntaxError propagates."""
    return [
        (node.lineno, ast.unparse(node))
        for node in ast.walk(_parse(source))
        if isinstance(node, ast.Assert | ast.Call) and _tautological(node)
    ]


def _tautological(node: ast.Assert | ast.Call) -> bool:
    if isinstance(node, ast.Assert):
        return _always_true(node.test)
    return isinstance(node.func, ast.Attribute) and _tautological_call(node.func.attr, node.args)


def _tautological_call(name: str, args: list[ast.expr]) -> bool:
    if name in _TRUTHY_CALLS and args:
        return _truthy_constant(args[0])
    return name in _EQUAL_CALLS and len(args) >= 2 and _same(args[0], args[1])


def _always_true(test: ast.expr) -> bool:
    if _truthy_constant(test):
        return True
    return (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], _REFLEXIVE)
        and _same(test.left, test.comparators[0])
    )


def _truthy_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and bool(node.value)


def _same(first: ast.expr, second: ast.expr) -> bool:
    return ast.dump(first) == ast.dump(second)


def comments(source: str) -> dict[int, str]:
    """Comment text by line. Source that does not tokenize falls back to text after '#'."""
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        return {t.start[0]: t.string for t in tokens if t.type == tokenize.COMMENT}
    except (tokenize.TokenError, SyntaxError):
        lines = enumerate(source.splitlines(), start=1)
        return {number: line[line.index("#") :] for number, line in lines if "#" in line}


def comment_rules(comment: str) -> list[str]:
    rules = [_codes(m.group("codes"), upper=True) or "noqa" for m in _NOQA.finditer(comment)]
    rules += [_codes(m.group("codes")) or "type-ignore" for m in _TYPE_IGNORE.finditer(comment)]
    rules += [f"mypy:{m.group('setting')}" for m in _MYPY.finditer(comment)]
    rules += ["no-cover" for _ in _NO_COVER.finditer(comment)]
    return rules


def _codes(text: str | None, *, upper: bool = False) -> str:
    parts = [part for part in re.split(r"[\s,]+", text or "") if part]
    return ",".join(sorted(part.upper() if upper else part for part in parts))


def marker_rules(source: str) -> list[tuple[int, str]]:
    """The (line, rule) of each skip or xfail marker and call in the syntax tree."""
    try:
        tree = _parse(source)
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _SKIP_MARKS:
            if _dotted(node.value) in ("pytest.mark", "mark"):
                found.append((node.lineno, node.attr))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SKIP_CALLS
            and _dotted(node.func.value) == "pytest"
        ):
            found.append((node.lineno, node.func.attr))
    return found


def autouse_fixtures(source: str) -> list[tuple[str, int]]:
    """The (name, decorator line) of each fixture declared with autouse=True."""
    try:
        tree = _parse(source)
    except SyntaxError:
        return []
    return [
        (node.name, decorator.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for decorator in node.decorator_list
        if _is_autouse_fixture(decorator)
    ]


def _is_autouse_fixture(decorator: ast.expr) -> bool:
    if not isinstance(decorator, ast.Call) or _dotted(decorator.func) not in (
        "pytest.fixture",
        "fixture",
    ):
        return False
    return any(
        keyword.arg == "autouse"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in decorator.keywords
    )


def _dotted(node: ast.expr) -> str:
    """'pytest.mark' for that attribute chain; '' for anything that is not a plain chain."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


# The Python guardrail floor of spec section 4. The seeded pyproject.toml meets it, and an existing
# project's settings are held to it by doctor.
REQUIRED_RUFF = (
    "E",
    "W",
    "F",
    "I",
    "B",
    "UP",
    "SIM",
    "C4",
    "RUF",
    "PGH",
    "ERA",
    "T20",
    "S",
    "TID",
    "PT",
    "PTH",
    "DTZ",
    "C90",
    "TD",
    "BLE",
    "ANN401",
)
ESCAPE_HATCHES = ("PGH003", "PGH004", "RUF100", "ERA001")
MYPY_CODES = ("ignore-without-code", "redundant-expr", "truthy-bool")
DEV_TOOLS = ("ruff", "mypy", "pytest", "import-linter")
_ASYNC_FRAMEWORKS = frozenset({"fastapi", "anyio", "trio"})


def _letters(code: str) -> str:
    return code.rstrip("0123456789")


def _covers(selectors: Sequence[str], code: str) -> bool:
    """Whether a Ruff selector list selects every rule that code names.

    A selector covers a code when it is ALL, or when it is a prefix of the code within one linter:
    its letters must equal the code's, so E does not select ERA. The legacy groups C and T cover
    C4 and C90, and T10 and T20, by the same rule.
    """
    return any(
        s == "ALL" or (code.startswith(s) and _letters(s) == _letters(code)) for s in selectors
    )


def toml_table(parent: Mapping[str, object], *keys: str) -> Mapping[str, object]:
    """The table at keys, or an empty one when a level is missing or is not a table."""
    current: object = parent
    for key in keys:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def toml_strings(value: object) -> list[str]:
    """The strings of a list setting; a bare string is a list of one."""
    if isinstance(value, str):
        return [value]
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _requirement_name(requirement: str) -> str | None:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement.strip())
    return re.sub(r"[-_.]+", "-", match.group()).lower() if match else None


def _requirement_names(value: object) -> set[str]:
    names = (_requirement_name(requirement) for requirement in toml_strings(value))
    return {name for name in names if name is not None}


def _ruff_gaps(pyproject: Mapping[str, object]) -> list[str]:
    lint = toml_table(pyproject, "tool", "ruff", "lint")
    frameworks = _requirement_names(toml_table(pyproject, "project").get("dependencies"))
    required = list(REQUIRED_RUFF)
    if frameworks & _ASYNC_FRAMEWORKS:
        required.append("ASYNC")
    if "fastapi" in frameworks:
        required.append("FAST")
    selected = [*toml_strings(lint.get("select")), *toml_strings(lint.get("extend-select"))]
    lacking = [code for code in required if not _covers(selected, code)]
    gaps = [f"ruff: select lacks {', '.join(lacking)}"] if lacking else []
    ignored = toml_strings(lint.get("ignore"))
    gaps += [f"ruff: ignore disables {code}" for code in ESCAPE_HATCHES if _covers(ignored, code)]
    if toml_table(lint, "flake8-tidy-imports").get("ban-relative-imports") != "all":
        gaps.append("ruff: relative imports are not banned")
    return gaps


def _mypy_gaps(pyproject: Mapping[str, object]) -> list[str]:
    mypy = toml_table(pyproject, "tool", "mypy")
    gaps = [
        f"mypy: {setting} is not true"
        for setting in ("strict", "warn_unreachable")
        if mypy.get(setting) is not True
    ]
    enabled = {
        part.strip()
        for item in toml_strings(mypy.get("enable_error_code"))
        for part in item.split(",")
    }
    lacking = [code for code in MYPY_CODES if code not in enabled]
    if lacking:
        gaps.append(f"mypy: enable_error_code lacks {', '.join(lacking)}")
    return gaps


def _import_linter_gaps(pyproject: Mapping[str, object]) -> list[str]:
    contracts = toml_table(pyproject, "tool", "importlinter").get("contracts")
    types = (
        {c.get("type") for c in contracts if isinstance(c, Mapping)}
        if isinstance(contracts, list)
        else set()
    )
    return [
        f"import-linter: no {kind} contract"
        for kind in ("layers", "forbidden")
        if kind not in types
    ]


def pytest_addopts(options: Mapping[str, object]) -> list[str]:
    value = options.get("addopts")
    if not isinstance(value, str):
        return toml_strings(value)
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def _pytest_gaps(pyproject: Mapping[str, object]) -> list[str]:
    options = toml_table(pyproject, "tool", "pytest", "ini_options")
    addopts = pytest_addopts(options)
    gaps = [
        f"pytest: addopts lacks {flag}"
        for flag in ("--strict-markers", "--strict-config")
        if flag not in addopts
    ]
    if options.get("xfail_strict") is not True:
        gaps.append("pytest: xfail_strict is not true")
    warnings = [
        line.strip()
        for item in toml_strings(options.get("filterwarnings"))
        for line in item.splitlines()
    ]
    if "error" not in warnings:
        gaps.append("pytest: filterwarnings lacks error")
    return gaps


def _dev_gaps(pyproject: Mapping[str, object]) -> list[str]:
    present = _requirement_names(toml_table(pyproject, "dependency-groups").get("dev"))
    lacking = [tool for tool in DEV_TOOLS if tool not in present]
    return [f"dev dependencies lack {', '.join(lacking)}"] if lacking else []


def guardrail_gaps(pyproject: Mapping[str, object]) -> list[str]:
    """The settings of spec section 4's Python floor that pyproject lacks, one message each."""
    return [
        *_ruff_gaps(pyproject),
        *_mypy_gaps(pyproject),
        *_import_linter_gaps(pyproject),
        *_pytest_gaps(pyproject),
        *_dev_gaps(pyproject),
    ]
