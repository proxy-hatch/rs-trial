"""Check step types and helpers shared by every step."""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from devctl import process
from devctl.process import ProcessResult
from devctl.repo import Repo
from devctl.settings import ProjectSettings

Tier = Literal["fast", "full"]
Status = Literal["passed", "failed", "blocked", "not_applicable"]

# Variables a check tool may need besides PATH and HOME. Never add one that can hold a token.
PASSED_VARIABLES = (
    "TMPDIR",
    "CI",
    "SSL_CERT_FILE",
    "UV_CACHE_DIR",
    "UV_PYTHON",
    "UV_PYTHON_INSTALL_DIR",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
)


@dataclass(frozen=True)
class StepResult:
    name: str
    status: Status
    details: tuple[str, ...]


@dataclass(frozen=True)
class CheckContext:
    repo: Repo
    project: ProjectSettings
    base: str | None  # base commit for the diff policy; None outside a change
    pr_body: str | None  # from GITHUB_EVENT_PATH or --pr-body; None outside a PR


class Step(Protocol):
    # A frozen dataclass cannot implement these settable members; use a class or plain dataclass.
    name: str
    tier: Tier

    def run(self, context: CheckContext) -> StepResult: ...


def passed(name: str, *details: str) -> StepResult:
    return StepResult(name, "passed", details)


def failed(name: str, *details: str) -> StepResult:
    return StepResult(name, "failed", details)


def blocked(name: str, *details: str) -> StepResult:
    return StepResult(name, "blocked", details)


def not_applicable(name: str, *details: str) -> StepResult:
    return StepResult(name, "not_applicable", details)


def tool_environment() -> dict[str, str]:
    """The variables a check tool gets besides PATH, HOME and the locale."""
    return process.passed_variables(PASSED_VARIABLES)


def run_tool(
    root: Path, argv: Sequence[str], timeout_seconds: float, env: Mapping[str, str] | None = None
) -> ProcessResult | None:
    """Run a check tool from the repository root; None when argv[0] is not on PATH."""
    try:
        return process.run_process(
            argv, root, timeout_seconds, env={**tool_environment(), **(env or {})}
        )
    except FileNotFoundError:
        return None


def output_tail(result: ProcessResult, lines: int = 40) -> tuple[str, ...]:
    tail = tuple((result.stdout + result.stderr).splitlines()[-lines:])
    if result.stdout_truncated or result.stderr_truncated:
        return ("(output over 1 MiB was cut; the end may be missing)", *tail)
    return tail


def command_result(
    name: str, argv: Sequence[str], result: ProcessResult | None, failed_codes: frozenset[int]
) -> StepResult:
    """A step result from a tool run: exit 0 passes, a listed code fails, anything else blocks."""
    if result is None:
        return blocked(name, f"{argv[0]} is not on PATH")
    if result.timed_out:
        return blocked(
            name, f"timed out after {result.duration_seconds:.0f} s", *output_tail(result)
        )
    if result.returncode == 0:
        return passed(name)
    if result.returncode in failed_codes:
        return failed(name, *output_tail(result))
    return blocked(name, f"{shlex.join(argv)} exited {result.returncode}", *output_tail(result))


@dataclass
class CommandStep:  # Not frozen: Step's members are settable.
    name: str
    tier: Tier
    argv: tuple[str, ...]
    failed_codes: frozenset[int] = frozenset({1})
    timeout_seconds: float = 1800.0

    def run(self, context: CheckContext) -> StepResult:
        result = run_tool(context.repo.root, self.argv, self.timeout_seconds)
        return command_result(self.name, self.argv, result, self.failed_codes)
