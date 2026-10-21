"""devctl check: run the project's steps and decide the exit code.

Exit 0 when every applicable step passed, 1 when a step failed, 2 when a step could not
run or the step list could not be built. A blocked step never counts as passed, and 2
outranks 1.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from devctl.check.diff import DiffStep
from devctl.check.evidence import EvidenceStep
from devctl.check.exceptions import ExceptionsStep
from devctl.check.filesize import FileSizeStep
from devctl.check.kit import KitStep
from devctl.check.map import MapStep
from devctl.check.stacks.agnostic import SecretsStep, WorkflowsStep
from devctl.check.stacks.python import CONTRACTS, FORMAT, LINT, TESTS, TYPES, AuditStep
from devctl.check.steps import CheckContext, Step, StepResult, Tier, blocked
from devctl.repo import Repo, RepoError
from devctl.settings import ProjectSettings

TIERS: dict[Tier, frozenset[Tier]] = {
    "fast": frozenset({"fast"}),
    "full": frozenset({"fast", "full"}),
}


class ChangeError(Exception):
    """The base commit or the PR body could not be determined."""


def core_steps(project: ProjectSettings) -> list[Step]:
    """Core steps for the project's stack in spec section 5's order."""
    python = project.stack == "python"
    # Typed apart: mypy reads the inline list of CommandStep and AuditStep as list[object].
    python_full: list[Step] = [TYPES, CONTRACTS, TESTS, AuditStep()] if python else []
    return [
        *([FORMAT, LINT] if python else []),
        MapStep(),
        ExceptionsStep(),
        FileSizeStep(),
        KitStep(),
        *python_full,
        SecretsStep(),
        WorkflowsStep(),
        DiffStep(),
        EvidenceStep(),
    ]


def all_steps(project: ProjectSettings) -> list[Step]:
    import devctl_project  # Imported late: devctl_project imports devctl.check.steps.

    steps = [*core_steps(project), *devctl_project.check_steps()]
    names: set[str] = set()
    for step in steps:
        if step.tier not in TIERS:
            raise ValueError(f"check step {step.name}: tier must be fast or full")
        if step.name in names:
            raise ValueError(f"two check steps are named {step.name}")
        names.add(step.name)
    return steps


def _blocked_on_error[T](name: str, action: Callable[[], T]) -> T | StepResult:
    """What action returns, or a blocked result named `name` when it raises.

    The project owns check_steps() and every step's code, so a bug there must not escape: an
    uncaught error would exit 1, which reads as a failure, not "could not run".
    """
    try:
        return action()
    except Exception as error:  # noqa: BLE001  # rs-exception: EX-2
        return blocked(name, f"{type(error).__name__}: {error}")


def _run_step(step: Step, context: CheckContext) -> StepResult:
    return step.run(context)


def run_check(context: CheckContext, tier: Tier) -> list[StepResult]:
    """Run the steps in the tier. A step list that cannot be built is one blocked result."""
    steps = _blocked_on_error("step list", partial(all_steps, context.project))
    if isinstance(steps, StepResult):
        return [steps]
    return [
        _blocked_on_error(step.name, partial(_run_step, step, context))
        for step in steps
        if step.tier in TIERS[tier]
    ]


def exit_code(results: Sequence[StepResult]) -> int:
    statuses = {result.status for result in results}
    if "blocked" in statuses:
        return 2
    return 1 if "failed" in statuses else 0


@dataclass(frozen=True)
class _PullRequest:
    base_sha: str
    head_sha: str
    body: str | None


def _member(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, dict) else None


def _read_pull_request(environ: Mapping[str, str]) -> _PullRequest:
    def unreadable(why: object) -> ChangeError:
        return ChangeError(f"cannot read the pull_request event: {why}")

    path = environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise unreadable("GITHUB_EVENT_PATH is not set")
    try:
        event = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:  # ValueError covers invalid JSON and invalid UTF-8
        raise unreadable(error) from error
    pull_request = _member(event, "pull_request")
    if not isinstance(pull_request, dict):
        raise unreadable("there is no pull_request object")
    base_sha = _member(_member(pull_request, "base"), "sha")
    head_sha = _member(_member(pull_request, "head"), "sha")
    if not isinstance(base_sha, str) or not isinstance(head_sha, str):
        raise unreadable("base.sha and head.sha must be strings")
    body = pull_request.get("body")
    if body is not None and not isinstance(body, str):
        raise unreadable("body must be text or null")
    return _PullRequest(base_sha, head_sha, body)


def _pull_request_body(path: Path | None, pull_request: _PullRequest | None) -> str | None:
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            raise ChangeError(f"cannot read the PR body: {error}") from error
    elif pull_request is not None:
        text = pull_request.body or ""
    else:
        return None
    return text.replace("\r\n", "\n")


def _explicit_base(repo: Repo, rev: str) -> str:
    try:
        return repo.git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()
    except RepoError as error:
        raise ChangeError(f"--base {rev} is not a commit") from error


def _pull_request_base(repo: Repo, pull_request: _PullRequest) -> str:
    # actions/checkout checks out GitHub's merge commit for a pull_request event. Its parents
    # are the base tip, then the PR head. The first is the base the change was merged onto,
    # even when the payload's base.sha is older.
    parents = repo.git("rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(parents) == 3 and parents[2] == pull_request.head_sha:
        return parents[1]
    unknown = ChangeError("cannot find the PR base; check out full history (fetch-depth: 0)")
    try:
        merge_base = repo.merge_base(pull_request.base_sha)
    except RepoError as error:
        raise unknown from error
    if merge_base is None:
        raise unknown
    return merge_base


def _default_branch_base(repo: Repo) -> str | None:
    try:
        default = repo.git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
        return repo.merge_base(default)
    except RepoError:
        return None


def resolve_change(
    repo: Repo, base: str | None, pr_body_path: Path | None, environ: Mapping[str, str]
) -> tuple[str | None, str | None]:
    """The base commit and PR body that diff and evidence steps compare against.

    The base is None when no change can be found: a push, a schedule or a manual run in CI,
    or a local checkout without origin's default branch.
    """
    pull_request = (
        _read_pull_request(environ) if environ.get("GITHUB_EVENT_NAME") == "pull_request" else None
    )
    body = _pull_request_body(pr_body_path, pull_request)
    if base is not None:
        return _explicit_base(repo, base), body
    if pull_request is not None:
        return _pull_request_base(repo, pull_request), body
    if environ.get("GITHUB_ACTIONS") == "true":
        return None, body
    return _default_branch_base(repo), body
