"""devctl doctor: report each item as ok, missing or unknown.

doctor_local.py builds the items for this machine and repository, and doctor_github.py the GitHub
items, which compare what they read with the settings in doctor_rules.py; doctor_skills.py adds
the --skills items. doctor_items.py holds the types they share and the owner's one way to read
GitHub. This module runs them, prints them and exits with the worst answer. It re-exports the
names that tests, and plan 6's test of rs-setup's ruleset, import from devctl.doctor.

No item checks a Harness's sign-in (Spec 1 section 6).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from devctl.doctor_github import github_items as github_items
from devctl.doctor_items import DoctorContext as DoctorContext
from devctl.doctor_items import Item as Item
from devctl.doctor_items import OwnerGet as OwnerGet
from devctl.doctor_items import OwnerReadError as OwnerReadError
from devctl.doctor_items import Status as Status
from devctl.doctor_items import gh_get as gh_get
from devctl.doctor_local import DEVCTL_DIRECTORY as DEVCTL_DIRECTORY
from devctl.doctor_local import KIT_SOURCE as KIT_SOURCE
from devctl.doctor_local import devctl_exclusion_gaps as devctl_exclusion_gaps
from devctl.doctor_local import git_item as git_item
from devctl.doctor_local import local_items as local_items
from devctl.doctor_rules import ADMIN_ROLE_ID as ADMIN_ROLE_ID
from devctl.doctor_rules import GITHUB_ACTIONS_APP_ID as GITHUB_ACTIONS_APP_ID
from devctl.doctor_rules import PROTECTED_PATHS as PROTECTED_PATHS
from devctl.doctor_rules import PYTHON_PROTECTED_PATHS as PYTHON_PROTECTED_PATHS
from devctl.doctor_rules import codeowners_problems as codeowners_problems
from devctl.doctor_rules import installation_problems as installation_problems
from devctl.doctor_rules import ruleset_problems as ruleset_problems
from devctl.doctor_skills import probe_worktree as probe_worktree
from devctl.doctor_skills import probed_harnesses as probed_harnesses
from devctl.doctor_skills import repository_skills as repository_skills
from devctl.doctor_skills import skills_items as skills_items
from devctl.github.app import project_access
from devctl.repo import Repo
from devctl.settings import load_project

STATUSES: tuple[Status, ...] = ("ok", "missing", "unknown")


def doctor_exit_code(items: Sequence[Item]) -> int:
    """2 when any item is unknown, else 1 when any is missing, else 0."""
    statuses = {item.status for item in items}
    if "unknown" in statuses:
        return 2
    return 1 if "missing" in statuses else 0


def _report(items: Sequence[Item]) -> int:
    for item in items:
        sys.stdout.write(f"{item.status}  {item.area}  {item.name}: {item.detail}\n")
    ok, missing, unknown = (sum(i.status == status for i in items) for status in STATUSES)
    sys.stdout.write(f"doctor: {ok} ok, {missing} missing, {unknown} unknown\n")
    return doctor_exit_code(items)


def run_doctor(ctx: DoctorContext, *, skills: bool = False) -> int:
    """Print every item for ctx, the local ones first, then a summary line; return the exit code.

    Only skills=True starts a Claude Code Session, in a temporary worktree of its own.
    """
    items = [*local_items(ctx), *github_items(ctx)]
    if skills:
        items.extend(skills_items(ctx))
    return _report(items)


def doctor_command(args: argparse.Namespace) -> int:
    item = git_item()
    if item.status != "ok":
        # Every later item needs git rev-parse --path-format=absolute.
        return _report([item])
    repo = Repo.discover(Path.cwd())  # cli.main maps RepoError and SettingsError to exit 2
    project = load_project(repo.root)
    # doctor reads the Apps only while Agent identity is on (Spec 1 section 6).
    access = project_access(project) if project.agent_identity else None
    return run_doctor(DoctorContext(repo.root, project, gh_get, access), skills=args.skills)


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "doctor",
        help="Report what this machine and repository lack",
        description="Report each item as ok, missing or unknown. Exit 0 when every item is ok, "
        "1 when one is missing, 2 when one is unknown.",
    )
    parser.add_argument(
        "--skills",
        action="store_true",
        help="also start one headless Claude Code Session when claude is on PATH, in a temporary "
        "worktree at HEAD, and compare the skills it loads; spends a little quota",
    )
    parser.set_defaults(handler=doctor_command)
