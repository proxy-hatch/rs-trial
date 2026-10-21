"""doctor's local items: the toolchain, guardrails and Kit files.

Nothing here reads GitHub or changes the repository. A tool runs by the absolute path
shutil.which found, from the temporary directory (git config, which reads the repository's
settings, runs in it). An item's detail never quotes a tool's output.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from devctl import process
from devctl.check.kit import KitLockError, kit_summary, rapid_ship_command
from devctl.check.stacks.agnostic import GITLEAKS_VERSION
from devctl.check.stacks.python import (
    guardrail_gaps,
    pytest_addopts,
    toml_strings,
    toml_table,
)
from devctl.doctor_items import DoctorContext, Item, Status, finished, run_installed
from devctl.repo import Repo

DEVCTL_DIRECTORY = "tools/devctl"
KIT_SOURCE = "rapid_ship/templates/kit.toml"  # tracked only where the Kit's templates live
CI_WORKFLOW = ".github/workflows/devctl-check.yml"
CI_JOB_NAME = "devctl check"  # the status check the ruleset requires
MINIMUM_GIT = (2, 31)  # git rev-parse --path-format=absolute
NO_PYPROJECT = "no pyproject.toml"
KIT_SOURCE_NOTE = "ruff, mypy and pytest not checked: this repository holds the Kit templates"

_GIT_VERSION = re.compile(r"git version ((\d+)\.(\d+)(?:\.\d+)*)")
_SEMVER = re.compile(r"\d+\.\d+\.\d+")
_HOOKS_PATH = ".githooks"


def _toolchain(name: str, status: Status, detail: str) -> Item:
    return Item("toolchain", name, status, detail)


def _on_path(tool: str, advice: str) -> Item:
    path = shutil.which(tool)
    return _toolchain(tool, "ok", path) if path else _toolchain(tool, "missing", advice)


def git_item() -> Item:
    """git 2.31 or later, because devctl relies on git rev-parse --path-format=absolute."""
    needed = ".".join(map(str, MINIMUM_GIT))
    result = run_installed("git", "--version")
    if result is None:
        return _toolchain("git", "missing", f"install git {needed} or later")
    match = _GIT_VERSION.match(process.first_line(result.stdout))
    if match is None or result.returncode != 0 or not finished(result):
        return _toolchain("git", "unknown", "git --version gave no readable version")
    if (int(match[2]), int(match[3])) < MINIMUM_GIT:
        detail = (
            f"git {match[1]} is older than {needed}; "
            "devctl needs git rev-parse --path-format=absolute"
        )
        return _toolchain("git", "missing", detail)
    return _toolchain("git", "ok", match[1])


def gitleaks_item() -> Item:
    """gitleaks at the version devctl check pins, which GITLEAKS_VERSION names."""
    result = run_installed("gitleaks", "version")
    if result is None:
        return _toolchain(
            "gitleaks",
            "missing",
            f"install gitleaks {GITLEAKS_VERSION}; devctl check blocks its secrets step without it",
        )
    found = process.first_line(result.stdout).removeprefix("v")
    if result.returncode != 0 or not finished(result) or not _SEMVER.fullmatch(found):
        return _toolchain("gitleaks", "unknown", "gitleaks version gave no readable version")
    if found != GITLEAKS_VERSION:
        return _toolchain(
            "gitleaks",
            "missing",
            f"gitleaks {found} is installed; devctl check pins {GITLEAKS_VERSION}",
        )
    return _toolchain("gitleaks", "ok", found)


def _gh_sign_in() -> Item:
    result = run_installed("gh", "auth", "status")
    if result is None:
        return _toolchain("gh sign-in", "missing", "gh is not installed")
    if not finished(result):
        return _toolchain("gh sign-in", "unknown", "gh auth status did not finish")
    if result.returncode != 0:
        return _toolchain("gh sign-in", "missing", "not signed in; run gh auth login")
    return _toolchain("gh sign-in", "ok", "gh auth status exited 0")


def _hooks_item(root: Path) -> Item:
    name = "core.hooksPath"
    result = run_installed("git", "config", "--get", "core.hooksPath", cwd=root)
    if result is None:
        return Item("guardrails", name, "missing", "git is not installed")
    if not finished(result):
        return Item("guardrails", name, "unknown", "git config did not finish")
    value = result.stdout.strip()
    if result.returncode == 0 and value == _HOOKS_PATH:
        return Item("guardrails", name, "ok", _HOOKS_PATH)
    found = f"core.hooksPath is {value}" if value else "core.hooksPath is not set"
    advice = (
        f"run git config core.hooksPath {_HOOKS_PATH}; "
        "the pre-commit hook runs devctl check --tier fast"
    )
    return Item("guardrails", name, "missing", f"{found}; {advice}")


def _job_names(workflow: object) -> list[object]:
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict):
        return []
    return [job.get("name") for job in jobs.values() if isinstance(job, dict)]


def _ci_item(root: Path, tracked: Sequence[str]) -> Item:
    name = "CI workflow"
    if CI_WORKFLOW not in tracked:
        return Item("guardrails", name, "missing", f"{CI_WORKFLOW} is not tracked")
    try:
        workflow = yaml.safe_load((root / CI_WORKFLOW).read_text(encoding="utf-8"))
    except OSError:
        return Item("guardrails", name, "unknown", f"cannot read {CI_WORKFLOW}")
    except (yaml.YAMLError, UnicodeDecodeError):
        return Item("guardrails", name, "missing", f"{CI_WORKFLOW} is not valid YAML")
    if CI_JOB_NAME in _job_names(workflow):
        return Item("guardrails", name, "ok", f"{CI_WORKFLOW} has a job named {CI_JOB_NAME}")
    detail = f"no job in {CI_WORKFLOW} is named {CI_JOB_NAME}, which the ruleset requires"
    return Item("guardrails", name, "missing", detail)


def _read_pyproject(root: Path) -> dict[str, object] | str:
    """The parsed pyproject.toml, or why it cannot be read."""
    try:
        return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NO_PYPROJECT
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return f"pyproject.toml: {error}"


def _guardrail_item(root: Path) -> Item:
    name = "Python guardrail settings"
    pyproject = _read_pyproject(root)
    if isinstance(pyproject, str):
        return Item("guardrails", name, "missing", pyproject)
    gaps = guardrail_gaps(pyproject)
    if gaps:
        return Item("guardrails", name, "missing", "; ".join(gaps))
    return Item("guardrails", name, "ok", "meets the Python floor of spec section 4")


def _glob(pattern: str) -> re.Pattern[str]:
    # uv workspace globs: * and ? stay inside one path segment; ** spans segments.
    parts = re.split(r"(\*\*|\*|\?)", pattern.removeprefix("./").rstrip("/"))
    wildcards = {"**": ".*", "*": "[^/]*", "?": "[^/]"}
    return re.compile("".join(wildcards.get(part, re.escape(part)) for part in parts))


def _normalised(entry: str) -> str:
    """A path setting without one leading ./ and any trailing / or /**."""
    return re.sub(r"(?:/\*\*|/)+$", "", entry.removeprefix("./")) or "."


def _ruff_exclusion_gaps(pyproject: Mapping[str, object]) -> list[str]:
    ruff = toml_table(pyproject, "tool", "ruff")
    entries = {
        _normalised(e) for k in ("exclude", "extend-exclude") for e in toml_strings(ruff.get(k))
    }
    if entries & {DEVCTL_DIRECTORY, "tools"}:
        return []
    return [f"ruff: {DEVCTL_DIRECTORY} is not in exclude or extend-exclude"]


def _mypy_exclusion_gaps(pyproject: Mapping[str, object]) -> list[str]:
    mypy = toml_table(pyproject, "tool", "mypy")
    probe = f"{DEVCTL_DIRECTORY}/devctl/cli.py"  # plan 3's rule for `type:` references
    files = (_normalised(entry) for entry in toml_strings(mypy.get("files")))
    if not any(entry in (".", probe) or probe.startswith(f"{entry}/") for entry in files):
        return []
    for pattern in toml_strings(mypy.get("exclude")):
        try:
            if re.search(pattern, probe):
                return []
        except re.error:
            continue
    return [f"mypy: files covers {DEVCTL_DIRECTORY} and no exclude pattern matches it"]


def _pytest_exclusion_gaps(pyproject: Mapping[str, object]) -> list[str]:
    options = toml_table(pyproject, "tool", "pytest", "ini_options")
    ignored = {
        _normalised(a.removeprefix("--ignore="))
        for a in pytest_addopts(options)
        if a.startswith("--ignore=")
    }
    paths = [_normalised(entry) for entry in toml_strings(options.get("testpaths"))]
    reaches = not paths or any(
        p in (".", DEVCTL_DIRECTORY)
        or DEVCTL_DIRECTORY.startswith(f"{p}/")
        or p.startswith(f"{DEVCTL_DIRECTORY}/")
        for p in paths
    )
    if reaches and DEVCTL_DIRECTORY not in ignored:
        return [f"pytest: testpaths does not leave out {DEVCTL_DIRECTORY}"]
    return []


def _uv_exclusion_gaps(pyproject: Mapping[str, object]) -> list[str]:
    workspace = toml_table(pyproject, "tool", "uv", "workspace")
    members = toml_strings(workspace.get("members"))
    excluded = toml_strings(workspace.get("exclude"))
    covers = [
        any(_glob(e).fullmatch(DEVCTL_DIRECTORY) for e in entries)
        for entries in (members, excluded)
    ]
    if covers[0] and not covers[1]:
        return [f"uv: [tool.uv.workspace] members match {DEVCTL_DIRECTORY}; add it to exclude"]
    return []


def devctl_exclusion_gaps(pyproject: Mapping[str, object], *, python: bool) -> list[str]:
    """The settings that make a project's own tools read tools/devctl, one message each.

    Ruff, mypy and pytest are checked only with python. The uv workspace is checked for either
    stack: without the exclude, the project's own uv lock fails (Checked facts).
    """
    return [
        *(_ruff_exclusion_gaps(pyproject) if python else []),
        *(_mypy_exclusion_gaps(pyproject) if python else []),
        *(_pytest_exclusion_gaps(pyproject) if python else []),
        *_uv_exclusion_gaps(pyproject),
    ]


def _exclusions_item(ctx: DoctorContext, tracked: Sequence[str]) -> Item:
    name = f"{DEVCTL_DIRECTORY} exclusions"
    holds_kit = KIT_SOURCE in tracked  # the Kit's own source checks tools/devctl itself
    pyproject = _read_pyproject(ctx.root)
    if isinstance(pyproject, str):
        if pyproject == NO_PYPROJECT and ctx.project.stack == "agnostic":
            return Item("guardrails", name, "ok", f"{NO_PYPROJECT}, so no uv workspace")
        return Item("guardrails", name, "missing", pyproject)
    python = ctx.project.stack == "python" and not holds_kit
    gaps = devctl_exclusion_gaps(pyproject, python=python)
    notes = [KIT_SOURCE_NOTE] if holds_kit else []
    detail = "; ".join([*gaps, *notes]) or f"{DEVCTL_DIRECTORY} is left out of the project's tools"
    return Item("guardrails", name, "missing" if gaps else "ok", detail)


def _kit_item(ctx: DoctorContext) -> Item:
    try:
        summary = kit_summary(ctx.root, ctx.project.excluded)
        update = rapid_ship_command(ctx.root, "status")
    except KitLockError as error:
        return Item("kit", "Kit files", "missing", str(error))
    counts = (
        f"Kit {summary.kit_version}: {len(summary.unchanged)} unchanged, "
        f"{len(summary.modified)} modified, {len(summary.excluded)} excluded"
    )
    parts = [counts]
    if summary.modified:
        parts.append(f"modified: {', '.join(summary.modified)}")
    if summary.missing:
        parts.append(f"missing: {', '.join(summary.missing)}")
    parts.append(f"run {update} to see template updates")
    return Item("kit", "Kit files", "missing" if summary.missing else "ok", "; ".join(parts))


def local_items(ctx: DoctorContext) -> list[Item]:
    """Every item that needs no GitHub: the toolchain, guardrails and Kit files."""
    tracked = Repo.discover(ctx.root).tracked_files()
    python = ctx.project.stack == "python"
    return [
        git_item(),
        _on_path("uv", "install uv; devctl check runs the project's tools through it"),
        _on_path("gh", "install gh; doctor reads GitHub with its login"),
        _gh_sign_in(),
        gitleaks_item(),
        _hooks_item(ctx.root),
        _ci_item(ctx.root, tracked),
        *([_guardrail_item(ctx.root)] if python else []),
        _exclusions_item(ctx, tracked),
        _kit_item(ctx),
    ]
