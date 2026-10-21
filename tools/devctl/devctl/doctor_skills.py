"""doctor --skills: the skills a Claude Code Session loads from a worktree at HEAD.

When claude is on PATH, doctor adds a detached worktree at HEAD in a new temporary directory,
starts one new headless Claude Code Session there and compares the skills it lists with the ones
committed at HEAD. git runs with the owner's own setup; no App token is minted. The worktree is
doctor's only local write, and it is removed again whatever the Session did. The Session spends a
little of the owner's quota. doctor probes Claude Code alone until the Kit integrates another
Harness (Spec 1 section 6).
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

from devctl import process
from devctl.check.markdown import front_matter
from devctl.doctor_items import DoctorContext, Item, Status, run_installed
from devctl.harness import PROBE_TIMEOUT_SECONDS, parse_skills, skills_args
from devctl.process import ProcessResult
from devctl.repo import Repo
from devctl.settings import ProjectSettings

_GIT_TIMEOUT_SECONDS = 120.0
_NOT_ON_PATH = "claude is not on PATH"


class ProbeError(Exception):
    """The probe worktree could not be added or removed."""


def _git_problem(result: ProcessResult) -> str | None:
    """Why git's answer cannot be used, or None. A timeout or cut output is never an answer."""
    if result.timed_out:
        return f"timed out after {_GIT_TIMEOUT_SECONDS:g} seconds"
    if result.stdout_truncated or result.stderr_truncated:
        return "its output was too long"
    if result.returncode != 0:
        return process.first_line(result.stderr) or f"exit code {result.returncode}"
    return None


def _git(cwd: Path, *args: str, failure: str) -> None:
    problem = _git_problem(process.run_process(["git", *args], cwd, _GIT_TIMEOUT_SECONDS))
    if problem is not None:
        raise ProbeError(f"{failure}: {problem}")


@contextmanager
def probe_worktree(repo: Repo) -> Iterator[Path]:
    """A detached worktree at HEAD in a new temporary directory, removed whatever the body did.

    The removal is forced twice, so files a Session wrote, or a lock it took, cannot keep the
    worktree. When the removal fails anyway, the worktree stays and ProbeError says how to remove
    it by hand.
    """
    scratch = Path(tempfile.mkdtemp(prefix="devctl-skills-")).resolve()
    path = scratch / "tree"
    try:
        _git(
            repo.root,
            "worktree",
            "add",
            "--detach",
            str(path),
            "HEAD",
            failure=f"could not add the probe worktree {path}",
        )
    except ProbeError:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    try:
        yield path
    finally:
        command = f"git worktree remove --force --force {path}"
        try:
            _git(
                repo.root,
                "worktree",
                "remove",
                "--force",
                "--force",
                str(path),
                failure=f"could not remove the probe worktree {path}",
            )
        except ProbeError as error:
            raise ProbeError(f"{error}; remove it with {command}") from None
        scratch.rmdir()


def _item(name: str, status: Status, detail: str) -> Item:
    return Item("skills", name, status, detail)


def _user_invoked(manifest: Path) -> bool:
    """True when the skill's front matter sets disable-model-invocation: true.

    Front matter that cannot be read leaves the skill checked; map rule 7 reports it.
    """
    try:
        data = front_matter(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and data.get("disable-model-invocation") is True


def repository_skills(tree: Path) -> tuple[frozenset[str], frozenset[str]]:
    """The skills under tree/.agents/skills, by directory name: (checked, user-invoked).

    A user-invoked skill is one that a Session may leave out of its list, so it is not checked.
    """
    checked: set[str] = set()
    user_invoked: set[str] = set()
    root = tree / ".agents" / "skills"
    for entry in root.iterdir() if root.is_dir() else ():
        manifest = entry / "SKILL.md"
        if manifest.is_file():
            (user_invoked if _user_invoked(manifest) else checked).add(entry.name)
    return frozenset(checked), frozenset(user_invoked)


def _compare(name: str, listed: frozenset[str], tree: Path) -> Item:
    """Missing when the Session did not list a checked skill; outside skills are only named."""
    checked, user_invoked = repository_skills(tree)
    missing = sorted(checked - listed)
    outside = sorted(listed - checked - user_invoked)
    parts = [
        f"missing: {', '.join(missing)}" if missing else f"{len(checked)} repository skills seen"
    ]
    if outside:
        parts.append(f"outside the repository: {', '.join(outside)}")
    if user_invoked:
        parts.append(f"not checked, user-invoked: {', '.join(sorted(user_invoked))}")
    return _item(name, "missing" if missing else "ok", "; ".join(parts))


def _session(name: str, tree: Path) -> Item:
    """Start one headless Claude Code Session in tree; compare what it lists with tree's skills."""
    result = run_installed("claude", *skills_args(), cwd=tree, timeout=PROBE_TIMEOUT_SECONDS)
    if result is None:
        return _item(name, "missing", _NOT_ON_PATH)
    report = parse_skills(result)
    if report.names is None:
        return _item(name, "unknown", report.error or "claude gave no skill list")
    return _compare(name, report.names, tree)


def _harness_item(repo: Repo, name: str) -> Item:
    """The Session in a worktree of its own, which is removed whatever the Session did."""
    try:
        with probe_worktree(repo) as tree:
            return _session(name, tree)
    except (ProbeError, OSError) as error:  # adding or removing the worktree
        return _item(name, "unknown", str(error))


def probed_harnesses(project: ProjectSettings) -> dict[str, str]:
    """claude with its item's name when it is on PATH, else nothing (Spec 1 section 6).

    doctor probes Claude Code alone until the Kit integrates another Harness. project never
    changes the answer, because the settings name no Harness.
    """
    return {"claude": "skills (claude)"} if shutil.which("claude") is not None else {}


def skills_items(ctx: DoctorContext) -> list[Item]:
    """The Claude Code item, or one missing item when claude is not on PATH."""
    names = probed_harnesses(ctx.project)
    if not names:
        return [_item("skills", "missing", _NOT_ON_PATH)]
    repo = Repo.discover(ctx.root)
    return [_harness_item(repo, name) for name in names.values()]
